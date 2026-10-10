"""Version 2 host settings: boundary rules, older transfer records, profile display and toggles.

Fixture names only: a `Private` folder component and `.codex-private*` homes stand for any
host's own convention. HOME is a temporary folder.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import attachments
import collector
import launch
import server
import settings
import transfer_workspace

ID = 'a' * 32
ROUTER = '.codex-router/0123456789abcdef'
PRIVATE_KEYS = ['CMUX_CODEX_HOOKS_DISABLED', 'GH_CONFIG_DIR', 'GIT_CONFIG_NOSYSTEM']
MESSAGES = [{'role': 'user', 'text': '이어서 진행해 주세요.'}]
CONFIG = {
    'version': 2,
    'environments': {
        'default': {'label': 'Standard', 'scope': 'standard', 'description': 'Standard folders.'},
        'private': {'label': 'Private', 'description': 'Private Git settings.', 'variables': {
            'GH_CONFIG_DIR': '~/.config/private/gh', 'GIT_CONFIG_NOSYSTEM': '1', 'CMUX_CODEX_HOOKS_DISABLED': '1'}},
        'isolated': {'label': 'Isolated', 'boundaryGroup': 'default'},
        'device': {'label': 'Device', 'scope': 'device', 'boundaryGroup': 'default'},
    },
    'profileRules': [
        {'agent': 'codex', 'homePrefix': '.codex-private', 'environment': 'private', 'label': 'Codex', 'suffixLabel': True},
        {'agent': 'codex', 'homePrefix': '.codex-router/', 'environment': 'isolated', 'suffixLabel': True},
        {'agent': 'codex', 'homePrefix': '.codex', 'label': 'Codex', 'suffixLabel': True},
    ],
    'profiles': [
        {'agent': 'codex', 'home': '.codex-private-beta', 'placeholder': True},
        {'agent': 'claude', 'home': '.claude', 'label': 'Claude', 'title': 'Device Claude',
         'environment': 'device', 'placeholder': True},
    ],
    'projects': [{'id': 'private', 'label': 'Private', 'root': 'Private', 'match': 'component',
                  'environment': 'private'}],
    'boundary': {'agents': ['codex'], 'claude': 'inherit-source', 'priority': 'private'},
    'legacy': {'sourcePersonal': {'true': 'private', 'false': 'default'}},
    'codexHomes': {'requireMarker': True},
}


class BoundaryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-boundary-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / 'state'
        self.config_file = self.home / '.config/sessionholic/config.json'
        self.config_file.parent.mkdir(parents=True, mode=0o700)
        self.write_config(CONFIG)
        for key in ('.codex', '.codex-work', '.codex-private', ROUTER):
            (self.home / key).mkdir(parents=True)
            (self.home / key / 'auth.json').write_text('{}')
        (self.home / '.codex-empty').mkdir()
        (self.home / '.claude').mkdir()
        for folder in ('Private/x', 'Work/Private/y', 'Work/z'):
            (self.home / folder).mkdir(parents=True)
        self.imported = self.home / '.local/share/sessionholic/workspaces' / ID / 'project'
        self.imported.mkdir(parents=True)
        self.record = self.home / '.local/state/sessionholic-transfers' / ID / 'transfer.json'
        self.record.parent.mkdir(parents=True, mode=0o700)
        for patcher in (patch.dict(os.environ, {'HOME': str(self.home)}),
                        patch.object(launch, '_binary', return_value='/fixture/bin/agent'),
                        patch.object(collector, 'HOME', self.home)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self, value):
        self.config_file.write_text(json.dumps(value))
        self.config_file.chmod(0o600)

    def write_record(self, value, mode=0o600):
        self.record.write_text(value if isinstance(value, str) else json.dumps(value))
        self.record.chmod(mode)

    def sources(self):
        claude = {'agent': 'claude', 'home': '.claude', 'id': 'claude-session-1', 'kind': 'background'}
        codex = {'agent': 'codex', 'id': '019aaaaa-0000-7000-8000-000000000001'}
        return {
            'private folder': {**codex, 'home': '.codex', 'cwd': str(self.home / 'Private/x')},
            'private component': {**codex, 'home': '.codex', 'cwd': str(self.home / 'Work/Private/y')},
            'private home': {**codex, 'home': '.codex-private-beta', 'cwd': str(self.home / 'Work/z')},
            'router home': {**codex, 'home': ROUTER, 'cwd': str(self.home / 'Work/z')},
            'standard': {**codex, 'home': '.codex', 'cwd': str(self.home / 'Work/z')},
            'imported': {**claude, 'cwd': str(self.imported)},
        }

    def decide(self, source, target):
        agent, home = target
        profile = {'id': 'claude:default' if agent == 'claude' else 'codex:' + home, 'agent': agent,
                   'home': home, 'host': 'h'}
        try:
            result = launch.build_launch({**source, 'host': 'h', 'phase': 'idle'}, profile, MESSAGES,
                                         self.state, home=self.home)
        except ValueError as error:
            return 'blocked', str(error)
        return 'allowed', sorted(key for key in result['env'] if key in settings.ENV_KEYS)


class DecisionTableTest(BoundaryFixture):
    TARGETS = {'standard codex': ('codex', '.codex-work'), 'private codex': ('codex', '.codex-private'),
               'claude': ('claude', '.claude')}

    def test_codex_targets_are_kept_apart_and_claude_inherits_the_source_environment(self):
        expected = {'private': {'standard codex': 'blocked', 'private codex': PRIVATE_KEYS, 'claude': PRIVATE_KEYS},
                    'standard': {'standard codex': [], 'private codex': 'blocked', 'claude': []}}
        kinds = {'private folder': 'private', 'private component': 'private', 'private home': 'private',
                 'router home': 'standard', 'standard': 'standard'}
        sources = self.sources()
        for name, kind in kinds.items():
            for target_name, target in self.TARGETS.items():
                with self.subTest(source=name, target=target_name):
                    state, detail = self.decide(sources[name], target)
                    want = expected[kind][target_name]
                    if want == 'blocked':
                        self.assertEqual((state, detail), ('blocked', '원본과 대상의 실행 환경 경계가 다릅니다.'))
                    else:
                        self.assertEqual((state, detail), ('allowed', want))

    def test_imported_workspace_follows_its_record_including_older_booleans(self):
        source = self.sources()['imported']
        cases = {'sourceEnvironment private': ({'sourceEnvironment': 'private'}, PRIVATE_KEYS),
                 'sourcePersonal true': ({'sourcePersonal': True}, PRIVATE_KEYS),
                 'sourcePersonal false': ({'sourcePersonal': False}, [])}
        for name, (record, keys) in cases.items():
            self.write_record(record)
            with self.subTest(name):
                self.assertEqual(self.decide(source, ('claude', '.claude')), ('allowed', keys))
                self.assertEqual(self.decide(source, ('codex', '.codex-private'))[0],
                                 'allowed' if keys else 'blocked')
                self.assertEqual(self.decide(source, ('codex', '.codex-work'))[0],
                                 'blocked' if keys else 'allowed')

    def test_missing_unsafe_or_non_boolean_records_fail_closed(self):
        source = self.sources()['imported']
        for record in ({}, {'sourcePersonal': 'true'}, {'sourcePersonal': 1}, {'sourcePersonal': None},
                       {'sourceEnvironment': None, 'sourcePersonal': True}, {'sourceEnvironment': '../x'}, '[]'):
            self.write_record(record)
            with self.subTest(record=record):
                for target in self.TARGETS.values():
                    self.assertEqual(self.decide(source, target)[0], 'blocked')
        self.write_record({'sourcePersonal': True}, mode=0o644)
        self.assertEqual(self.decide(source, ('claude', '.claude'))[0], 'blocked')
        self.record.unlink()
        elsewhere = self.home / 'elsewhere.json'
        elsewhere.write_text('{"sourcePersonal": true}')
        elsewhere.chmod(0o600)
        self.record.symlink_to(elsewhere)
        self.assertEqual(self.decide(source, ('claude', '.claude'))[0], 'blocked')
        # Without a `legacy` mapping an older boolean record is refused.
        self.record.unlink()
        self.write_record({'sourcePersonal': True})
        self.write_config({key: value for key, value in CONFIG.items() if key != 'legacy'})
        self.assertEqual(self.decide(source, ('claude', '.claude'))[0], 'blocked')

    def test_priority_signals_decide_before_the_imported_record(self):
        private_folder = self.imported.parent / 'Private'
        private_folder.mkdir()
        codex = {'agent': 'codex', 'id': '019aaaaa-0000-7000-8000-000000000001'}
        cases = [  # (record, file mode, source home, folder, expected group or None for refused)
            ({'sourcePersonal': False}, 0o600, '.codex-private-beta', self.imported, 'private'),
            ({'sourcePersonal': False}, 0o600, '.codex', private_folder, 'private'),
            ({'sourceEnvironment': 'default'}, 0o600, '.codex-private', self.imported, 'private'),
            ({'sourcePersonal': True}, 0o600, '.codex', self.imported, 'private'),
            ({'sourcePersonal': False}, 0o600, ROUTER, self.imported, 'default'),
            # A signal decides without reading the record, so a missing or unsafe one does not matter.
            ({}, 0o600, '.codex-private-beta', self.imported, 'private'),
            ({'sourcePersonal': 'yes'}, 0o600, '.codex', private_folder, 'private'),
            ({'sourcePersonal': False}, 0o644, '.codex-private-beta', self.imported, 'private'),
            ({'sourcePersonal': False}, 0o644, '.codex', self.imported, None),
            ({}, 0o600, '.codex', self.imported, None),
        ]
        for record, mode, home, folder, expected in cases:
            self.write_record(record, mode)
            source = {**codex, 'home': home, 'cwd': str(folder)}
            with self.subTest(record=record, mode=oct(mode), home=home, folder=folder.name):
                if expected is None:
                    with self.assertRaisesRegex(ValueError, '이전 기록'):
                        launch.source_environment(source, self.home)
                else:
                    self.assertEqual(launch.source_environment(source, self.home), expected)
        self.write_record({'sourcePersonal': False})
        signal = {**codex, 'home': '.codex-private-beta', 'cwd': str(self.imported)}
        self.assertEqual(self.decide(signal, ('codex', '.codex-work'))[0], 'blocked')
        self.assertEqual(self.decide(signal, ('claude', '.claude')), ('allowed', PRIVATE_KEYS))
        # Without `boundary.priority` an imported workspace follows its record only.
        self.write_config({**CONFIG, 'boundary': {'agents': ['codex']}})
        self.assertEqual(launch.source_environment(signal, self.home), 'default')
        self.assertEqual(self.decide(signal, ('codex', '.codex-work'))[0], 'allowed')

    def test_priority_signal_wins_over_the_longest_root(self):
        self.write_config({**CONFIG, 'environments': {**CONFIG['environments'], 'team': {}},
                           'projects': CONFIG['projects'] + [
                               {'id': 'work', 'label': 'Work', 'root': '~/Work', 'environment': 'team'}]})
        def environment(folder, home='.codex'):
            return launch.source_environment({'agent': 'codex', 'home': home, 'cwd': str(self.home / folder)}, self.home)
        self.assertEqual(environment('Work/z'), 'team')
        self.assertEqual(environment('Work/Private/y'), 'private')
        self.assertEqual(environment('Work/z', '.codex-private'), 'private')
        self.write_config({**CONFIG, 'boundary': {'agents': ['codex'], 'priority': 'missing'}})
        with self.assertRaises(ValueError):
            settings.load(self.home)

    def test_resolved_link_into_a_private_folder_is_private(self):
        link = self.home / 'Work/shortcut'
        link.symlink_to(self.home / 'Private/x')
        source = {'agent': 'codex', 'home': '.codex', 'id': '019aaaaa-0000-7000-8000-000000000001', 'cwd': str(link)}
        self.assertEqual(launch.source_environment(source, self.home), 'private')
        self.assertEqual(self.decide(source, ('codex', '.codex-work'))[0], 'blocked')

    def test_existing_session_in_a_private_folder_can_attach_its_exact_home(self):
        socket_path = self.home / '.codex/app-server-control/app-server-control.sock'
        socket_path.parent.mkdir(mode=0o700)
        import socket
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(socket_path))
        source = self.sources()['private folder']
        state, keys = self.decide(source, ('codex', '.codex'))
        self.assertEqual((state, keys), ('allowed', PRIVATE_KEYS))

    def test_without_boundary_settings_every_agent_is_checked(self):
        self.write_config({key: value for key, value in CONFIG.items() if key != 'boundary'})
        self.assertEqual(self.decide(self.sources()['private folder'], ('claude', '.claude'))[0], 'blocked')
        self.assertEqual(self.decide(self.sources()['router home'], ('claude', '.claude'))[0], 'allowed')

    def test_transferred_launch_reads_the_record_fields_in_order(self):
        metadata = {'transferId': ID, 'sourceHost': 'one', 'targetHost': 'two', 'sourceCwd': '/origin/work',
                    'destinationCwd': str(self.imported), 'projectRoot': str(self.imported.parent),
                    'sourceStopped': True}
        source = {'host': 'one', 'agent': 'codex', 'home': '.codex', 'id': '019aaaaa-0000-7000-8000-000000000001',
                  'cwd': '/origin/work'}

        def build(extra, target):
            agent, home = target
            profile = {'id': 'claude:default' if agent == 'claude' else 'codex:' + home, 'agent': agent,
                       'home': home, 'host': 'two'}
            try:
                result = launch.build_transferred_launch(source, profile, MESSAGES, self.state, str(self.imported),
                                                         {**metadata, **extra}, home=self.home)
            except ValueError as error:
                return 'blocked', str(error)
            return 'allowed', sorted(key for key in result['env'] if key in settings.ENV_KEYS)

        self.assertEqual(build({'sourcePersonal': True}, ('codex', '.codex-private')), ('allowed', PRIVATE_KEYS))
        self.assertEqual(build({'sourcePersonal': True}, ('claude', '.claude')), ('allowed', PRIVATE_KEYS))
        self.assertEqual(build({'sourcePersonal': False}, ('codex', '.codex-private'))[0], 'blocked')
        self.assertEqual(build({'sourceEnvironment': 'isolated'}, ('codex', '.codex-work')), ('allowed', []))
        for extra in ({}, {'sourcePersonal': 'true'}, {'sourcePersonal': 1}):
            self.assertEqual(build(extra, ('claude', '.claude')), ('blocked', '원본 실행 환경을 확인하지 못했습니다.'))


class TransferRecordTest(BoundaryFixture):
    def execute(self, environment, profile_environment):
        from transfer import Transfers
        hosts = [{'name': 'target', 'label': 'Target', 'local': True},
                 {'name': 'origin', 'label': 'Origin', 'local': False, 'ssh': 'origin', 'python': '/usr/bin/python3'}]
        profile = {'id': 'codex:.codex-private', 'agent': 'codex', 'home': '.codex-private', 'available': True,
                   'environment': profile_environment}
        sent = []
        flow = Transfers(hosts, str(self.state / environment))

        def rpc(host, op, args=None, timeout=30):
            flow.runtime_paths[host['name']] = '/safe/runtime.pyz'
            replies = {'profiles': {'profiles': [profile]}, 'cleanup': {'ok': True}, 'inspect': {'fingerprint': 'f'},
                       'interrupt': {'confirmed': True, 'safeToTransfer': True, 'phase': 'idle', 'revision': 'r'},
                       'state': {'safeToTransfer': True, 'revision': 'r'},
                       'export': {'sha256': 'b' * 64, 'summary': {'fingerprint': 'f', 'sourceEnvironment': environment}}}
            if op == 'prepare':
                sent.append(args['metadata'])
                return {'cwd': '/new/work', 'warnings': []}
            return replies[op]
        flow.rpc = rpc
        flow._copy = lambda *args: None
        source = {'host': 'origin', 'agent': 'codex', 'home': '.codex', 'id': 'a' * 32, 'cwd': '/work', 'phase': 'idle'}
        flow.execute(source, {'host': 'target', 'agent': 'codex', 'account': profile['id']}, hosts[1], hosts[0],
                     profile, 'request-' + environment, lambda: [{'role': 'user', 'text': '이어가기'}])
        return sent[0]

    def test_new_records_also_carry_the_older_boolean_when_legacy_is_configured(self):
        metadata = self.execute('private', 'private')
        self.assertEqual((metadata['sourceEnvironment'], metadata['sourcePersonal']), ('private', True))
        self.assertIs(self.execute('isolated', 'default')['sourcePersonal'], False)
        self.write_config({key: value for key, value in CONFIG.items() if key != 'legacy'})
        self.assertNotIn('sourcePersonal', self.execute('default', 'default'))


class WorkflowBoundaryTest(BoundaryFixture):
    HOST = {'name': 'local', 'label': 'Local', 'local': True}

    def plan(self, source, account, agent):
        row = {**source, 'phase': 'idle', 'title': '작업', 'updatedAt': 100}
        board = server.Board([self.HOST], 60, lambda host, args, timeout: {
            'codex': [row] if row['agent'] == 'codex' else [], 'claude': [row] if row['agent'] == 'claude' else [],
            'errors': []})
        board.poll(self.HOST)
        terminals = type('Terminals', (), {'list': lambda self: []})()
        flow = server.Workflow(board, terminals, str(self.state))
        ref = {'host': 'local', 'agent': row['agent'], 'home': row.get('home'), 'id': row['id']}
        with patch('server.shutil.which', return_value='/usr/bin/tmux'):
            return flow.plan({'source': ref, 'target': {'host': 'local', 'agent': agent, 'account': account}})

    def test_plan_checks_codex_targets_only_and_resolves_local_paths(self):
        sources = self.sources()
        blocked = self.plan(sources['private component'], 'codex:.codex-work', 'codex')
        self.assertEqual((blocked['allowed'], blocked['reason']), (False, '원본과 대상의 실행 환경 경계가 다릅니다.'))
        self.assertTrue(self.plan(sources['private component'], 'codex:.codex-private', 'codex')['allowed'])
        self.assertTrue(self.plan(sources['private component'], 'claude:default', 'claude')['allowed'])
        self.assertTrue(self.plan(sources['router home'], 'codex:.codex-work', 'codex')['allowed'])
        self.write_record({'sourcePersonal': 'yes'})
        with self.assertRaises(ValueError):
            self.plan(sources['imported'], 'codex:.codex-work', 'codex')

    def test_another_hosts_path_is_estimated_from_names(self):
        config = settings.load(self.home)
        def quick(cwd, home='.codex'):
            return settings.quick_environment({'agent': 'codex', 'home': home, 'cwd': cwd}, config=config)
        self.assertEqual(quick('/remote/Private'), 'private')
        self.assertEqual(quick('/remote/a/Private/b'), 'private')
        self.assertEqual(quick('/remote/Privately/b'), 'default')
        self.assertEqual(quick('/remote/work', '.codex-private-gamma'), 'private')
        self.assertEqual(quick('/remote/work', ROUTER), 'isolated')
        self.assertEqual(quick(None), 'default')


class ProfileDisplayTest(BoundaryFixture):
    def test_placeholders_markers_scopes_and_rule_labels(self):
        profiles = {row['id']: row for row in launch.discover_profiles(self.home)}
        self.assertEqual(list(profiles), ['codex:.codex', 'codex:.codex-private', 'codex:.codex-private-beta',
                                          'codex:.codex-router/0123456789abcdef', 'codex:.codex-work', 'claude:default'])
        shown = {key: (row['label'], row['scope'], row['scopeDescription'], row['available'])
                 for key, row in profiles.items()}
        self.assertEqual(shown['codex:.codex'], ('Standard · Codex', 'standard', 'Standard folders.', True))
        self.assertEqual(shown['codex:.codex-work'][:2], ('Standard · work', 'standard'))
        self.assertEqual(shown['codex:.codex-private'][:3], ('Private · Codex', 'private', 'Private Git settings.'))
        self.assertEqual(shown['codex:.codex-private-beta'], ('Private · beta', 'private', 'Private Git settings.', False))
        self.assertEqual(profiles['codex:.codex-private-beta']['reason'], '이 호스트에 계정 디렉터리가 없습니다.')
        self.assertEqual(shown['codex:' + ROUTER][:2], ('Isolated · 0123456789abcdef', 'isolated'))
        self.assertEqual(shown['claude:default'][:2], ('Device Claude', 'device'))
        self.assertEqual(profiles['claude:default']['accountLabel'], 'Claude')

    def test_version_one_files_keep_the_previous_profiles(self):
        self.write_config({'version': 1})
        profiles = {row['id']: row for row in launch.discover_profiles(self.home)}
        self.assertIn('codex:.codex-empty', profiles)
        self.assertNotIn('codex:.codex-private-beta', profiles)
        self.assertEqual(profiles['codex:.codex-work']['label'], '기본 환경 · .codex-work')
        self.assertEqual(profiles['codex:.codex-work']['scope'], 'default')
        self.assertEqual(settings.load(self.home)['boundary'], {'agents': ['codex', 'claude'], 'claude': 'inherit-source'})
        self.assertEqual({k: v for k, v in settings.load(self.home).items()
                          if k not in ('version', 'profiles', 'projects', 'environments')}, settings.defaults())


class SettingsValidationTest(BoundaryFixture):
    def test_version_two_fields_are_validated_without_echoing_values(self):
        bad = [
            {'environments': {'a': {'boundaryGroup': 'missing'}}},
            {'environments': {'a': {'boundaryGroup': 'b'}, 'b': {'boundaryGroup': 'default'}}},
            {'environments': {'a': {'scope': 'SENTINEL value'}}},
            {'profileRules': [{'agent': 'codex', 'homePrefix': '../SENTINEL'}]},
            {'profileRules': [{'agent': 'claude', 'homePrefix': '.codex'}]},
            {'profileRules': [{'agent': 'codex', 'homePrefix': '.codex', 'environment': 'missing'}]},
            {'profiles': [{'agent': 'codex', 'home': '.codex', 'placeholder': 'yes'}]},
            {'profiles': [{'agent': 'codex', 'home': '.codex', 'project': '../SENTINEL'}]},
            {'projects': [{'id': 'p', 'label': 'P', 'root': 'a/b', 'match': 'component'}]},
            {'projects': [{'id': 'p', 'label': 'P', 'root': '~/p', 'match': 'glob'}]},
            {'boundary': {'agents': []}},
            {'boundary': {'agents': ['codex', 'codex']}},
            {'boundary': {'agents': ['codex'], 'claude': 'block'}},
            {'legacy': {'sourcePersonal': {'true': 'missing', 'false': 'default'}}},
            {'legacy': {'sourcePersonal': {'true': 'default'}}},
            {'codexHomes': {'excludeSuspended': True}},
            {'codexHomes': {'allowSymlinks': 1}},
            {'agentRouter': {}},
            {'agentRouter': {'projectsFile': 'relative/SENTINEL'}},
            {'tmux': {'titleOption': 'SENTINEL'}},
            {'transfer': {'agentConfig': 'everything'}},
            {'attachments': {'gitExclude': 'no'}},
            {'usage': {'provider': 'helper', 'resources': '~/r', 'python': '/usr/bin/python3'}},
            {'usage': {'provider': 'other', 'resources': '~/r', 'python': '/p', 'claude': '/c', 'claudeHelper': 'c.py'}},
            {'usage': {'provider': 'helper', 'resources': '~/r', 'python': '/p', 'codex': '/c', 'codexHelper': 'c.py'}},
            {'usage': {'provider': 'helper', 'resources': '~/r', 'python': '/p', 'claude': '/c',
                       'claudeHelper': '../SENTINEL'}},
            {'usage': {'provider': 'helper', 'resources': '~/r', 'python': '/p', 'claude': '/c', 'claudeHelper': 'c.py',
                       'intervalSeconds': 1}},
        ]
        for value in bad:
            self.write_config({'version': 2, **value})
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    settings.load(self.home)
                self.assertNotIn('SENTINEL', str(caught.exception))

    def test_full_version_two_file_and_extra_environment_key(self):
        self.write_config({**CONFIG, 'agentRouter': {'accountPolicy': '~/policy.json'},
                           'codexHomes': {'excludeSuspended': True, 'allowSymlinks': True},
                           'tmux': {'titleOption': '@title', 'taskOption': '@task'},
                           'transfer': {'agentConfig': 'exclude-all'}, 'attachments': {'gitExclude': False},
                           'usage': {'provider': 'helper', 'resources': '~/helpers', 'python': '/usr/bin/python3',
                                     'claude': '/fixture/claude', 'claudeHelper': 'claude-usage.py'}})
        config = settings.load(self.home)
        self.assertEqual(config['agentRouter'], {'accountPolicy': str(self.home / 'policy.json')})
        self.assertEqual(config['codexHomes'], {'allowSymlinks': True, 'excludeSuspended': True, 'requireMarker': False})
        self.assertEqual(config['usage']['resources'], str(self.home / 'helpers'))
        self.assertEqual((config['usage']['intervalSeconds'], config['usage']['manualIntervalSeconds']), (300, 20))
        self.assertEqual(settings.variables('private', self.home)['CMUX_CODEX_HOOKS_DISABLED'], '1')


class ToggleTest(BoundaryFixture):
    def test_agent_config_folders_stay_behind_only_when_configured(self):
        names = ['.claude/settings.json', '.claude/agents/review.md', '.codex/config.toml', 'docs/.codex-notes.md']
        self.assertEqual([transfer_workspace._classify(name) for name in names], [None, None, None, None])
        self.write_config({**CONFIG, 'transfer': {'agentConfig': 'exclude-all'}})
        self.assertEqual([transfer_workspace._classify(name) for name in names], ['credential'] * 4)
        self.assertTrue(transfer_workspace._credential_dir('.claude'))

    @unittest.skipUnless(shutil.which('git'), 'requires local git')
    def test_upload_leaves_the_repository_untouched_when_configured(self):
        repo = self.home / 'Work/repo'
        repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        subprocess.run(['git', '-C', str(repo), 'init', '-q', '--template='], check=True, env=env, timeout=10)
        self.write_config({**CONFIG, 'attachments': {'gitExclude': False}})
        attachments.store(repo, 'screen.png', b'\x89PNG\r\n\x1a\nfixture')
        self.assertFalse((repo / '.git' / 'info' / 'exclude').exists())
        self.write_config(CONFIG)
        attachments.store(repo, 'screen.png', b'\x89PNG\r\n\x1a\nfixture')
        self.assertTrue((repo / '.git' / 'info' / 'exclude').exists())

    def test_tmux_user_options_become_title_and_task(self):
        calls = []
        def run(args, timeout=20):
            calls.append(args[-1])
            fields = args[-1].split('\t')
            row = ['work', '1', '100', 'zsh'] + (['Custom title', 'Task'] if len(fields) == 7 else []) + ['pane']
            return '\t'.join(row) + '\n'
        with patch.object(collector, 'which', return_value='/usr/bin/tmux'), patch.object(collector, 'run', run):
            plain = collector.tmux_sessions()
            self.write_config({**CONFIG, 'tmux': {'titleOption': '@title', 'taskOption': '@task'}})
            custom = collector.tmux_sessions()
        self.assertEqual((plain[0]['title'], plain[0]['task']), ('pane', ''))
        self.assertEqual((custom[0]['title'], custom[0]['task']), ('Custom title', 'Task'))
        self.assertEqual(calls[1].split('\t')[4:6], ['#{@title}', '#{@task}'])


if __name__ == '__main__':
    unittest.main()
