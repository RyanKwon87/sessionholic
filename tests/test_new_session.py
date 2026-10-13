""""새 세션": a host's own launcher (settings `launcher`) opened in a board terminal.

HOME is a temporary folder and the launcher is a fixture script that is never run here;
os.execve is replaced, SSH is never started and no agent or model is involved.
"""
from contextlib import contextmanager, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import launch
import server
import settings
from transfer import Transfers
import transfer_worker


class TempHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-new-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.path = self.home / '.config/sessionholic/config.json'
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.program = self.home / 'bin/start'
        self.program.parent.mkdir()
        self.program.write_text('#!/bin/sh\nexit 0\n')
        self.program.chmod(0o700)

    def write(self, launcher=None, version=2):
        data = {'version': version}
        if launcher is not None:
            data['launcher'] = launcher
        self.path.write_text(json.dumps(data))
        self.path.chmod(0o600)


class LauncherSettingsTest(TempHome):
    def test_launcher_is_optional_and_parsed_from_a_version_2_file(self):
        self.write()
        self.assertIsNone(settings.load(self.home)['launcher'])
        self.write({'command': ['~/bin/start', '--menu', 'a b'], 'label': '런처', 'cwd': '~/work'})
        launcher = settings.load(self.home)['launcher']
        self.assertEqual(launcher, {'command': [str(self.home / 'bin/start'), '--menu', 'a b'], 'label': '런처',
                                    'cwd': str(self.home / 'work')})
        self.write({'command': ['/bin/sh']})
        self.assertEqual(settings.load(self.home)['launcher']['label'], '새 세션')

    def test_malformed_launcher_is_refused(self):
        for launcher in ({}, {'command': []}, {'command': 'start'}, {'command': ['bin/start']},
                         {'command': ['/bin/sh', 1]}, {'command': ['/bin/sh', 'a\nb']},
                         {'command': ['/bin/sh'] * 17}, {'command': ['/bin/sh'], 'env': {}},
                         {'command': ['/bin/sh'], 'label': ''}, {'command': ['/bin/sh'], 'cwd': 'work'}):
            self.write(launcher)
            with self.assertRaises(ValueError, msg=launcher):
                settings.load(self.home)
        self.write({'command': ['/bin/sh']}, version=1)
        with self.assertRaises(ValueError):
            settings.load(self.home)


class NewSessionLaunchTest(TempHome):
    def test_launch_runs_the_configured_program_with_terminal_variables_only(self):
        self.write({'command': ['~/bin/start', '--menu'], 'label': '런처'})
        outer = {'PATH': '/usr/bin:/bin', 'LANG': 'ko_KR.UTF-8', 'SSH_AUTH_SOCK': '/tmp/agent.sock',
                 'OPENAI_API_KEY': 'fixture-never-copied', 'CODEX_HOME': '/elsewhere',
                 'SSH_CONNECTION': '1 2 3 4', 'TMUX': '/tmp/tmux,1,0'}
        with patch.dict(os.environ, outer, clear=True):
            spec = launch.new_session(self.home)
            status = launch.new_session_status(self.home)
        self.assertEqual(spec['argv'], [str(self.program), '--menu'])
        self.assertEqual((spec['cwd'], spec['label']), (str(self.home), '런처'))
        self.assertEqual(spec['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'ko_KR.UTF-8', 'SSH_AUTH_SOCK': '/tmp/agent.sock',
                                       'HOME': str(self.home), 'TERM': 'xterm-256color'})
        self.assertEqual(status, {'available': True, 'label': '런처', 'reason': None})

    def test_missing_or_unusable_launcher_reports_why(self):
        self.write()
        self.assertIn('설정되지 않았습니다', launch.new_session_status(self.home)['reason'])
        self.write({'command': ['~/bin/missing']})
        self.assertIn('찾지 못했습니다', launch.new_session_status(self.home)['reason'])
        self.program.chmod(0o600)
        self.write({'command': ['~/bin/start']})
        self.assertIn('찾지 못했습니다', launch.new_session_status(self.home)['reason'])
        self.program.chmod(0o700)
        self.write({'command': ['~/bin/start'], 'cwd': '~/missing'})
        status = launch.new_session_status(self.home)
        self.assertEqual((status['available'], status['label']), (False, None))
        self.assertIn('폴더', status['reason'])


