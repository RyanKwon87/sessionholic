""""새 세션": a host's own launcher (settings `launcher`) opened in a board terminal.

HOME is a temporary folder and the launcher is a fixture script that is never run here;
os.execve is replaced, SSH is never started and no agent or model is involved.
"""
from contextlib import contextmanager, redirect_stderr
import http.client
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import launch
import server
import settings
import terminal
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

    def test_a_preset_reaches_the_launcher_as_one_checked_variable(self):
        self.write({'command': ['~/bin/start', '--menu']})
        with self.remote_home() as (execve, _), patch.object(sys, 'argv', ['worker.pyz', 'new-session', '--preset', 'fast']):
            transfer_worker._main()
        env = execve.call_args.args[2]
        self.assertEqual(json.loads(env['SESSIONHOLIC_LAUNCH_PRESET']),
                         {'claude': {'model': 'sonnet', 'effort': 'medium'}, 'codex': {'effort': 'low'}})
        # Without one, the launcher gets none; an unknown one runs nothing.
        with self.remote_home() as (execve, _):
            transfer_worker._main()
        self.assertNotIn('SESSIONHOLIC_LAUNCH_PRESET', execve.call_args.args[2])
        error = io.StringIO()
        with self.remote_home() as (execve, _), redirect_stderr(error), \
                patch.object(sys, 'argv', ['worker.pyz', 'new-session', '--preset', 'turbo']):
            with self.assertRaises(SystemExit):
                transfer_worker._main()
        execve.assert_not_called()
        self.assertIn('실행 설정이 올바르지 않습니다', error.getvalue())
        # Any other shape of the step is refused in its own branch.
        error = io.StringIO()
        with self.remote_home() as (execve, _), redirect_stderr(error), \
                patch.object(sys, 'argv', ['worker.pyz', 'new-session', '--preset']):
            with self.assertRaises(SystemExit):
                transfer_worker._main()
        execve.assert_not_called()
        self.assertIn('실행 요청 형식', error.getvalue())

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
        def slow(**_preset):
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

    def test_a_preset_goes_with_the_step_and_names_the_terminal(self):
        self.flow.new_session({'host': 'remote', 'requestId': 'request-0021', 'preset': 'max'})
        created = self.terms.created[0]
        self.assertEqual(created['argv'][-1], 'exec /usr/bin/python3 -I ' + RUNTIME + ' new-session --preset max')
        self.assertEqual(created['metadata']['title'], '원격 런처 · 최대')
        # Locally it is the launcher's variable; a held run carries it in its step.
        with patch('launch.new_session', return_value={'argv': ['/bin/sh'], 'cwd': '/tmp', 'env': {}, 'label': '런처'}) as made:
            self.flow.new_session({'host': 'local', 'requestId': 'request-0022', 'preset': 'fast'})
        made.assert_called_once_with(preset='fast')
        self.flow.transfers.hold_support['remote'] = '/opt/homebrew/bin/tmux'
        self.flow.new_session({'host': 'remote', 'requestId': 'request-0023', 'preset': 'fast'})
        self.assertTrue(json.loads(self.terms.created[-1]['argv'][4])['first'][-1].endswith(' new-session --preset fast'))
        # The same request with another preset is another launch; an unknown preset is refused.
        with self.assertRaisesRegex(ValueError, '이미 사용한 요청'):
            self.flow.new_session({'host': 'remote', 'requestId': 'request-0021', 'preset': 'fast'})
        with self.assertRaisesRegex(ValueError, '실행 설정이 올바르지 않습니다'):
            self.flow.new_session({'host': 'remote', 'requestId': 'request-0024', 'preset': 'turbo'})

    def test_a_remote_that_holds_runs_opens_the_reconnect_loop_and_names_its_hold(self):
        self.flow.transfers.hold_support['remote'] = '/opt/homebrew/bin/tmux'
        self.flow.new_session({'host': 'remote', 'requestId': 'request-0013'})
        created = self.terms.created[0]
        self.assertEqual(created['argv'][3], '--hold-loop')
        self.assertRegex(created['metadata']['holdName'], r'^sh-[0-9a-f]{16}$')
        self.assertIn(' hold %s new-session' % created['metadata']['holdName'], json.loads(created['argv'][4])['first'][-1])

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


# ---------- runs held in the host's own tmux (`hold`) and the board's reconnect loop ----------
# tmux, SSH and every exec are replaced; no agent runs.
HOLD = 'sh-0123456789abcdef'
HOLD_TMUX = '/opt/fixture/bin/tmux'
PC = {'name': 'pc', 'label': 'PC', 'local': False, 'ssh': 'pc', 'platform': 'windows'}


