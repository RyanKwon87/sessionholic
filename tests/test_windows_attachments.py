"""PC attachment storage (windows_attachments), run on any OS: a Windows reparse point is an
lstat result carrying FILE_ATTRIBUTE_REPARSE_POINT, a link is a symlink."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attachments
import windows_attachments as wa

PNG = b'\x89PNG\r\n\x1a\nfixture'


def as_reparse_point(info):
    """`info` as Windows reports a junction, a mount point or another reparse point."""
    fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
    return SimpleNamespace(**{**fields, 'st_file_attributes': stat.FILE_ATTRIBUTE_REPARSE_POINT})


class WindowsAttachmentsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        self.managed = self.cwd / wa.DIRECTORY

    def reparse_at(self, *paths):
        """os.lstat reporting each of `paths` as a reparse point."""
        real, marked = os.lstat, {os.path.normcase(str(path)) for path in paths}

        def lstat(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            return as_reparse_point(info) if os.path.normcase(str(path)) in marked else info
        return patch.object(wa.os, 'lstat', side_effect=lstat)

    def ids(self):
        return sorted(os.listdir(self.managed)) if self.managed.exists() else []

    def test_a_file_is_stored_once_read_only_with_relative_metadata_and_read_back(self):
        data = b'\x00\xfffixture\x80'
        result = wa.store(str(self.cwd), '자료.bin', data, hashlib.sha256(data).hexdigest())
        path = Path(result['path'])
        self.assertEqual(path, self.cwd / wa.DIRECTORY / result['id'] / '자료.bin')
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual((result['size'], result['sha256'], result['mime']),
                         (len(data), hashlib.sha256(data).hexdigest(), 'application/octet-stream'))
        self.assertEqual(result['relativePath'], wa.DIRECTORY + '/' + result['id'] + '/자료.bin')
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        # No temporary name is left beside the file and its metadata.
        self.assertEqual(set(os.listdir(path.parent)), {'자료.bin', '.metadata.json'})
        metadata = json.loads((path.parent / '.metadata.json').read_text(encoding='utf-8'))
        self.assertNotIn('path', metadata)
        self.assertEqual(wa.resolve(str(self.cwd), result['id']), result)
        # The same shape as a Mac's record, path included.
        mac = attachments.store(self.cwd, '자료.bin', data)
        self.assertEqual(set(mac), set(result))

    def test_names_follow_the_mac_rules_and_refuse_what_windows_would_change(self):
        for name in ('../outside', '/absolute', 'a/b', 'a\\b', '.', '..', '.metadata.json', 'x\x00y', '', 'a' * 241, 7):
            with self.subTest(name=name):
                with self.assertRaises(attachments.AttachmentError):
                    attachments.safe_name(name)
                with self.assertRaises(wa.AttachmentError):
                    wa.safe_name(name)
        # ":" would write an NTFS stream; a trailing dot or space and device names are not kept as written.
        for name in ('a:b.txt', 'memo.txt:hidden', 'what?.txt', 'x*.png', 'a<b', 'a>b', 'a|b', 'a"b', 'name.', 'name ',
                     'CON', 'con.txt', 'Nul', 'aux .md', 'LPT1.log', 'COM9', 'COM\xb9.txt', 'CONIN$', '.METADATA.JSON'):
            with self.subTest(name=name), self.assertRaises(wa.AttachmentError):
                wa.store(str(self.cwd), name, b'x')
        self.assertEqual(self.ids(), [])
        for name in ('보고서.pdf', 'photo 1.png', 'CONSOLE.txt', 'COM10.txt', 'a.b.c', '.hidden', 'a' * 240):
            with self.subTest(name=name):
                self.assertEqual(wa.safe_name(name), attachments.safe_name(name))

    def test_types_match_the_mac_rules(self):
        cases = [('a.png', PNG), ('a.png', b'not an image'), ('a.jpg', b'\xff\xd8\xff\xe0'), ('a.JPEG', b'\xff\xd8\xff\xe0'),
                 ('a.gif', b'GIF89a fixture'), ('a.webp', b'RIFF\x00\x00\x00\x00WEBPVP8 '), ('a.webp', b'RIFF'),
                 ('a.svg', b'<svg/>'), ('a.bmp', b'BM'), ('file', PNG), ('a.txt', b'x'), ('a.pdf', b'%PDF-1.7'),
                 ('a.json', b'{}'), ('a.zip', b'PK\x03\x04')]
        for name, data in cases:
            with self.subTest(name=name, data=data):
                self.assertEqual(wa._mime(name, data), attachments._mime(name, data))
        self.assertEqual(wa.IMAGE_TYPES, ('image/png', 'image/jpeg', 'image/gif', 'image/webp'))

    def test_a_reparse_point_from_the_work_folder_down_is_refused(self):
        # The work folder itself.
        with self.reparse_at(self.cwd), self.assertRaises(wa.AttachmentError):
            wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertFalse(self.managed.exists())
        # The managed folder (a junction to elsewhere looks like a plain folder otherwise).
        self.managed.mkdir()
        with self.reparse_at(self.managed), self.assertRaises(wa.AttachmentError):
            wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertEqual(self.ids(), [])
        # An attachment's folder, its file and its metadata, when a send looks it up.
        stored = wa.store(str(self.cwd), 'note.txt', b'x')
        folder = Path(stored['path']).parent
        for path in (self.managed, folder, folder / 'note.txt', folder / '.metadata.json'):
            with self.subTest(path=path.name), self.reparse_at(path), self.assertRaises(wa.AttachmentError):
                wa.resolve(str(self.cwd), stored['id'])
        self.assertEqual(wa.resolve(str(self.cwd), stored['id']), stored)

    def test_links_never_lead_a_file_out_of_the_work_folder(self):
        self.managed.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(wa.AttachmentError):
            wa.store(str(self.cwd), 'note.txt', b'x')
        # Even when the module's own lstat misses the link (a swap after the check), the path as
        # resolved (os.path.realpath, which looks for itself) is checked.
        real = os.lstat

        def unseen(path, *args, **kwargs):
            if (sys._getframe(1).f_globals.get('__name__') == 'windows_attachments'
                    and os.path.normcase(str(path)) == os.path.normcase(str(self.managed))):
                return os.stat(path)
            return real(path, *args, **kwargs)
        with patch.object(wa.os, 'lstat', side_effect=unseen), self.assertRaises(wa.AttachmentError):
            wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertEqual(list(self.outside.iterdir()), [])
        self.managed.unlink()
        # A stored file swapped for a link to another file.
        stored = wa.store(str(self.cwd), 'note.txt', b'original')
        secret = self.outside / 'private.txt'
        secret.write_bytes(b'original')
        path = Path(stored['path'])
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(secret)
        with self.assertRaises(wa.AttachmentError):
            wa.resolve(str(self.cwd), stored['id'])
        self.assertEqual(secret.read_bytes(), b'original')
        # A work folder that is itself a link, or not absolute.
        linked = self.root / 'linked'
        linked.symlink_to(self.cwd, target_is_directory=True)
        for cwd in (str(linked), 'project', '', None):
            with self.subTest(cwd=cwd), self.assertRaises(wa.AttachmentError):
                wa.store(cwd, 'note.txt', b'x')

    def test_nothing_is_overwritten_and_a_failed_write_leaves_nothing(self):
        folder = self.root / 'folder'
        folder.mkdir()
        (folder / 'x.txt').write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            wa._write_new(folder, 'x.txt', b'new')
        self.assertEqual((folder / 'x.txt').read_bytes(), b'old')
        self.assertEqual(os.listdir(folder), ['x.txt'])
        with patch.object(wa.os, 'fsync', side_effect=OSError('synthetic full disk')):
            with self.assertRaisesRegex(wa.AttachmentError, '저장하지 못했습니다'):
                wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertEqual(self.ids(), [])
        # A lookup that fails after the files were written removes the new folder too.
        with patch.object(wa, '_resolve_folder', side_effect=wa.AttachmentError('synthetic')):
            with self.assertRaises(wa.AttachmentError):
                wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertEqual(self.ids(), [])

    def test_tampered_metadata_extra_files_and_unknown_ids_are_refused(self):
        stored = wa.store(str(self.cwd), 'note.txt', b'original')
        folder = Path(stored['path']).parent
        (folder / 'extra.txt').write_bytes(b'x')
        with self.assertRaises(wa.AttachmentError):
            wa.resolve(str(self.cwd), stored['id'])
        (folder / 'extra.txt').unlink()
        metadata = folder / '.metadata.json'
        original = metadata.read_text(encoding='utf-8')
        metadata.chmod(0o600)
        metadata.write_text(original.replace('"size": 8', '"size": 9'), encoding='utf-8')
        with self.assertRaises(wa.AttachmentError):
            wa.resolve(str(self.cwd), stored['id'])
        for identifier in ('../outside', 'x' * 32, '0' * 31, '0' * 32, None):
            with self.subTest(identifier=identifier), self.assertRaises(wa.AttachmentError):
                wa.resolve(str(self.cwd), identifier)

    def test_an_upload_is_bounded_and_checked_before_it_is_stored(self):
        data = PNG + os.urandom(10000)
        encoded = base64.b64encode(data).decode()
        chunks = [encoded[start:start + 4096] for start in range(0, len(encoded), 4096)]
        args = {'cwd': str(self.cwd), 'name': 'shot.png', 'sha256': hashlib.sha256(data).hexdigest()}
        stored = wa.receive(args, chunks)
        self.assertEqual((Path(stored['path']).read_bytes(), stored['mime']), (data, 'image/png'))
        before = self.ids()
        limit = ((wa.MAX_FILE_BYTES + 2) // 3) * 4
        for request, lines in (({**args, 'sha256': '0' * 64}, chunks), ({**args, 'sha256': None}, chunks),
                               (args, ['***']), (args, 'not a list'), (args, [1]),
                               (args, ['A' * limit, 'AAAA'])):
            with self.subTest(lines=str(lines)[:20]), self.assertRaises(wa.AttachmentError):
                with patch.object(wa.base64, 'b64decode', wraps=wa.base64.b64decode) as decode:
                    wa.receive(request, lines)
            if lines and isinstance(lines, list) and len(lines) == 2:
                decode.assert_not_called()
        self.assertEqual(self.ids(), before)

    def test_the_folder_name_from_the_board_is_one_plain_component(self):
        for name in ('..', 'a/b', 'a\\b', 'x.', '', 'a' * 65, None):
            with self.subTest(name=name), patch.object(wa, 'DIRECTORY', name), self.assertRaises(wa.AttachmentError):
                wa.store(str(self.cwd), 'note.txt', b'x')
        with patch.object(wa, 'DIRECTORY', '.example-attachments'):
            stored = wa.store(str(self.cwd), 'note.txt', b'x')
        self.assertTrue(stored['relativePath'].startswith('.example-attachments/'))

    def test_git_exclude_is_added_once_and_never_through_a_link(self):
        (self.cwd / '.git').mkdir()
        wa.store(str(self.cwd), 'a.txt', b'x')
        wa.store(str(self.cwd), 'b.txt', b'x')
        exclude = self.cwd / '.git/info/exclude'
        self.assertEqual(exclude.read_text().splitlines().count(wa.DIRECTORY + '/'), 1)
        # A linked worktree: its `.git` file names a Git folder whose commondir holds the excludes.
        common = self.root / 'main-repo/.git'
        (common / 'worktrees/feature').mkdir(parents=True)
        (common / 'worktrees/feature/commondir').write_text('../..\n')
        tree = self.root / 'feature'
        tree.mkdir()
        (tree / '.git').write_text('gitdir: ' + str(common / 'worktrees/feature') + '\n')
        self.assertTrue(wa.exclude_from_git(tree))
        self.assertIn(wa.DIRECTORY + '/', (common / 'info/exclude').read_text().splitlines())
        # A `.git` that is a link, or info/ as a reparse point: nothing is written, the upload goes on.
        linked = self.root / 'linked-repo'
        linked.mkdir()
        (linked / '.git').symlink_to(common, target_is_directory=True)
        (common / 'info/exclude').write_text('')
        self.assertFalse(wa.exclude_from_git(linked))
        plain = self.root / 'plain-repo'
        (plain / '.git/info').mkdir(parents=True)
        with self.reparse_at(plain / '.git/info'):
            self.assertFalse(wa.exclude_from_git(plain))
            stored = wa.store(str(plain), 'note.txt', b'x')
        self.assertFalse((plain / '.git/info/exclude').exists())
        self.assertEqual((common / 'info/exclude').read_text(), '')
        self.assertTrue(Path(stored['path']).is_file())


if __name__ == '__main__':
    unittest.main()
