import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import transfer_workspace as workspace


@unittest.skipUnless(shutil.which('git'), 'requires local git')
class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / 'project'
        self.repo.mkdir()
        self.output = self.base / 'exports'
        self.destination = self.base / 'imports'
        self.git('init', '-q', '-b', 'feature/work')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'text.txt').write_text('original\n')
        (self.repo / 'delete-staged.txt').write_text('delete this staged\n')
        (self.repo / 'delete-unstaged.txt').write_text('delete this unstaged\n')
        (self.repo / 'binary.dat').write_bytes(bytes(range(256)))
        (self.repo / '.gitignore').write_text('.env\nnode_modules/\nignored.txt\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.head = self.git('rev-parse', 'HEAD').strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, root=None, allow_failure=False):
        result = subprocess.run(['git', '-C', str(root or self.repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, env=workspace._git_env())
        if not allow_failure:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode('utf-8')

    def transfer(self, root=None, transfer_id='fixture-0001'):
        exported = workspace.export_workspace(root or self.repo, self.output)
        imported = workspace.import_workspace(exported['archivePath'], self.destination,
                                               transfer_id, exported['sha256'])
        return exported, imported

    def stage_changes(self, root=None):
        root = root or self.repo
        (root / 'text.txt').write_text('staged\n')
        (root / 'binary.dat').write_bytes(b'\x00\xffstaged-binary\x80')
        (root / 'delete-staged.txt').unlink()
        (root / 'staged-new.txt').write_text('new staged\n')
        (root / 'run.sh').write_text('#!/bin/sh\nprintf fixture\n')
        (root / 'run.sh').chmod(0o755)
        (root / 'link').symlink_to('text.txt')
        self.git('add', '-A', root=root)
        (root / 'text.txt').write_text('unstaged after staging\n')
        (root / 'binary.dat').write_bytes(b'\x00\xfeunstaged-binary\x81')
        (root / 'delete-unstaged.txt').unlink()
        (root / 'untracked.txt').write_text('untracked\n')
        (root / '.env').write_text('SECRET=never-copy\n')
        (root / 'ignored.txt').write_text('ignored\n')
        (root / 'node_modules').mkdir()
        (root / 'node_modules' / 'package.js').write_text('ignored package')

    def test_real_git_staged_unstaged_binary_delete_modes_links_and_remote(self):
        self.git('remote', 'add', 'origin', 'https://example.invalid/org/project.git')
        self.git('remote', 'add', 'secret', 'https://secret-token@example.invalid/org/repo.git')
        (self.repo / '.git' / 'hooks' / 'dangerous-hook').write_text('do-not-copy')
        self.stage_changes()
        before = self.git('status', '--porcelain=v1', '--untracked-files=all')
        exported, imported = self.transfer()
        target = Path(imported['root'])
        self.assertEqual(self.git('rev-parse', 'HEAD', root=target).strip(), self.head)
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD', root=target).strip(), 'feature/work')
        self.assertEqual(self.git('status', '--porcelain=v1', '--untracked-files=all', root=target), before)
        self.assertEqual(self.git('show', ':text.txt', root=target), 'staged\n')
        self.assertEqual((target / 'text.txt').read_text(), 'unstaged after staging\n')
        self.assertEqual((target / 'binary.dat').read_bytes(), b'\x00\xfeunstaged-binary\x81')
        self.assertFalse((target / 'delete-staged.txt').exists())
        self.assertFalse((target / 'delete-unstaged.txt').exists())
        self.assertTrue((target / 'run.sh').stat().st_mode & 0o111)
        self.assertEqual(os.readlink(target / 'link'), 'text.txt')
        self.assertFalse((target / '.env').exists())
        self.assertFalse((target / 'node_modules').exists())
        self.assertFalse((target / '.git/hooks/dangerous-hook').exists())
        self.assertEqual(self.git('remote', root=target).strip(), 'origin')
        self.assertEqual(self.git('remote', 'get-url', 'origin', root=target).strip(), 'https://example.invalid/org/project.git')
        self.assertTrue(exported['summary']['warnings'])
        self.assertNotIn('secret-token', json.dumps(exported))
        self.assertEqual(self.git('status', '--porcelain=v1', '--untracked-files=all'), before)
        self.assertEqual(imported['summary']['originalRoot'], str(self.repo.resolve()))
        with tarfile.open(exported['archivePath']) as archive:
            self.assertTrue(all(member.isfile() for member in archive))
            self.assertNotIn(b'SECRET=never-copy', Path(exported['archivePath']).read_bytes())
        with self.assertRaises(workspace.WorkspaceTransferError):
            workspace.import_workspace(exported['archivePath'], self.destination, 'fixture-0001', exported['sha256'])

    def test_worktree_nested_cwd_and_roundtrip(self):
        worktree = self.base / 'linked-worktree'
        self.git('worktree', 'add', '-qb', 'linked-branch', str(worktree))
        self.assertTrue((worktree / '.git').is_file())
        self.stage_changes(worktree)
        (worktree / 'nested').mkdir()
        (worktree / 'nested' / 'note.txt').write_text('nested source\n')
        exported, first = self.transfer(worktree / 'nested')
        root = Path(first['root'])
        self.assertEqual(Path(first['cwd']), root / 'nested')
        self.assertTrue((root / '.git').is_dir())
        self.assertEqual(self.git('symbolic-ref', '--short', 'HEAD', root=root).strip(), 'linked-branch')
        self.assertEqual(self.git('show', ':text.txt', root=root), 'staged\n')
        (root / 'untracked.txt').write_text('edited on receiver\n')
        returned_export = workspace.export_workspace(first['cwd'], self.base / 'returned-export')
        returned = workspace.import_workspace(returned_export['archivePath'], self.base / 'returned',
                                               'return-0001', returned_export['sha256'])
        self.assertEqual((Path(returned['root']) / 'untracked.txt').read_text(), 'edited on receiver\n')
        self.assertEqual(self.git('status', '--porcelain=v1', '--untracked-files=all', root=root),
                         self.git('status', '--porcelain=v1', '--untracked-files=all', root=Path(returned['root'])))
        self.assertEqual(self.git('show', ':text.txt', root=Path(returned['root'])), 'staged\n')
        self.assertEqual((worktree / 'untracked.txt').read_text(), 'untracked\n')

    def test_sensitive_tracked_and_deleted_history_are_rejected(self):
        (self.repo / 'auth.json').write_text('fixture credential never transport')
        self.git('add', 'auth.json')
        for method in (workspace.inspect_workspace, lambda p: workspace.export_workspace(p, self.output)):
            with self.assertRaisesRegex(workspace.WorkspaceTransferError, '추적'):
                method(self.repo)
        self.git('commit', '-qm', 'credential fixture')
        self.git('rm', '-q', 'auth.json')
        self.git('commit', '-qm', 'remove credential fixture')
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, '이력'):
            workspace.export_workspace(self.repo, self.output)

    def test_non_git_folder_and_default_exclusions(self):
        folder = self.base / 'plain'
        folder.mkdir()
        (folder / 'app.py').write_text('print("fixture")\n')
        (folder / '.env.example').write_text('EXAMPLE=\n')
        (folder / '.env').write_text('SECRET=fixture')
        (folder / 'auth.json').write_text('credential')
        (folder / '.venv').mkdir()
        (folder / '.venv' / 'binary').write_text('ignored')
        (folder / 'link').symlink_to('app.py')
        exported, imported = self.transfer(folder)
        target = Path(imported['root'])
        self.assertFalse(exported['summary']['git'])
        self.assertTrue((target / '.env.example').exists())
        self.assertFalse((target / '.env').exists())
        self.assertFalse((target / 'auth.json').exists())
        self.assertFalse((target / '.venv').exists())
        self.assertEqual(os.readlink(target / 'link'), 'app.py')
        self.assertFalse((target / '.git').exists())

    def test_empty_git_index_and_detached_head(self):
        empty = self.base / 'empty'
        empty.mkdir()
        self.git('init', '-qb', 'new-project', root=empty)
        (empty / 'first.txt').write_text('staged before first commit\n')
        self.git('add', 'first.txt', root=empty)
        exported, imported = self.transfer(empty)
        self.assertIsNone(exported['summary']['head'])
        self.assertEqual(self.git('show', ':first.txt', root=Path(imported['root'])), 'staged before first commit\n')
        self.git('checkout', '-q', '--detach')
        exported, imported = self.transfer(transfer_id='detached-0001')
        self.assertIsNone(exported['summary']['branch'])
        self.assertEqual(self.git('rev-parse', 'HEAD', root=Path(imported['root'])).strip(), self.head)

    def test_symlink_escape_fifo_and_system_root_rejected(self):
        (self.repo / 'escape').symlink_to('../outside')
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, '심볼릭'):
            workspace.export_workspace(self.repo, self.output)
        (self.repo / 'escape').unlink()
        os.mkfifo(self.repo / 'pipe')
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, 'FIFO'):
            workspace.export_workspace(self.repo, self.output)
        with self.assertRaises(workspace.WorkspaceTransferError):
            workspace.inspect_workspace(Path.home())
        with self.assertRaises(workspace.WorkspaceTransferError):
            workspace.inspect_workspace('/')

    def test_source_change_during_export_fails_and_publishes_no_archive(self):
        original_git = workspace._git
        def mutate(root, *args, **kwargs):
            result = original_git(root, *args, **kwargs)
            if args[:2] == ('bundle', 'create'):
                (self.repo / 'text.txt').write_text('changed while bundling\n')
            return result
        with patch('transfer_workspace._git', side_effect=mutate):
            with self.assertRaisesRegex(workspace.WorkspaceTransferError, '변경'):
                workspace.export_workspace(self.repo, self.output)
        self.assertEqual(list(self.output.glob('*.tar.gz')), [])

    def test_inspect_fingerprint_matches_export_and_detects_post_export_changes(self):
        inspect = workspace.inspect_workspace(self.repo)
        exported = workspace.export_workspace(self.repo, self.output)
        self.assertEqual(inspect['fingerprint'], exported['summary']['fingerprint'])
        self.assertEqual(inspect['fingerprint'], workspace.inspect_workspace(self.repo)['fingerprint'])
        original_stat = (self.repo / 'text.txt').stat()
        (self.repo / 'text.txt').write_text('modified\n')
        os.utime(self.repo / 'text.txt', ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        edited = workspace.inspect_workspace(self.repo)
        self.assertNotEqual(edited['fingerprint'], inspect['fingerprint'])
        self.git('add', 'text.txt')
        staged = workspace.inspect_workspace(self.repo)
        self.assertNotEqual(staged['fingerprint'], edited['fingerprint'])
        self.git('reset', '-q', 'HEAD', '--', 'text.txt')
        self.assertNotEqual(workspace.inspect_workspace(self.repo)['fingerprint'], staged['fingerprint'])

    def test_empty_nested_working_directory_is_preserved(self):
        (self.repo / 'empty' / 'nested').mkdir(parents=True)
        exported, imported = self.transfer(self.repo / 'empty' / 'nested')
        self.assertEqual(exported['summary']['cwdRelative'], 'empty/nested')
        self.assertTrue(Path(imported['cwd']).is_dir())

    def test_managed_local_workspace_roundtrip_and_credential_boundary(self):
        fake_home = self.base / 'managed-home'
        fake_home.mkdir()
        destination = fake_home / '.local/share/sessionholic/workspaces'
        self.stage_changes()
        exported = workspace.export_workspace(self.repo, self.output)
        with patch('transfer_workspace.Path.home', return_value=fake_home):
            received = workspace.import_workspace(exported['archivePath'], destination,
                                                  'managed-0001', exported['sha256'])
            inspected = workspace.inspect_workspace(received['cwd'])
            self.assertEqual(inspected['branch'], 'feature/work')
            (Path(received['root']) / 'untracked.txt').write_text('receiver managed edit\n')
            returned_export = workspace.export_workspace(received['cwd'], self.base / 'managed-export')
            returned = workspace.import_workspace(returned_export['archivePath'], self.base / 'returned-managed',
                                                   'managed-return-0001', returned_export['sha256'])
            self.assertEqual(self.git('show', ':text.txt', root=Path(returned['root'])), 'staged\n')
            self.assertEqual((Path(returned['root']) / 'untracked.txt').read_text(), 'receiver managed edit\n')
            credential = fake_home / '.local/share/other-credentials'
            credential.mkdir()
            (credential / 'private.txt').write_text('do not inspect or transport')
            with self.assertRaises(workspace.WorkspaceTransferError):
                workspace.inspect_workspace(credential)
            linked = destination / 'linked-0001'
            linked.symlink_to(self.repo, target_is_directory=True)
            with self.assertRaisesRegex(workspace.WorkspaceTransferError, '상위 경로'):
                workspace.inspect_workspace(linked)
            with self.assertRaisesRegex(workspace.WorkspaceTransferError, '상위 경로'):
                workspace.import_workspace(exported['archivePath'], linked / 'nested',
                                           'bad-parent-0001', exported['sha256'])
            wrong_owner = os.getuid() + 1
            with patch('transfer_workspace.os.getuid', return_value=wrong_owner):
                with self.assertRaisesRegex(workspace.WorkspaceTransferError, '사용자 소유'):
                    workspace.inspect_workspace(received['cwd'])

    def test_size_file_limits_and_wrong_archive_sha(self):
        with patch('transfer_workspace.MAX_FILES', 2):
            with self.assertRaises(workspace.WorkspaceTransferError):
                workspace.export_workspace(self.repo, self.output)
        with patch('transfer_workspace.MAX_BYTES', 100):
            with self.assertRaises(workspace.WorkspaceTransferError):
                workspace.export_workspace(self.repo, self.output)
        exported = workspace.export_workspace(self.repo, self.output)
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, 'SHA-256'):
            workspace.import_workspace(exported['archivePath'], self.destination, 'fixture-0001', '0'*64)
        self.assertFalse(self.destination.exists())

    def test_gitignored_attachments_follow_nested_cwd_and_roundtrip(self):
        import attachments
        (self.repo / 'nested').mkdir()
        (self.repo / '.gitignore').write_text((self.repo / '.gitignore').read_text() + '.sessionholic-attachments/\n')
        source = attachments.store(self.repo / 'nested', 'phone.png', b'\x89PNG\r\n\x1a\nfixture-image')
        self.assertIn('.sessionholic-attachments', self.git('ls-files', '--others', '--ignored', '--exclude-standard', '--directory'))
        exported, imported = self.transfer(self.repo / 'nested')
        received = attachments.resolve(imported['cwd'], source['id'])
        self.assertEqual(Path(received['path']).read_bytes(), Path(source['path']).read_bytes())
        self.assertNotEqual(received['path'], source['path'])
        self.assertEqual(received['sha256'], source['sha256'])
        self.assertEqual(imported['summary']['attachmentCount'], 1)
        self.assertEqual(imported['attachmentsPathMap'][0]['from'], source['path'])
        self.assertEqual(imported['attachmentsPathMap'][0]['to'], received['path'])
        self.assertEqual(Path(received['path']).stat().st_mode & 0o777, 0o400)
        returned_export = workspace.export_workspace(imported['cwd'], self.base / 'attachments-returned-export')
        returned = workspace.import_workspace(returned_export['archivePath'], self.base / 'attachments-returned',
                                               'attachment-return-0001', returned_export['sha256'])
        self.assertEqual(attachments.resolve(returned['cwd'], source['id'])['sha256'], source['sha256'])
        self.assertEqual(attachments.resolve(self.repo / 'nested', source['id']), source)

    def test_corrupt_managed_attachment_and_symlink_are_never_exported(self):
        import attachments
        record = attachments.store(self.repo, 'note.txt', b'original')
        (self.repo / '.gitignore').write_text('.sessionholic-attachments/\n')
        path = Path(record['path'])
        path.chmod(0o600)
        path.write_bytes(b'changed')
        path.chmod(0o400)
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, '첨부'):
            workspace.export_workspace(self.repo, self.output)
        path.unlink()
        path.symlink_to(self.repo / 'text.txt')
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, '첨부'):
            workspace.export_workspace(self.repo, self.output)

    def test_attachment_metadata_tamper_rejected_with_updated_tar_hashes(self):
        import attachments
        record = attachments.store(self.repo, 'note.txt', b'original')
        exported = workspace.export_workspace(self.repo, self.output)
        altered = self.base / 'attachment-tampered.tar.gz'
        metadata_path = Path(record['relativePath']).with_name('.metadata.json').as_posix()
        with tarfile.open(exported['archivePath']) as source:
            members = [(member, source.extractfile(member).read()) for member in source]
        replacement = b'{"id":"wrong","name":"note.txt"}'
        for member, data in members:
            if member.name == 'manifest.json':
                manifest = json.loads(data)
                for entry in manifest['entries']:
                    if entry['path'] == metadata_path:
                        entry.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
                break
        with tarfile.open(altered, 'w:gz') as target:
            for member, data in members:
                if member.name == 'manifest.json':
                    data = json.dumps(manifest).encode()
                elif member.name == 'payload/' + metadata_path:
                    data = replacement
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, '첨부'):
            workspace.import_workspace(altered, self.destination, 'bad-attachment-0001',
                                       hashlib.sha256(altered.read_bytes()).hexdigest())
        self.assertFalse((self.destination / 'bad-attachment-0001').exists())

    def make_tar(self, members):
        archive = self.base / 'unsafe.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            for name, kind, data in members:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = '../../escaped'
                stream.addfile(member, io.BytesIO(data) if member.size else None)
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_tar_traversal_links_special_and_duplicate_paths_rejected(self):
        for members in ([('../outside', tarfile.REGTYPE, b'evil')],
                        [('/absolute', tarfile.REGTYPE, b'evil')],
                        [('payload/link', tarfile.SYMTYPE, b'')],
                        [('payload/link', tarfile.LNKTYPE, b'')],
                        [('payload/fifo', tarfile.FIFOTYPE, b'')],
                        [('manifest.json', tarfile.REGTYPE, b'{}'), ('manifest.json', tarfile.REGTYPE, b'{}')]):
            archive, digest = self.make_tar(members)
            with self.subTest(members=members), self.assertRaises(workspace.WorkspaceTransferError):
                workspace.import_workspace(archive, self.destination, 'unsafe-0001', digest)
            self.assertFalse((self.destination / 'unsafe-0001').exists())
        self.assertFalse((self.base / 'outside').exists())

    def test_payload_hash_tamper_rejected_even_with_valid_archive_sha(self):
        exported = workspace.export_workspace(self.repo, self.output)
        original = Path(exported['archivePath'])
        altered = self.base / 'tampered.tar.gz'
        with tarfile.open(original) as source, tarfile.open(altered, 'w:gz') as target:
            for member in source:
                data = source.extractfile(member).read()
                if member.name == 'payload/text.txt':
                    data = b'tampered\n'
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
        digest = hashlib.sha256(altered.read_bytes()).hexdigest()
        with self.assertRaisesRegex(workspace.WorkspaceTransferError, 'SHA-256'):
            workspace.import_workspace(altered, self.destination, 'tamper-0001', digest)
        self.assertFalse((self.destination / 'tamper-0001').exists())


if __name__ == '__main__':
    unittest.main()
