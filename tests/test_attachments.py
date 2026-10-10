import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import attachments


class AttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name) / 'project'
        self.cwd.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_binary_unicode_readback_and_private_immutable_files(self):
        data = b'\x00\xfffixture\x80'
        result = attachments.store(self.cwd, '자료.bin', data)
        self.assertEqual(Path(result['path']).read_bytes(), data)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result, attachments.resolve(self.cwd, result['id']))
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777, 0o400)
        self.assertEqual(Path(result['path']).parent.stat().st_mode & 0o777, 0o700)
        metadata = json.loads(Path(result['path']).with_name('.metadata.json').read_text())
        self.assertNotIn('path', metadata)
        self.assertEqual(len(attachments.workspace_files(self.cwd)), 2)

    def test_image_extension_and_magic_must_agree(self):
        png = attachments.store(self.cwd, 'mobile.png', b'\x89PNG\r\n\x1a\nfixture')
        self.assertEqual(png['mime'], 'image/png')
        misleading = attachments.store(self.cwd, 'mobile.png', b'not an image')
        self.assertEqual(misleading['mime'], 'application/octet-stream')
        svg = attachments.store(self.cwd, 'image.svg', b'<svg/>')
        self.assertEqual(svg['mime'], 'application/octet-stream')
        no_extension = attachments.store(self.cwd, 'file', b'\x89PNG\r\n\x1a\nfixture')
        self.assertEqual(no_extension['mime'], 'application/octet-stream')

    def test_names_and_browser_paths_cannot_escape(self):
        for name in ('../outside', '/absolute', 'a/b', 'a\\b', '.', '..', '.metadata.json', 'x\x00y', '', 'a'*241):
            with self.subTest(name=name), self.assertRaises(attachments.AttachmentError):
                attachments.store(self.cwd, name, b'x')
        for identifier in ('../outside', '/absolute', 'x'*32, '0'*31):
            with self.subTest(identifier=identifier), self.assertRaises(attachments.AttachmentError):
                attachments.resolve(self.cwd, identifier)
        self.assertFalse((self.cwd.parent / 'outside').exists())

    def test_untrusted_symlink_and_public_directory_rejected(self):
        outside = self.cwd.parent / 'outside'
        outside.mkdir(mode=0o700)
        managed = self.cwd / attachments.DIRECTORY
        managed.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(attachments.AttachmentError):
            attachments.store(self.cwd, 'note.txt', b'x')
        self.assertEqual(list(outside.iterdir()), [])
        managed.unlink()
        managed.mkdir(mode=0o755)
        with self.assertRaises(attachments.AttachmentError):
            attachments.store(self.cwd, 'note.txt', b'x')

    def test_symlink_file_and_metadata_tamper_are_rejected(self):
        record = attachments.store(self.cwd, 'note.txt', b'original')
        path = Path(record['path'])
        outside = self.cwd.parent / 'private.txt'
        outside.write_bytes(b'original')
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(attachments.AttachmentError):
            attachments.resolve(self.cwd, record['id'])
        self.assertEqual(outside.read_bytes(), b'original')
        path.unlink()
        path.write_bytes(b'mutated')
        path.chmod(0o400)
        with self.assertRaises(attachments.AttachmentError):
            attachments.resolve(self.cwd, record['id'])

    def test_fifo_is_not_read_and_extra_files_are_rejected(self):
        record = attachments.store(self.cwd, 'note.txt', b'original')
        path = Path(record['path'])
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(attachments.AttachmentError):
            attachments.resolve(self.cwd, record['id'])
        path.unlink()
        path.write_bytes(b'original')
        path.chmod(0o400)
        path.with_name('unexpected').write_text('extra')
        with self.assertRaises(attachments.AttachmentError):
            attachments.workspace_files(self.cwd)

    def test_hash_base64_and_bounded_binary_transport(self):
        data = bytes(range(256))
        args = {'cwd': str(self.cwd), 'name': 'native.bin', 'data': base64.b64encode(data).decode(),
                'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        result = attachments.receive(args)
        self.assertEqual(Path(result['path']).read_bytes(), data)
        binary = attachments.receive_binary(args, io.BytesIO(data))
        self.assertEqual(Path(binary['path']).read_bytes(), data)
        with self.assertRaises(attachments.AttachmentError):
            attachments.receive({**args, 'data': '??=='})
        with self.assertRaises(attachments.AttachmentError):
            attachments.receive({**args, 'sha256': '0'*64})
        with self.assertRaises(attachments.AttachmentError):
            attachments.receive_binary(args, io.BytesIO(data[:-1]))
        with patch('attachments.MAX_FILE_BYTES', 3):
            with self.assertRaises(attachments.AttachmentError):
                attachments.store(self.cwd, 'large', b'1234')
            with self.assertRaises(attachments.AttachmentError):
                attachments.receive(args)

    def test_existing_attachment_is_never_overwritten_on_uuid_collision(self):
        record = attachments.store(self.cwd, 'note.txt', b'original')
        with patch('attachments.uuid.uuid4') as mocked:
            mocked.return_value.hex = record['id']
            with self.assertRaises(FileExistsError):
                attachments.store(self.cwd, 'note.txt', b'replacement')
        self.assertEqual(attachments.resolve(self.cwd, record['id']), record)

    def git(self, root, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                               *args], check=True, capture_output=True, env=env, timeout=10).stdout.decode()

    def excluded_lines(self, git_dir):
        return (git_dir / 'info' / 'exclude').read_text().splitlines().count(attachments.DIRECTORY + '/')

    @unittest.skipUnless(shutil.which('git'), 'requires local git')
    def test_git_repository_excludes_uploads_once_from_root_and_nested_cwd(self):
        self.git(self.cwd, 'init', '-q', '--template=')
        self.assertFalse((self.cwd / '.git' / 'info').exists())
        nested = self.cwd / 'packages' / 'app'
        nested.mkdir(parents=True)
        for cwd in (self.cwd, self.cwd, nested):
            record = attachments.store(cwd, 'screen.png', b'\x89PNG\r\n\x1a\nfixture')
        self.assertEqual(self.excluded_lines(self.cwd / '.git'), 1)
        self.assertEqual(self.git(self.cwd, 'status', '--porcelain', '--untracked-files=all'), '')
        self.git(self.cwd, 'check-ignore', '-q', 'packages/app/' + record['relativePath'])
        self.git(self.cwd, 'add', '-A')
        self.assertEqual(self.git(self.cwd, 'diff', '--cached', '--name-only'), '')
        self.assertEqual(attachments.resolve(nested, record['id']), record)

    @unittest.skipUnless(shutil.which('git'), 'requires local git')
    def test_existing_exclude_is_appended_on_a_new_line_and_not_duplicated(self):
        self.git(self.cwd, 'init', '-q')
        exclude = self.cwd / '.git' / 'info' / 'exclude'
        exclude.write_text('# local rules\n*.log')
        attachments.store(self.cwd, 'a.txt', b'a')
        self.assertEqual(exclude.read_text(), '# local rules\n*.log\n' + attachments.DIRECTORY + '/\n')
        exclude.write_text(attachments.DIRECTORY + '\n')
        attachments.store(self.cwd, 'b.txt', b'b')
        self.assertEqual(exclude.read_text(), attachments.DIRECTORY + '\n')

    @unittest.skipUnless(shutil.which('git'), 'requires local git')
    def test_linked_worktree_uses_the_common_git_directory(self):
        repo = self.cwd / 'repo'
        repo.mkdir()
        self.git(repo, 'init', '-q')
        (repo / 'file.txt').write_text('x')
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'initial')
        worktree = self.cwd / 'linked'
        self.git(repo, 'worktree', 'add', '-q', str(worktree))
        self.assertTrue((worktree / '.git').is_file())
        (worktree / 'sub').mkdir()
        attachments.store(worktree / 'sub', 'note.txt', b'note')
        self.assertEqual(self.excluded_lines(repo / '.git'), 1)
        self.assertEqual(self.git(worktree, 'status', '--porcelain', '--untracked-files=all'), '')

    def test_outside_git_or_unsafe_exclude_never_blocks_the_upload(self):
        attachments.store(self.cwd, 'plain.txt', b'plain')
        self.assertFalse(os.path.lexists(self.cwd / '.git'))
        git_dir = self.cwd / '.git' / 'info'
        git_dir.mkdir(parents=True)
        outside = self.cwd.parent / 'outside-exclude'
        outside.write_text('preserve\n')
        (git_dir / 'exclude').symlink_to(outside)
        record = attachments.store(self.cwd, 'linked.txt', b'linked')
        self.assertEqual(attachments.resolve(self.cwd, record['id'])['name'], 'linked.txt')
        self.assertEqual(outside.read_text(), 'preserve\n')
        (self.cwd / '.git' / 'info' / 'exclude').unlink()
        (self.cwd / '.git' / 'info').rmdir()
        with patch('attachments.os.getuid', return_value=os.getuid() + 1):
            self.assertFalse(attachments.exclude_from_git(self.cwd))
        self.assertFalse((self.cwd / '.git' / 'info').exists())
        (self.cwd / '.git').rmdir()
        (self.cwd / '.git').write_text('gitdir: ../missing-git-dir\n')
        self.assertEqual(attachments.store(self.cwd, 'dangling.txt', b'x')['name'], 'dangling.txt')
        self.assertFalse((self.cwd.parent / 'missing-git-dir').exists())

    def test_other_session_and_wrong_owner_cannot_resolve(self):
        record = attachments.store(self.cwd, 'note.txt', b'original')
        other = self.cwd.parent / 'other'
        other.mkdir()
        with self.assertRaises(attachments.AttachmentError):
            attachments.resolve(other, record['id'])
        with patch('attachments.os.getuid', return_value=os.getuid()+1):
            with self.assertRaises(attachments.AttachmentError):
                attachments.resolve(self.cwd, record['id'])


if __name__ == '__main__':
    unittest.main()
