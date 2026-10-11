"""The optional agent-router adapter with fixture files (fake HOME, fake projects and accounts)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_router
import collector
import launch
import server
import settings

ROOT = Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-router-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.router_dir = self.home / '.config/router'
        self.router_dir.mkdir(parents=True)
        (self.router_dir / 'projects.tsv').write_text(
            '# id\tlabel\troot\n\nalpha\tAlpha 프로젝트\t' + str(self.home / 'alpha') + '\n'
            'short\tonly-two\n'
            'beta\tBeta\tssh://elsewhere/beta\n', encoding='utf-8')
        (self.router_dir / 'policy.json').write_text(json.dumps({
            'accounts': {'one': 'fixture-one@example.invalid', 'two': 'fixture-two@example.invalid'},
            'suspended_accounts': ['two']}))
        self.section = {'projectsFile': str(self.router_dir / 'projects.tsv'),
                        'workspacesRoot': str(self.router_dir / 'workspaces'),
                        'accountPolicy': str(self.router_dir / 'policy.json')}
        self.adapter = agent_router.AgentRouter(self.section)


class AdapterTest(Fixture):
    def test_project_rows_skip_comments_blank_and_short_rows(self):
        projects = self.adapter.projects()
        self.assertEqual([p['id'] for p in projects], ['alpha', 'beta'])
        self.assertEqual(projects[0]['roots'], [str(self.home / 'alpha'), str(self.router_dir / 'workspaces/alpha')])
        self.assertEqual(agent_router.AgentRouter({'projectsFile': str(self.home / 'missing.tsv')}).projects(), [])

    def test_account_keys_only_and_suspended_accounts(self):
        self.assertEqual(self.adapter.accounts(), ['one', 'two'])
        self.assertEqual(self.adapter.suspended(), ['two'])
        self.assertNotIn('example.invalid', json.dumps(self.adapter.labels()))
        broken = self.router_dir / 'broken.json'
        broken.write_text('[1, 2]')
        self.assertEqual(agent_router.AgentRouter({'accountPolicy': str(broken)}).accounts(), [])

    def test_router_hash_labels_scope_fallback_and_unknown(self):
        digest = hashlib.sha256('one\0alpha\0default'.encode()).hexdigest()[:16]
        self.assertEqual(agent_router.router_hash('one', 'alpha'), digest)
        labels = self.adapter.labels()
        self.assertEqual(self.adapter.home_label('.codex-router/' + digest, self.home, labels), ('alpha', 'one'))
        scoped = self.home / '.codex-router/ffffffffffffffff'
        (scoped / 'ag-sessions').mkdir(parents=True)
        (scoped / 'ag-sessions/1.json').write_text(json.dumps({'scope': {'project': 'gamma', 'account': 'three'}}))
        self.assertEqual(self.adapter.home_label('.codex-router/ffffffffffffffff', scoped, labels), ('gamma', 'three'))
        self.assertEqual(self.adapter.home_label('.codex-router/eeeeeeeeeeeeeeee', self.home / 'none', labels),
                         (None, '미확인 eeeeee'))
        self.assertEqual(self.adapter.home_label('.codex', self.home, labels), (None, '기본'))
        self.assertEqual(self.adapter.home_label('.codex-private-one', self.home, labels), (None, 'private-one'))


class CollectorTest(Fixture):
    def setUp(self):
        super().setUp()
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True, mode=0o700)
        self.config = config
        self.write({'version': 2, 'agentRouter': {
            'projectsFile': '~/.config/router/projects.tsv', 'workspacesRoot': '~/.config/router/workspaces',
            'accountPolicy': '~/.config/router/policy.json'}, 'codexHomes': {'excludeSuspended': True}})
        for patcher in (patch.object(collector, 'HOME', self.home),
                        patch.object(collector, 'ROUTER', self.home / '.codex-router')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.digest = agent_router.router_hash('one', 'alpha')
        for key in ('.codex', '.codex-two', '.codex-personal-two', '.codex-one', '.codex-router/' + self.digest):
            socket_path = self.home / key / 'app-server-control/app-server-control.sock'
            socket_path.parent.mkdir(parents=True)
            socket_path.write_text('')

    def write(self, value):
        self.config.write_text(json.dumps(value))
        self.config.chmod(0o600)

    def test_projects_include_workspace_roots_and_router_homes_are_labelled(self):
        projects = collector.load_projects()
        self.assertEqual(collector.project_for(str(self.router_dir / 'workspaces/alpha/src'), projects), 'Alpha 프로젝트')
        self.assertEqual(collector.project_for(str(self.home / 'alpha'), projects), 'Alpha 프로젝트')
        self.assertEqual(collector.project_for(str(self.home / 'other'), projects), 'other')
        homes = collector.codex_homes()
        self.assertEqual([key for key, _ in homes], ['.codex', '.codex-one', '.codex-router/' + self.digest])
        self.assertEqual(collector.codex_labels(homes), [(None, '기본'), (None, 'one'), ('alpha', 'one')])

    def test_hidden_homes_are_neither_collected_nor_launch_targets(self):
        (self.home / '.codex-retired/app-server-control').mkdir(parents=True)
        (self.home / '.codex-retired/app-server-control/app-server-control.sock').write_text('')
        for key in ('.codex-retired', '.codex-one'):
            (self.home / key / 'auth.json').write_text('fixture-only-never-read')
        self.write({'version': 2, 'codexHomes': {'hide': ['.codex-retired', '.codex-two']}})
        self.assertNotIn('.codex-retired', [key for key, _ in collector.codex_homes()])
        self.assertNotIn('.codex-two', [key for key, _ in collector.codex_homes()])
        with patch.object(launch, '_binary', return_value='/fixture/codex'):
            homes = [p['home'] for p in launch.discover_profiles(self.home)]
        self.assertIn('.codex-one', homes)
        self.assertNotIn('.codex-retired', homes)
        for bad in (['../x'], '.codex-x', [1]):
            self.write({'version': 2, 'codexHomes': {'hide': bad}})
            with self.assertRaises(ValueError):
                settings.load(self.home)

    def test_profile_rows_label_first_and_settings_without_router_ignore_its_files(self):
        self.write({'version': 2, 'agentRouter': {'projectsFile': '~/.config/router/projects.tsv'},
                    'profiles': [{'agent': 'codex', 'home': '.codex-one', 'label': 'First', 'project': 'alpha'}]})
        homes = [(key, self.home / key) for key in ('.codex-one', '.codex-router/' + self.digest)]
        # Without an account policy no hash is known.
        self.assertEqual(collector.codex_labels(homes), [('alpha', 'First'), (None, '미확인 ' + self.digest[:6])])
        self.write({'version': 1})
        self.assertEqual(collector.load_projects(), [])
        self.assertEqual([key for key, _ in collector.codex_homes()],
                         ['.codex', '.codex-one', '.codex-personal-two', '.codex-two', '.codex-router/' + self.digest])
        self.assertEqual(collector.codex_labels(homes), [(None, '.codex-one'), (None, '.codex-router/' + self.digest)])

    def test_unreadable_daemon_reply_is_reported_for_that_home_only(self):
        class Server:
            def __init__(self, path):
                self.home = Path(path).parent.parent.name
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def call(self, method, params):
                if self.home == '.codex-one' and method == 'thread/loaded/list':
                    raise TypeError('fixture')
                return ({'data': [{'id': 't' + self.home, 'status': {'type': 'active'}}]}
                        if method == 'thread/list' else {'data': []})

        errors = []
        homes = [(key, self.home / key) for key in ('.codex', '.codex-one')]
        with patch.object(collector, 'codex_homes', return_value=homes), patch.object(collector, 'AppServer', Server):
            rows = collector.codex_threads(errors, [])
        self.assertEqual([(row['home'], row['phase']) for row in rows], [('.codex', 'working')])
        self.assertEqual(errors, ['Codex daemon(one) 응답을 읽지 못했습니다.'])

    def test_remote_stdin_program_carries_the_adapter(self):
        probe = ('import json, sys\nnames = {"__name__": "remote_collector"}\n'
                 'exec(compile(sys.stdin.read(), "<collector>", "exec"), names)\n'
                 'print(json.dumps([names["load_projects"]()[0]["roots"], names["codex_labels"](names["codex_homes"]())]))')
        program = server.remote_program().decode('utf-8')
        done = subprocess.run([sys.executable, '-I', '-c', probe], input=program.encode(), capture_output=True,
                              env={'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                              timeout=60, cwd=str(self.home))
        self.assertEqual(done.returncode, 0, done.stderr.decode()[-800:])
        roots, labels = json.loads(done.stdout)
        self.assertEqual(roots[1], str(self.router_dir / 'workspaces/alpha'))
        self.assertEqual(labels[-1], ['alpha', 'one'])
        self.assertIn("sys.modules['agent_router']", server.remote_prelude())


if __name__ == '__main__':
    unittest.main()
