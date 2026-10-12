"""Windows runtime routing; every transport here is synthetic."""
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
REASON = server.WINDOWS_READ_ONLY
LAUNCHER = {'label': 'PC 새 세션', 'choices': [
    {'label': 'Codex · 예제', 'command': ['C:\\Tools\\codex.exe', '--cd', 'C:\\Example\\project'], 'cwd': 'C:\\Example\\project'},
    {'label': "Claude · it's", 'command': ['C:\\Tools\\claude.exe']}]}


def decoded(argv):
    command = argv[-1]
    prefix = 'powershell.exe -NoProfile -EncodedCommand '
    assert command.startswith(prefix), command
    return base64.b64decode(command[len(prefix):]).decode('utf-16le')


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import pathlib
    import shutil
    import tempfile
    from unittest import mock
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", pathlib.Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


class WindowsConfigurationTest(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'hosts.json'
            path.write_text(json.dumps(rows))
            return server.load_hosts(path)

    def test_only_a_pc_takes_literal_new_session_choices(self):
        pc = {'name': 'example-pc', 'ssh': 'example-pc', 'platform': 'windows', 'launcher': LAUNCHER}
        self.assertEqual(self.load([pc])[0]['launcher'], {'label': 'PC 새 세션', 'choices': LAUNCHER['choices']})
        self.assertEqual(self.load([{**pc, 'launcher': {'choices': LAUNCHER['choices'][1:]}}])[0]['launcher']['label'], '새 세션')
        with self.assertRaisesRegex(ValueError, 'Windows 기기에만'):
            self.load([{'name': 'example-mac', 'ssh': 'example-mac', 'launcher': LAUNCHER}])
        choice = LAUNCHER['choices'][0]
        for bad in ({'choices': []}, {'choices': [choice] * 13}, {'choices': [choice], 'extra': 1},
                    {'choices': [{**choice, 'command': ['codex.exe']}]},
                    {'choices': [{**choice, 'command': ['\\\\server\\share\\codex.exe']}]},
                    {'choices': [{**choice, 'command': [choice['command'][0], 'say "hi"']}]},
                    {'choices': [{**choice, 'command': [choice['command'][0], 'line\nbreak']}]},
                    {'choices': [{**choice, 'cwd': 'relative'}]},
                    {'choices': [{**choice, 'label': ''}]},
                    {'choices': [{**choice, 'run': 'x'}]}):
            with self.assertRaisesRegex(ValueError, '새 세션 선택지 형식'):
                self.load([{**pc, 'launcher': bad}])

    def test_every_powershell_single_quote_is_doubled_and_altered_arguments_are_refused(self):
        import transfer
        # PowerShell also ends a single-quoted string at ‘ ’ ‚ ‛.
        self.assertEqual(transfer.ps_quote("Ryan’s ‘x’ ‚y‛ 'z' $a `b; c"), "'Ryan’’s ‘‘x’’ ‚‚y‛‛ ''z'' $a `b; c'")
        launcher = server.windows_launcher_setting({'label': 'Ryan’s PC', 'choices': [
            {'label': 'Codex ’; Start-Process calc; ’', 'command': ['C:\\Tools\\codex.exe', 'it’s']},
            {'label': 'two', 'command': ['C:\\Tools\\codex.exe']}]})
        script = decoded([transfer.windows_encoded_command(transfer.windows_launcher_script(launcher))])
        self.assertIn("Write-Host 'Ryan’’s PC'", script)
        self.assertIn("Label='Codex ’’; Start-Process calc; ’’'", script)
        self.assertIn("Args=@('it’’s')", script)
        choice = LAUNCHER['choices'][0]
        for argument in ('C:\\My Folder\\', 'say "hi"'):
            with self.assertRaisesRegex(ValueError, '새 세션 선택지 형식'):
                server.windows_launcher_setting({'choices': [{**choice, 'command': [choice['command'][0], argument]}]})
        server.windows_launcher_setting({'choices': [{**choice, 'command': [choice['command'][0], 'C:\\Folder\\']}]})
        # A label or folder is no program argument: any name a PC allows.
        server.windows_launcher_setting({'label': '"PC" 메뉴', 'choices': [
            {**choice, 'label': 'Codex "main"', 'cwd': 'C:\\Example Folder\\Projects\\'}]})
        long = [{**choice, 'command': [choice['command'][0]] + ['x' * 300] * 4} for _ in range(12)]
        with self.assertRaisesRegex(ValueError, '너무 많거나 깁니다'):
            server.windows_launcher_setting({'choices': long})
        # A PC folder named with a curly quote stays one literal path in the terminal command.
        spec = {'argv': ['C:\\Tools\\claude.exe', '--resume', SID], 'cwd': 'C:\\Projects\\b’; whoami; ’', 'env': {}}
        script = decoded(Transfers([HOST], Path(tempfile.mkdtemp())).windows_terminal(HOST, spec))
        self.assertIn("Set-Location -LiteralPath 'C:\\Projects\\b’’; whoami; ’’'; & ", script)
        for argv in (['C:\\Tools\\codex.exe', '--cd', 'C:\\My Folder\\'], ['C:\\Tools\\codex.exe', 'a"b']):
            with self.assertRaisesRegex(ValueError, '연결 정보'):
                Transfers([HOST], Path(tempfile.mkdtemp())).windows_terminal(HOST, {**spec, 'argv': argv})

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

    def test_existing_codex_accounts_allow_attach_but_new_session_is_unavailable(self):
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('remote helper invoked')):
            result = self.flow.capabilities()['hosts'][0]
        self.assertEqual(result['platform'], 'windows', "the terminal's keys follow the host")
        self.assertFalse(result['newSession']['available'])
        codex = next(agent for agent in result['agents'] if agent['id'] == 'codex')
        self.assertTrue(all(account['available'] for account in codex['accounts']))

    def test_app_server_stays_unsupported_and_new_session_needs_the_hosts_choices(self):
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('write RPC')):
            with self.assertRaisesRegex(ValueError, REASON):
                self.flow.start_app_server({'host': HOST['name'], 'home': '.codex'})
            with self.assertRaisesRegex(ValueError, '새 세션 선택지가 기기 설정에 없습니다'):
                self.flow.new_session({'host': HOST['name'], 'requestId': 'fixture-launch-0001'})
        self.terms.reserve.assert_not_called()

    def test_new_session_opens_the_hosts_menu_in_a_board_terminal(self):
        host = self.board.hosts[0]
        host['launcher'] = server.windows_launcher_setting(LAUNCHER)
        created = []
        reserve = Mock()
        reserve.return_value.__enter__ = Mock(return_value=None)
        reserve.return_value.__exit__ = Mock(return_value=False)
        self.terms.reserve = reserve
        self.terms.create = lambda key, argv, cwd, env, meta: created.append((key, argv, meta)) or {'id': 'fixture-term'}
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('no helper for a PC menu')), \
                patch.object(self.flow.transfers, 'launcher', side_effect=AssertionError('no Mac launcher read')):
            status = self.flow.new_session_status(host)
            result = self.flow.new_session({'host': HOST['name'], 'requestId': 'fixture-launch-0002'})
            again = self.flow.new_session({'host': HOST['name'], 'requestId': 'fixture-launch-0002'})
        self.assertEqual(status, {'available': True, 'label': 'PC 새 세션', 'reason': None})
        self.assertEqual((result, again), ({'terminal': {'id': 'fixture-term'}, 'mode': 'new-session'},) * 2)
        self.assertEqual(len(created), 1, 'a repeated request opens no second terminal')
        key, argv, meta = created[0]
        self.assertEqual((meta['title'], meta['host'], meta['mode']), ('PC 새 세션', HOST['name'], 'new-session'))
        self.assertEqual(argv[-2:-1], [HOST['ssh']])
        self.assertIn('-tt', argv)
        script = decoded(argv)
        self.assertIn("Write-Host 'PC 새 세션'", script)
        self.assertIn("@{Label='Codex · 예제'; Exe='C:\\Tools\\codex.exe'; Args=@('--cd', 'C:\\Example\\project'); "
                      "Cwd='C:\\Example\\project'}", script)
        self.assertIn("@{Label='Claude · it''s'; Exe='C:\\Tools\\claude.exe'; Args=@(); Cwd=''}", script)
        self.assertIn("Read-Host '선택'", script)
        self.assertNotIn(HOST['label'], script)
        # One choice runs straight away, with no menu.
        one = decoded(self.flow.transfers.windows_launcher(HOST, server.windows_launcher_setting(
            {'choices': LAUNCHER['choices'][:1]})))
        self.assertNotIn('Read-Host', one)
        self.assertIn("& $c.Exe @($c.Args)", one)
        self.board.state[HOST['name']]['ok'] = False
        self.assertFalse(self.flow.new_session_status(host, online=False)['available'])
        with self.assertRaisesRegex(RuntimeError, '연결할 수 없어'):
            self.flow.new_session({'host': HOST['name'], 'requestId': 'fixture-launch-0003'})

    def test_same_windows_session_can_plan_terminal_without_reserving_it(self):
        with patch.object(self.flow.transfers, 'rpc', return_value={'argv': ['fixture'], 'cwd': ROW['cwd']}) as rpc, \
                patch.object(server.shutil, 'which', return_value='/fixture/tmux'):
            result = self.flow.plan({'source': REF, 'target': {'host': HOST['name'], 'agent': 'codex', 'account': 'codex:.codex'}})
        self.assertTrue(result['allowed'])
        self.assertEqual(result['mode'], 'attach')
        self.assertEqual(rpc.call_args.args[1], 'terminal_plan')
        self.terms.reserve.assert_not_called()

    def test_a_pc_conversation_resumed_in_a_board_terminal_is_reopened_not_resumed_again(self):
        import hashlib
        target = {'host': HOST['name'], 'agent': 'claude', 'account': 'claude:default'}
        with patch.object(self.flow.transfers, 'rpc', return_value={'argv': ['fixture'], 'cwd': ROW['cwd'], 'resumed': True}) as rpc, \
                patch.object(server.shutil, 'which', return_value='/fixture/tmux'):
            first = self.flow.plan({'source': CLAUDE_REF, 'target': target})
            self.assertEqual((first['mode'], first['allowed']), ('attach', True))
            self.assertIn('PC에 저장된 대화', first['summary'])
            # It now runs in that terminal, so the PC would refuse a second resume.
            key = hashlib.sha256(json.dumps([self.flow.identity(self.board.source(CLAUDE_REF)), target],
                                            sort_keys=True).encode()).hexdigest()
            self.terms.list = lambda: [{'alive': True, 'key': key + ':fixture'}]
            rpc.side_effect = ValueError('이 Claude 세션은 PC 터미널에서 실행 중입니다.')
            again = self.flow.plan({'source': CLAUDE_REF, 'target': target})
        self.assertEqual((again['mode'], again['allowed']), ('reconnect', True))
        self.assertEqual(rpc.call_count, 1)

    def test_cross_host_or_account_plan_remains_unsupported(self):
        with self.assertRaisesRegex(ValueError, REASON):
            self.flow.plan({'source': REF, 'target': {'host': HOST['name'], 'agent': 'codex', 'account': 'codex:.codex-other'}})

    def test_windows_runtime_uses_stdin_and_does_not_install_posix_worker(self):
        envelope = json.dumps({'ok': True, 'result': READ}).encode()
        with patch('transfer.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=envelope)) as run, \
                patch.object(self.flow.transfers, '_exchange', side_effect=AssertionError('POSIX runtime installed')):
            result = self.flow.transfers.rpc(HOST, 'chat', {'action': 'read', 'source': ROW}, timeout=20)
        self.assertEqual(result, READ)
        self.assertIn(b'windows_runtime', run.call_args.kwargs['input'])
        self.assertNotIn(SID, ' '.join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        self.assertFalse(self.flow.transfers.runtime_installed)

    def test_unsupported_operations_rejected_before_transport(self):
        for operation in ('attachment', 'profiles', 'start_app_server', 'stage_handoff'):
            with patch('transfer.subprocess.run', side_effect=AssertionError('unexpected transport')):
                with self.assertRaisesRegex(ValueError, REASON):
                    self.flow.transfers.rpc(HOST, operation, {'source': ROW})

    def test_text_capability_comes_from_native_runtime(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(self.flow.transfers, 'rpc', return_value={**READ, 'readOnly': False, 'capabilities': {'canSend': True}}):
            result = service.state({'source': REF})
        self.assertTrue(result['capability']['supported'])
        self.assertFalse(result['capability']['attachments'])
        self.assertEqual(result['messages'], READ['messages'])

    def test_wrong_scope_rejected_before_transport(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('wrong scope RPC')):
            for reference in ({**REF, 'host': 'not-registered'}, {**REF, 'home': '.codex-other'}):
                with self.assertRaises(ValueError): service.state({'source': reference})

    def test_unknown_runtime_remains_explicit(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(self.flow.transfers, 'rpc', return_value={**READ, 'phase': 'unknown', 'runtimeKnown': False}):
            result = service.state({'source': REF})
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['runtimeKnown'])

    def test_one_shot_claude_without_persistence_is_readable_status_not_missing_history(self):
        service = chat.Chat(self.board, self.flow, self.root)
        native = {'phase': 'working', 'runtimeKnown': True, 'messages': [], 'readOnly': True,
                  'executionMode': 'one-shot', 'transcriptAvailability': 'disabled',
                  'historyNotice': {'code': 'not_persisted', 'message': 'native notice'}}
        with patch.object(self.flow.transfers, 'rpc', return_value=native):
            result = service.state({'source': CLAUDE_REF})
        self.assertEqual(result['historyNotice']['code'], 'not_persisted')
        self.assertEqual(result['executionMode'], 'one-shot')
        self.assertEqual(result['messages'], [])
        self.assertFalse(result['capability']['supported'])
        self.assertFalse(result['capability']['attachments'])
        self.assertNotIn('action', result['capability'])
        self.assertFalse(result['promptOpenable'])
        self.assertFalse(result['requiresNativeTerminal'])
        self.assertIn('일회성', result['capability']['reason'])

    def test_one_shot_with_saved_messages_preserves_history_without_terminal_advice(self):
        for persistence in ({}, {'transcriptAvailability': 'disabled'}):
            with self.subTest(persistence=persistence):
                service = chat.Chat(self.board, self.flow, self.root)
                native = {**READ, 'phase': 'working', 'runtimeKnown': True, 'readOnly': True,
                          'executionMode': 'one-shot', **persistence}
                with patch.object(self.flow.transfers, 'rpc', return_value=native):
                    result = service.state({'source': CLAUDE_REF})
                self.assertEqual(result['messages'], READ['messages'])
                self.assertEqual(result['executionMode'], 'one-shot')
                self.assertNotIn('historyNotice', result)
                self.assertNotIn('transcriptAvailability', result)
                self.assertFalse(result['capability']['supported'])
                self.assertFalse(result['capability']['attachments'])
                self.assertNotIn('action', result['capability'])
                self.assertIsNone(result['prompt'])
                self.assertFalse(result['promptOpenable'])

    def test_text_send_is_once_and_upload_stays_unsupported(self):
        service = chat.Chat(self.board, self.flow, self.root)
        with patch.object(self.flow.transfers, 'rpc', return_value={'delivery': 'accepted', 'confirmed': True}) as rpc:
            result = service.send({'source': REF, 'requestId': 'fixture-send-0001', 'text': '합성 입력', 'attachments': []})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.args[2]['action'], 'send')
        with patch.object(self.flow.transfers, 'rpc', side_effect=AssertionError('attachment RPC')):
            with self.assertRaisesRegex(ValueError, '텍스트만'):
                service.upload(REF, 'fixture.txt', b'fixture')

    def test_windows_terminal_quotes_literal_args_and_preserves_profile(self):
        spec = {'argv': [r'C:\Program Files\Codex\codex.exe', 'resume', SID, '--remote', 'unix://C:/profile/app.sock'],
                'cwd': "C:/Work/O'Brien $(literal)", 'env': {'CODEX_HOME': 'C:/Users/example/.codex-personal'}}
        command = self.flow.transfers.windows_terminal(HOST, spec)
        self.assertIn('-tt', command)
        script = base64.b64decode(command[-1].split()[-1]).decode('utf-16le')
        self.assertIn("'C:/Work/O''Brien $(literal)'", script)
        self.assertIn("$env:CODEX_HOME='C:/Users/example/.codex-personal'", script)
        self.assertIn("'" + SID + "'", script)
        with self.assertRaises(ValueError):
            self.flow.transfers.windows_terminal(HOST, {**spec, 'env': {'TOKEN': 'value'}})


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

    def test_external_windows_preview_routes_to_native_control(self):
        preview = {'phase': 'idle', 'targetProof': 'aaa.' + 'a' * 64,
                   'capabilities': {'terminate': {'supported': True}}}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value=preview) as rpc:
            response, result = self.request('POST', '/api/session/preview', {'source': REF})
        self.assertEqual(response.status, 200)
        self.assertEqual(result, preview)
        self.assertEqual(rpc.call_args.args[1], 'control_preview')

    def test_external_terminate_dispatches_once_and_archive_remains_separate(self):
        receipt = {'confirmed': True, 'applied': True, 'status': 'terminated'}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value=receipt) as rpc, \
                patch.object(self.board, 'request_refresh'):
            response, result = self.request('POST', '/api/session/action', {'source': REF, 'action': 'terminate',
                                            'requestId': 'fixture-control-0001', 'targetProof': 'aaa.' + 'a' * 64})
            self.assertEqual(response.status, 200)
            self.assertEqual(result, receipt)
            self.assertEqual(rpc.call_count, 1)
            self.assertEqual(rpc.call_args.args[1], 'control_execute')
            response, result = self.request('POST', '/api/session/archive', {'source': REF, 'archived': True})
        self.assertEqual(response.status, 200)
        self.assertFalse(result['executionChanged'])


if __name__ == '__main__':
    unittest.main()
