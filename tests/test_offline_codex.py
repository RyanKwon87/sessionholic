"""A Codex home without its app-server daemon: listed from its state DB read only, read and
sent to only after "앱 서버 켜기" starts that account's daemon.

HOME is a temporary folder with fixture state DBs; no daemon, CLI or model is started.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import collector
import launch
import managed_launch
import server
import transfer_worker

SCHEMA = ("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL, "
          "updated_at INTEGER NOT NULL, source TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL, "
          "archived INTEGER NOT NULL DEFAULT 0, first_user_message TEXT NOT NULL DEFAULT '', "
          "preview TEXT NOT NULL DEFAULT '', name TEXT)")


def state_db(folder, rows, name='state_5.sqlite'):
    folder.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(folder / name))
    connection.execute(SCHEMA)
    connection.executemany("INSERT INTO threads (id, rollout_path, created_at, updated_at, source, cwd, title, archived, "
                           "first_user_message, preview, name) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


class OfflineHomeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-off-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for patcher in (patch.object(collector, 'HOME', self.home), patch.object(collector, 'ROUTER', self.home / '.codex-router')):
            patcher.start()
            self.addCleanup(patcher.stop)
        # .codex: no daemon, a state DB; .codex-live: a daemon socket; a kept router home: no daemon.
        state_db(self.home / '.codex', [
            ('019a0000-0000-7000-8000-000000000001', 100, 300, 'cli', '/work/a', '첫 작업', 0, '안녕', '', None),
            ('019a0000-0000-7000-8000-000000000002', 110, 500, 'vscode', '/work/b', '자동 제목', 0, '두 번째 질문', '미리보기', '이름 붙인 작업'),
            ('019a0000-0000-7000-8000-000000000003', 120, 900, 'exec', '/work/c', '자동화', 0, '', '', None),
            ('019a0000-0000-7000-8000-000000000004', 130, 950, 'cli', '/work/d', '보관함', 1, '', '', None)])
        socket_path = self.home / '.codex-live' / collector.SOCKET_REL
        socket_path.parent.mkdir(parents=True)
        socket_path.write_text('')
        state_db(self.home / '.codex-router/0123456789abcdef', [
            ('019a0000-0000-7000-8000-000000000005', 100, 300, 'cli', '/old', '옛 작업', 0, '', '', None)])

    def test_homes_without_a_daemon_are_listed_from_the_state_db_read_only(self):
        self.assertEqual([key for key, _ in collector.codex_homes()], ['.codex-live'])
        self.assertEqual([key for key, _ in collector.codex_offline_homes()], ['.codex'])
        before = (self.home / '.codex/state_5.sqlite').read_bytes()
        listed, loaded, failure = collector.codex_db_threads(self.home / '.codex')
        self.assertEqual((loaded, failure), (set(), None))
        # Interactive, not archived, newest first; no exec run.
        self.assertEqual([t['id'][-1] for t in listed['data']], ['2', '1'])
        rows, errors = collector.codex_home_rows('.codex', None, '기본', (listed, loaded, failure), [], offline=True)
        self.assertEqual(errors, [])
        self.assertEqual([(r['title'], r['phase'], r['appServer'], r['source'], r['loaded']) for r in rows],
                         [('이름 붙인 작업', 'closed', 'off', 'vscode', False), ('안녕', 'closed', 'off', 'cli', False)])
        self.assertEqual((rows[0]['startedAt'], rows[0]['updatedAt']), (110, 500))
        self.assertEqual((self.home / '.codex/state_5.sqlite').read_bytes(), before)

    def test_snapshot_adds_offline_rows_and_reports_an_unreadable_db(self):
        class Live:
            def __init__(self, path, timeout=10.0):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def call(self, method, params):
                if method == 'thread/list':
                    return {'data': [{'id': 'live-thread', 'status': {'type': 'idle'}, 'source': 'cli'}]}
                return {'data': []}
        errors = []
        with patch.object(collector, 'AppServer', Live):
            rows = collector.codex_threads(errors, [])
        self.assertEqual([(r['home'], r.get('appServer')) for r in rows],
                         [('.codex-live', None), ('.codex', 'off'), ('.codex', 'off')])
        self.assertEqual(errors, [])
        (self.home / '.codex/state_5.sqlite').write_bytes(b'not a database')
        errors = []
        with patch.object(collector, 'AppServer', Live):
            rows = collector.codex_threads(errors, [])
        self.assertEqual([r['home'] for r in rows], ['.codex-live'])
        self.assertEqual(errors, ['Codex(기본) 기록을 읽지 못했습니다.'])

    def test_a_wal_database_left_without_its_shm_is_read_as_immutable_and_never_written(self):
        folder = self.home / '.codex-left'
        state_db(folder, [('019a0000-0000-7000-8000-000000000007', 1, 2, 'cli', '/l', '남은 작업', 0, '', '', None)])
        connection = sqlite3.connect(str(folder / 'state_5.sqlite'))
        connection.execute('PRAGMA journal_mode=WAL')
        connection.close()
        for extra in ('state_5.sqlite-shm', 'state_5.sqlite-wal'):
            (folder / extra).unlink(missing_ok=True)
        before = sorted(p.name for p in folder.iterdir())
        listed, _, failure = collector.codex_db_threads(folder)
        self.assertIsNone(failure)
        self.assertEqual([t['preview'] for t in listed['data']], ['남은 작업'])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before)

    def test_reading_a_thread_of_a_home_without_a_daemon_says_why(self):
        result = collector.read('codex', '019a0000-0000-7000-8000-000000000001', '.codex')
        self.assertEqual((result['messages'], result['code']), ([], 'app_server_off'))
        self.assertIn('앱 서버가 꺼져', result['error'])

    def test_newest_state_db_and_hidden_homes(self):
        state_db(self.home / '.codex', [('019a0000-0000-7000-8000-000000000009', 1, 2, 'cli', '/n', '새 DB', 0, '', '', None)],
                 name='state_12.sqlite')
        self.assertEqual(collector.state_db(self.home / '.codex').name, 'state_12.sqlite')
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text(json.dumps({'version': 2, 'codexHomes': {'hide': ['.codex']}}))
        config.chmod(0o600)
        self.assertEqual(collector.codex_offline_homes(), [])


class StartAppServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-off-start-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text(json.dumps({'version': 2, 'environments': {
            'private': {'label': 'Private', 'variables': {'GIT_CONFIG_NOSYSTEM': '1'}}},
            'profileRules': [{'agent': 'codex', 'homePrefix': '.codex-private', 'environment': 'private'}]}))
        config.chmod(0o600)
        for key in ('.codex', '.codex-private'):
            (self.home / key).mkdir()
            (self.home / key / 'auth.json').write_text('fixture-only-never-read')
        self.binary = self.home / 'codex'
        self.binary.write_text('#!/bin/sh\nexit 1\n')
        self.binary.chmod(0o700)

    @contextmanager
    def daemon(self, started=True):
        runs = []
        running = set()

        def run(argv, **kwargs):
            runs.append((argv, kwargs['env']))
            if started:
                running.add(Path(kwargs['env']['CODEX_HOME']).name)

            class Done:
                returncode = 0 if started else 1
            return Done()
        with patch.dict(os.environ, {'PATH': '/usr/bin:/bin', 'SSH_AUTH_SOCK': '/tmp/agent.fixture'}, clear=True), \
                patch.object(launch, '_binary', return_value=str(self.binary)), \
                patch('managed_launch.subprocess.run', side_effect=run), \
                patch('managed_launch.app_server_running', side_effect=lambda profile, home=None: profile['home'] in running), \
                patch('managed_launch.APP_SERVER_WAIT_SECONDS', 0.3):
            yield runs

    def test_start_uses_the_accounts_launch_variables_and_the_ssh_agent_only_when_plain(self):
        with self.daemon() as runs:
            self.assertEqual(launch.start_app_server('.codex', self.home), {'started': True})
            self.assertEqual(launch.start_app_server('.codex-private', self.home), {'started': True})
        (plain_argv, plain), (_, private) = runs
        self.assertEqual(plain_argv, [str(self.binary), 'app-server', 'daemon', 'start'])
        self.assertEqual((plain['CODEX_HOME'], plain['SSH_AUTH_SOCK']), (str(self.home / '.codex'), '/tmp/agent.fixture'))
        self.assertEqual((private['CODEX_HOME'], private['GIT_CONFIG_NOSYSTEM']), (str(self.home / '.codex-private'), '1'))
        self.assertNotIn('SSH_AUTH_SOCK', private)

    def test_unknown_account_or_failed_start_is_refused(self):
        with self.daemon(started=False):
            for key in ('.codex-none', '../x', None):
                with self.assertRaises(ValueError):
                    launch.start_app_server(key, self.home)
            with self.assertRaisesRegex(ValueError, '앱 서버를 시작하지 못했습니다'):
                launch.start_app_server('.codex', self.home)

    def test_worker_starts_the_account_named_by_the_board(self):
        with patch('launch.start_app_server', return_value={'started': True}) as start:
            self.assertEqual(transfer_worker._rpc({'op': 'start_app_server', 'args': {'home': '.codex'}}), {'started': True})
        start.assert_called_once_with('.codex')


LOCAL = {'name': 'local', 'label': 'Local', 'local': True}
REMOTE = {'name': 'remote', 'label': 'Remote', 'local': False, 'ssh': 'remote', 'python': '/usr/bin/python3'}


class WorkflowStartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-off-flow-')
        self.addCleanup(self.temp.cleanup)
        runner = lambda host, args, timeout: {'codex': [], 'claude': [], 'errors': []}
        self.board = server.Board([LOCAL, REMOTE], 60, runner)
        for host in (LOCAL, REMOTE):
            self.board.poll(host)
        self.flow = server.Workflow(self.board, None, self.temp.name)
        self.refreshed = []
        self.board.request_refresh = lambda name=None, force=False: self.refreshed.append((name, force))

    def test_local_and_remote_start_then_read_the_host_again(self):
        with patch('launch.start_app_server', return_value={'started': True}) as start:
            self.assertEqual(self.flow.start_app_server({'host': 'local', 'home': '.codex'}), {'ok': True, 'started': True})
        start.assert_called_once_with('.codex')
        with patch.object(self.flow.transfers, 'rpc', return_value={'started': False}) as rpc:
            self.assertEqual(self.flow.start_app_server({'host': 'remote', 'home': '.codex-beta'}), {'ok': True, 'started': False})
        self.assertEqual(rpc.call_args.args[1:3], ('start_app_server', {'home': '.codex-beta'}))
        self.assertGreaterEqual(rpc.call_args.kwargs['timeout'], 60)
        self.assertEqual(self.refreshed, [('local', True), ('remote', True)])

    def test_bad_requests_and_an_offline_host_start_nothing(self):
        with patch('launch.start_app_server') as start, patch.object(self.flow.transfers, 'rpc') as rpc:
            for payload in ({'host': 'local', 'home': '../x'}, {'host': 'nowhere', 'home': '.codex'}, [], {'host': 'local'}):
                with self.assertRaises(ValueError):
                    self.flow.start_app_server(payload)
            self.board.state['remote']['ok'] = False
            with self.assertRaisesRegex(RuntimeError, '연결할 수 없어'):
                self.flow.start_app_server({'host': 'remote', 'home': '.codex'})
        start.assert_not_called()
        rpc.assert_not_called()
        self.assertEqual(self.refreshed, [])


if __name__ == '__main__':
    unittest.main()
