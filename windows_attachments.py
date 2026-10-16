"""Immutable per-workspace attachments on a Windows PC, with attachments.py's rules.

The board injects this module with windows_runtime over SSH stdin (transfer.windows_rpc);
the session cwd comes from the PC's own fresh read, never from the browser. Windows offers
Python no O_NOFOLLOW or directory handles, so every step from the cwd down (the cwd, the
managed folder, an attachment's folder and its files) is checked with lstat for links and
reparse points (symbolic links, junctions, mount points), and each path must still resolve
inside the cwd. A file is written whole under a temporary name created exclusively, then
linked to its name, which must not exist yet: nothing is ever overwritten.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

# The board's attachment folder in each work folder (instance `remote.attachmentsDir`);
# transfer.windows_rpc sets it, as the PC reads no instance file of its own.
DIRECTORY = '.sessionholic-attachments'
MAX_FILE_BYTES = 20 * 1024 * 1024
ID = re.compile(r'[a-f0-9]{32}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_METADATA = '.metadata.json'
_FOLDER = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
# The types Codex takes as a native image input (native_chat._send).
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
# What Win32 does not keep as written: these characters (":" would name an NTFS stream)
# and the device names, with or without an extension (os.path.isreserved, Python 3.13).
_RESERVED_CHARS = frozenset('<>:"|?*')
_RESERVED_NAMES = frozenset(['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$']
                            + [prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789\xb9\xb2\xb3'])
_EXCLUDE_LIMIT = 1024 * 1024
_types = None


class AttachmentError(ValueError):
    pass


def _fail(message):
    raise AttachmentError(message)


def safe_name(value):
    """attachments.safe_name, and a name Windows keeps exactly: no reserved character or
    device name, no trailing dot or space, and not the metadata file in any letter case."""
    try:
        encoded_length = len(value.encode('utf-8')) if isinstance(value, str) else 0
    except UnicodeError:
        _fail('첨부 파일 이름이 안전하지 않습니다.')
    if (not isinstance(value, str) or not value or encoded_length > 240
            or value in ('.', '..') or value.casefold() == _METADATA or '/' in value or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in _RESERVED_CHARS for c in value) or value[-1] in '. '
            or value.partition('.')[0].rstrip(' ').upper() in _RESERVED_NAMES):
        _fail('첨부 파일 이름이 안전하지 않습니다.')
    return value


def _mime(name, data):
    """As attachments._mime: a native image input needs a matching extension and signature.
    The built-in type table only: the PC's registry does not decide what a file is."""
    global _types
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
    if _types is None:
        _types = mimetypes.MimeTypes()
    mime = _types.guess_type(name)[0] or 'application/octet-stream'
    return 'application/octet-stream' if mime.startswith('image/') else mime


def _linked(info):
    """A symbolic link, or on Windows any reparse point (a junction, a mount point)."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _plain(path, directory):
    """lstat of `path`: a plain directory or regular file, neither a link nor a reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        _fail('첨부 저장 경로를 확인하지 못했습니다.')
    if _linked(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _fail('첨부 저장 경로에 링크 또는 잘못된 폴더가 있습니다.')
    return info


def _inside(path, cwd):
    """`path` as Windows resolves it must lie within the real work folder."""
    real, base = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(cwd))
    if real != base and not real.startswith(base.rstrip(os.sep) + os.sep):
        _fail('첨부 저장 경로가 작업 폴더 밖을 가리킵니다.')


def _cwd(cwd):
    """The session's real work folder: absolute (a drive and root on Windows), a plain folder."""
    if not isinstance(cwd, str) or not cwd or any(ord(c) < 32 for c in cwd) or not Path(cwd).is_absolute():
        _fail('첨부할 세션의 실제 작업 폴더가 필요합니다.')
    try:
        info = os.lstat(cwd)
    except OSError:
        _fail('첨부할 세션의 실제 작업 폴더가 필요합니다.')
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        _fail('첨부할 세션의 실제 작업 폴더가 필요합니다.')
    return Path(os.path.realpath(cwd))


def _root(cwd, create=False):
    if not isinstance(DIRECTORY, str) or not _FOLDER.fullmatch(DIRECTORY) or DIRECTORY.endswith('.'):
        _fail('첨부 저장 폴더 이름이 올바르지 않습니다.')
    root = cwd / DIRECTORY
    if create:
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
        except OSError:
            _fail('첨부 저장 폴더를 만들지 못했습니다.')
    _plain(root, True)
    _inside(root, cwd)
    return root


