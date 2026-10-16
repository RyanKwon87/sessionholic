"""Synthetic native responses only; no SSH, model calls, signals or real sessions."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_attachments as wa
import windows_collector as wc
import windows_runtime as wr


class FakeReader:
    def __init__(self, home):
        self.home = home
        self.codex_home = home / '.codex'
        self.claude_home = home / '.claude'
        self.binary = home / 'AppData/Local/Programs/OpenAI/Codex/bin/codex.exe'
        self.hostname = 'fixture-pc'
        self.sid = str(uuid.uuid4())
        self.cwd = home / 'Projects/fixture'
        self.cwd.mkdir(parents=True)
        self.path = self.codex_home / 'agents-servers/fixture/app.sock'
        self.path.parent.mkdir(parents=True)
        self.path.touch()
        self.binary.parent.mkdir(parents=True)
        self.binary.touch()
        (self.path.parent / 'server-state.json').write_text(json.dumps({'server_pid': 12}))
        self.processes = {12: {'pid': 12, 'start': '134362358929022995', 'exe': str(self.binary)}}
        self.row = {'id': self.sid, 'cwd': str(self.cwd), 'archived': 0}
        self.paths = [self.path]
        self.calls = []
        self.loaded = [self.sid]
        self.thread = {'id': self.sid, 'cwd': str(self.cwd), 'source': 'cli', 'canAcceptDirectInput': True,
                       'status': {'type': 'idle', 'activeFlags': []}, 'turns': []}
        self.fail_send = False
        self.fail_after = False
        self.send_calls = 0
        self.mutate_hook = None

    def endpoints(self):
        return self.paths

    def writer_owners(self, ids):
        return {sid: self.processes[12] for sid in ids if sid in getattr(self, 'held', ())}

    def project(self, cwd):
        relative = Path(cwd).relative_to(self.home / 'Projects') if str(cwd).startswith(str(self.home / 'Projects')) else Path(cwd)
        return relative.parts[0] if relative.parts else '기타'

    def _transcripts(self):
        folder = self.claude_home / 'projects/fixture'
        return {path.stem: path for path in folder.glob('*.jsonl')} if folder.is_dir() else {}

    def endpoint(self, project):
        return next((path for path in self.paths if path.parent.name == project), None)

    def database_rows(self, sid=None):
        return [dict(self.row)] if sid in (None, self.sid) else []

    def claude_rows(self, target_sid=None):
        return [dict(row) for row in getattr(self, 'claude_list', []) if target_sid in (None, row['id'])]

    def process_reader(self, pid):
        return copy.deepcopy(self.processes.get(pid))

    def _claude_executable(self, exe):
        return Path(exe).name == 'claude.exe' and Path(exe).is_file()

    def read(self, *args):
        return {'messages': [], 'readOnly': True, 'phase': 'unknown', 'runtimeKnown': False}

    def proxy(self, binary, path, deadline):
        owner = self
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def call(self, method, params):
                owner.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/loaded/list':
                    if owner.fail_after and owner.send_calls:
                        raise TimeoutError('synthetic lost response')
                    return {'data': list(owner.loaded)}
                if method == 'thread/read':
                    return {'thread': copy.deepcopy(owner.thread)}
                if method == 'turn/start':
                    owner.send_calls += 1
                    if owner.fail_send:
                        raise TimeoutError('synthetic lost response')
                    turn = {'id': 'turn-sent', 'status': 'inProgress', 'items': [
                        {'type': 'userMessage', 'clientId': params['clientUserMessageId'], 'content': params['input']}]}
                    owner.thread['turns'].append(turn)
                    owner.thread['status']['type'] = 'active'
                    return {'turn': {'id': 'turn-sent'}}
                if method == 'turn/interrupt':
                    for turn in owner.thread['turns']:
                        if turn['id'] == params['turnId']:
                            turn['status'] = 'interrupted'
                    owner.thread['status']['type'] = 'idle'
                    if owner.mutate_hook:
                        owner.mutate_hook(method)
                    return {}
                if method == 'thread/archive':
                    owner.loaded = []
                    owner.row['archived'] = 1
                    if owner.mutate_hook:
                        owner.mutate_hook(method)
                    return {}
                raise AssertionError('unexpected native operation: ' + method)
        return Client()


class WindowsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.reader = FakeReader(self.home)
        self.kills = []
        self.runtime = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy,
            terminate_process=lambda process: self.kills.append(process) or {'applied': True, 'confirmed': True, 'status': 'terminated'})
        self.source = {'host': 'fixture-pc', 'agent': 'codex', 'id': self.reader.sid, 'home': '.codex', 'cwd': str(self.reader.cwd)}

    def send(self, request='request-1234', text='합성 입력'):
        return self.runtime.chat({'action': 'send', 'source': self.source, 'requestId': request,
                                  'input': [{'type': 'text', 'text': text}]})

    def active(self):
        self.reader.thread['status']['type'] = 'active'
        self.reader.thread['turns'] = [{'id': 'turn-original', 'status': 'inProgress', 'items': []}]

    def execute(self, preview, action='terminate', request='request-1234', source=None):
        return self.runtime.execute({'source': source or self.source, 'action': action,
                                     'requestId': request, 'targetProof': preview['targetProof']})

    def claude(self, kind='interactive'):
        sid = str(uuid.uuid4())
        binary = self.home / '.local/bin/claude.exe'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.touch()
        process = {'pid': 45, 'start': '134362358929022995', 'exe': str(binary)}
        self.reader.processes[45] = process
        path = self.reader.claude_home / 'sessions/45.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'sessionId': sid, 'pid': 45, 'procStart': process['start'], 'pidDomain': 'win32:fixture-pc',
               'kind': kind, 'cwd': str(self.reader.cwd), 'status': 'idle'}
        path.write_text(json.dumps(row))
        source = {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'sessionId': sid, 'home': '.claude',
                  'kind': 'background' if kind == 'bg' else 'interactive', 'cwd': str(self.reader.cwd)}
        return source, row, path

    def test_live_read_exposes_actual_capability_and_launch_support(self):
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertTrue(value['capabilities']['canSend'])
        self.assertTrue(value['capabilities']['canLaunch'])
        self.assertFalse(value['readOnly'])
        self.assertFalse(self.runtime.store.root.exists())
        self.assertEqual(self.reader.send_calls, 0)

    def test_live_read_keeps_the_boards_window(self):
        self.assertIs(self.runtime.chat({'action': 'read', 'source': self.source})['olderOmitted'], False)
        self.reader.thread['turns'] = [{'id': 'turn-%d' % number, 'status': 'completed', 'items': [
            {'id': 'input-%d' % number, 'type': 'userMessage', 'content': [{'type': 'text', 'text': '합성 입력 %d' % number}]},
            {'id': 'command-%d' % number, 'type': 'commandExecution', 'command': 'dir'},
            {'id': 'answer-%d' % number, 'type': 'agentMessage', 'text': '합성 답변 %d' % number}]} for number in range(100)]
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertEqual((len(value['messages']), value['messages'][0]['text'], value['olderOmitted']), (120, '합성 입력 60', True))

    def test_live_read_names_the_threads_model_and_effort(self):
        self.reader.thread.update(model='example-model', reasoningEffort='xhigh')
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        # As a Mac read (native_chat.model_settings); a PC keeps no board choice for the next input.
        self.assertEqual(value['modelSettings'], {'model': 'example-model', 'effort': 'xhigh'})

    def test_claude_read_names_the_newest_responses_model_and_effort(self):
        reader = wc.WindowsCollector(self.home, hostname='fixture-pc', process_reader=lambda pid: None)
        sid = str(uuid.uuid4())
        folder = self.home / '.claude/projects/fixture'
        folder.mkdir(parents=True)
        (folder / (sid + '.jsonl')).write_text(''.join(json.dumps(entry) + '\n' for entry in [
            {'type': 'user', 'message': {'content': '합성 요청'}},
            {'type': 'assistant', 'effort': 'xhigh', 'message': {'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': '합성 답변'}]}}]))
        value = wr.WindowsRuntime(reader=reader).chat(
            {'action': 'read', 'source': {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'home': '.claude'}})
        self.assertEqual(value['modelSettings'], {'model': 'claude-opus-5-5', 'effort': 'xhigh', 'observed': 'last_response'})
        self.assertEqual(([m['text'] for m in value['messages']], value['olderOmitted']), (['합성 요청', '합성 답변'], False))

    def test_stored_or_unloaded_session_remains_readable_without_execution(self):
        self.reader.loaded = []
        value = self.runtime.chat({'action': 'read', 'source': self.source})
        self.assertTrue(value['readOnly'])
        self.assertFalse(value['capabilities']['canSend'])
        self.assertEqual(self.reader.send_calls, 0)

    def test_direct_input_must_be_native_confirmed(self):
        for value in (None, False):
            self.reader.thread['canAcceptDirectInput'] = value
            result = self.send()
            self.assertEqual(result['delivery'], 'rejected')
        self.assertEqual(self.reader.send_calls, 0)

    def test_remote_and_unknown_status_do_not_allow_input(self):
        for change in ({'source': 'remote'}, {'turns': [{'id': 'new', 'status': 'new-future-status'}]}):
            saved = copy.deepcopy(self.reader.thread)
            self.reader.thread.update(change)
            self.assertEqual(self.send()['delivery'], 'rejected')
            self.reader.thread = saved
        self.assertEqual(self.reader.send_calls, 0)

    def test_send_exact_once_receipt_and_no_message_in_ledger(self):
        first = self.send(text='sensitive-fixture-body')
        replay = self.send(text='sensitive-fixture-body')
        self.assertTrue(first['confirmed'])
        self.assertTrue(first['readbackConfirmed'])
        self.assertEqual(replay, first)
        self.assertEqual(self.reader.send_calls, 1)
        saved = ''.join(path.read_text() for path in self.runtime.store.root.glob('*.json'))
        self.assertNotIn('sensitive-fixture-body', saved)
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(set(mutation), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(mutation['threadId'], self.source['id'])

    def test_same_request_different_input_is_rejected(self):
        self.send()
        with self.assertRaises(ValueError):
            self.send(text='다른 합성 입력')
        self.assertEqual(self.reader.send_calls, 1)

    def test_lost_send_response_is_durable_unknown_and_not_retried(self):
        self.reader.fail_send = True
        first = self.send()
        self.reader.fail_send = False
        second = self.send()
        self.assertEqual(first['delivery'], 'unknown')
        self.assertEqual(second, first)
        self.assertEqual(self.reader.send_calls, 1)

    def test_acknowledged_send_with_lost_readback_stays_accepted(self):
        self.reader.fail_after = True
        result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['readbackConfirmed'])
        self.assertEqual(result['delivery'], 'accepted')

    def test_receipt_reads_native_client_message_identity(self):
        self.send()
        result = self.runtime.chat({'action': 'read', 'source': self.source, 'requestIds': ['request-1234']})
        self.assertEqual(result['receipts'][0]['turnId'], 'turn-sent')
        self.assertTrue(result['receipts'][0]['confirmed'])

    def stored(self, name, data):
        return wa.store(str(self.reader.cwd), name, data)

    def send_files(self, ids, request='request-1234', text='합성 입력', source=None):
        return self.runtime.chat({'action': 'send', 'source': source or self.source, 'requestId': request,
                                  'input': [{'type': 'text', 'text': text}] if text else [], 'attachmentIds': ids})

    def test_files_go_once_as_native_images_and_path_lines_with_their_names(self):
        image = self.stored('screen.png', b'\x89PNG\r\n\x1a\nfixture-image')
        memo = self.stored('memo.txt', b'fixture-memo')
        first = self.send_files([image['id'], memo['id']])
        self.assertTrue(first['confirmed'])
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        # As native_chat._send on a Mac: the image as a native input, any other file as its path.
        self.assertEqual(mutation['input'], [
            {'type': 'text', 'text': '합성 입력'}, {'type': 'localImage', 'path': image['path']},
            {'type': 'text', 'text': '첨부 파일: ' + json.dumps(memo['path'], ensure_ascii=False)}])
        self.assertEqual([(item['name'], item['kind'], item['type']) for item in first['attachments']],
                         [('screen.png', 'image', 'image/png'), ('memo.txt', 'file', 'text/plain')])
        self.assertTrue(all(set(item) == {'id', 'name', 'type', 'size', 'kind', 'source'} for item in first['attachments']))
        # The same request is answered from its record; other files under its ID are refused.
        self.assertEqual(self.send_files([image['id'], memo['id']]), first)
        with self.assertRaisesRegex(ValueError, '같은 요청 ID'):
            self.send_files([image['id']])
        self.assertEqual(self.reader.send_calls, 1)
        # A later read names the files with its receipt; the record keeps no contents, paths or words.
        read = self.runtime.chat({'action': 'read', 'source': self.source, 'requestIds': ['request-1234']})
        self.assertEqual(read['receipts'][0]['attachments'], first['attachments'])
        self.assertEqual(read['messages'][0]['attachments'][0]['kind'], 'image')
        saved = ''.join(path.read_text() for path in self.runtime.store.root.glob('*.json'))
        for value in ('fixture-image', 'fixture-memo', image['path'], memo['path'], '합성 입력'):
            self.assertNotIn(value, saved)

    def test_a_message_of_files_alone_goes_and_bad_attachment_lists_never_do(self):
        image = self.stored('screen.png', b'\x89PNG\r\n\x1a\nfixture')
        refused = ([image['id'], image['id']], ['%032x' % number for number in range(9)], ['../outside'],
                   [1], 'not-a-list', ['0' * 32], [])
        for ids in refused:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.send_files(ids, text='')
        with self.assertRaises(ValueError):
            self.send_files([image['id']], source={key: value for key, value in self.source.items() if key != 'cwd'})
        self.assertEqual(self.reader.send_calls, 0)
        self.assertTrue(self.send_files([image['id']], text='')['confirmed'])
        mutation = next(params for method, params in self.reader.calls if method == 'turn/start')
        self.assertEqual(mutation['input'], [{'type': 'localImage', 'path': image['path']}])

    def test_images_are_offered_exactly_when_direct_input_is(self):
        capabilities = self.runtime.chat({'action': 'read', 'source': self.source})['capabilities']
        self.assertEqual((capabilities['canSend'], capabilities['canAttachImages']), (True, True))
        self.reader.thread['canAcceptDirectInput'] = False
        capabilities = self.runtime.chat({'action': 'read', 'source': self.source})['capabilities']
        self.assertEqual((capabilities['canSend'], capabilities['canAttachImages']), (False, False))

    def test_an_upload_lands_only_in_the_work_folder_the_pc_records_for_its_session(self):
        data = b'\x89PNG\r\n\x1a\nfixture'
        args = {'source': self.source, 'cwd': str(self.reader.cwd), 'name': 'shot.png',
                'sha256': hashlib.sha256(data).hexdigest()}
        chunks = [base64.b64encode(data).decode()]
        stored = self.runtime.attachment(args, chunks)
        self.assertEqual(Path(stored['path']).read_bytes(), data)
        self.assertEqual(wa.resolve(str(self.reader.cwd), stored['id']), stored)
        other = self.home / 'Projects/other'
        other.mkdir()
        with self.assertRaises(ValueError):
            self.runtime.attachment({**args, 'cwd': str(other)}, chunks)
        self.assertEqual(list(other.iterdir()), [])
        # A Claude session's folder is the one its list row names.
        sid = str(uuid.uuid4())
        claude = {'host': 'fixture-pc', 'agent': 'claude', 'id': sid, 'sessionId': sid, 'home': '.claude'}
        with self.assertRaises(ValueError):
            self.runtime.attachment({**args, 'source': claude}, chunks)
        self.reader.claude_list = [{'id': sid, 'cwd': str(self.reader.cwd)}]
        self.assertTrue(Path(self.runtime.attachment({**args, 'source': claude}, chunks)['path']).is_file())
        self.reader.row['archived'] = 1
        with self.assertRaises(ValueError):
            self.runtime.attachment(args, chunks)
        # Only an upload carries a file, and an upload always does.
        with self.assertRaises(ValueError):
            wr.handle({'op': 'chat', 'args': {'source': self.source, 'action': 'read'}}, chunks)
        with self.assertRaises(ValueError):
            wr.handle({'op': 'attachment', 'args': args})

    def test_duplicate_endpoint_refuses_mutation(self):
        self.reader.paths.append(self.reader.path)
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_cwd_mismatch_refuses_mutation(self):
        self.reader.thread['cwd'] += '-different'
        with self.assertRaises(ValueError):
            self.send()
        self.assertEqual(self.reader.send_calls, 0)

    def test_new_turn_between_preview_and_execute_is_stale(self):
        self.active()
        preview = self.runtime.preview(self.source)
        self.reader.thread['turns'][0]['id'] = 'turn-new'
        result = self.execute(preview, 'interrupt')
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(any(method == 'turn/interrupt' for method, params in self.reader.calls))

    def test_codex_interrupt_exact_turn_and_readback(self):
        self.active()
        preview = self.runtime.preview(self.source)
        first = self.execute(preview, 'interrupt')
        second = self.execute(preview, 'interrupt')
        self.assertTrue(first['confirmed'])
        self.assertEqual(first['status'], 'interrupted')
        self.assertTrue(second['replayed'])
        calls = [params for method, params in self.reader.calls if method == 'turn/interrupt']
        self.assertEqual(calls, [{'threadId': self.source['id'], 'turnId': 'turn-original'}])

    def test_archive_explicit_descendant_scope_and_exact_readback(self):
        preview = self.runtime.preview(self.source)
        cap = preview['capabilities']['terminate']
        self.assertEqual(cap['method'], 'codex-thread-archive')
        self.assertEqual(cap['scope'], 'session-and-descendants')
        self.assertIn('하위 에이전트', cap['effect'])
        result = self.execute(preview)
        self.assertEqual(result['status'], 'terminated')
        self.assertTrue(result['confirmed'])
        calls = [(method, params) for method, params in self.reader.calls if method in ('thread/archive', 'thread/unsubscribe')]
        self.assertEqual(calls, [('thread/archive', {'threadId': self.source['id']})])
        self.assertFalse(self.kills)

    def test_archive_without_db_readback_is_unconfirmed(self):
        self.reader.mutate_hook = lambda method: self.reader.row.update(archived=0)
        result = self.execute(self.runtime.preview(self.source))
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertFalse(result['confirmed'])
        self.assertTrue(result['applied'])

    def test_archive_without_unloaded_readback_is_unconfirmed(self):
        self.reader.mutate_hook = lambda method: self.reader.loaded.append(self.source['id'])
        result = self.execute(self.runtime.preview(self.source))
        self.assertEqual(result['status'], 'unconfirmed')

    def test_server_restart_invalidates_preview(self):
        preview = self.runtime.preview(self.source)
        self.reader.processes[12]['start'] = '134362358929099999'
        result = self.execute(preview)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(any(method == 'thread/archive' for method, params in self.reader.calls))

    def test_proof_tampering_expiry_wrong_source_rejected(self):
        preview = self.runtime.preview(self.source)
        original = preview['targetProof']
        preview['targetProof'] = original[:-1] + ('a' if original[-1] != 'a' else 'b')
        with self.assertRaises(ValueError):
            self.execute(preview)
        preview['targetProof'] = original
        with patch.object(wr.time, 'time', return_value=time.time() + 60):
            with self.assertRaises(ValueError):
                self.execute(preview)
        with self.assertRaises(ValueError):
            self.execute(preview, source={**self.source, 'host': 'other-host'})

    def test_used_proof_does_not_repeat_with_new_request_id(self):
        preview = self.runtime.preview(self.source)
        first = self.execute(preview)
        second = self.execute(preview, request='other-request')
        self.assertTrue(first['confirmed'])
        self.assertTrue(second['replayed'])
        self.assertEqual(sum(method == 'thread/archive' for method, params in self.reader.calls), 1)

    def test_codex_terminal_plan_preserves_account_and_existing_endpoint(self):
        plan = self.runtime.terminal_plan(self.source)
        self.assertEqual(plan['argv'], [str(self.reader.binary), 'resume', self.source['id'], '--remote',
                                       'unix://' + self.reader.path.as_posix(), '--cd', str(self.reader.cwd)])
        self.assertEqual(plan['env'], {'CODEX_HOME': str(self.reader.codex_home)})
        self.assertEqual(plan['nativeSource']['id'], self.source['id'])
        self.assertEqual(self.reader.send_calls, 0)

    def test_claude_external_interactive_process_can_terminate(self):
        source, row, path = self.claude()
        preview = self.runtime.preview(source)
        self.assertTrue(preview['capabilities']['terminate']['supported'])
        result = self.execute(preview, source=source)
        self.assertTrue(result['confirmed'])
        self.assertEqual([item['pid'] for item in self.kills], [45])

    def test_claude_pid_reuse_is_stale(self):
        source, row, path = self.claude()
        preview = self.runtime.preview(source)
        self.reader.processes[45]['start'] = '134362358929099999'
        result = self.execute(preview, source=source)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(self.kills)

    def test_claude_domain_executable_cwd_or_duplicate_owner_refused(self):
        source, row, path = self.claude()
        for field, value in [('pidDomain', 'win32:other-machine'), ('procStart', '134362358929099999'), ('cwd', str(self.home / 'other'))]:
            path.write_text(json.dumps({**row, field: value}))
            self.assertFalse(self.runtime.preview(source)['capabilities']['terminate']['supported'])
        path.write_text(json.dumps(row))
        self.reader.processes[45]['exe'] = str(self.home / 'unrelated.exe')
        self.assertFalse(self.runtime.preview(source)['capabilities']['terminate']['supported'])
        self.assertFalse(self.kills)

    def test_claude_background_without_native_attach_id_and_interactive_refused(self):
        source, row, path = self.claude('bg')
        with self.assertRaisesRegex(ValueError, '연결 식별자'):
            self.runtime.terminal_plan(source)
        path.write_text(json.dumps({**row, 'kind': 'interactive'}))
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(source)

    def test_a_running_claude_background_session_attaches_by_its_job_id(self):
        source, row, path = self.claude('bg')
        job = source['id'][:8]
        path.write_text(json.dumps({**row, 'jobId': job}))
        plan = self.runtime.terminal_plan(source)
        self.assertEqual(plan['argv'], [str(self.home / '.local/bin/claude.exe'), 'attach', job])
        self.assertEqual((plan['cwd'], plan['env'], plan['connectionMode']), (str(self.reader.cwd), {}, 'attach'))
        self.assertEqual((plan['nativeSource']['id'], plan['nativeSource']['kind']), (source['id'], 'background'))
        self.assertNotIn('resumed', plan)
        # Not a job id `claude attach` takes: no attach to some other run.
        for value in (source['id'], 'ABCD1234', '1234567', '12345678 ', 'a1b2c3d4e', 12345678):
            path.write_text(json.dumps({**row, 'jobId': value}))
            with self.assertRaisesRegex(ValueError, '연결 식별자'):
                self.runtime.terminal_plan(source)
        # Mid-update the run keeps its renamed old image while no claude.exe exists yet.
        path.write_text(json.dumps({**row, 'jobId': job}))
        old = self.home / '.local/bin/claude.exe.old.1792000000000.45'
        old.touch()
        self.reader.processes[45]['exe'] = str(old)
        (self.home / '.local/bin/claude.exe').unlink()
        with patch.object(self.reader, '_claude_executable', lambda exe: Path(exe).is_file()), \
                self.assertRaisesRegex(ValueError, '실행 파일'):
            self.runtime.terminal_plan(source)
        self.reader.processes[45]['exe'] = str(self.home / '.local/bin/claude.exe')
        # A record another process now owns is no live run: its old job id is never attached.
        (self.home / '.local/bin/claude.exe').touch()
        path.write_text(json.dumps({**row, 'jobId': job, 'procStart': '134362358929099999'}))
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(source)

    def test_native_vendor_remote_nested_sources_are_rejected(self):
        for value in ('vendorRemote', {'vendorRemote': {}}, {'wrapper': [{'vendorRemote': 'x'}]}):
            self.reader.thread['threadSource'] = value
            self.assertEqual(self.send()['delivery'], 'rejected')
        self.assertEqual(self.reader.send_calls, 0)

    def test_single_request_id_is_included_in_read_receipts(self):
        self.send()
        result = self.runtime.chat({'action': 'read', 'source': self.source, 'requestId': 'request-1234'})
        self.assertEqual(len(result['receipts']), 1)
        self.assertTrue(result['receipts'][0]['confirmed'])

    def test_instance_state_directories_are_isolated(self):
        with patch.object(wr, 'STORE_RELATIVE', '.local/state/dev-fixture/windows'):
            other = wr.WindowsRuntime(reader=self.reader, proxy_factory=self.reader.proxy)
        self.assertNotEqual(other.store.root, self.runtime.store.root)
        self.assertEqual(other.store.root, self.home / '.local/state/dev-fixture/windows')
        with patch.object(wr, 'STORE_RELATIVE', '../escaped'):
            with self.assertRaises(ValueError):
                wr.Store(self.home)

    def test_resume_preview_distinguishes_loaded_from_stored(self):
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.reader.loaded = []
        # Stored, its project's server running: it resumes into that server.
        self.assertTrue(self.runtime.resume_preview(self.source)['allowed'])
        self.reader.paths = []
        result = self.runtime.resume_preview(self.source)
        self.assertFalse(result['allowed'])
        self.assertFalse(result['blockedByBoard'])
        self.assertIn('서버가 PC에서 꺼져', result['reason'])

    def test_a_stored_codex_session_resumes_into_its_projects_running_server_only(self):
        self.reader.loaded = []
        plan = self.runtime.terminal_plan(self.source)
        self.assertEqual(plan['argv'], [str(self.reader.binary), 'resume', self.reader.sid, '--remote',
                                        'unix://' + self.reader.path.as_posix(), '--cd', str(self.reader.cwd)])
        self.assertEqual((plan['resumed'], plan['connectionMode']), (True, 'attach'))
        self.assertEqual(plan['env'], {'CODEX_HOME': str(self.reader.codex_home)})
        # The folder's own server, in any case; while it exists, never another one.
        other = self.reader.codex_home / 'agents-servers/fixture-us/app.sock'
        other.parent.mkdir(parents=True)
        other.touch()
        self.reader.paths = [other]
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        self.reader.path.parent.rename(self.reader.path.parent.with_name('FIXTURE'))
        upper = self.reader.codex_home / 'agents-servers/FIXTURE/app.sock'
        self.reader.paths = [upper, other]
        self.assertIn('unix://' + upper.as_posix(), self.runtime.terminal_plan(self.source)['argv'])
        # With none of its name, the only <folder>-… server is the folder's (a project named apart)...
        upper.parent.rename(upper.parent.with_name('retired'))
        self.reader.paths = [other]
        self.assertIn('unix://' + other.as_posix(), self.runtime.terminal_plan(self.source)['argv'])
        # ...unless a project folder of that name owns it, or another such server exists.
        (self.home / 'Projects/fixture-us').mkdir()
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        (self.home / 'Projects/fixture-us').rmdir()
        twin = self.reader.codex_home / 'agents-servers/fixture-eu'
        twin.mkdir()
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        twin.rmdir()
        # Outside ~/Projects no server is guessed from the folder name.
        outside = self.home / 'Elsewhere/fixture'
        outside.mkdir(parents=True)
        self.reader.row['cwd'] = str(outside)
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan({**self.source, 'cwd': str(outside)})
        self.reader.row['cwd'] = str(self.reader.cwd)
        # Never an embedded runtime in the elevated SSH session.
        self.reader.paths = []
        with self.assertRaisesRegex(ValueError, '꺼져'):
            self.runtime.terminal_plan(self.source)
        # A thread another Codex process writes (a codex exec) is never resumed alongside it.
        self.reader.paths = [self.reader.path]
        self.reader.held = {self.reader.sid}
        with self.assertRaisesRegex(ValueError, '다른 Codex 실행'):
            self.runtime.terminal_plan(self.source)
        self.reader.held = set()
        self.reader.row['archived'] = 1
        with self.assertRaises(ValueError):
            self.runtime.terminal_plan(self.source)

    def test_an_unreadable_claude_record_of_a_live_claude_process_blocks_a_resume(self):
        source, row, registry = self.claude()
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        registry.write_text('{')  # being rewritten, say
        with self.assertRaisesRegex(ValueError, '실행 기록을 읽지 못했습니다'):
            self.runtime.terminal_plan(source)
        self.reader.processes.pop(45)  # that process is gone: the record cannot be this run
        self.assertTrue(self.runtime.terminal_plan(source)['resumed'])

    def test_a_background_run_whose_process_ended_resumes_like_any_stored_conversation(self):
        source, row, registry = self.claude(kind='bg')
        self.assertEqual(source['kind'], 'background')
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        self.reader.processes.pop(45)  # its registry record stays
        self.assertEqual(self.runtime.terminal_plan(source)['argv'][1:], ['--resume', source['id']])

    def test_a_stored_claude_conversation_no_pc_process_runs_resumes_in_a_board_terminal(self):
        source, row, registry = self.claude()
        transcript = self.reader.claude_home / 'projects/fixture' / (source['id'] + '.jsonl')
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({'type': 'user', 'cwd': str(self.reader.cwd), 'sessionId': source['id']}) + '\n')
        with self.assertRaisesRegex(ValueError, '실행 중'):
            self.runtime.terminal_plan(source)
        self.assertFalse(self.runtime.resume_preview(source)['allowed'])
        # The process ended (its record may stay): the conversation continues here.
        self.reader.processes.pop(45)
        plan = self.runtime.terminal_plan(source)
        self.assertEqual(plan['argv'], [str(self.home / '.local/bin/claude.exe'), '--resume', source['id']])
        self.assertEqual((plan['cwd'], plan['env'], plan['resumed']), (str(self.reader.cwd), {}, True))
        self.assertEqual(plan['nativeSource']['kind'], 'interactive')
        self.assertTrue(self.runtime.resume_preview(source)['allowed'])
        registry.unlink()
        transcript.unlink()
        with self.assertRaisesRegex(ValueError, '기록'):
            self.runtime.terminal_plan(source)

    def test_windows_process_termination_never_executes_on_non_windows(self):
        with self.assertRaises(ValueError):
            wr._terminate_windows({'pid': 123, 'start': 'x', 'exe': 'fixture'})

    def test_runtime_refuses_shell_upload_and_control_without_proof(self):
        for op in ('shell', 'upload', 'start_app_server', 'new_session'):
            with self.assertRaises(ValueError):
                wr.handle({'op': op, 'args': {}})
        with self.assertRaises(ValueError):
            self.runtime.execute({'source': self.source, 'action': 'terminate', 'requestId': 'request-1234'})

    def test_symlink_state_folder_is_refused(self):
        target = self.home / 'other-state'
        target.mkdir()
        self.runtime.store.root.parent.mkdir(parents=True)
        self.runtime.store.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.runtime.preview(self.source)

    def test_long_websocket_input_uses_64_bit_frame_length(self):
        proxy = object.__new__(wr.NativeProxy)
        proxy.deadline = time.monotonic() + 1
        output = []
        proxy._write = output.append
        value = {'input': '한' * 20000}
        proxy._send(value)
        self.assertEqual(output[0][1], 0xff)
        self.assertEqual(struct.unpack('!Q', output[0][2:10])[0], len(json.dumps(value).encode()))

    def test_readonly_collector_never_gains_mutation_permissions(self):
        self.assertNotIn('turn/start', wc.Proxy.ALLOWED)
        self.assertNotIn('thread/archive', wc.Proxy.ALLOWED)
        self.assertIn('turn/start', wr.NativeProxy.ALLOWED)
        self.assertIn('thread/archive', wr.NativeProxy.ALLOWED)

    def test_loaded_list_paginates_and_truncation_does_not_prove_absence(self):
        class Paged:
            def __init__(self, pages):
                self.pages, self.calls = pages, []
            def call(self, method, args):
                self.calls.append(args)
                return self.pages[len(self.calls) - 1]
        client = Paged([{'data': ['first'], 'nextCursor': 'page-2'}, {'data': ['target'], 'nextCursor': None}])
        self.assertEqual(wr._loaded(client), ({'first', 'target'}, True))
        self.assertEqual(client.calls[1]['cursor'], 'page-2')
        partial = Paged([{'data': [], 'nextCursor': 'page-' + str(index)} for index in range(3)])
        self.assertEqual(wr._loaded(partial), (set(), False))

    def test_archive_partial_loaded_list_does_not_claim_termination(self):
        original = wr._loaded
        def loaded(client):
            if self.reader.row['archived']:
                return set(), False
            return original(client)
        with patch.object(wr, '_loaded', side_effect=loaded):
            result = self.execute(self.runtime.preview(self.source))
        self.assertTrue(result['applied'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['scopeReadback'], 'selected-session')
        self.assertFalse(result['descendantsConfirmed'])

    def test_terminate_checks_identity_and_waits_on_same_windows_handle(self):
        class Function:
            def __init__(self, fn):
                self.fn = fn
            def __call__(self, *args):
                return self.fn(*args)
        calls = []
        start = 134362358929022995
        def times(handle, created, *args):
            self.assertEqual(handle, 99)
            created._obj.dwHighDateTime = start >> 32
            created._obj.dwLowDateTime = start & 0xffffffff
            return True
        def image(handle, flags, output, length):
            self.assertEqual(handle, 99)
            output.value = 'C:\\Users\\fixture\\claude.exe'
            return True
        class Kernel:
            OpenProcess = Function(lambda rights, inherit, pid: calls.append(('open', pid)) or 99)
            CloseHandle = Function(lambda handle: calls.append(('close', handle)))
            GetProcessTimes = Function(times)
            QueryFullProcessImageNameW = Function(image)
            TerminateProcess = Function(lambda handle, code: calls.append(('terminate', handle)) or True)
            WaitForSingleObject = Function(lambda handle, ms: calls.append(('wait', handle)) or 0)
        expected = {'pid': 45, 'start': str(start), 'exe': 'C:\\Users\\fixture\\claude.exe'}
        with patch.object(wr.os, 'name', 'nt'), patch.object(wr.ctypes, 'WinDLL', return_value=Kernel(), create=True):
            result = wr._terminate_windows(expected)
            self.assertTrue(result['confirmed'])
            self.assertIn(('terminate', 99), calls)
            self.assertIn(('wait', 99), calls)
            calls.clear()
            result = wr._terminate_windows({**expected, 'start': 'different'})
            self.assertEqual(result['status'], 'stale')
            self.assertFalse(any(call[0] == 'terminate' for call in calls))
            self.assertEqual(calls[-1], ('close', 99))


if __name__ == '__main__':
    unittest.main()
