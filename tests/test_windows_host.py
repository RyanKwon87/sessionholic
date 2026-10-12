"""Windows collection routing stays read-only; every transport here is synthetic."""
import base64
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat
import server
from transfer import Transfers
import test_goals_http as http_fixture

HOST = {'name': 'example-pc', 'label': '예제 PC', 'local': False, 'ssh': 'example-pc',
        'python': 'python.exe', 'platform': 'windows'}
SID = '019aaaaa-0000-7000-8000-000000000009'
REF = {'host': HOST['name'], 'agent': 'codex', 'home': '.codex', 'id': SID}
CLAUDE_REF = {'host': HOST['name'], 'agent': 'claude', 'home': '.claude',
              'id': '019aaaaa-0000-7000-8000-000000000010'}
ROW = {**REF, 'cwd': r'C:\Example\project', 'phase': 'idle', 'title': '예제 PC 작업'}
CLAUDE_ROW = {**CLAUDE_REF, 'cwd': r'C:\Example\project', 'phase': 'unknown', 'kind': 'interactive',
              'sessionId': CLAUDE_REF['id'], 'title': '예제 PC Claude 작업'}
READ = {'phase': 'idle', 'messages': [{'role': 'assistant', 'messageId': 'answer-one', 'text': '예제 PC 결과'}],
        'readOnly': True, 'runtimeKnown': True}
REASON = 'Windows 기기는 현재 세션 목록과 대화 읽기를 지원합니다.'