def _publish(temporary, target):
    """Give a finished file its name without replacing anything already there: a hard link
    fails on an existing name on NTFS as on POSIX. A volume without hard links (FAT, some
    shares) takes a rename, which on Windows refuses an existing name too."""
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise
    except OSError:
        if os.name != 'nt':
            raise
        os.rename(temporary, target)
        return
    os.unlink(temporary)


def _write_new(folder, name, data):
    """A new read-only file (attachments._write's 0o400): never partly written under its name."""
    temporary = folder / ('.' + secrets.token_hex(8) + '.tmp')
    try:
        with open(temporary, 'xb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        _plain(temporary, False)
        _publish(temporary, folder / name)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    os.chmod(folder / name, stat.S_IREAD)


def _discard(folder, names):
    """Remove what a failed store left in its own new folder; never through a link."""
    for name in names:
        path = folder / name
        try:
            info = os.lstat(path)
            if not _linked(info) and stat.S_ISREG(info.st_mode):
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(path)
        except OSError:
            pass
    try:
        os.rmdir(folder)
    except OSError:
        pass


def _read(path, limit):
    """A regular file's bytes, at most `limit`, unchanged while it was read."""
    before = _plain(path, False)
    if before.st_size > limit or before.st_nlink != 1:
        _fail('첨부 파일의 종류·크기가 안전하지 않습니다.')
    try:
        with open(path, 'rb') as source:
            opened = os.fstat(source.fileno())
            data = source.read(limit + 1)
            closed = os.fstat(source.fileno())
    except OSError:
        _fail('첨부 파일을 안전하게 읽을 수 없습니다.')
    after = _plain(path, False)
    if (len(data) > limit or len(data) != closed.st_size or not stat.S_ISREG(opened.st_mode)
            or (opened.st_size, opened.st_mtime_ns) != (closed.st_size, closed.st_mtime_ns)
            or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)):
        _fail('첨부 파일이 읽는 중 변경되었습니다.')
    return data


def _small_text(path, limit=4096):
    info = os.lstat(path)
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise OSError('unexpected Git metadata file')
    with open(path, 'rb') as source:
        return source.read(limit).decode('utf-8')


def _git_dir(cwd):
    """The repository folder holding info/exclude, as attachments._git_dir, without Git."""
    for directory in (cwd,) + tuple(cwd.parents):
        marker = directory / '.git'
        try:
            info = os.lstat(marker)
        except FileNotFoundError:
            continue
        if _linked(info):
            return None
        if stat.S_ISDIR(info.st_mode):
            return marker
        if not stat.S_ISREG(info.st_mode):
            return None
        text = _small_text(marker)
        if not text.startswith('gitdir: '):
            return None
        git_dir = Path(os.path.normpath(directory / text[len('gitdir: '):].strip()))
        if os.path.lexists(git_dir / 'commondir'):
            git_dir = Path(os.path.normpath(git_dir / _small_text(git_dir / 'commondir').strip()))
        return git_dir
    return None


def _lock(stream):
    # Concurrent uploads must not append the same line twice.
    if os.name == 'nt':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def exclude_from_git(cwd):
    """Best effort, as attachments.exclude_from_git: add the managed folder to the enclosing
    repository's info/exclude so `git add -A` leaves uploads out. Nothing is written through a
    link or reparse point and a failure never blocks an upload. The PC reads no host settings
    file, so `attachments.gitExclude: false` does not reach it: the line is always added."""
    try:
        git_dir = _git_dir(Path(cwd))
        if git_dir is None:
            return False
        _plain(git_dir, True)
        info = git_dir / 'info'
        try:
            os.mkdir(info, 0o755)
        except FileExistsError:
            pass
        _plain(info, True)
        path = info / 'exclude'
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        if existing is not None and (_linked(existing) or not stat.S_ISREG(existing.st_mode)
                                     or existing.st_size > _EXCLUDE_LIMIT):
            return False
        line = (DIRECTORY + '/').encode()
        with open(path, 'a+b') as exclude:
            _lock(exclude)
            current = os.lstat(path)
            if _linked(current) or not stat.S_ISREG(current.st_mode) or current.st_size > _EXCLUDE_LIMIT:
                return False
            exclude.seek(0)
            text = exclude.read(_EXCLUDE_LIMIT)
            if {row.strip() for row in text.split(b'\n')} & {line, DIRECTORY.encode()}:
                return True
            exclude.write((b'\n' if text and not text.endswith(b'\n') else b'') + line + b'\n')
            exclude.flush()
            return True
    except Exception:  # Never let ignore bookkeeping block an upload.
        return False