class WorkerNewSessionTest(TempHome):
    @contextmanager
    def remote_home(self):
        with patch.dict(os.environ, {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, clear=True), \
                patch.object(sys, 'argv', ['worker.pyz', 'new-session']), \
                patch('os.execve') as execve, patch('os.chdir') as chdir:
            yield execve, chdir

    def test_new_session_step_execs_this_hosts_own_launcher(self):
        self.write({'command': ['~/bin/start', '--menu']})
        with self.remote_home() as (execve, chdir):
            transfer_worker._main()
        chdir.assert_called_once_with(str(self.home))
        program, argv, env = execve.call_args.args
        self.assertEqual((program, argv), (str(self.program), [str(self.program), '--menu']))
        self.assertEqual(env['HOME'], str(self.home))

    def test_new_session_step_refusal_prints_the_reason_and_runs_nothing(self):
        self.write()
        error = io.StringIO()
        with self.remote_home() as (execve, _), redirect_stderr(error):
            with self.assertRaises(SystemExit) as stopped:
                transfer_worker._main()
        self.assertEqual(stopped.exception.code, 1)
        execve.assert_not_called()
        self.assertEqual(error.getvalue().strip(),
                         '새 세션을 열지 못했습니다. 이 기기에는 새 세션 실행 명령이 설정되지 않았습니다.')

    def test_profiles_reply_carries_this_hosts_launcher_status(self):
        self.write({'command': ['~/bin/start'], 'label': '런처'})
        with patch.dict(os.environ, {'HOME': str(self.home)}), patch('transfer_native.profiles', return_value=[]):
            reply = transfer_worker._rpc({'op': 'profiles'})
        self.assertEqual(reply['launcher'], {'available': True, 'label': '런처', 'reason': None})


LOCAL = {'name': 'local', 'label': 'Local', 'local': True}
REMOTE = {'name': 'remote', 'label': 'Remote', 'local': False, 'ssh': 'remote', 'python': '/usr/bin/python3'}
RUNTIME = '/home/user/.local/state/sessionholic-transfer-runtime/worker-' + 'a' * 64 + '.pyz'


class FakeTerminals:
    max_terminals = 4

    def __init__(self):
        self.created = []

    def list(self):
        return []

    @contextmanager
    def reserve(self, key):
        yield

    def create(self, key, argv, cwd, env, metadata):
        self.created.append({'key': key, 'argv': argv, 'cwd': cwd, 'env': env, 'metadata': metadata})
        return {'id': 'e' * 32, 'key': key, **metadata, 'alive': True}


class WorkflowNewSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-new-flow-')
        self.addCleanup(self.temp.cleanup)
        runner = lambda host, args, timeout: {'codex': [], 'claude': [], 'errors': []}
        self.board = server.Board([LOCAL, REMOTE], 60, runner)
        for host in (LOCAL, REMOTE):
            self.board.poll(host)
        self.terms = FakeTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.temp.name)
        self.remote_status = {'available': True, 'label': '원격 런처', 'reason': None}
        self.asked = []

        def launcher(host, refresh=False):
            self.asked.append((host['name'], refresh))
            self.flow.transfers.runtime_paths[host['name']] = RUNTIME
            return dict(self.remote_status)
        self.flow.transfers.launcher = launcher
        local = {'argv': ['/bin/sh'], 'cwd': '/tmp', 'env': {'HOME': '/tmp'}, 'label': '런처'}
        patcher = patch('launch.new_session', return_value=local)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_new_session_opens_the_launcher_once_per_request(self):
        first = self.flow.new_session({'host': 'local', 'requestId': 'request-0001'})
        again = self.flow.new_session({'host': 'local', 'requestId': 'request-0001'})
        self.assertEqual(first, again)
        self.assertEqual(len(self.terms.created), 1)
        created = self.terms.created[0]
        self.assertEqual((created['key'], created['argv'], created['cwd']), ('new-session:request-0001', ['/bin/sh'], '/tmp'))
        opened = created['metadata'].pop('createdAt')
        self.assertEqual(type(opened), int)
        self.assertEqual(created['metadata'], {'title': '런처', 'host': 'local', 'mode': 'new-session'})
        self.assertEqual(first['mode'], 'new-session')
        with self.assertRaisesRegex(ValueError, '이미 사용한 요청'):
            self.flow.new_session({'host': 'remote', 'requestId': 'request-0001'})
        for payload in ({'host': 'local', 'requestId': 'bad id'}, {'host': 'nowhere', 'requestId': 'request-0002'}, []):
            with self.assertRaises(ValueError):
                self.flow.new_session(payload)
        self.assertEqual(len(self.terms.created), 1)

    def test_a_new_session_request_repeated_while_it_opens_waits_and_opens_once(self):
        started, release = threading.Event(), threading.Event()
        local = {'argv': ['/bin/sh'], 'cwd': '/tmp', 'env': {'HOME': '/tmp'}, 'label': '런처'}
        def slow():
            started.set()
            release.wait(5)
            return dict(local)
        results = []
        with patch('launch.new_session', side_effect=slow):
            first = threading.Thread(target=lambda: results.append(self.flow.new_session({'host': 'local', 'requestId': 'request-0009'})))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=lambda: results.append(self.flow.new_session({'host': 'local', 'requestId': 'request-0009'})))
            second.start()
            second.join(0.2)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(5); second.join(5)
        self.assertEqual(len(self.terms.created), 1)
        self.assertEqual(results[0], results[1])

    def test_remote_new_session_runs_the_hosts_own_step_over_ssh(self):
        result = self.flow.new_session({'host': 'remote', 'requestId': 'request-0003'})
        self.assertEqual(self.asked, [('remote', True)])
        argv = self.terms.created[0]['argv']
        self.assertIn('-tt', argv)
        self.assertEqual(argv[-2:], ['remote', 'exec /usr/bin/python3 -I ' + RUNTIME + ' new-session'])
        self.assertEqual(self.terms.created[0]['metadata']['title'], '원격 런처')
        # No launcher setting or value crosses the SSH command.
        self.assertNotIn('HOME=', ' '.join(argv))
        self.assertEqual(result['terminal']['mode'], 'new-session')

    def test_unavailable_or_offline_remote_is_refused_without_a_terminal(self):
        self.remote_status = {'available': False, 'label': None, 'reason': '이 기기에는 새 세션 실행 명령이 설정되지 않았습니다.'}
        with self.assertRaisesRegex(ValueError, '설정되지 않았습니다'):
            self.flow.new_session({'host': 'remote', 'requestId': 'request-0004'})
        self.board.state['remote']['ok'] = False
        with self.assertRaisesRegex(RuntimeError, '연결할 수 없어'):
            self.flow.new_session({'host': 'remote', 'requestId': 'request-0005'})
        self.assertEqual(self.terms.created, [])

    def test_capabilities_report_each_hosts_new_session(self):
        self.flow.profiles = lambda: []
        self.flow.transfers.profiles = lambda host, refresh=False: []
        with patch('launch.new_session_status', return_value={'available': True, 'label': '런처', 'reason': None}), \
                patch('server.shutil.which', return_value='/usr/bin/tmux'):
            hosts = {h['name']: h['newSession'] for h in self.flow.capabilities()['hosts']}
            self.assertEqual(hosts['local'], {'available': True, 'label': '런처', 'reason': None})
            self.assertEqual(hosts['remote'], self.remote_status)
            self.board.state['remote']['ok'] = False
            offline = {h['name']: h['newSession'] for h in self.flow.capabilities()['hosts']}['remote']
        self.assertEqual((offline['available'], offline['reason']), (False, '기기에 연결할 수 없습니다.'))


