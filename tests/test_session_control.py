import copy
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import session_control as control
import transfer_native as native
import transfer_worker


class Client:
    def __init__(self, threads, archive=None, loaded=None):
        self.threads = iter(threads)
        self.calls = []
        self.archive, self.loaded = archive, loaded

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/queue/list':
            return {'data': [], 'nextCursor': None}
        if method == 'turn/interrupt':
            return {}
        if method == 'thread/archive' and self.archive:
            self.archive()
            return {}
        if method == 'thread/loaded/list':
            return {'data': self.loaded or [], 'nextCursor': None}
        if method == 'thread/read':
            result = next(self.threads)
            if isinstance(result, Exception):
                raise result
            # The metadata read; the turns come by thread/turns/list from the same snapshot.
            self.current = result
            thread = result.get('thread') if isinstance(result, dict) else None
            return {**result, 'thread': {**thread, 'turns': []}} if isinstance(thread, dict) and not params.get('includeTurns') else result
        if method == 'thread/turns/list':
            turns = self.current['thread'].get('turns')
            return {'data': list(reversed(turns)) if isinstance(turns, list) else turns, 'nextCursor': None}
        raise AssertionError(method)


class SessionControlTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.api = control.SessionControl(self.home, self.home / 'control')
        self.codex = {'agent': 'codex', 'id': str(uuid.uuid4()), 'home': '.codex'}
        self.claude = {'agent': 'claude', 'id': 'terminal-1234', 'home': '.claude', 'cwd': '/fixture/project'}
        self.sid = str(uuid.uuid4())
        self.process = {'pid': 424242, 'uid': os.getuid(), 'ppid': 20, 'start': '42.100',
                        'procStart': 'Mon Oct 12 00:00:00 2026', 'exe': '/fixture/claude',
                        'zombie': False, 'binding': 'claude-session-registry'}
        self.addCleanup(self.temporary.cleanup)

    def thread(self, active=True, turn='turn-1', updated=1):
        return {'thread': {'id': self.codex['id'], 'updatedAt': updated,
                           'status': {'type': 'active' if active else 'idle'},
                           'turns': [{'id': turn, 'status': 'inProgress' if active else 'interrupted'}]}}

    def claude_row(self, kind='interactive', state='working'):
        return {'id': self.claude['id'], 'sessionId': self.sid, 'cwd': self.claude['cwd'],
                'kind': kind, 'pid': self.process['pid'], 'state': state,
                'status': 'busy' if state == 'working' else 'idle', 'startedAt': 100}

    def observed(self, agent='claude'):
        source = self.claude if agent == 'claude' else self.codex
        method = 'claude-cli-process' if agent == 'claude' else 'codex-cli-process'
        return {'source': native._identity(source), 'phase': 'working', 'process': self.process,
                'native': {'safeToTransfer': True},
                'target': {'identity': native._identity(source), 'process': control._process_key(self.process)},
                'capabilities': {'interrupt': control._capability(False, reason='fixture'),
                                 'terminate': control._capability(True, method)}}

    def codex_record(self):
        folder = self.home / '.codex'
        folder.mkdir()
        cwd = self.home / 'project'
        cwd.mkdir()
        self.codex['cwd'] = str(cwd)
        database = folder / 'state_5.sqlite'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE threads (id TEXT,cwd TEXT,archived INTEGER)')
            connection.execute('INSERT INTO threads VALUES (?,?,0)', (self.codex['id'], str(cwd)))
        return database

    def test_external_codex_without_exact_pid_can_archive_only_its_thread(self):
        database = self.codex_record()
        def archive():
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('UPDATE threads SET archived=1 WHERE id=?', (self.codex['id'],))
        clients = [Client([self.thread()]), Client([self.thread()]), Client([self.thread()], archive=archive)]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None), patch.object(os, 'kill') as kill:
            preview = self.api.preview(self.codex)
            capability = preview['capabilities']['terminate']
            self.assertEqual(capability['method'], 'codex-thread-archive')
            self.assertIn('하위 에이전트', capability['effect'])
            result = self.api.execute(self.codex, 'terminate', preview['targetProof'], 'request-archive')
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['archived'])
        self.assertEqual(result['scope'], 'thread_and_descendants')
        self.assertEqual(result['scopeReadback'], 'selected-session')
        self.assertEqual([call for call in clients[-1].calls if call[0] == 'thread/archive'],
                         [('thread/archive', {'threadId': self.codex['id']})])
        self.assertTrue(self.api.close_provenance(self.codex)['blockedByBoard'])
        kill.assert_not_called()

    def test_archive_without_both_readbacks_is_unconfirmed_and_blocks_resume(self):
        self.codex_record()
        clients = [Client([self.thread()]), Client([self.thread()]),
                   Client([self.thread()], archive=lambda: None, loaded=[self.codex['id']])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            result = self.api.execute(self.codex, 'terminate', preview['targetProof'], 'request-archive')
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertTrue(self.api.close_provenance(self.codex)['blockedByBoard'])

    def test_archive_requires_exact_saved_cwd_and_account(self):
        self.codex_record()
        with patch.object(native, '_client', return_value=Client([self.thread()])), patch.object(control, '_codex_process', return_value=None):
            view = self.api.preview({**self.codex, 'cwd': str(self.home)})
        self.assertFalse(view['capabilities']['terminate']['supported'])

    def test_archive_rechecks_native_revision_before_mutation(self):
        self.codex_record()
        clients = [Client([self.thread()]), Client([self.thread()]), Client([self.thread(updated=2)])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            result = self.api.execute(self.codex, 'terminate', preview['targetProof'], 'request-archive')
        self.assertFalse(result['applied'])
        self.assertEqual(result['status'], 'stale')
        self.assertNotIn('thread/archive', [call[0] for call in clients[-1].calls])
        self.assertFalse(self.api.close_provenance(self.codex)['blockedByBoard'])

    def test_unknown_termination_blocks_resume_but_interrupt_does_not(self):
        with patch.object(self.api, '_observe', return_value=self.observed()), patch.object(self.api, '_apply', side_effect=TimeoutError()):
            preview = self.api.preview(self.claude)
            self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-unknown')
        self.assertTrue(self.api.close_provenance(self.claude)['blockedByBoard'])
        self.assertFalse(self.api.close_provenance(self.codex)['blockedByBoard'])

    def test_legacy_confirmed_termination_receipt_remains_blocked(self):
        self.api.directory.mkdir(mode=0o700)
        record = {'result': {'source': native._identity(self.codex), 'action': 'terminate',
                            'applied': True, 'status': 'terminated'}}
        control._save(self.api.directory / 'request-legacy.json', record)
        self.assertTrue(self.api.close_provenance(self.codex)['blockedByBoard'])

    def test_stale_unapplied_termination_does_not_mark_closed(self):
        with patch.object(self.api, '_observe', return_value=self.observed()), patch.object(self.api, '_apply',
                return_value={'applied': False, 'confirmed': False, 'status': 'stale'}):
            preview = self.api.preview(self.claude)
            self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-stale')
        self.assertFalse(self.api.close_provenance(self.claude)['blockedByBoard'])

    def test_exact_codex_active_turn_interrupt_without_board_origin(self):
        clients = [Client([self.thread()]), Client([self.thread(updated=2)]),
                   Client([self.thread(updated=3), self.thread(False, updated=4)])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            self.assertTrue(preview['capabilities']['interrupt']['supported'])
            self.assertFalse(preview['capabilities']['terminate']['supported'])
            result = self.api.execute(self.codex, 'interrupt', preview['targetProof'], 'request-1234')
        self.assertEqual(result['status'], 'interrupted')
        self.assertTrue(result['confirmed'])
        self.assertEqual([call for call in clients[-1].calls if call[0] == 'turn/interrupt'],
                         [('turn/interrupt', {'threadId': self.codex['id'], 'turnId': 'turn-1'})])
        self.assertFalse(any(method in ('thread/archive', 'thread/unsubscribe', 'thread/delete')
                             for client in clients for method, args in client.calls))

    def test_new_turn_invalidates_preview_and_is_never_interrupted(self):
        clients = [Client([self.thread()]), Client([self.thread(turn='new-turn')])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            result = self.api.execute(self.codex, 'interrupt', preview['targetProof'], 'request-1234')
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(any(method == 'turn/interrupt' for client in clients for method, args in client.calls))

    def test_timeout_after_interrupt_records_uncertainty_and_never_retries(self):
        clients = [Client([self.thread()]), Client([self.thread()]), Client([self.thread(), TimeoutError()])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            result = self.api.execute(self.codex, 'interrupt', preview['targetProof'], 'request-1234')
            replay = self.api.execute(self.codex, 'interrupt', preview['targetProof'], 'request-1234')
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertFalse(result['confirmed'])
        self.assertIsNone(result['applied'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(sum(method == 'turn/interrupt' for client in clients for method, args in client.calls), 1)

    def test_codex_no_progress_readback_never_claims_success(self):
        clients = [Client([self.thread()]), Client([self.thread()]), Client([self.thread(), self.thread()])]
        with patch.object(native, '_client', side_effect=clients), patch.object(control, '_codex_process', return_value=None):
            preview = self.api.preview(self.codex)
            result = self.api.execute(self.codex, 'interrupt', preview['targetProof'], 'request-1234')
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertTrue(result['applied'])

    def test_proof_expired_tampered_or_cross_account_cannot_mutate(self):
        with patch.object(self.api, '_observe', return_value=self.observed()), patch.object(self.api, '_apply') as apply:
            preview = self.api.preview(self.claude)
            with self.assertRaises(ValueError):
                self.api.execute(self.claude, 'terminate', preview['targetProof'] + 'x', 'request-1234')
            with patch.object(control.time, 'time', return_value=time.time() + 90), self.assertRaises(ValueError):
                self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
            with self.assertRaises(ValueError):
                self.api.execute(self.codex, 'terminate', preview['targetProof'], 'request-1234')
            apply.assert_not_called()

    def test_pid_reuse_invalidates_target_before_signal(self):
        before, after = self.observed(), self.observed()
        after['target'] = copy.deepcopy(after['target'])
        after['target']['process']['start'] = '42.999'
        with patch.object(self.api, '_observe', side_effect=[before, after]), patch.object(os, 'kill') as kill:
            preview = self.api.preview(self.claude)
            result = self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
        self.assertEqual(result['status'], 'stale')
        kill.assert_not_called()

    def test_same_request_or_same_proof_never_mutates_twice(self):
        done = {'confirmed': True, 'applied': True, 'status': 'terminated', 'reason': None}
        with patch.object(self.api, '_observe', return_value=self.observed()), patch.object(self.api, '_apply', return_value=done) as apply:
            preview = self.api.preview(self.claude)
            first = self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
            replay = self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
            other = self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-5678')
            with self.assertRaises(ValueError):
                self.api.execute(self.claude, 'interrupt', preview['targetProof'], 'request-1234')
        self.assertTrue(first['confirmed'])
        self.assertTrue(replay['replayed'])
        self.assertTrue(other['replayed'])
        self.assertEqual(apply.call_count, 1)

    def test_pending_receipt_precedes_mutation_and_survives_lost_reply(self):
        def interrupted(*args):
            receipt = json.loads((self.home / 'control/request-1234.json').read_text())
            self.assertEqual(receipt['result']['status'], 'unconfirmed')
            raise TimeoutError()
        with patch.object(self.api, '_observe', return_value=self.observed()), patch.object(self.api, '_apply', side_effect=interrupted) as apply:
            preview = self.api.preview(self.claude)
            self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
            self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
        self.assertEqual(apply.call_count, 1)

    def test_preview_does_not_expose_process_command_or_pid(self):
        with patch.object(self.api, '_observe', return_value=self.observed()):
            preview = self.api.preview(self.claude)
        encoded = json.dumps(preview)
        self.assertNotIn(str(self.process['pid']), encoded)
        self.assertNotIn('/fixture/claude', encoded)
        self.assertEqual((self.home / 'control').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / 'control/proof-key').stat().st_mode & 0o777, 0o600)

    def test_external_claude_background_stop_preserves_record(self):
        running, stopped = self.claude_row('background'), self.claude_row('background', 'stopped')
        outputs = [json.dumps([running]).encode(), json.dumps([running]).encode(), b'', json.dumps([stopped]).encode()]
        with patch.object(native, '_claude_binary', return_value='/fixture/claude'), patch.object(native, '_claude_run', side_effect=outputs) as run:
            preview = self.api.preview(self.claude)
            self.assertFalse(preview['capabilities']['interrupt']['supported'])
            self.assertTrue(preview['capabilities']['terminate']['supported'])
            result = self.api.execute(self.claude, 'terminate', preview['targetProof'], 'request-1234')
        self.assertTrue(result['confirmed'])
        commands = [call.args[1] for call in run.call_args_list]
        self.assertEqual(commands.count(['stop', self.claude['id']]), 1)
        self.assertFalse(any(command[0] in ('rm', 'purge', 'attach') for command in commands))

    def test_background_replacement_is_not_confirmed_as_stopped(self):
        observed = self.observed()
        observed.update(binary='/fixture/claude', native=self.claude_row('background'))
        observed['capabilities']['terminate']['method'] = 'claude-background-stop'
        replaced = {**self.claude_row('background', 'stopped'), 'sessionId': str(uuid.uuid4())}
        with patch.object(native, '_claude_run', side_effect=[b'', json.dumps([replaced]).encode()]):
            result = self.api._apply(self.claude, 'terminate', observed, time.monotonic() + 3)
        self.assertFalse(result['confirmed'])

    def test_interactive_claude_lookup_uses_native_pid_registry(self):
        row = self.claude_row()
        with patch.object(native, '_claude_binary', return_value='/fixture/claude'), patch.object(native, '_claude_run', return_value=json.dumps([row]).encode()), patch.object(control, '_claude_process', return_value=self.process):
            preview = self.api.preview(self.claude)
        self.assertTrue(preview['capabilities']['terminate']['supported'])
        self.assertEqual(preview['capabilities']['terminate']['method'], 'claude-cli-process')

    def test_daemon_and_missing_or_ambiguous_claude_id_are_unsupported(self):
        for rows in ([], [self.claude_row(), self.claude_row()], [self.claude_row('daemon')]):
            with self.subTest(rows=len(rows)), patch.object(native, '_claude_binary', return_value='/fixture/claude'), patch.object(native, '_claude_run', return_value=json.dumps(rows).encode()), patch.object(os, 'kill') as kill:
                preview = self.api.preview(self.claude)
            self.assertFalse(preview['capabilities']['terminate']['supported'])
            kill.assert_not_called()

    def registry(self, row=None, name=None):
        folder = self.home / '.claude/sessions'
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = folder / (name or (str(self.process['pid']) + '.json'))
        record = {**self.claude_row(), 'procStart': self.process['procStart'], 'pidDomain': 'darwin'}
        record.update(row or {})
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return path

    def test_registry_requires_exact_session_start_kind_uid_and_executable(self):
        self.registry()
        with patch.object(control, '_process_info', return_value=self.process), patch.object(control, '_agent_executable', return_value=True):
            found = control._claude_process(self.claude_row(), self.home, time.monotonic() + 3)
        self.assertEqual(found['pid'], self.process['pid'])
        for changed in ({'sessionId': str(uuid.uuid4())}, {'procStart': 'different'}, {'kind': 'daemon'},
                        {'cwd': '/other/project'}, {'pid': 999999}):
            self.registry(changed)
            with self.subTest(changed=changed), patch.object(control, '_process_info', return_value=self.process), patch.object(control, '_agent_executable', return_value=True):
                self.assertIsNone(control._claude_process(self.claude_row(), self.home, time.monotonic() + 3))

    def test_registry_symlink_or_multiple_live_owners_never_targeted(self):
        original = self.registry()
        other = original.with_name('525252.json')
        other.symlink_to(original)
        with patch.object(control, '_process_info', return_value=self.process), patch.object(control, '_agent_executable', return_value=True):
            self.assertEqual(control._claude_process(self.claude_row(), self.home, time.monotonic() + 3)['pid'], self.process['pid'])
        original.unlink()
        original.symlink_to(self.home / 'missing')
        with patch.object(control, '_process_info', return_value=self.process), patch.object(control, '_agent_executable', return_value=True):
            self.assertIsNone(control._claude_process(self.claude_row(), self.home, time.monotonic() + 3))

    def test_codex_process_requires_exact_resume_and_home_boundary(self):
        process = {**self.process, 'exe': '/fixture/codex'}
        row = '%s %s /fixture/codex\n' % (process['pid'], os.getuid())
        args = ['/fixture/codex', 'resume', self.codex['id']]
        for env, expected in (({'CODEX_HOME': str(self.home / '.codex')}, True),
                              ({'HOME': str(self.home)}, True),
                              ({'CODEX_HOME': str(self.home / '.codex-other')}, False), ({}, False)):
            with self.subTest(env=env), patch.object(control, '_ps', return_value=row), patch.object(control, '_process_info', return_value=process), patch.object(control, '_agent_executable', return_value=True), patch.object(control, '_process_args', return_value=(args, env)):
                self.assertEqual(control._codex_process(self.codex, self.home, time.monotonic() + 3) is not None, expected)

    def test_codex_fork_remote_last_and_prompt_uuid_do_not_bind(self):
        for args in (['codex', 'resume', '--last'], ['codex', 'fork', self.codex['id']],
                     ['codex', 'resume', self.codex['id'], '--remote', 'unix://elsewhere'],
                     ['codex', 'do something with ' + self.codex['id']]):
            self.assertFalse(control._codex_resume_argument(args, self.codex['id']))

    def test_process_revalidation_prevents_pid_reuse_at_last_boundary(self):
        observed = self.observed()
        with patch.object(control, '_descendants', return_value=[]), patch.object(control, '_process_info', return_value={**self.process, 'start': 'other'}), patch.object(os, 'kill') as kill:
            result = self.api._terminate_process(observed, time.monotonic() + 3)
        self.assertEqual(result['status'], 'stale')
        kill.assert_not_called()
        # The last read before the signal is the kernel's alone: no `ps` widens the gap.
        with patch.object(control, '_descendants', return_value=[]), \
                patch.object(control, '_process_info', return_value=self.process) as info, \
                patch.object(control, '_process_ended', return_value=True), patch.object(os, 'kill'):
            self.api._terminate_process(observed, time.monotonic() + 3)
        self.assertIs(info.call_args_list[0].kwargs.get('start_label'), False)

    def test_live_descendants_prevent_full_termination_confirmation(self):
        child = {**self.process, 'pid': 555555}
        with patch.object(control, '_descendants', return_value=[child]), patch.object(control, '_process_info', side_effect=[self.process, None, child]), patch.object(control, '_process_ended', side_effect=[True, False]), patch.object(os, 'kill') as kill:
            result = self.api._terminate_process(self.observed(), time.monotonic() + 3)
        self.assertFalse(result['confirmed'])
        self.assertEqual(kill.call_args.args, (self.process['pid'], signal.SIGTERM))

    def test_unreadable_process_does_not_confirm_exit(self):
        with patch.object(control, '_darwin_info', return_value=None), patch.object(os, 'kill', side_effect=PermissionError()), patch.object(control.sys, 'platform', 'darwin'):
            self.assertFalse(control._process_ended(self.process, None))

    def test_darwin_args_preserve_argv_without_returning_environment(self):
        if sys.platform != 'darwin':
            self.skipTest('Darwin argument reader')
        child = subprocess.Popen(['/bin/sleep', '20'])
        try:
            args, selectors = control._process_args(child.pid)
            self.assertEqual(args, ['/bin/sleep', '20'])
            self.assertLessEqual(set(selectors), {'HOME', 'CODEX_HOME'})
        finally:
            child.terminate()
            child.wait(timeout=2)

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'native process identity unavailable')
    def test_real_isolated_child_termination_does_not_touch_neighbor(self):
        target = subprocess.Popen(['/bin/sleep', '20'])
        neighbor = subprocess.Popen(['/bin/sleep', '20'])
        try:
            observed = self.observed()
            observed['process'] = control._process_info(target.pid, time.monotonic() + 3)
            result = self.api._terminate_process(observed, time.monotonic() + 3)
            self.assertTrue(result['confirmed'])
            target.wait(timeout=2)
            self.assertEqual(target.returncode, -signal.SIGTERM)
            self.assertIsNone(neighbor.poll())
        finally:
            for child in (target, neighbor):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=2)

    def test_state_symlink_and_public_proof_key_are_rejected(self):
        elsewhere = self.home / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        self.api.directory.symlink_to(elsewhere)
        with patch.object(self.api, '_observe', return_value=self.observed()), self.assertRaises(ValueError):
            self.api.preview(self.claude)
        self.api.directory.unlink()
        with patch.object(self.api, '_observe', return_value=self.observed()):
            self.api.preview(self.claude)
            (self.api.directory / 'proof-key').chmod(0o644)
            with self.assertRaises(ValueError):
                self.api.preview(self.claude)

    def test_remote_worker_routes_same_controls(self):
        with patch.object(control, 'SessionControl') as cls:
            transfer_worker.rpc({'op': 'control_preview', 'args': {'source': self.codex}})
            cls.return_value.preview.assert_called_once_with(self.codex)
            transfer_worker.rpc({'op': 'control_execute', 'args': {'source': self.codex, 'action': 'interrupt', 'targetProof': 'fixture-proof', 'requestId': 'request-1234'}})
            cls.return_value.execute.assert_called_once_with(self.codex, 'interrupt', 'fixture-proof', 'request-1234')


if __name__ == '__main__':
    unittest.main()
