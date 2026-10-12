import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_collector as wc


class SessionOriginTest(unittest.TestCase):
    def test_native_subagent_variants_keep_agent_origin_without_spawn_details(self):
        for sub in ({'thread_spawn': {}}, {'other': 'native_worker'}, 'review', 'compact', 'memory_consolidation'):
            for key in ('subAgent', 'subagent'):
                with self.subTest(key=key, sub=sub):
                    for source in ({key: sub}, json.dumps({key: sub})):
                        origin = wc._origin({'source': source})
                        self.assertEqual(origin['initiator'], 'agent')
                        self.assertEqual(origin['initiatorEvidence'], 'subagent')

    def test_execution_mode_and_parent_alone_do_not_imply_agent_origin(self):
        for row in ({'source': 'exec'}, {'source': 'cli'}, {'kind': 'background'},
                    {'parentSessionId': 'parent-thread'}, {'source': {'subAgent': {'unrecognized': True}}}):
            with self.subTest(row=row):
                self.assertEqual(wc._origin(row)['initiator'], 'unknown')
        self.assertEqual(wc._origin({'initiator': 'human', 'source': {'subAgent': 'review'}})['initiator'], 'human')

    def test_native_names_and_worker_role_are_preserved_separately(self):
        origin = wc._origin({'initiator': {'type': 'agent', 'name': 'fallback'},
                             'initiatorName': 'Planner', 'agentName': 'Reviewer',
                             'source': {'subAgent': {'thread_spawn': {'agent_type': 'reviewer', 'parent_thread_id': 'parent-thread'}}}})
        self.assertEqual(origin['initiatorName'], 'Planner')
        self.assertEqual(origin['agentName'], 'Reviewer')
        self.assertEqual(origin['agentRole'], 'reviewer')
        self.assertEqual(origin['parentSessionId'], 'parent-thread')


class FakeProxy:
    def __init__(self, rows, calls, read=None):
        self.rows, self.calls, self.read = rows, calls, read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/loaded/list':
            return {'data': [row['id'] for row in self.rows]}
        if method == 'thread/list':
            return {'data': self.rows}
        if method == 'thread/read':
            return {'thread': self.read or self.rows[0]}
        raise AssertionError('unexpected native mutation: ' + method)


class WindowsCollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.sid = str(uuid.uuid4())
        self.parent = str(uuid.uuid4())
        self.cwd = str(self.home / 'Projects/fixture-project')
        self.calls = []
        self.live = {'id': self.sid, 'cwd': self.cwd, 'status': {'type': 'active', 'activeFlags': []},
                     'name': '합성 진행 작업', 'updatedAt': 200, 'turns': []}
        self.reader = wc.WindowsCollector(self.home, hostname='fixture-pc', proxy_factory=lambda *args: FakeProxy([self.live], self.calls),
            process_reader=lambda pid: {'pid': pid, 'start': '123456', 'exe': str(self.reader.binary)})
        self.reader.codex_home.mkdir()
        self.db = self.reader.codex_home / 'state_5.sqlite'
        connection = sqlite3.connect(self.db)
        connection.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, title TEXT, created_at INTEGER, updated_at INTEGER, archived INTEGER, source TEXT, name TEXT, originator TEXT, agent_nickname TEXT, agent_role TEXT, parent_thread_id TEXT, rollout_path TEXT)')
        self.rollout = self.reader.codex_home / 'sessions/2026/10/12' / ('rollout-' + self.sid + '.jsonl')
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.sid, 'cwd': self.cwd}}) + '\n' +
            json.dumps({'type': 'turn_context', 'payload': {'turn_id': 'turn-fixture'}}) + '\n' +
            json.dumps({'type': 'response_item', 'timestamp': '2026-10-12T00:00:00Z', 'payload': {'type': 'message', 'role': 'user', 'id': 'user-fixture', 'content': [{'type': 'input_text', 'text': '합성 요청'}]}}) + '\n' +
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'id': 'answer-fixture', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': '합성 결과'}]}}) + '\n')
        source = json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': self.parent, 'agent_nickname': '검토 워커', 'agent_role': 'reviewer'}}})
        connection.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (self.sid, self.cwd, '합성 기록', 100, 200, 0, source, None, 'codex_cli', '검토 워커', 'reviewer', self.parent, str(self.rollout)))
        connection.commit()
        connection.close()

    def endpoint(self, folder_name='fixture-server', **changes):
        folder = self.reader.codex_home / 'agents-servers' / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        socket = folder / 'app.sock'
        socket.touch()
        self.reader.binary.parent.mkdir(parents=True, exist_ok=True)
        self.reader.binary.touch()
        state = {'project': folder_name, 'endpoint': 'unix://' + socket.as_posix(),
                 'elevated': False, 'exit_code': None, 'server_pid': 424242, **changes}
        (folder / 'server-state.json').write_text(json.dumps(state))
        return folder, state

    def claude_transcript(self, sid=None, messages=None):
        sid = sid or str(uuid.uuid4())
        folder = self.reader.claude_home / 'projects' / 'fixture-project'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (sid + '.jsonl')
        entries = messages if messages is not None else [
            {'type': 'user', 'cwd': self.cwd, 'uuid': 'message-user', 'message': {'content': '합성 Claude 요청'}},
            {'type': 'assistant', 'uuid': 'message-answer', 'message': {'content': [{'type': 'text', 'text': '합성 Claude 결과'}]}}]
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        return sid, path

    def test_saved_records_are_visible_without_guessing_running_or_done(self):
        before = self.db.read_bytes()
        result = self.reader.snapshot()
        self.assertEqual(len(result['codex']), 1)
        row = result['codex'][0]
        self.assertEqual((row['id'], row['home'], row['phase']), (self.sid, '.codex', 'unknown'))
        self.assertFalse(row['runtimeKnown'])
        self.assertTrue(row['readOnly'])
        self.assertTrue(row['canRead'])
        self.assertEqual(result['capabilities'], wc.CAPABILITIES)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_explicit_subagent_metadata_survives_db(self):
        row = self.reader.snapshot()['codex'][0]
        self.assertEqual(row['initiator'], 'agent')
        self.assertEqual(row['parentSessionId'], self.parent)
        self.assertEqual(row['agentName'], '검토 워커')
        self.assertEqual(row['agentRole'], 'reviewer')
        self.assertEqual(row['originator'], 'codex_cli')

    def test_only_existing_exact_project_endpoint_reports_live_status(self):
        self.endpoint()
        result = self.reader.snapshot()
        row = result['codex'][0]
        self.assertEqual(row['phase'], 'working')
        self.assertTrue(row['runtimeKnown'])
        self.assertEqual([method for method, params in self.calls], ['thread/loaded/list', 'thread/list'])
        self.assertTrue(self.calls[1][1]['useStateDbOnly'])
        self.assertEqual(self.calls[1][1]['sourceKinds'], wc.CODEX_SOURCES)
        self.assertEqual(row['project'], 'fixture-project')

    def test_elevated_or_wrong_endpoint_process_or_project_does_not_connect(self):
        for changes in ({'elevated': True}, {'project': 'other'}, {'endpoint': 'unix://C:/other/app.sock'}, {'exit_code': 1}):
            with self.subTest(changes=changes):
                self.endpoint(**changes)
                self.assertIsNone(self.reader.endpoint('fixture-server'))
        self.endpoint()
        with patch.object(self.reader, 'process_reader', return_value={'exe': 'C:/other/codex.exe'}):
            self.assertIsNone(self.reader.endpoint('fixture-server'))
        self.assertEqual(self.calls, [])

    def test_versioned_codex_executable_is_valid_only_under_same_home(self):
        self.endpoint()
        binary = self.reader.codex_home / 'packages/standalone/releases/0.162.1-x86_64-pc-windows-msvc/bin/codex.exe'
        binary.parent.mkdir(parents=True)
        binary.touch()
        with patch.object(self.reader, 'process_reader', return_value={'exe': str(binary)}):
            self.assertEqual(self.reader.snapshot()['codex'][0]['phase'], 'working')
        for candidate in (str(binary).replace('0.162.1-x86_64', 'arbitrary-x86_64'), str(self.home.parent / binary.name)):
            with self.subTest(candidate=candidate):
                self.assertFalse(self.reader._codex_executable(candidate))

    def test_duplicate_loaded_owners_have_unknown_runtime(self):
        self.endpoint('fixture-one')
        self.endpoint('fixture-two')
        self.assertEqual(self.reader.snapshot()['codex'][0]['phase'], 'unknown')
        self.assertFalse(self.reader.read('codex', self.sid, '.codex')['runtimeKnown'])

    def test_cross_account_symlink_roots_are_not_read(self):
        actual = self.home / 'other-account-codex'
        self.reader.codex_home.rename(actual)
        self.reader.codex_home.symlink_to(actual, target_is_directory=True)
        result = self.reader.snapshot()
        self.assertEqual(result['codex'], [])
        self.assertEqual(len(result['errors']), 1)

    def test_failed_proxy_is_not_retried_and_saved_records_remain(self):
        self.endpoint()
        with patch.object(self.reader, 'proxy_factory', side_effect=TimeoutError()) as factory:
            result = self.reader.snapshot()
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(result['codex'][0]['phase'], 'unknown')
        self.assertEqual(len(result['errors']), 1)

    def test_live_thread_cwd_must_match_stored_identity(self):
        self.endpoint()
        self.live['cwd'] = str(self.home / 'Projects/other-project')
        self.assertEqual(self.reader.snapshot()['codex'][0]['phase'], 'unknown')

    def test_extended_windows_cwd_matches_native_drive_path(self):
        self.endpoint()
        standard = r'C:\Users\example\Projects\example'
        connection = sqlite3.connect(self.db)
        connection.execute('UPDATE threads SET cwd=?', ('\\\\?\\' + standard,))
        connection.commit()
        connection.close()
        self.live['cwd'] = standard
        self.assertEqual(self.reader.snapshot()['codex'][0]['phase'], 'working')
        self.assertTrue(self.reader.read('codex', self.sid, '.codex')['runtimeKnown'])

    def test_extended_unc_equivalence_does_not_rewrite_device_namespaces(self):
        self.assertEqual(wc._norm(r'\\?\UNC\server\share\folder'), wc._norm(r'\\server\share\folder'))
        self.assertEqual(wc._norm(r'\\?\c:\Users\example'), wc._norm('C:/Users/example/'))
        self.assertNotEqual(wc._norm(r'\\?\C:relative'), wc._norm(r'C:relative'))
        self.assertNotEqual(wc._norm(r'\\.\C:\folder'), wc._norm(r'C:\folder'))
        self.assertNotEqual(wc._norm(r'\\?\Volume{fixture}\folder'), wc._norm(r'Volume{fixture}\folder'))
        self.assertNotEqual(wc._norm(r'\\?\UNC\server'), wc._norm(r'\\server'))

    def test_live_read_is_exact_and_never_resumes_or_sends(self):
        self.endpoint()
        self.live['turns'] = [{'id': 'turn-1', 'status': 'completed', 'startedAt': 100, 'completedAt': 200,
                              'items': [{'id': 'input-1', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '합성 입력'}]},
                                        {'id': 'answer-1', 'type': 'agentMessage', 'text': '합성 답변', 'phase': 'final_answer'}]}]
        result = self.reader.read('codex', self.sid, '.codex')
        self.assertEqual([m['text'] for m in result['messages']], ['합성 입력', '합성 답변'])
        self.assertEqual(result['phase'], 'working')
        self.assertTrue(result['runtimeKnown'])
        self.assertEqual(self.calls, [('thread/loaded/list', {'limit': wc.LIMIT}),
                                      ('thread/read', {'threadId': self.sid, 'includeTurns': True})])
        self.assertEqual(result['messages'][-1]['turnId'], 'turn-1')
        self.assertEqual(result['messages'][-1]['messageId'], 'answer-1')

    def test_saved_rollout_read_is_bound_to_native_id_and_cwd(self):
        result = self.reader.read('codex', self.sid, '.codex')
        self.assertEqual([row['text'] for row in result['messages']], ['합성 요청', '합성 결과'])
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['runtimeKnown'])
        self.assertEqual(result['messages'][-1]['turnId'], 'turn-fixture')
        self.rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(uuid.uuid4()), 'cwd': self.cwd}}))
        with self.assertRaises(ValueError):
            self.reader.read('codex', self.sid, '.codex')

    def test_cross_home_ids_and_path_escape_are_rejected(self):
        for home in ('.codex-other', '../.codex', None):
            with self.subTest(home=home), self.assertRaises(ValueError):
                self.reader.read('codex', self.sid, home)
        with self.assertRaises(ValueError):
            self.reader.read('codex', str(uuid.uuid4()), '.codex')
        connection = sqlite3.connect(self.db)
        connection.execute('UPDATE threads SET rollout_path=?', (str(self.home / 'outside.jsonl'),))
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            self.reader.read('codex', self.sid, '.codex')

    def test_duplicate_claude_transcript_is_not_read(self):
        sid, path = self.claude_transcript()
        other = path.parent.parent / 'other' / path.name
        other.parent.mkdir()
        other.write_bytes(path.read_bytes())
        with self.assertRaises(ValueError):
            self.reader.read('claude', sid)

    def test_claude_stored_record_read_without_false_runtime(self):
        sid, path = self.claude_transcript()
        snapshot = self.reader.snapshot()
        row = snapshot['claude'][0]
        self.assertEqual(row['id'], sid)
        self.assertTrue(row['canRead'])
        self.assertEqual(row['phase'], 'unknown')
        result = self.reader.read('claude', sid)
        self.assertEqual([row['text'] for row in result['messages']], ['합성 Claude 요청', '합성 Claude 결과'])
        self.assertFalse(result['runtimeKnown'])

    def test_claude_runtime_requires_pid_start_identity(self):
        sid, path = self.claude_transcript()
        folder = self.reader.claude_home / 'sessions'
        folder.mkdir()
        binary = self.home / '.local/bin/claude.exe'
        binary.parent.mkdir(parents=True)
        binary.touch()
        stamp = '134362358929022995'
        state = {'sessionId': sid, 'kind': 'bg', 'cwd': self.cwd, 'status': 'busy', 'procStart': stamp,
                 'pidDomain': 'win32:fixture-pc', 'pid': 424242, 'name': '합성 작업'}
        registry = folder / '424242.json'
        registry.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8-sig')
        with patch.object(self.reader, 'process_reader', return_value={'exe': str(binary), 'start': stamp}):
            row = self.reader.claude_rows()[0]
            self.assertEqual(row['phase'], 'working')
            self.assertEqual(row['kind'], 'background')
            self.assertEqual(row['title'], '합성 작업')
            old = binary.with_name('claude.exe.old.1791762305835.20564')
            binary.rename(old)
            with patch.object(self.reader, 'process_reader', return_value={'exe': str(old), 'start': stamp}):
                self.assertEqual(self.reader.claude_rows()[0]['phase'], 'working')
            old.rename(binary)
            state['status'] = 'future-status'
            registry.write_text(json.dumps(state))
            unknown = self.reader.claude_rows()[0]
            self.assertEqual(unknown['phase'], 'unknown')
            self.assertFalse(unknown['runtimeKnown'])
            state['status'] = 'busy'
            state['procStart'] = 'reused-pid'
            registry.write_text(json.dumps(state))
            self.assertEqual(self.reader.claude_rows()[0]['phase'], 'unknown')
            state['procStart'] = stamp
            state['pidDomain'] = 'win32:other-pc'
            registry.write_text(json.dumps(state))
            self.assertEqual(self.reader.claude_rows()[0]['phase'], 'unknown')
            state['pidDomain'] = 'win32:fixture-pc'
            state['pid'] = 123
            registry.write_text(json.dumps(state))
            self.assertEqual(self.reader.claude_rows()[0]['phase'], 'unknown')

    def test_renamed_claude_binary_requires_exact_updater_name_and_native_bin(self):
        folder = self.home / '.local/bin'
        folder.mkdir(parents=True)
        good = folder / 'claude.exe.old.1791762305835.20564'
        good.touch()
        self.assertTrue(self.reader._claude_executable(str(good)))
        for name in ('claude.exe.old.any.20564', 'claude.exe.old.1791762305835.0',
                     'claude.exe.old.1791762305835.20564.bak', 'claude.exe.other.1791762305835.20564'):
            candidate = folder / name
            candidate.touch()
            with self.subTest(name=name):
                self.assertFalse(self.reader._claude_executable(str(candidate)))
        arbitrary = self.home / 'Documents/bin' / good.name
        arbitrary.parent.mkdir(parents=True)
        arbitrary.touch()
        self.assertFalse(self.reader._claude_executable(str(arbitrary)))

    def test_tail_limits_bytes_and_message_count(self):
        sid, path = self.claude_transcript(messages=[{'type': 'user', 'message': {'content': '합성 %d' % index}} for index in range(100)])
        result = self.reader.read('claude', sid)
        self.assertEqual(len(result['messages']), wc.MESSAGE_LIMIT)
        self.assertEqual(result['messages'][0]['text'], '합성 20')

    def test_cli_errors_are_json_without_paths_or_raw_exception(self):
        with patch.object(wc, 'WindowsCollector', return_value=self.reader), contextlib.redirect_stdout(io.StringIO()) as output:
            wc.main(['read', 'codex', self.sid, '../secret'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['code'], 'windows_read_unavailable')
        self.assertNotIn(str(self.home), output.getvalue())

    def test_full_snapshot_is_read_only_alias(self):
        with patch.object(wc, 'WindowsCollector', return_value=self.reader), contextlib.redirect_stdout(io.StringIO()) as output:
            wc.main(['snapshot', '--full'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['platform'], 'windows')
        self.assertEqual(len(result['codex']), 1)
        self.assertEqual(self.calls, [])

    def test_proxy_rejects_every_mutation_before_writing(self):
        proxy = wc.Proxy.__new__(wc.Proxy)
        for method in ('turn/start', 'thread/resume', 'thread/archive', 'turn/interrupt'):
            with self.subTest(method=method), self.assertRaises(ValueError):
                proxy.call(method, {})

    def test_proxy_frame_fragmentation_ping_and_size_bound(self):
        payload = json.dumps({'id': 1, 'result': {'data': []}}).encode()
        pieces = bytes([1, 5]) + payload[:5] + bytes([0x89, 1]) + b'p' + bytes([0x80, len(payload) - 5]) + payload[5:]
        proxy = wc.Proxy.__new__(wc.Proxy)
        stream = io.BytesIO(pieces)
        proxy._read = lambda size: stream.read(size)
        with patch.object(proxy, '_send') as send:
            self.assertEqual(proxy._message(), {'id': 1, 'result': {'data': []}})
            send.assert_called_once_with(b'p', 10)
        oversized = io.BytesIO(bytes([0x81, 127]) + struct.pack('!Q', 9 * 1024 * 1024))
        proxy._read = lambda size: oversized.read(size)
        with self.assertRaises(ValueError):
            proxy._message()

    def test_blocked_proxy_write_has_a_deadline_and_only_closes_fixture(self):
        proxy = wc.Proxy.__new__(wc.Proxy)
        proxy.process = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proxy.closed = threading.Event()
        proxy.deadline = time.monotonic() + .05
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                proxy._write(b'x' * (1024 * 1024))
            self.assertLess(time.monotonic() - started, 1)
        finally:
            proxy.close()
        self.assertIsNotNone(proxy.process.poll())

    def test_expired_deadline_never_starts_a_proxy(self):
        with patch.object(wc.subprocess, 'Popen') as start, self.assertRaises(TimeoutError):
            wc.Proxy('fixture.exe', 'fixture.sock', time.monotonic() - 1)
        start.assert_not_called()

    def test_real_proxy_transport_uses_only_fixture_child(self):
        script = r'''
import sys,base64,hashlib,json,struct
r,w=sys.stdin.buffer,sys.stdout.buffer
header=b''
while not header.endswith(b'\r\n\r\n'):header+=r.read(1)
key=next(line.split(b': ',1)[1] for line in header.split(b'\r\n') if line.startswith(b'Sec-WebSocket-Key:'))
accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
w.write(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n');w.flush()
while True:
 h=r.read(2)
 if len(h)!=2:break
 size=h[1]&127
 if size==126:size=struct.unpack('!H',r.read(2))[0]
 mask=r.read(4);data=r.read(size)
 request=json.loads(bytes(value^mask[index%4] for index,value in enumerate(data)))
 if 'id' not in request:continue
 value=json.dumps({'id':request['id'],'result':{'data':[]}}).encode()
 w.write(bytes([129,len(value)])+value);w.flush()
'''
        real = subprocess.Popen
        called = []
        def start(args, **kwargs):
            called.append(args)
            return real([sys.executable, '-u', '-c', script], **kwargs)
        with patch.object(wc.subprocess, 'Popen', side_effect=start):
            with wc.Proxy('fixture-codex.exe', 'fixture.sock', time.monotonic() + 4) as proxy:
                self.assertEqual(proxy.call('thread/loaded/list', {}), {'data': []})
                process = proxy.process
        self.assertIsNotNone(process.poll())
        self.assertEqual(called, [['fixture-codex.exe', 'app-server', 'proxy', '--sock', 'fixture.sock']])


if __name__ == '__main__':
    unittest.main()