class WindowsConfigurationTest(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'hosts.json'
            path.write_text(json.dumps(rows))
            return server.load_hosts(path)

    def test_existing_mac_shape_and_posix_command_remain_unchanged(self):
        mac = {'name': 'example-mac', 'label': '예제 Mac', 'local': False,
               'ssh': 'example-mac', 'python': '/usr/bin/python3'}
        self.assertEqual(self.load([mac]), [mac])
        command = server.collector_command(mac, ['snapshot'])
        self.assertTrue(command[-1].startswith('PATH='))
        self.assertIn('/usr/bin/python3 -I - snapshot', command[-1])
        self.assertNotIn('EncodedCommand', command[-1])

    def test_windows_python_bare_or_absolute_executable_is_accepted(self):
        for executable in ('python.exe', r'C:\Python312\python.exe', r'C:\Program Files\Python\python.exe'):
            with self.subTest(executable=executable):
                self.assertEqual(self.load([{**HOST, 'python': executable}])[0]['python'], executable)
        self.assertEqual(self.load([HOST])[0]['platform'], 'windows')

    def test_windows_python_arguments_relative_paths_and_metacharacters_are_rejected(self):
        invalid = ['python.exe -c x', 'py -3', '/usr/bin/python3', r'bin\python.exe',
                   r'C:python.exe', r'\\server\share\python.exe', r'C:\..\python.exe',
                   r'C:\Python\python.exe & echo x', "C:\\O'Brien\\python.exe", 'python.exe\n']
        for executable in invalid:
            with self.subTest(executable=executable):
                with self.assertRaises(ValueError):
                    self.load([{**HOST, 'python': executable}])

    def test_unknown_platform_and_local_windows_are_rejected(self):
        for changes in ({'platform': 'unknown'}, {'local': True}, {'platform': 3}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.load([{**HOST, **changes}])

    def test_windows_command_is_encoded_powershell_and_separate_literal_arguments(self):
        command = server.collector_command({**HOST, 'python': r'C:\Program Files\Python\python.exe'},
                                           ['read', 'codex', SID, '.codex'])
        self.assertIn('--', command)
        self.assertEqual(command[-2], HOST['ssh'])
        remote = command[-1]
        self.assertIn('-NoProfile', remote)
        self.assertIn('-NonInteractive', remote)
        encoded = re.search(r'-EncodedCommand\s+([A-Za-z0-9+/=]+)', remote)
        self.assertIsNotNone(encoded)
        script = base64.b64decode(encoded.group(1)).decode('utf-16-le')
        self.assertIn("'C:\\Program Files\\Python\\python.exe'", script)
        for argument in ('read', 'codex', SID, '.codex'):
            self.assertIn("'" + argument + "'", script)
        self.assertNotIn('PATH=', script)
        self.assertNotIn('exec ', script)

    def test_remote_windows_stdin_is_standalone_collector_without_posix_prelude(self):
        with patch.object(server, 'remote_program', side_effect=AssertionError('POSIX prelude used')), \
                patch.object(server.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{"codex":[],"claude":[]}')) as run:
            result = server.run_collector(HOST, ['snapshot'], 30)
        self.assertEqual(result, {'codex': [], 'claude': []})
        stdin = run.call_args.kwargs['input']
        self.assertEqual(stdin, (Path(server.__file__).parent / 'windows_collector.py').read_bytes())
        self.assertNotIn(b"m.INJECTED=", stdin)
        self.assertNotIn(b"sys.modules['settings']", stdin)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_windows_watch_is_rejected_before_process_spawn(self):
        with patch.object(server.subprocess, 'Popen', side_effect=AssertionError('watch spawned')):
            with self.assertRaises((ValueError, RuntimeError)):
                server.start_watch(HOST, 'a' * 16)

    def test_windows_host_does_not_start_posix_resident_watcher(self):
        board = server.Board([HOST], 60, runner=lambda *_: {}, streamer=lambda *_: None)
        board.state[HOST['name']].update(ok=True, paused=False)
        entered = threading.Event()
        original = board.live_wanted
        def wanted(now):
            entered.set()
            return original(now)
        with patch.object(board, 'live_wanted', side_effect=wanted), \
                patch.object(server, 'HostStream', side_effect=AssertionError('POSIX watcher constructed')) as stream:
            try:
                board.viewer_joined()
                self.assertTrue(entered.wait(1))
            finally:
                board.close_live()
            stream.assert_not_called()


class WindowsRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = server.Board([HOST], 60, runner=lambda *_: self.fail('unexpected native collection'), live=False)
        self.board.state[HOST['name']].update(ok=True, data={'codex': [copy.deepcopy(ROW)], 'claude': [copy.deepcopy(CLAUDE_ROW)]})
        self.terms = SimpleNamespace(list=lambda: [], reserve=Mock(side_effect=AssertionError('terminal reserved')))
        self.flow = server.Workflow(self.board, self.terms, self.root)
        self.flow.profiles = Mock(return_value=[])

    def tearDown(self):
        self.temporary.cleanup()

    def test_capabilities_are_read_only_without_remote_profile_or_launcher_bootstrap(self):
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('remote helper invoked')):
            result = self.flow.capabilities()['hosts'][0]
        self.assertFalse(result['newSession']['available'])
        self.assertEqual(result['newSession']['reason'], REASON)
        accounts = [account for agent in result['agents'] for account in agent['accounts']]
        self.assertTrue(accounts)
        self.assertTrue(all(account['available'] is False for account in accounts))
        self.assertTrue(all(account['reason'] == REASON for account in accounts))

    def test_launch_app_server_and_plan_are_rejected_before_native_work(self):
        operations = [lambda: self.flow.start_app_server({'host': HOST['name'], 'home': '.codex'}),
                      lambda: self.flow.new_session({'host': HOST['name'], 'requestId': 'fixture-launch-0001'}),
                      lambda: self.flow.plan({'source': REF, 'target': {'host': HOST['name'], 'agent': 'codex', 'account': 'codex:.codex'}})]
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('write RPC')):
            for operation in operations:
                with self.assertRaisesRegex(ValueError, REASON):
                    operation()
        self.terms.reserve.assert_not_called()
        self.assertEqual(self.flow.requests, {})

    def test_windows_source_or_target_blocks_cross_host_transfer(self):
        mac = {'name': 'example-mac', 'label': '예제 Mac', 'local': True, 'python': '/usr/bin/python3'}
        self.board.hosts.append(mac)
        mac_source = {**ROW, 'host': mac['name'], 'cwd': '/example/project'}
        self.board.state[mac['name']] = {'name': mac['name'], 'label': mac['label'], 'ok': True, 'fetchedAt': 100, 'data': {'codex': [mac_source], 'claude': []}}
        targets = [({'host': HOST['name'], 'agent': 'codex', 'id': SID, 'home': '.codex'}, mac['name']),
                   ({'host': mac['name'], 'agent': 'codex', 'id': SID, 'home': '.codex'}, HOST['name'])]
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('transfer RPC')):
            for source, target in targets:
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, REASON):
                        self.flow.plan({'source': source, 'target': {'host': target, 'agent': 'codex', 'account': 'codex:.codex'}})

    def test_windows_read_rpc_uses_standalone_collector_and_never_installs_runtime(self):
        with patch.object(server, 'run_collector', return_value=READ) as read, \
                patch.object(self.flow.transfers, '_exchange', side_effect=AssertionError('runtime installed')):
            result = self.flow.transfers.rpc(HOST, 'chat', {'action': 'read', 'source': ROW}, timeout=20)
        self.assertEqual(result['messages'], READ['messages'])
        self.assertFalse(result['capabilities']['canSend'])
        self.assertEqual(result['capabilities']['reason'], REASON)
        self.assertEqual(read.call_args.args[0], HOST)
        self.assertEqual(read.call_args.args[1], ['read', 'codex', SID, '.codex'])
        self.assertFalse(self.flow.transfers.runtime_installed)
        self.assertFalse(self.flow.transfers.runtime_paths)

    def test_windows_transfer_rpc_refuses_every_unimplemented_operation(self):
        cases = [('chat', {'action': 'send', 'source': ROW}), ('attachment', {'cwd': ROW['cwd']}),
                 ('profiles', {}), ('start_app_server', {'home': '.codex'}), ('stage_handoff', {}),
                 ('control_execute', {'source': ROW}), ('control_preview', {'source': ROW})]
        with patch.object(server, 'run_collector', side_effect=AssertionError('unexpected collector')), \
                patch.object(self.flow.transfers, '_exchange', side_effect=AssertionError('runtime installed')):
            for operation, payload in cases:
                with self.subTest(operation=operation, payload=payload):
                    with self.assertRaisesRegex(ValueError, REASON):
                        self.flow.transfers.rpc(HOST, operation, payload)

    def test_chat_read_works_but_composer_attachment_and_terminal_actions_are_unavailable(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(server, 'run_collector', return_value=READ):
            result = service.state({'source': REF})
        self.assertEqual(result['messages'], READ['messages'])
        self.assertFalse(result['capability']['supported'])
        self.assertFalse(result['capability']['attachments'])
        self.assertEqual(result['capability']['reason'], REASON)
        self.assertNotIn('action', result['capability'])
        self.assertFalse(result['promptOpenable'])

    def test_wrong_host_or_account_reference_is_rejected_before_read_transport(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(server, 'run_collector', side_effect=AssertionError('wrong scope read')):
            for reference in ({**REF, 'host': 'not-registered'}, {**REF, 'home': '.codex-other'}):
                with self.subTest(reference=reference):
                    with self.assertRaises(ValueError):
                        service.state({'source': reference})

    def test_unknown_windows_runtime_remains_readable_and_explicit_in_chat(self):
        service = chat.Chat(self.board, self.flow, self.root)
        historical = {**READ, 'phase': 'unknown', 'runtimeKnown': False}
        with patch.object(server, 'run_collector', return_value=historical):
            result = service.state({'source': REF})
        self.assertEqual(result['phase'], 'unknown')
        self.assertTrue(result['readOnly'])
        self.assertFalse(result['runtimeKnown'])
        self.assertEqual(result['messages'], historical['messages'])

    def test_windows_claude_read_does_not_suggest_opening_a_posix_terminal(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(server, 'run_collector', return_value=READ):
            result = service.state({'source': CLAUDE_REF})
        self.assertFalse(result['capability']['supported'])
        self.assertFalse(result['capability']['attachments'])
        self.assertEqual(result['capability']['reason'], REASON)
        self.assertNotIn('action', result['capability'])
        self.assertFalse(result['promptOpenable'])

    def test_send_and_upload_are_rejected_before_any_remote_call_or_attachment_file(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('remote write')):
            with self.assertRaisesRegex(ValueError, REASON):
                service.send({'source': REF, 'requestId': 'fixture-send-0001', 'text': '합성 입력', 'attachments': []})
            with self.assertRaisesRegex(ValueError, REASON):
                service.upload(REF, 'fixture.txt', b'fixture')
        self.assertFalse(list(self.root.rglob('attachments/*.json')))


class WindowsHTTPTest(unittest.TestCase):
    request = http_fixture.GoalsHTTPTest.request
    tearDown = http_fixture.GoalsHTTPTest.tearDown

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.board = server.Board([HOST], 60, runner=lambda *_: self.fail('unexpected native collector'), live=False)
        self.board.state[HOST['name']].update(ok=True, data={'codex': [copy.deepcopy(ROW)], 'claude': []})
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'fixture-token', state_dir=self.temp.name,
                                       terminals=SimpleNamespace(list=lambda: [], close=lambda: None))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        response, result = self.request('POST', '/login', {'token': 'fixture-token'}, authenticated=False)
        self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        self.csrf = result['csrfToken']

    def test_preview_reports_unsupported_controls_without_native_rpc(self):
        with patch.object(self.httpd.workflow.transfers, 'rpc', side_effect=AssertionError('native RPC')):
            response, result = self.request('POST', '/api/session/preview', {'source': REF})
        self.assertEqual(response.status, 200)
        self.assertEqual(result['phase'], ROW['phase'])
        self.assertIsNone(result['targetProof'])
        self.assertFalse(result['capabilities']['interrupt']['supported'])
        self.assertFalse(result['capabilities']['terminate']['supported'])

    def test_terminate_is_rejected_and_board_archival_remains_available(self):
        with patch.object(self.httpd.workflow.transfers, 'rpc', side_effect=AssertionError('native RPC')):
            response, result = self.request('POST', '/api/session/action', {'source': REF, 'action': 'terminate',
                                            'requestId': 'fixture-control-0001', 'targetProof': 'aaa.' + 'a' * 64})
            self.assertEqual(response.status, 400)
            self.assertEqual(result['error'], REASON)
            response, result = self.request('POST', '/api/session/archive', {'source': REF, 'archived': True})
        self.assertEqual(response.status, 200)
        self.assertEqual(result, {'archived': True, 'changed': 1, 'executionChanged': False})


if __name__ == '__main__':
    unittest.main()