class TransfersLauncherTest(unittest.TestCase):
    def test_launcher_status_is_read_with_the_profiles_reply(self):
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-new-tr-') as folder:
            flow = Transfers([REMOTE], folder)
            replies = [{'profiles': [], 'launcher': {'available': True, 'label': 'L', 'reason': None}},
                       {'profiles': []},
                       {'profiles': [], 'launcher': {'available': 'yes', 'label': 3, 'reason': ['x']}}]
            with patch.object(flow, 'rpc', side_effect=replies):
                self.assertEqual(flow.launcher(REMOTE), {'available': True, 'label': 'L', 'reason': None})
                missing = flow.launcher(REMOTE, refresh=True)
                self.assertEqual((missing['available'], missing['label']), (False, None))
                self.assertEqual(flow.launcher(REMOTE, refresh=True), {'available': False, 'label': None, 'reason': None})


class LinkTerminals(FakeTerminals):
    """Terminals as the board lists them, with the native session a terminal records."""
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.linked = []

    def list(self):
        return [dict(row) for row in self.rows]

    def link_native(self, terminal_id, native, confidence="exact"):
        row = next(row for row in self.rows if row['id'] == terminal_id)
        if row.get('nativeSource'):
            return False
        row['nativeSource'] = native
        row['linkConfidence'] = confidence
        self.linked.append((terminal_id, native))
        return True


