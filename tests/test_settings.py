import base64
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import collector
import launch
import server
import settings
from transfer import Transfers
import transfer_worker


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.path = self.home / '.config/sessionholic/config.json'
        self.path.parent.mkdir(parents=True, mode=0o700)

    def config(self, **fields):
        self.path.write_text(json.dumps({'version': 1, **fields}))
        return settings.load(self.home)

    def test_empty_home_has_no_phantom_profiles_and_ignores_legacy_files(self):
        legacy = self.home / '.config/agent-router'
        legacy.mkdir()
        (legacy / 'account-policy.json').write_text('{"accounts":{"DO-NOT-IMPORT":"private"}}')
        (legacy / 'projects.tsv').write_text('old\tDO-NOT-IMPORT\t/private/path')
        with patch.object(launch, '_binary', return_value='/fixture/codex'), patch.object(collector, 'HOME', self.home):
            self.assertEqual(launch.discover_profiles(self.home), [])
            self.assertEqual(collector.load_projects(), [])
        self.assertEqual(settings.load(self.home)['projects'], [])

    def test_longest_explicit_boundary_wins_and_label_only_project_keeps_profile_group(self):
        self.config(environments={'team': {}, 'sandbox': {}}, profiles=[
            {'agent': 'codex', 'home': '.codex-beta', 'environment': 'team'}], projects=[
            {'id': 'parent', 'label': 'Parent', 'root': '~/projects', 'environment': 'sandbox'},
            {'id': 'child', 'label': 'Child', 'root': '~/projects/child', 'environment': 'team'},
            {'id': 'label', 'label': 'Label only', 'root': '~/other'}])
        def source(path): return {'agent': 'codex', 'home': '.codex-beta', 'cwd': str(self.home / path)}
        self.assertEqual(settings.source_environment(source('projects'), self.home), 'sandbox')
        self.assertEqual(settings.source_environment(source('projects/child/src'), self.home), 'team')
        self.assertEqual(settings.source_environment(source('other/subdir'), self.home), 'team')

    def test_folder_or_home_name_does_not_inject_git_ssh_environment(self):
        project = self.home / 'Personal/project'
        project.mkdir(parents=True)
        source = {'agent': 'codex', 'home': '.codex-personal', 'cwd': str(project)}
        self.assertEqual(launch.source_environment(source, self.home), 'default')
        env = launch._environment(self.home, 'codex', '.codex-personal', 'default')
        self.assertTrue(settings.ENV_KEYS.isdisjoint(env))

    def test_home_os_alias_preserves_project_and_imported_environment_boundaries(self):
        alias_home = Path('/tmp') / self.home.name
        self.config(environments={'team': {}}, projects=[
            {'id': 'project', 'label': 'Project', 'root': '~/project', 'environment': 'team'}])
        project = self.home / 'project'; project.mkdir()
        source = {'agent': 'codex', 'home': '.codex', 'cwd': str(project)}
        self.assertEqual(launch.source_environment(source, alias_home), 'team')
        identifier = 'a' * 32
        imported = self.home / '.local/share/sessionholic/workspaces' / identifier / 'project'
        imported.mkdir(parents=True)
        record = self.home / '.local/state/sessionholic-transfers' / identifier / 'transfer.json'
        record.parent.mkdir(parents=True, mode=0o700)
        record.write_text('{"sourceEnvironment":"team"}'); record.chmod(0o600)
        self.assertEqual(launch.source_environment({**source, 'cwd': str(imported)}, alias_home), 'team')

    def test_configured_environment_values_are_not_in_profile_response(self):
        self.config(environments={'team': {'label': 'Team', 'variables': {'GH_CONFIG_DIR': '~/fixture-private-gh'}}},
                    profiles=[{'agent': 'codex', 'home': '.codex-beta', 'environment': 'team', 'label': 'Example'}])
        home = self.home / '.codex-beta'
        home.mkdir()
        (home / 'auth.json').write_text('credential-sentinel-never-read')
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            if path.name == 'auth.json': raise AssertionError('credential content was read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', guarded), patch.object(launch, '_binary', return_value='/fixture/codex'):
            profiles = launch.discover_profiles(self.home)
        self.assertEqual(profiles[0]['environment'], 'team')
        self.assertEqual(profiles[0]['accountLabel'], 'Example')
        self.assertNotIn('fixture-private-gh', json.dumps(profiles))
        self.assertNotIn('credential-sentinel', json.dumps(profiles))
        self.assertEqual(settings.variables('team', self.home)['GH_CONFIG_DIR'], str(self.home / 'fixture-private-gh'))

    def test_malformed_settings_fail_closed_without_echoing_values(self):
        bad = [{'version': True}, {'version': 2}, {'version': 1, 'unexpected': 'SENTINEL'},
               {'version': 1, 'profiles': [{'agent': 'codex', 'home': '../other'}]},
               {'version': 1, 'profiles': [{'agent': 'claude', 'home': '.claude-other'}]},
               {'version': 1, 'profiles': [{'agent': 'codex', 'home': '.codex', 'environment': 'missing'}]},
               {'version': 1, 'environments': {'team': {'variables': {'OPENAI_API_KEY': 'SENTINEL'}}}},
               {'version': 1, 'environments': {'team': {'variables': {'PATH': '/malicious'}}}},
               {'version': 1, 'projects': [{'id': 'demo', 'label': 'Demo', 'root': '../project'}]},
               {'version': 1, 'profiles': [{'agent': 'codex', 'home': '.codex'}] * 2}]
        for value in bad:
            self.path.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                settings.load(self.home)
            self.assertNotIn('SENTINEL', str(error.exception))

    def test_config_ancestor_link_and_oversized_file_are_rejected(self):
        self.path.write_text(' ' * 65537)
        with self.assertRaises(ValueError): settings.load(self.home)
        self.path.unlink()
        self.path.parent.rmdir()
        destination = self.home / 'other-config'; destination.mkdir()
        self.path.parent.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError): settings.load(self.home)

    def test_token_links_ancestor_links_public_files_and_nonregular_files_rejected(self):
        token = self.home / '.config/sessionholic/token'
        token.write_text('fixture-only-token'); token.chmod(0o644)
        with self.assertRaises(ValueError): server.load_token(token)
        token.chmod(0o600)
        self.assertEqual(server.load_token(token), 'fixture-only-token')
        token.unlink(); token.symlink_to(self.home / 'missing')
        with self.assertRaises(ValueError): server.load_token(token)
        token.unlink(); os.mkfifo(str(token), 0o600)
        with self.assertRaises(ValueError): server.load_token(token)
        token.unlink(); self.path.parent.rmdir()
        alternate = self.home / 'alternate'; alternate.mkdir(mode=0o700)
        self.path.parent.symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(ValueError): server.load_token(token)

    def test_direct_server_rejects_state_ancestor_link_before_any_collection(self):
        alternate = self.home / 'other-state'; alternate.mkdir(mode=0o700)
        state = self.home / 'linked-state'; state.symlink_to(alternate, target_is_directory=True)
        with patch.object(Path, 'home', return_value=self.home), patch('server.run_collector') as collect, \
             patch('server.load_token') as token, patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit): server.main(['--state-dir', str(state)])
        collect.assert_not_called(); token.assert_not_called()

    def test_owning_worker_attach_uses_local_environment_and_never_creates_or_inputs_thread(self):
        identifier = '019aaaaa-0000-7000-8000-000000000001'
        self.config(environments={'team': {'variables': {'GH_CONFIG_DIR': '~/fixture-gh'}}}, profiles=[
            {'agent': 'codex', 'home': '.codex-beta', 'environment': 'team'},
            {'agent': 'claude', 'home': '.claude', 'environment': 'team'}])
        project = self.home / 'project'; project.mkdir()
        codex = self.home / '.codex-beta'; codex.mkdir()
        (codex / 'auth.json').write_text('fixture-only-never-read')
        (self.home / '.claude').mkdir()
        daemon = codex / launch.SOCKET_REL
        daemon.parent.mkdir()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(str(daemon))
        for agent, key, sid, kind in [('codex', '.codex-beta', identifier, None),
                                     ('claude', '.claude', 'fixture-background-id', 'background')]:
            source = {'agent': agent, 'home': key, 'id': sid, 'host': 'remote',
                      'cwd': str(project), 'phase': 'idle', 'kind': kind}
            with self.subTest(agent=agent), patch.object(Path, 'home', return_value=self.home), \
                 patch.object(launch, '_binary', return_value='/fixture/native'), \
                 patch('transfer_worker.os.execve') as execute, patch('transfer_worker.os.chdir'), \
                 patch('native_chat._client') as native_client, patch('managed_launch.bind') as new_thread:
                transfer_worker.attach(source)
            native_client.assert_not_called(); new_thread.assert_not_called()
            argv, env = execute.call_args.args[1:]
            self.assertEqual(argv[1:3], ['resume' if agent == 'codex' else 'attach', sid])
            self.assertEqual(env['GH_CONFIG_DIR'], str(self.home / 'fixture-gh'))
            self.assertNotIn('fixture-only-never-read', json.dumps(env))

    def test_remote_attach_command_carries_identity_without_environment_values(self):
        host = {'name': 'remote', 'label': 'Remote', 'local': False, 'ssh': 'fixture-no-network', 'python': '/usr/bin/python3'}
        board = server.Board([host], 60, runner=lambda *a: {})
        flow = server.Workflow(board, None, self.home)
        flow.transfers.runtime_paths['remote'] = '/fixture/worker.pyz'
        source = {'agent': 'codex', 'home': '.codex-beta', 'id': '019aaaaa-0000-7000-8000-000000000001',
                  'host': 'remote', 'cwd': '/fixture/project', 'phase': 'idle', 'privateExtra': 'DO-NOT-COPY'}
        with patch.object(flow.transfers, 'profiles', return_value=[{'id': 'codex:.codex-beta', 'available': True,
                          'environment': 'team'}]), patch('transfer.shutil.which', return_value='/fixture/ssh'):
            spec = flow.remote_attach(host, source)
        self.assertIn('attach', spec['argv'][-1])
        self.assertIn(source['id'], spec['argv'][-1])
        self.assertNotIn('DO-NOT-COPY', spec['argv'][-1])
        self.assertNotIn('GH_CONFIG_DIR', spec['argv'][-1])