def store(cwd, name, data, expected_sha256=None):
    """Store once, never overwrite; return verified PC-local input data (attachments.store)."""
    name = safe_name(name)
    if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
        _fail('첨부 파일은 20MiB 이하의 바이너리여야 합니다.')
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256) or digest != expected_sha256):
        _fail('첨부 파일 SHA-256 검증에 실패했습니다.')
    cwd = _cwd(cwd)
    root = _root(cwd, True)
    exclude_from_git(cwd)
    identifier = uuid.uuid4().hex
    folder = root / identifier
    try:
        os.mkdir(folder, 0o700)
    except OSError:
        _fail('첨부 저장 폴더를 만들지 못했습니다.')
    try:
        _plain(folder, True)
        _inside(folder, cwd)
        relative = DIRECTORY + '/' + identifier + '/' + name
        result = {'id': identifier, 'name': name, 'size': len(data), 'sha256': digest,
                  'relativePath': relative, 'mime': _mime(name, data)}
        _write_new(folder, name, data)
        # Relative metadata only, as on a Mac.
        _write_new(folder, _METADATA, json.dumps(result, ensure_ascii=False).encode('utf-8'))
        return _resolve_folder(cwd, identifier, folder)
    except OSError:
        _discard(folder, (name, _METADATA))
        _fail('첨부 파일을 PC에 저장하지 못했습니다.')
    except BaseException:
        _discard(folder, (name, _METADATA))
        raise


def receive(args, chunks):
    """transfer.windows_rpc's upload: the request names the file and its SHA-256; the file's
    base64 arrives as the program's short literal lines, bounded before they are joined."""
    if not isinstance(args, dict) or not isinstance(args.get('sha256'), str):
        _fail('첨부 파일 SHA-256 검증에 실패했습니다.')
    if (not isinstance(chunks, list) or len(chunks) > 65536 or any(not isinstance(chunk, str) for chunk in chunks)
            or sum(len(chunk) for chunk in chunks) > ((MAX_FILE_BYTES + 2) // 3) * 4):
        _fail('첨부 파일이 허용 크기를 넘습니다.')
    try:
        data = base64.b64decode(''.join(chunks), validate=True)
    except (binascii.Error, ValueError):
        _fail('첨부 바이너리 인코딩이 올바르지 않습니다.')
    return store(args.get('cwd'), args.get('name'), data, args['sha256'])


def _resolve_folder(cwd, identifier, folder):
    _plain(folder, True)
    _inside(folder, cwd)
    try:
        metadata = json.loads(_read(folder / _METADATA, 4096).decode('utf-8'))
    except (ValueError, UnicodeError):
        _fail('첨부 메타데이터가 올바르지 않습니다.')
    if not isinstance(metadata, dict):
        _fail('첨부 메타데이터가 올바르지 않습니다.')
    name = safe_name(metadata.get('name'))
    relative = DIRECTORY + '/' + identifier + '/' + name
    if metadata.get('id') != identifier or metadata.get('relativePath') != relative:
        _fail('첨부 경로 검증에 실패했습니다.')
    try:
        names = set(os.listdir(folder))
    except OSError:
        _fail('첨부 저장 폴더를 읽지 못했습니다.')
    if names != {name, _METADATA}:
        _fail('첨부 저장 폴더에 예상하지 못한 파일이 있습니다.')
    path = folder / name
    data = _read(path, MAX_FILE_BYTES)
    _inside(path, cwd)
    if (type(metadata.get('size')) is not int or metadata['size'] != len(data)
            or metadata.get('sha256') != hashlib.sha256(data).hexdigest()
            or metadata.get('mime') != _mime(name, data)):
        _fail('첨부 파일 SHA-256 또는 종류 검증에 실패했습니다.')
    return {**{key: metadata[key] for key in ('id', 'name', 'size', 'sha256', 'relativePath', 'mime')},
            'path': str(path)}


def resolve(cwd, identifier):
    """An ID looked up within this session cwd; a browser-supplied path is never used."""
    if not isinstance(identifier, str) or not ID.fullmatch(identifier):
        _fail('첨부 식별자가 올바르지 않습니다.')
    cwd = _cwd(cwd)
    return _resolve_folder(cwd, identifier, _root(cwd) / identifier)
