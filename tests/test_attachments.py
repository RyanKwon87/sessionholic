import base64
import hashlib
import io
import json
import os
from pathlib import Path
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