class RemoteBundleTest(unittest.TestCase):
    """A fake SSH executable execs a local interpreter in a private synthetic HOME."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.ssh = self.home / 'fake-ssh'
        self.ssh.write_text('#!/usr/bin/python3\nimport os,shlex,sys\nargs=shlex.split(sys.argv[-1])\n'
                            'if args[0]=="exec":args=args[1:]\n'
                            'if args[0].startswith("PATH="):args=args[1:]\n'
                            'os.execv(args[0],args)\n')
        self.ssh.chmod(0o700)
        self.host = {'name': 'remote', 'label': 'Synthetic remote', 'local': False,
                     'ssh': 'fixture-no-network', 'python': '/usr/bin/python3'}

    def test_real_worker_zip_executes_settings_on_fake_ssh(self):
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'version': 1, 'environments': {'team': {}}, 'profiles': [
            {'agent': 'codex', 'home': '.codex-sample', 'label': 'Synthetic', 'environment': 'team'}]}))
        profile = self.home / '.codex-sample'; profile.mkdir()
        (profile / 'auth.json').write_text('fixture-only-do-not-read')
        transfer = Transfers([self.host], self.home / 'board-state')
        zipped = zipfile.ZipFile(io.BytesIO(base64.b64decode(transfer.runtime()['runtime'])))
        self.assertIn('settings.py', zipped.namelist())
        with patch.dict(os.environ, {'HOME': str(self.home)}), patch('transfer.shutil.which', return_value=str(self.ssh)):
            result = transfer.rpc(self.host, 'profiles', timeout=10)
        self.assertEqual(result['profiles'][0]['environment'], 'team')
        self.assertEqual(result['profiles'][0]['accountLabel'], 'Synthetic')
        self.assertNotIn('fixture-only-do-not-read', json.dumps(result))
        self.assertIn('/sessionholic-transfer-runtime/', transfer.runtime_paths['remote'])

    def test_actual_remote_collector_stdin_imports_settings_in_isolated_mode(self):
        with patch.dict(os.environ, {'HOME': str(self.home)}), patch('server.collector_command', return_value=[
                str(self.ssh), 'fixture-no-network', '/usr/bin/python3 -I - read claude fixture-id']):
            result = server.run_collector(self.host, ['read', 'claude', 'fixture-id'], 10)
        self.assertEqual(result['messages'], [])
        self.assertIn('기록', result['error'])


if __name__ == '__main__':
    unittest.main()
