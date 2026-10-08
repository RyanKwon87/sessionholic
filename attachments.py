"""Immutable per-workspace attachments, stored on the actual execution host.

Callers resolve the session cwd on the server; browser paths are never trusted.
Only native directory handles are used beneath the managed attachment directory.
"""
import base64
import binascii
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import uuid

DIRECTORY = '.sessionholic-attachments'
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 20000
MAX_WORKSPACE_BYTES = 128 * 1024 * 1024
_ID = re.compile(r'[a-f0-9]{32}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_METADATA = '.metadata.json'


class AttachmentError(ValueError):
    pass


def _fail(message):
    raise AttachmentError(message)


def safe_name(value):
    try:
        encoded_length = len(value.encode('utf-8')) if isinstance(value, str) else 0
    except UnicodeError:
        _fail('첨부 파일 이름이 안전하지 않습니다.')
    if (not isinstance(value, str) or not value or encoded_length > 240
            or value in ('.', '..', _METADATA) or '/' in value or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail('첨부 파일 이름이 안전하지 않습니다.')
    return value


def _cwd(cwd):
    path = Path(cwd).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        _fail('첨부할 세션의 실제 작업 폴더가 필요합니다.')
    return path.resolve()


def _dir(parent_fd, name, create=False):
    try:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            _fail('첨부 저장 폴더의 소유자 또는 권한이 안전하지 않습니다.')
        return fd
    except OSError:
        _fail('첨부 저장 경로에 링크 또는 잘못된 폴더가 있습니다.')


def _open_root(cwd, create=False):
    path = _cwd(cwd)
    parent = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return path, _dir(parent, DIRECTORY, create)
    finally:
        os.close(parent)


def _read(fd, name, limit):
    try:
        opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(opened, 'rb') as source:
            before = os.fstat(source.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size > limit):
                _fail('첨부 파일의 종류·권한·크기가 안전하지 않습니다.')
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            if len(data) > limit or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                _fail('첨부 파일이 읽는 중 변경되었습니다.')
            return data
    except OSError:
        _fail('첨부 파일을 안전하게 읽을 수 없습니다.')


def _mime(name, data):
    """Native image inputs require both a matching extension and signature."""
    suffix = Path(name).suffix.lower()
    signatures = {
        '.png': ('image/png', data.startswith(b'\x89PNG\r\n\x1a\n')),
        '.jpg': ('image/jpeg', data.startswith(b'\xff\xd8\xff')),
        '.jpeg': ('image/jpeg', data.startswith(b'\xff\xd8\xff')),
        '.gif': ('image/gif', data.startswith((b'GIF87a', b'GIF89a'))),
        '.webp': ('image/webp', len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP'),
    }
    if suffix in signatures:
        mime, matched = signatures[suffix]
        return mime if matched else 'application/octet-stream'
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return 'application/octet-stream' if mime.startswith('image/') else mime


def _write(fd, name, data):
    opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(opened, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
        os.fchmod(target.fileno(), 0o400)


def store(cwd, name, data, expected_sha256=None):
    """Store once, never overwrite; return verified host-local native input data."""
    name = safe_name(name)
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        _fail('첨부 파일은 20MiB 이하의 바이너리여야 합니다.')
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256) or digest != expected_sha256):
        _fail('첨부 파일 SHA-256 검증에 실패했습니다.')
    cwd, root_fd = _open_root(cwd, True)
    identifier = uuid.uuid4().hex
    folder_fd = None
    created = False
    try:
        os.mkdir(identifier, 0o700, dir_fd=root_fd)
        created = True
        folder_fd = _dir(root_fd, identifier)
        relative = DIRECTORY + '/' + identifier + '/' + name
        result = {'id': identifier, 'name': name, 'size': len(data), 'sha256': digest,
                  'path': str(cwd / relative), 'relativePath': relative, 'mime': _mime(name, data)}
        _write(folder_fd, name, data)
        # Store relative metadata only, so the same attachment survives a host move.
        _write(folder_fd, _METADATA, json.dumps({k: v for k, v in result.items() if k != 'path'}, ensure_ascii=False).encode('utf-8'))
        verified = _resolve_folder(cwd, identifier, folder_fd)
        return verified
    except BaseException:
        if folder_fd is not None:
            for filename in (name, _METADATA):
                try:
                    os.unlink(filename, dir_fd=folder_fd)
                except FileNotFoundError:
                    pass
        if created:
            try:
                os.rmdir(identifier, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        if folder_fd is not None:
            os.close(folder_fd)
        os.close(root_fd)


def receive(args):
    """Worker-only JSON transport. Enforce decoded size before allocating it."""
    encoded = args.get('data')
    if not isinstance(encoded, str) or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        _fail('첨부 파일이 허용 크기를 넘습니다.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        _fail('첨부 바이너리 인코딩이 올바르지 않습니다.')
    return store(args['cwd'], args['name'], data, args['sha256'])


def receive_binary(args, stream):
    """Read a bounded binary upload from the finite SSH worker stdin."""
    if type(args.get('size')) is not int or not 0 <= args['size'] <= MAX_FILE_BYTES:
        _fail('첨부 파일 크기가 올바르지 않습니다.')
    data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) != args['size']:
        _fail('첨부 바이너리 크기가 일치하지 않습니다.')
    return store(args['cwd'], args['name'], data, args['sha256'])


def _resolve_folder(cwd, identifier, fd):
    try:
        metadata = json.loads(_read(fd, _METADATA, 4096).decode('utf-8'))
    except (ValueError, UnicodeError):
        _fail('첨부 메타데이터가 올바르지 않습니다.')
    if not isinstance(metadata, dict):
        _fail('첨부 메타데이터가 올바르지 않습니다.')
    name = safe_name(metadata.get('name'))
    relative = DIRECTORY + '/' + identifier + '/' + name
    if metadata.get('id') != identifier or metadata.get('relativePath') != relative:
        _fail('첨부 경로 검증에 실패했습니다.')
    if set(os.listdir(fd)) != {name, _METADATA}:
        _fail('첨부 저장 폴더에 예상하지 못한 파일이 있습니다.')
    data = _read(fd, name, MAX_FILE_BYTES)
    if (type(metadata.get('size')) is not int or metadata['size'] != len(data)
            or metadata.get('sha256') != hashlib.sha256(data).hexdigest()
            or metadata.get('mime') != _mime(name, data)):
        _fail('첨부 파일 SHA-256 또는 종류 검증에 실패했습니다.')
    return {k: metadata[k] for k in ('id', 'name', 'size', 'sha256', 'relativePath', 'mime')} | {'path': str(cwd / relative)}


def resolve(cwd, identifier):
    """Ignore browser paths: look up an ID within this resolved session cwd."""
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        _fail('첨부 식별자가 올바르지 않습니다.')
    cwd, root_fd = _open_root(cwd)
    folder_fd = None
    try:
        folder_fd = _dir(root_fd, identifier)
        return _resolve_folder(cwd, identifier, folder_fd)
    finally:
        if folder_fd is not None:
            os.close(folder_fd)
        os.close(root_fd)


def workspace_files(cwd):
    """Explicitly include this managed directory even when Git ignores it."""
    cwd = _cwd(cwd)
    if not os.path.lexists(cwd / DIRECTORY):
        return []
    cwd, root_fd = _open_root(cwd)
    files, total = [], 0
    try:
        identifiers = os.listdir(root_fd)
        if len(identifiers) * 2 > MAX_FILES:
            _fail('첨부 파일 수가 이전 한도를 넘습니다.')
        for identifier in sorted(identifiers):
            if not _ID.fullmatch(identifier):
                _fail('첨부 저장 폴더에 잘못된 식별자가 있습니다.')
            fd = _dir(root_fd, identifier)
            try:
                record = _resolve_folder(cwd, identifier, fd)
                total += record['size'] + len(_read(fd, _METADATA, 4096))
                if total > MAX_WORKSPACE_BYTES:
                    _fail('첨부 파일 총 크기가 이전 한도를 넘습니다.')
                files.extend([record['relativePath'], DIRECTORY + '/' + identifier + '/' + _METADATA])
            finally:
                os.close(fd)
        return files
    finally:
        os.close(root_fd)