class Ran:
    """subprocess.run stand-in: answers by tmux subcommand, records every call."""
    def __init__(self, alive=False, started=0, listed='', server=False):
        self.alive, self.started, self.listed, self.server, self.calls = alive, started, listed, server or alive, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        step = argv[5]
        code = {'has-session': 0 if self.alive else 1, 'new-session': self.started,
                'list-sessions': 0 if self.server or self.listed else 1, 'kill-session': 0}.get(step, 0)
        return SimpleNamespace(returncode=code, stdout=self.listed if step == 'list-sessions' else '')


class WorkerHoldTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-hold-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.cwd = self.home / 'project'
        self.cwd.mkdir()
        # A step execs from its own folder; each test starts there and leaves it after.
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

    @contextmanager
    def remote(self, tmux=HOLD_TMUX, ran=None):
        ran = ran or Ran()
        with patch.dict(os.environ, {'HOME': str(self.home), 'TERM': 'xterm-256color', 'TMUX': '/ignored'}, clear=True), \
                patch.object(transfer_worker, '_tmux', return_value=tmux), \
                patch.object(transfer_worker.subprocess, 'run', side_effect=ran), \
                patch('os.execve', side_effect=SystemExit('exec')) as execve, \
                patch('os.get_terminal_size', return_value=os.terminal_size((120, 40))):
            yield ran, execve

    def holds(self):
        return transfer_worker.root() / 'holds'

    def test_a_held_step_starts_in_its_own_tmux_session_and_attaches_to_it(self):
        with self.remote() as (ran, execve), patch.object(transfer_worker, '_hold', HOLD):
            with self.assertRaises(SystemExit):
                transfer_worker._execve(['/usr/bin/agent', '--flag'], {'HOME': str(self.home), 'SECRET_FREE': '1'})
            envelope = self.holds() / (HOLD + '.json')
            self.assertEqual(stat.S_IMODE(envelope.stat().st_mode), 0o600)
            self.assertEqual(json.loads(envelope.read_text()),
                             {'argv': ['/usr/bin/agent', '--flag'], 'env': {'HOME': str(self.home), 'SECRET_FREE': '1'},
                              'cwd': str(self.cwd)})
        start = next(argv for argv in ran.calls if argv[5] == 'new-session')
        self.assertEqual(start[:5], [HOLD_TMUX, '-L', 'sessionholic-hold', '-f', '/dev/null'])
        self.assertEqual(start[6:12], ['-d', '-s', HOLD, '-x', '120', '-y'])
        self.assertIn('hold-run', start)
        self.assertNotIn('/usr/bin/agent', start, 'argv and environment never ride on tmux argv')
        # The server's options, set by the session that starts it; its client draws on the main screen.
        for option in (['status', 'off'], ['prefix', 'None'], ['prefix2', 'None'], ['mouse', 'off'], ['escape-time', '10'],
                       ['terminal-overrides', '*:smcup@:rmcup@']):
            self.assertIn(';', start)
            self.assertTrue(any(start[i:i + 4] == ['set-option', '-g'] + option for i in range(len(start))), option)
        program, argv, env = execve.call_args.args
        self.assertEqual((program, argv), (HOLD_TMUX, [HOLD_TMUX, '-L', 'sessionholic-hold', '-f', '/dev/null',
                                                   'attach-session', '-t', '=' + HOLD]))
        self.assertNotIn('TMUX', env)
        self.assertEqual(env['TERM'], 'xterm-256color')

    def test_without_tmux_or_when_tmux_refuses_the_step_runs_here_as_before(self):
        for tmux, ran in ((None, Ran()), (HOLD_TMUX, Ran(started=1))):
            with self.subTest(tmux=tmux), self.remote(tmux=tmux, ran=ran) as (_, execve), \
                    patch.object(transfer_worker, '_hold', HOLD):
                with self.assertRaises(SystemExit):
                    transfer_worker._execve(['/usr/bin/agent'], {'HOME': str(self.home)})
                self.assertEqual(execve.call_args.args[:2], ('/usr/bin/agent', ['/usr/bin/agent']))
                self.assertFalse((self.holds() / (HOLD + '.json')).exists())

    def test_a_running_hold_server_keeps_its_options(self):
        with self.remote(ran=Ran(server=True)) as (ran, _), patch.object(transfer_worker, '_hold', HOLD):
            with self.assertRaises(SystemExit):
                transfer_worker._execve(['/usr/bin/agent'], {})
        start = next(argv for argv in ran.calls if argv[5] == 'new-session')
        self.assertNotIn('set-option', start, 'options are set once, never appended again')

    def test_a_refused_start_never_runs_twice(self):
        # tmux answered an error, yet the session is up: attach to it, never run here as well.
        class Racing(Ran):
            def __call__(self, argv, **kwargs):
                result = super().__call__(argv, **kwargs)
                if argv[5] == 'new-session':
                    self.alive = True
                return result
        with self.remote(ran=Racing(started=1)) as (ran, execve), patch.object(transfer_worker, '_hold', HOLD):
            with self.assertRaises(SystemExit):
                transfer_worker._execve(['/usr/bin/agent'], {})
            (self.holds() / (HOLD + '.json')).unlink()  # As hold-run would have.
        self.assertEqual(execve.call_args.args[1][-3:], ['attach-session', '-t', '=' + HOLD])
        # tmux failed and the session is gone, but its envelope was used: it ran there; not again.
        with self.remote(ran=Ran(started=1)) as (ran, execve), patch.object(transfer_worker, '_hold', HOLD), \
                patch.object(transfer_worker, 'save'), patch('sys.stdout', new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit) as stopped:
                transfer_worker._execve(['/usr/bin/agent'], {})
        self.assertEqual(stopped.exception.code, 1)
        execve.assert_not_called()
        self.assertIn('바로 끝났습니다', out.getvalue())

    def test_an_existing_hold_is_attached_without_starting_anything(self):
        with self.remote(ran=Ran(alive=True)) as (ran, execve), patch.object(transfer_worker, '_hold', HOLD):
            with self.assertRaises(SystemExit):
                transfer_worker._execve(['/usr/bin/agent'], {})
        self.assertFalse(any(argv[5] == 'new-session' for argv in ran.calls))
        self.assertEqual(execve.call_args.args[1][-3:], ['attach-session', '-t', '=' + HOLD])

    def test_no_hold_requested_execs_directly(self):
        with self.remote() as (ran, execve):
            with self.assertRaises(SystemExit):
                transfer_worker._execve(['/usr/bin/agent'], {'A': '1'})
        self.assertEqual(ran.calls, [])
        self.assertEqual(execve.call_args.args, ('/usr/bin/agent', ['/usr/bin/agent'], {'A': '1'}))

    def test_hold_run_execs_the_envelope_once_on_the_panes_terminal(self):
        with self.remote() as (_, execve):
            folder = transfer_worker.private_dir(self.holds())
            envelope = folder / (HOLD + '.json')
            transfer_worker.save(envelope, {'argv': ['/usr/bin/agent'], 'cwd': str(self.cwd),
                                            'env': {'TERM': 'xterm-256color', 'TMUX': 'x', 'TMUX_PANE': '%1', 'KEEP': 'y'}})
            os.environ['TERM'] = 'tmux-256color'
            with self.assertRaises(SystemExit):
                transfer_worker.hold_run(str(envelope))
            self.assertFalse(envelope.exists(), 'one use')
            program, argv, env = execve.call_args.args
            self.assertEqual((program, argv), ('/usr/bin/agent', ['/usr/bin/agent']))
            self.assertEqual(env, {'TERM': 'tmux-256color', 'KEEP': 'y'})
            for path in (str(self.home / 'elsewhere.json'), str(folder / 'not-a-hold.json')):
                with self.assertRaises(ValueError):
                    transfer_worker.hold_run(path)

    def test_reattach_only_attaches_and_never_starts_a_step(self):
        refused = AssertionError('reattach must never start anything')
        with patch('launch.new_session', side_effect=refused), patch.object(transfer_worker, 'run_handoff', side_effect=refused), \
                patch.object(transfer_worker, 'attach', side_effect=refused):
            with self.remote(ran=Ran(alive=False)) as (ran, execve), patch.object(sys, 'argv', ['w.pyz', 'reattach', HOLD]), \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                with self.assertRaises(SystemExit) as stopped:
                    transfer_worker.main()
            self.assertEqual(stopped.exception.code, 0)
            self.assertIn('이미 끝났습니다', out.getvalue())
            execve.assert_not_called()
            with self.remote(ran=Ran(alive=True)) as (ran, execve), patch.object(sys, 'argv', ['w.pyz', 'reattach', HOLD]):
                with self.assertRaises(SystemExit):
                    transfer_worker.main()
            self.assertEqual(execve.call_args.args[1][-3:], ['attach-session', '-t', '=' + HOLD])
            self.assertEqual([argv[5] for argv in ran.calls], ['has-session'])
            with patch.object(sys, 'argv', ['w.pyz', 'reattach', 'other-name']), self.assertRaises(ValueError):
                transfer_worker.main()

    def test_a_hold_prefix_runs_the_step_with_its_final_exec_held(self):
        seen = []
        with patch.object(transfer_worker, 'new_session_main', side_effect=lambda: seen.append(transfer_worker._hold)), \
                patch.object(sys, 'argv', ['w.pyz', 'hold', HOLD, 'new-session']):
            transfer_worker.main()
        self.assertEqual(seen, [HOLD])
        transfer_worker._hold = None
        with patch.object(sys, 'argv', ['w.pyz', 'hold', 'bad', 'new-session']), self.assertRaises(ValueError):
            transfer_worker.main()

    def test_holds_are_listed_by_age_and_ended_by_name(self):
        now = 2_000_000_000
        listed = 'sh-0123456789abcdef %d\nsomeone-else %d\nsh-fedcba9876543210 %d\n' % (now - 300, now, now - 5)
        with self.remote(ran=Ran(alive=True, listed=listed)) as (ran, _), patch.object(transfer_worker.time, 'time', return_value=now):
            self.assertEqual(transfer_worker._rpc({'op': 'hold_list', 'args': {}}),
                             {'holds': [{'name': HOLD, 'age': 300}, {'name': 'sh-fedcba9876543210', 'age': 5}]})
            self.assertEqual(transfer_worker._rpc({'op': 'hold_kill', 'args': {'name': HOLD}}), {'killed': True})
            self.assertEqual(ran.calls[-1][5:], ['kill-session', '-t', '=' + HOLD])
            # An envelope never run is removed once old; a fresh one stays.
            folder = transfer_worker.private_dir(self.holds())
            old, fresh = folder / 'sh-0000000000000001.json', folder / 'sh-0000000000000002.json'
            old.write_text('{}'); fresh.write_text('{}')
            os.utime(old, (now - 3600, now - 3600))
            os.utime(fresh, (now - 60, now - 60))
            with patch.object(transfer_worker.time, 'time', return_value=now):
                transfer_worker.hold_list()
            self.assertEqual((old.exists(), fresh.exists()), (False, True))
            with self.assertRaises(ValueError):
                transfer_worker._rpc({'op': 'hold_kill', 'args': {'name': 'other'}})
        with self.remote(tmux=None):
            self.assertEqual(transfer_worker.hold_list(), {'holds': []})
            self.assertEqual(transfer_worker.hold_kill(HOLD), {'killed': False})

    def test_profiles_say_whether_and_with_which_tmux_this_host_holds_runs(self):
        for tmux, expected in ((HOLD_TMUX, True), (None, False)):
            with self.subTest(tmux=tmux), patch.dict(os.environ, {'HOME': str(self.home)}), \
                    patch('transfer_native.profiles', return_value=[]), patch.object(transfer_worker, '_tmux', return_value=tmux):
                reply = transfer_worker._rpc({'op': 'profiles'})
                self.assertEqual((reply['holds'], reply['holdTmux']), (expected, tmux))


class TransfersTerminalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-hold-tr-')
        self.addCleanup(self.temp.cleanup)
        self.flow = Transfers([LOCAL, REMOTE, PC], self.temp.name)

    def test_a_host_that_holds_runs_gets_the_reconnect_loop(self):
        with patch.object(self.flow, 'rpc', return_value={'profiles': [], 'holds': True, 'holdTmux': '/opt/homebrew/bin/tmux'}):
            self.flow.profiles(REMOTE, refresh=True)
        argv, name = self.flow.terminal(REMOTE, ['-I', RUNTIME, 'new-session'])
        self.assertRegex(name, r'^sh-[0-9a-f]{16}$')
        self.assertTrue(self.flow.holds_mine(name))
        self.assertFalse(self.flow.holds_mine('sh-' + ('0' if name[3] != '0' else '1') + name[4:]), "another board's")
        self.assertEqual(argv[:4], [sys.executable, '-I', str(Path(terminal.__file__).resolve()), '--hold-loop'])
        spec = json.loads(argv[4])
        self.assertEqual(spec['first'][-1], 'exec /usr/bin/python3 -I %s hold %s new-session' % (RUNTIME, name))
        # Attaching again needs only the host's tmux, never the helper file of the day it opened.
        self.assertEqual(spec['again'][-1], "exec /opt/homebrew/bin/tmux -L sessionholic-hold -f /dev/null attach-session -t '=" + name + "'")
        # A Mac's login shell (zsh) reads a bare "=word" as a command path: the target reaches tmux as written.
        if os.path.exists('/bin/zsh'):
            words = subprocess.run(['/bin/zsh', '-c', spec['again'][-1].replace('exec /opt/homebrew/bin/tmux', "printf '%s\\n'", 1)],
                                   capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(words, ['-L', 'sessionholic-hold', '-f', '/dev/null', 'attach-session', '-t', '=' + name])
        self.assertNotIn(RUNTIME, spec['again'][-1])
        self.assertEqual(spec['wait'], str(self.flow.hold_waiting / name))
        for command in (spec['first'], spec['again']):
            self.assertIn('ControlPath=none', command)
            self.assertEqual(command[1], '-tt')
        self.assertNotEqual(self.flow.terminal(REMOTE, ['-I', RUNTIME, 'new-session'])[1], name, 'a new name each time')

    def test_elsewhere_the_plain_command_and_no_name(self):
        for reply in ({'holds': True, 'holdTmux': 'tmux'}, {'holds': True, 'holdTmux': '/bin/t mux'}):
            with self.subTest(reply=reply), patch.object(self.flow, 'rpc', return_value={'profiles': [], **reply}):
                self.flow.profiles(REMOTE, refresh=True)
                self.assertIsNone(self.flow.terminal(REMOTE, ['-I', RUNTIME, 'new-session'])[1], 'an unsafe tmux path is not used')
        with patch.object(self.flow, 'rpc', return_value={'profiles': [], 'holds': False}):
            self.flow.profiles(REMOTE, refresh=True)
        argv, name = self.flow.terminal(REMOTE, ['-I', RUNTIME, 'new-session'])
        self.assertIsNone(name)
        self.assertEqual(argv, self.flow.command(REMOTE, ['-I', RUNTIME, 'new-session'], tty=True))
        self.flow.hold_support.update(local='/usr/bin/tmux', pc='/usr/bin/tmux')
        self.assertIsNone(self.flow.terminal(LOCAL, ['-I', RUNTIME, 'new-session'])[1])


class HoldLoopTest(unittest.TestCase):
    SPEC = {'first': ['ssh', 'first'], 'again': ['ssh', 'again']}

    def loop(self, codes, durations, sleeps=None):
        ran, slept, now = [], [], [0.0]
        codes, durations = list(codes), list(durations)

        def run(argv):
            ran.append(argv[1])
            now[0] += durations.pop(0)
            return codes.pop(0)

        def sleep(seconds):
            slept.append(seconds)
            if sleeps:
                raise sleeps
        out = io.StringIO()
        with patch.object(terminal.termios, 'tcflush') as flush:
            code = terminal._hold_loop(self.SPEC, run=run, sleep=sleep, clock=lambda: now[0], out=out)
        return code, ran, slept, out.getvalue(), flush

    def test_a_dropped_connection_attaches_again_until_the_run_ends(self):
        code, ran, slept, out, flush = self.loop([255, 255, 255, 0], [3600, 5, 70, 10])
        self.assertEqual(code, 0)
        self.assertEqual(ran, ['first', 'again', 'again', 'again'])
        # Waits grow while reconnecting fails, and start over after a connection that lasted.
        self.assertEqual(slept, [2, 5, 2])
        self.assertIn('기기 연결이 끊겼습니다', out)
        self.assertGreaterEqual(flush.call_count, 6, 'keys typed while away are dropped')

    def test_the_wait_is_marked_for_the_board_and_leaves_the_runs_modes(self):
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-hold-wait-') as folder:
            mark = Path(folder) / 'hold-waiting' / HOLD
            seen = []
            spec = {**self.SPEC, 'wait': str(mark)}
            codes, now = [255, 0], [0.0]

            def run(argv):
                seen.append((argv[1], mark.exists()))
                now[0] += 3600
                return codes.pop(0)
            out = io.StringIO()
            with patch.object(terminal.termios, 'tcflush'):
                terminal._hold_loop(spec, run=run, sleep=lambda s: seen.append(('sleep', mark.exists())),
                                    clock=lambda: now[0], out=out)
            # Marked while waiting and while it attaches again (a send then could be split or
            # dropped); gone after the loop.
            self.assertEqual(seen, [('first', False), ('sleep', True), ('again', True)])
            self.assertFalse(mark.exists())
            self.assertTrue(out.getvalue().startswith(terminal.HOLD_RESET), 'mouse and paste modes off first')
            self.assertIn('그 기기의 실행도 끝냅니다', out.getvalue())
            # Attached again for a moment, the mark goes and the board takes sends again.
            started = []

            class Timer:
                def __init__(self, delay, callback, args):
                    started.append(delay)
                    self.fire = lambda: callback(*args)
                def start(self):
                    pass
                def cancel(self):
                    pass
            marks, codes, now[0] = [], [255, 0], 0.0

            def run(argv):
                if argv[1] == 'again':
                    timers[-1].fire()
                    marks.append(mark.exists())
                now[0] += 3600
                return codes.pop(0)
            timers = []
            with patch.object(terminal.termios, 'tcflush'):
                terminal._hold_loop(spec, run=run, sleep=lambda s: None, clock=lambda: now[0], out=io.StringIO(),
                                    timer=lambda *a: timers.append(Timer(*a)) or timers[-1])
            self.assertEqual((started, marks), ([terminal.HOLD_BACK], [False]))
            # Ctrl+C while waiting: no attaching again, and the board will end the run there.
            with patch.object(terminal.termios, 'tcflush'):
                code = terminal._hold_loop(spec, run=lambda argv: 255, sleep=lambda s: (_ for _ in ()).throw(KeyboardInterrupt()),
                                           clock=iter(range(0, 10**6, 3600)).__next__, out=io.StringIO())
            self.assertEqual(code, 130)
            self.assertTrue(mark.with_name(mark.name + '.ended').exists())
            # ... and while attaching again (SSH still connecting, so Ctrl+C reaches the loop).
            mark.with_name(mark.name + '.ended').unlink()
            calls = []

            def interrupted(argv):
                calls.append(argv[1])
                if argv[1] == 'again':
                    raise KeyboardInterrupt
                return 255
            with patch.object(terminal.termios, 'tcflush'):
                code = terminal._hold_loop(spec, run=interrupted, sleep=lambda s: None,
                                           clock=iter(range(0, 10**6, 3600)).__next__, out=io.StringIO())
            self.assertEqual((code, calls), (130, ['first', 'again']))
            self.assertTrue(mark.with_name(mark.name + '.ended').exists())

    def test_other_ends_are_the_terminals_end(self):
        for codes, durations, expected in (([0], [100], 0), ([1], [100], 1), ([-15], [100], 1),
                                           ([255], [3], 255)):
            with self.subTest(codes=codes):
                code, ran, slept, _, _ = self.loop(codes, durations)
                self.assertEqual((code, ran, slept), (expected, ['first'], []))
        code, ran, slept, _, _ = self.loop([255], [600], sleeps=KeyboardInterrupt())
        self.assertEqual((code, ran), (130, ['first']))


class BoardHoldTest(unittest.TestCase):
    TAG = 'feedface'

    def flow(self, terminals=(), holds=(), online=True, support='/usr/bin/tmux'):
        calls = []

        def rpc(host, op, args, timeout=30):
            calls.append((host['name'], op, args))
            return {'holds': list(holds)} if op == 'hold_list' else {'killed': True}
        waiting = Path(tempfile.mkdtemp(dir='/tmp', prefix='sh-hold-ended-'))
        self.addCleanup(lambda: [f.unlink() for f in waiting.iterdir()] and None or waiting.rmdir())
        transfers = SimpleNamespace(lock=threading.RLock(), hold_support={'remote': support, 'local': '/usr/bin/tmux'}, rpc=rpc,
                                    holds_mine=lambda name: isinstance(name, str) and name.startswith('sh-' + self.TAG),
                                    hold_waiting=waiting)
        board = SimpleNamespace(hosts=[LOCAL, REMOTE, PC], state={'local': {'ok': True}, 'remote': {'ok': online}, 'pc': {'ok': True}})
        flow = SimpleNamespace(board=board, transfers=transfers, terminals=SimpleNamespace(list=lambda: list(terminals)),
                               HOLD_GRACE_SECONDS=server.Workflow.HOLD_GRACE_SECONDS,
                               HOLD_KEEP_SECONDS=server.Workflow.HOLD_KEEP_SECONDS)
        flow.hold_ends = lambda host, item: server.Workflow.hold_ends(flow, host, item)
        return flow, calls

    def name(self, last):
        return 'sh-' + self.TAG + '0000000' + last

    def test_the_sweep_ends_what_its_user_or_the_board_closed_and_keeps_a_run_that_lost_its_terminal(self):
        now = 2_000_000_000
        rows = [{'id': 'a' * 32, 'host': 'remote', 'holdName': self.name('a'), 'closed': False},
                {'id': 'b' * 32, 'host': 'remote', 'metadata': {'holdName': self.name('b')}, 'closed': True, 'closedReason': 'user'},
                {'id': 'c' * 32, 'host': 'remote', 'holdName': self.name('c'), 'closed': True, 'closedReason': 'released'},
                # Ended by itself (a reattach that could not happen): its run may go on; kept a day.
                {'id': 'd' * 32, 'host': 'remote', 'holdName': self.name('d'), 'closed': True,
                 'closedReason': 'connection_ended', 'closedAt': now - 3600},
                {'id': 'e' * 32, 'host': 'remote', 'holdName': self.name('e'), 'closed': True,
                 'closedReason': 'connection_ended', 'closedAt': now - 2 * 86400}]
        holds = [{'name': self.name(x), 'age': 9999} for x in 'abcde'] + [
            {'name': self.name('f'), 'age': 600}, {'name': self.name('9'), 'age': 2 * 86400},  # no record
            {'name': self.name('8'), 'age': 10},  # just created
            {'name': 'sh-0123456789abcdef', 'age': 9 * 86400}]  # another board's
        flow, calls = self.flow(rows, holds)
        with patch('sys.stderr', new_callable=io.StringIO), patch.object(server.time, 'time', return_value=now):
            server.Workflow.sweep_holds(flow)
        self.assertEqual([call[2]['name'] for call in calls if call[1] == 'hold_kill'],
                         [self.name('b'), self.name('c'), self.name('e'), self.name('9')])
        # A terminal its user stopped with Ctrl+C while waiting: ended once its host is back.
        flow, calls = self.flow(rows[3:4], [{'name': self.name('d'), 'age': 9999}])
        ended = flow.transfers.hold_waiting / (self.name('d') + '.ended')
        ended.touch()
        with patch('sys.stderr', new_callable=io.StringIO), patch.object(server.time, 'time', return_value=now):
            server.Workflow.sweep_holds(flow)
        self.assertEqual([call[2]['name'] for call in calls if call[1] == 'hold_kill'], [self.name('d')])
        self.assertFalse(ended.exists(), 'its mark goes with it')
        holds = [{'name': self.name('d'), 'age': 600}]
        for offline, support in ((False, '/usr/bin/tmux'), (True, None)):
            flow, calls = self.flow([], holds, online=offline, support=support)
            server.Workflow.sweep_holds(flow)
            self.assertEqual(calls, [], 'an offline or non-holding host is left alone')

    def test_what_tests_left_is_ended_and_nothing_else(self):
        # Seen: 17 orphaned Codex daemons from test homes (461MB) and thousands of test tmux
        # socket files (tmux keeps them after kill-server) piled up on the Macs.
        temp = '/private/var/folders/xx/yy/T'
        daemon = temp + '/tmpab12cd/.codex-isolated/packages/app-server-daemon/releases/0.1/bin/codex'
        uid = str(os.getuid())
        listing = '\n'.join([
            '101 1 %s 2-07:56:02 %s app-server --listen unix:// --managed-daemon %s/tmpab12cd' % (uid, daemon, temp),
            '102 1 %s 1:00:00 %s app-server --listen unix://' % (uid, daemon),
            '103 1 %s 59:59 %s app-server' % (uid, daemon),  # younger than an hour
            '104 555 %s 3-00:00:00 %s app-server' % (uid, daemon),  # still has its parent
            '105 1 %s 3-00:00:00 /Users/u/.codex/packages/app-server-daemon/releases/0.1/bin/codex app-server' % uid,
            '106 1 999999 3-00:00:00 %s app-server' % daemon,  # another user's
            '107 1 %s 3-00:00:00 %s exec run' % (uid, daemon),
            '108 1 %s 3-00:00:00 /usr/bin/python3 %s app-server' % (uid, daemon),
            # Not a Codex home right inside a temporary folder a test made.
            '109 1 %s 3-00:00:00 %s/tmpab12cd/work/.codex/packages/app-server-daemon/r/bin/codex app-server' % (uid, temp),
            '110 1 %s 3-00:00:00 %s/cache/.codex/packages/app-server-daemon/r/bin/codex app-server' % (uid, temp),
            '111 1 %s 3-00:00:00 %s/tmpab12cd/home/packages/app-server-daemon/r/bin/codex app-server' % (uid, temp),
            'garbage line'])
        ended = []
        folder = Path(tempfile.mkdtemp(dir='/tmp'))
        self.addCleanup(lambda: [path.unlink() for path in folder.iterdir()] and None or folder.rmdir())
        def socket_file(name, age, live=False):
            import socket as sockets
            server_socket = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
            server_socket.bind(str(folder / name))
            if live:
                server_socket.listen(4)
                self.addCleanup(server_socket.close)
            else:
                server_socket.close()
            os.utime(folder / name, (now - age, now - age))
        now = 2_000_000_000
        socket_file('sb-test-old', 2 * 86400)
        socket_file('sb-browser-old', 2 * 86400)
        socket_file('sb-test-young', 3600)
        socket_file('sb-test-live', 2 * 86400, live=True)
        socket_file('session-board', 9 * 86400)
        socket_file('default', 9 * 86400)
        (folder / 'sb-test-file').write_text('not a socket'); os.utime(folder / 'sb-test-file', (0, 0))
        result = transfer_worker.sweep_leftovers(listing=listing, temp=temp, sockets=folder, now=now,
                                                 kill=lambda pid, sig: ended.append((pid, sig)))
        self.assertEqual(ended, [(101, transfer_worker.signal.SIGTERM), (102, transfer_worker.signal.SIGTERM)])
        self.assertEqual(result, {'processes': 2, 'sockets': 2})
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ['default', 'sb-test-file', 'sb-test-live', 'sb-test-young', 'session-board'])
        # The board asks each Mac it reaches (itself directly), never a PC or an offline host.
        asked = []
        board = SimpleNamespace(hosts=[LOCAL, REMOTE, PC], state={'local': {'ok': False}, 'remote': {'ok': True}, 'pc': {'ok': True}})
        flow = SimpleNamespace(board=board, transfers=SimpleNamespace(
            rpc=lambda host, op, args, timeout: asked.append((host['name'], op)) or {'processes': 1, 'sockets': 0}))
        with patch.object(transfer_worker, 'sweep_leftovers', return_value={'processes': 0, 'sockets': 3}), \
                patch('sys.stderr', new_callable=io.StringIO) as log:
            server.Workflow.sweep_leftovers(flow)
        self.assertEqual(asked, [('remote', 'leftovers')])
        self.assertIn('실행 0개와 tmux 소켓 파일 3개를 정리했습니다: local', log.getvalue())
        self.assertIn('실행 1개와 tmux 소켓 파일 0개를 정리했습니다: remote', log.getvalue())
        board.state['remote']['ok'] = False
        asked.clear()
        with patch.object(transfer_worker, 'sweep_leftovers', return_value={'processes': 0, 'sockets': 0}):
            server.Workflow.sweep_leftovers(flow)
        self.assertEqual(asked, [])

    def test_a_terminal_list_failure_ends_nothing(self):
        flow, calls = self.flow([], [{'name': self.name('d'), 'age': 9 * 86400}])
        flow.terminals = SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError('tmux')))
        server.Workflow.sweep_holds(flow)
        self.assertEqual([call for call in calls if call[1] == 'hold_kill'], [])

    def test_a_closed_terminal_ends_its_hold_now_or_says_it_will(self):
        row = {'id': 'a' * 32, 'host': 'remote', 'holdName': 'sh-000000000000000a'}
        flow, calls = self.flow()
        done = threading.Event()
        original = flow.transfers.rpc
        flow.transfers.rpc = lambda *a, **k: (original(*a, **k), done.set())[0]
        self.assertFalse(server.Workflow.end_hold(flow, row))
        self.assertTrue(done.wait(5))
        self.assertEqual(calls, [('remote', 'hold_kill', {'name': 'sh-000000000000000a'})])
        flow, calls = self.flow(online=False)
        self.assertTrue(server.Workflow.end_hold(flow, row))
        self.assertEqual(calls, [])
        self.assertFalse(server.Workflow.end_hold(flow, {'id': 'b' * 32, 'host': 'remote'}), 'nothing held')

    def test_closing_a_held_terminal_while_its_host_is_away_says_so(self):
        temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-hold-http-')
        self.addCleanup(temp.cleanup)
        board = server.Board([LOCAL, REMOTE], 60, runner=lambda *a: self.fail('collection'), live=False)
        board.state['remote'].update(ok=False, data={'codex': [], 'claude': []})
        row = {'id': 'c' * 32, 'host': 'remote', 'mode': 'new-session', 'alive': True, 'closed': False,
               'holdName': 'sh-000000000000000c'}
        terminals = SimpleNamespace(list=lambda: [row], close=lambda: None, close_terminal=lambda tid: {'ok': True})
        httpd = server.make_server('127.0.0.1', 0, board, 'example-token', state_dir=temp.name, terminals=terminals)
        httpd.workflow.bot_requests = SimpleNamespace(snapshot=lambda: {'requests': []})
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), thread.join(2), httpd.server_close()))

        def request(path, body, cookie=None, csrf=None):
            client = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=5)
            headers = {'Content-Type': 'application/json'}
            if cookie:
                headers.update({'Cookie': cookie, 'X-CSRF-Token': csrf})
            client.request('POST', path, body=json.dumps(body), headers=headers)
            response = client.getresponse()
            result = json.loads(response.read())
            client.close()
            return response, result
        response, login = request('/login', {'token': 'example-token'})
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        with patch('sys.stderr', new_callable=io.StringIO):
            response, result = request('/api/terminal/%s/close' % ('c' * 32), {}, cookie, login['csrfToken'])
        self.assertEqual((response.status, result.get('remoteCleanupPending')), (200, True))


if __name__ == '__main__':
    unittest.main()