def codex_row(suffix, started, **fields):
    return {'id': '019a0000-0000-7000-8000-00000000000' + suffix, 'home': '.codex', 'cwd': '/work/project',
            'startedAt': started, 'source': 'vscode', **fields}


class LinkNewSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-link-')
        self.addCleanup(self.temp.cleanup)
        self.rows = {'local': {'codex': [], 'claude': []}, 'remote': {'codex': [], 'claude': []}}
        runner = lambda host, args, timeout: {**self.rows[host['name']], 'errors': []}
        self.board = server.Board([LOCAL, REMOTE], 60, runner)
        self.terminal = {'id': 'f' * 32, 'mode': 'new-session', 'host': 'remote', 'alive': True, 'closed': False,
                         'createdAt': 1000}
        self.terms = LinkTerminals([self.terminal])
        self.flow = server.Workflow(self.board, self.terms, self.temp.name)

    def link(self):
        for host in (LOCAL, REMOTE):
            self.board.poll(host)
        self.flow.link_new_sessions()
        return [native for _, native in self.terms.linked]

    def test_the_first_interactive_session_after_the_terminal_opened_on_its_host_is_its_own(self):
        self.rows['remote']['codex'] = [codex_row('1', 900), codex_row('2', 1060, source='exec'),
                                        codex_row('3', 1100), codex_row('4', 1050)]
        self.rows['remote']['claude'] = [{'id': 'bg-agent-1', 'kind': 'background', 'cwd': '/work', 'startedAt': 1001}]
        self.rows['local']['codex'] = [codex_row('5', 1001)]
        self.assertEqual(self.link(), [{'host': 'remote', 'agent': 'codex', 'id': codex_row('4', 0)['id'],
                                        'home': '.codex', 'cwd': '/work/project'}])
        self.assertEqual(self.terminal['linkConfidence'], 'estimated')
        # Recorded once: a later, earlier-looking session never moves it.
        self.rows['remote']['codex'].append(codex_row('6', 1010))
        self.assertEqual(len(self.link()), 1)

    def test_a_claude_session_counts_with_its_session_id(self):
        self.rows['remote']['claude'] = [{'id': 'agent-0001', 'kind': 'interactive', 'cwd': '/work/project',
                                          'sessionId': '0f9c3a52-1b7e-4c1a-9d6e-5a4b3c2d1e0f', 'startedAt': 1020}]
        self.assertEqual(self.link(), [{'host': 'remote', 'agent': 'claude', 'id': 'agent-0001', 'home': None,
                                        'cwd': '/work/project', 'kind': 'interactive',
                                        'sessionId': '0f9c3a52-1b7e-4c1a-9d6e-5a4b3c2d1e0f'}])

    def test_nothing_is_taken_late_twice_or_for_an_ended_terminal(self):
        # Too late, already another terminal's, or the terminal ended: no link.
        self.rows['remote']['codex'] = [codex_row('1', 1000 + server.Workflow.LINK_WINDOW + 1)]
        self.assertEqual(self.link(), [])
        self.rows['remote']['codex'] = [codex_row('2', 1005)]
        self.terms.rows.append({'id': 'a' * 32, 'mode': 'handoff', 'host': 'remote', 'alive': True,
                                'nativeSource': {'host': 'remote', 'agent': 'codex', 'id': codex_row('2', 0)['id'],
                                                 'home': '.codex', 'cwd': '/work/project'}})
        self.assertEqual(self.link(), [])
        self.terms.rows.pop()
        self.terminal['alive'] = False
        self.assertEqual(self.link(), [])
        self.terminal.update(alive=True, createdAt=None)
        self.assertEqual(self.link(), [])


if __name__ == '__main__':
    unittest.main()
