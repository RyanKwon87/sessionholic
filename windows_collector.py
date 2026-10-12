"""Bounded, standalone Windows session reader sent over SSH on stdin.

Uses existing project app-server proxies only. It never starts a daemon, resumes
a thread, sends input, reads credentials, or installs files on the owning PC.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

LIMIT = 100
ENDPOINT_LIMIT = 16
TAIL_BYTES = 2 * 1024 * 1024
MESSAGE_LIMIT = 80
SID = re.compile(r'[0-9A-Za-z-]{8,64}\Z')
CAPABILITIES = {'readOnly': True, 'canRead': True, 'canSend': False, 'canLaunch': False, 'canControl': False}
CODEX_SOURCES = ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'unknown']
CLAUDE_PROBE_LIMIT = 16
CLAUDE_PROBE_SECONDS = 2
ANCESTRY_LIMIT = 6
NOT_PERSISTED = {'code': 'not_persisted', 'message': '대화 기록을 저장하지 않는 일회성 실행입니다. 실행 상태를 확인하고 결과는 요청한 에이전트에서 확인해 주세요.'}


def _text(value, limit=4000):
    if not isinstance(value, str):
        return ''
    return value[:limit]


def _metadata(value, limit=160):
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value) else None


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value / 1000 if value > 100000000000 else value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
        except (ValueError, OverflowError):
            pass
    return None


def _windows_path(path):
    value = str(path).replace('\\', '/')
    # These two Win32 spellings name the same absolute path. Do not rewrite
    # device namespaces, volume GUIDs, incomplete UNC names, or drive-relative paths.
    if re.match(r'^//\?/[A-Za-z]:/', value):
        return value[4:]
    if re.match(r'^//\?/UNC/[^/]+/[^/]+(?:/|$)', value, re.IGNORECASE):
        return '//' + value[8:]
    return value


def _norm(path):
    return _windows_path(path).rstrip('/').casefold()


def _safe(path, root):
    path, root = Path(_windows_path(path)), Path(_windows_path(root))
    if not path.is_absolute():
        path = root / path
    resolved, base = path.resolve(), root.resolve()
    if path.is_symlink() or (resolved != base and base not in resolved.parents):
        raise ValueError('허용한 세션 저장 경로가 아닙니다.')
    # Windows junctions/reparse points must not escape the account root.
    for parent in (path,) + tuple(path.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('세션 저장 경로의 링크는 읽지 않습니다.')
    return resolved


def _json(path, root, limit=65536):
    path = _safe(path, root)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('세션 메타데이터 크기가 제한을 초과했습니다.')
    return json.loads(raw.decode('utf-8-sig'))


def _origin(row):
    source = row.get('source') or row.get('thread_source')
    if isinstance(source, str) and len(source) <= 4096 and source.lstrip().startswith(('{', '"')):
        try:
            source = json.loads(source)
        except ValueError:
            pass
    if not isinstance(source, (str, dict)) or len(json.dumps(source)) > 4096:
        source = None
    sub = (source.get('subAgent', source.get('subagent'))) if isinstance(source, dict) else None
    spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    explicit = row.get('initiator')
    detail = explicit if isinstance(explicit, dict) else {}
    kind = detail.get('type', detail.get('kind')) if detail else explicit
    kind = {'user': 'human', 'human': 'human', 'agent': 'agent'}.get(kind) if isinstance(kind, str) else None
    subagent = (isinstance(sub, str) and sub in ('review', 'compact', 'memory_consolidation')) or \
        (isinstance(sub, dict) and
         (isinstance(sub.get('thread_spawn'), dict) or isinstance(sub.get('other'), str)))
    parent = next((_metadata(row.get(key)) for key in ('parentThreadId', 'parent_thread_id', 'parentSessionId', 'parent_session_id') if _metadata(row.get(key))), None)
    return {'source': source, 'originator': _metadata(row.get('originator')),
            'initiator': kind or ('agent' if subagent else 'unknown'),
            'initiatorEvidence': 'explicit' if kind else 'subagent' if subagent else 'unknown',
            'initiatorName': _metadata(row.get('initiatorName')) or _metadata(row.get('initiator_name')) or _metadata(detail.get('name')),
            'parentSessionId': parent or _metadata(spawn.get('parent_thread_id')),
            'agentName': _metadata(row.get('agentName')) or _metadata(row.get('agentNickname')) or _metadata(row.get('agent_nickname')) or _metadata(spawn.get('agent_nickname')),
            'agentRole': _metadata(row.get('agentRole')) or _metadata(row.get('agent_role')) or _metadata(spawn.get('agent_role')) or _metadata(spawn.get('agent_type'))}


def _phase(status, known=True):
    if not known or not isinstance(status, dict):
        return 'unknown'
    kind = status.get('type')
    if kind == 'active':
        if status.get('activeFlags') is not None and not isinstance(status.get('activeFlags'), list):
            return 'unknown'
        return 'needs_input' if status.get('activeFlags') else 'working'
    return 'idle' if kind == 'idle' else 'unknown'


def _flags(status):
    values = status.get('activeFlags') if isinstance(status, dict) else None
    return [value for value in values[:20] if _metadata(value)] if isinstance(values, list) else []


def _process_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    return kernel


def _process_identity(kernel, handle, pid):
    code = wintypes.DWORD()
    created, exited, kernel_time, user_time = [wintypes.FILETIME() for unused in range(4)]
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
        return None
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
        return None
    length = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(length.value)
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
        return None
    stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return {'pid': pid, 'start': str(stamp), 'exe': image.value}


def _same_process(left, right):
    return bool(isinstance(left, dict) and isinstance(right, dict)
                and type(left.get('pid')) is int and left['pid'] > 0
                and left['pid'] == right.get('pid')
                and isinstance(left.get('start'), str) and re.fullmatch(r'\d{17,20}', left['start'])
                and left['start'] == right.get('start')
                and isinstance(left.get('exe'), str) and left['exe']
                and _norm(left['exe']) == _norm(right.get('exe')))


def _process(pid):
    """Windows process creation FILETIME, executable and running state, no argv/env."""
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        return None
    kernel = _process_api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        return _process_identity(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def _parents(deadline):
    """One bounded Toolhelp snapshot; names and command lines are not retained."""
    if os.name != 'nt' or time.monotonic() >= deadline:
        return {}
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = _process_api()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle in (None, ctypes.c_void_p(-1).value):
        return {}
    try:
        entry, result = Entry(), {}
        entry.dwSize = ctypes.sizeof(entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while valid and len(result) < 8192 and time.monotonic() < deadline:
            result[entry.th32ProcessID] = entry.th32ParentProcessID
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
        return result
    finally:
        kernel.CloseHandle(handle)


def _claude_options(arguments):
    """Conservative option-prefix parser; prompt/unknown options end observation."""
    flags = {'print': False, 'noSessionPersistence': False}
    values = {'--model', '--output-format', '--input-format', '--name', '-n', '--agent',
              '--system-prompt', '--append-system-prompt', '--system-prompt-file',
              '--append-system-prompt-file', '--session-id', '--permission-mode',
              '--max-turns', '--max-budget-usd', '--fallback-model', '--effort',
              '--json-schema', '--settings', '--setting-sources', '--mcp-config'}
    switches = {'--verbose', '--debug', '--dangerously-skip-permissions',
                '--allow-dangerously-skip-permissions', '--strict-mcp-config',
                '--include-partial-messages', '--replay-user-messages', '--disable-slash-commands'}
    index = 1  # executable argv[0]
    while index < len(arguments):
        value = arguments[index]
        if value == '--' or not value.startswith('-'):
            break
        option, equal, unused = value.partition('=')
        if option in ('-p', '--print', '--no-session-persistence') and not equal:
            flags['noSessionPersistence' if option == '--no-session-persistence' else 'print'] = True
        elif option in values:
            if not equal:
                index += 1
                if index >= len(arguments):
                    return {}
        elif option not in switches or equal:
            break
        index += 1
    return flags


def _query_claude_flags(kernel, handle):
    """Read one handle's bounded command line, returning only two booleans.

    ProcessCommandLineInformation is optional: unsupported Windows versions fail
    closed. No PEB/environment read or PowerShell/CIM fallback is performed.
    """
    class UnicodeString(ctypes.Structure):
        _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT), ('Buffer', ctypes.c_void_p)]
    query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.LONG
    size = wintypes.ULONG()
    query(handle, 60, None, 0, ctypes.byref(size))
    if size.value < ctypes.sizeof(UnicodeString) or size.value > 65536:
        return {}
    buffer = ctypes.create_string_buffer(size.value)
    if query(handle, 60, buffer, len(buffer), ctypes.byref(size)) < 0:
        return {}
    value = UnicodeString.from_buffer(buffer)
    begin, end = ctypes.addressof(buffer), ctypes.addressof(buffer) + len(buffer)
    if (value.Length == 0 or value.Length % 2 or value.Length > value.MaximumLength
            or not value.Buffer or value.Buffer < begin + ctypes.sizeof(UnicodeString)
            or value.Buffer + value.Length > end):
        return {}
    command = ctypes.wstring_at(value.Buffer, value.Length // 2)
    if '\0' in command:
        return {}
    shell = ctypes.WinDLL('shell32')
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
    try:
        return _claude_options([arguments[i] for i in range(count.value)]) if arguments and 0 < count.value <= 256 else {}
    finally:
        if arguments:
            kernel.LocalFree(arguments)


def _claude_flags(expected, deadline):
    if os.name != 'nt' or not _same_process(expected, expected) or time.monotonic() >= deadline:
        return {}
    kernel = _process_api()
    handle = kernel.OpenProcess(0x1000, False, expected['pid'])
    if not handle:
        return {}
    try:
        if not _same_process(expected, _process_identity(kernel, handle, expected['pid'])):
            return {}
        flags = _query_claude_flags(kernel, handle)
        if time.monotonic() >= deadline or not _same_process(expected, _process_identity(kernel, handle, expected['pid'])):
            return {}
        return flags
    except (OSError, ValueError, AttributeError):
        return {}
    finally:
        kernel.CloseHandle(handle)


class Proxy:
    """Read-only JSON-RPC over the native Windows socket proxy's WebSocket bytes."""
    ALLOWED = {'initialize', 'thread/list', 'thread/read', 'thread/loaded/list'}

    def __init__(self, binary, socket_path, deadline):
        self.deadline, self.pending, self.frames, self.sequence = deadline, bytearray(), queue.Queue(maxsize=256), 0
        self._remaining()
        self.process = subprocess.Popen([str(binary), 'app-server', 'proxy', '--sock', str(socket_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        self.closed = threading.Event()
        def receive():
            try:
                while not self.closed.is_set():
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    self.frames.put(chunk, timeout=max(.05, self.deadline - time.monotonic()))
                    if not chunk:
                        return
            except (OSError, queue.Full):
                try:
                    self.frames.put_nowait(b'')
                except queue.Full:
                    pass
        threading.Thread(target=receive, daemon=True).start()
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self._write(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                header.extend(self._read(1))
                if len(header) > 8192:
                    raise ValueError('PC 서버 연결 헤더가 너무 큽니다.')
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            headers = {line.split(b':', 1)[0].lower(): line.split(b':', 1)[1].strip()
                       for line in bytes(header).split(b'\r\n')[1:] if b':' in line}
            if not header.startswith(b'HTTP/1.1 101 ') or headers.get(b'sec-websocket-accept') != expected:
                raise ValueError('PC 서버의 연결 응답을 확인하지 못했습니다.')
            self.call('initialize', {'clientInfo': {'name': 'sessionholic_windows_reader', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('PC 서버 읽기 시간이 초과되었습니다.')
        return remaining

    def _read(self, size):
        self._remaining()
        while len(self.pending) < size:
            try:
                chunk = self.frames.get(timeout=self._remaining())
            except queue.Empty:
                raise TimeoutError('PC 서버 읽기 시간이 초과되었습니다.') from None
            if not chunk:
                raise ValueError('PC 서버 읽기 연결이 끝났습니다.')
            self.pending.extend(chunk)
        raw = bytes(self.pending[:size])
        del self.pending[:size]
        return raw

    def _send(self, value, opcode=1):
        self._remaining()
        raw = json.dumps(value).encode() if isinstance(value, dict) else value
        mask = os.urandom(4)
        length = len(raw)
        if length < 126:
            prefix = bytes([0x80 | opcode, 0x80 | length])
        elif length <= 65535:
            prefix = bytes([0x80 | opcode, 0xfe]) + struct.pack('!H', length)
        else:
            prefix = bytes([0x80 | opcode, 0xff]) + struct.pack('!Q', length)
        self._write(prefix + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw)))

    def _write(self, raw):
        # Windows anonymous pipe writes may block when the proxy stops reading.
        # The caller's timeout closes only this proxy, which releases this writer.
        finished, failure = threading.Event(), []
        deadline = self._remaining()
        fd = self.process.stdin.fileno()
        def send():
            try:
                offset = 0
                while offset < len(raw):
                    size = os.write(fd, raw[offset:offset + 65536])
                    if size <= 0:
                        raise OSError('PC 서버 연결 쓰기가 끝났습니다.')
                    offset += size
            except OSError as error:
                failure.append(error)
            finally:
                finished.set()
        threading.Thread(target=send, daemon=True).start()
        if not finished.wait(deadline):
            raise TimeoutError('PC 서버 연결 시간이 초과되었습니다.')
        if failure:
            raise failure[0]

    def _message(self):
        fragments = bytearray()
        started = False
        while True:
            head = self._read(2)
            final, opcode, length = bool(head[0] & 0x80), head[0] & 15, head[1] & 127
            if head[1] & 0x80 or head[0] & 0x70:
                raise ValueError('PC 서버 프레임 형식이 올바르지 않습니다.')
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
            if length > 8 * 1024 * 1024 or len(fragments) + length > 8 * 1024 * 1024:
                raise ValueError('PC 서버 응답 크기가 제한을 초과했습니다.')
            raw = self._read(length)
            if opcode == 9 and final and length <= 125:
                self._send(raw, 10)
                continue
            if opcode == 10 and final and length <= 125:
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise ValueError('PC 서버 메시지 형식이 올바르지 않습니다.')
            fragments.extend(raw)
            if final:
                value = json.loads(fragments)
                if not isinstance(value, dict):
                    raise ValueError('PC 서버 JSON 형식이 올바르지 않습니다.')
                return value

    def call(self, method, params):
        if method not in self.ALLOWED:
            raise ValueError('PC 수집기는 읽기 요청만 허용합니다.')
        self.sequence += 1
        request = self.sequence
        self._send({'id': request, 'method': method, 'params': params})
        for unused in range(1000):
            message = self._message()
            if message.get('id') == request:
                if 'error' in message or 'result' not in message:
                    raise ValueError('PC 서버가 읽기 요청을 거부했습니다.')
                return message['result']
        raise ValueError('PC 서버 알림이 제한을 초과했습니다.')

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()  # Only the proxy this reader just started.
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class WindowsCollector:
    def __init__(self, home=None, proxy_factory=Proxy, process_reader=_process, hostname=None,
                 parent_reader=_parents, flags_reader=_claude_flags):
        self.home = Path(home) if home is not None else Path.home()
        self.codex_home = self.home / '.codex'
        self.claude_home = self.home / '.claude'
        self.binary = self.home / 'AppData/Local/Programs/OpenAI/Codex/bin/codex.exe'
        self.proxy_factory, self.process_reader = proxy_factory, process_reader
        self.parent_reader, self.flags_reader = parent_reader, flags_reader
        self.hostname = hostname or socket.gethostname()

    def project(self, cwd):
        candidate = _windows_path(cwd or '').rstrip('/')
        root = _windows_path(self.home / 'Projects').rstrip('/') + '/'
        if candidate.casefold().startswith(root.casefold()):
            return candidate[len(root):].split('/')[0] or '기타'
        return candidate.rsplit('/', 1)[-1] or '기타'

    def _database(self):
        found = [(int(match.group(1)), path) for path in self.codex_home.glob('state_*.sqlite')
                 for match in [re.fullmatch(r'state_(\d{1,4})\.sqlite', path.name)] if match]
        return _safe(max(found, key=lambda pair: pair[0])[1], self.codex_home) if found else None

    def database_rows(self, sid=None):
        database = self._database()
        if database is None:
            return []
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        deadline = time.monotonic() + 1.5
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            required = {'id', 'cwd', 'title', 'created_at', 'updated_at', 'archived', 'source'}
            if not required <= columns:
                raise ValueError('PC 세션 DB 형식이 예상과 다릅니다.')
            optional = [key for key in ('name', 'preview', 'first_user_message', 'originator', 'agent_nickname',
                'agent_role', 'thread_source', 'parent_thread_id', 'parent_session_id', 'initiator', 'initiator_name', 'rollout_path') if key in columns]
            select = 'SELECT ' + ','.join(sorted(required) + optional) + ' FROM threads WHERE '
            if sid:
                rows = connection.execute(select + 'id=? LIMIT 2', (sid,)).fetchall()
                if len(rows) > 1:
                    raise ValueError('PC 세션 ID가 중복됩니다.')
            else:
                rows = connection.execute(select + 'archived=0 ORDER BY updated_at DESC LIMIT ?', (LIMIT,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def endpoint(self, project):
        if not _metadata(project) or project in ('.', '..') or '/' in project or '\\' in project or not self.binary.is_file():
            return None
        directory = self.codex_home / 'agents-servers' / project
        try:
            state = _json(directory / 'server-state.json', self.codex_home)
            path = _safe(directory / 'app.sock', self.codex_home)
            endpoint = 'unix://' + path.as_posix()
            process = self.process_reader(state.get('server_pid')) if isinstance(state, dict) else None
            if (not isinstance(state, dict) or state.get('project') != project
                    or _norm(state.get('endpoint')) != _norm(endpoint) or state.get('elevated') is not False
                    or state.get('exit_code') is not None or not path.exists() or not process
                    or not self._codex_executable(process.get('exe'))):
                return None
            return path
        except (OSError, ValueError):
            return None

    def _codex_executable(self, executable):
        if not isinstance(executable, str):
            return False
        try:
            path = _safe(executable, self.home)
            if not path.is_file():
                return False
            if _norm(path) == _norm(self.binary):
                return True
            releases = _safe(self.codex_home / 'packages/standalone/releases', self.codex_home)
            parts = path.relative_to(releases).parts
            return (len(parts) == 3 and parts[1:] == ('bin', 'codex.exe')
                    and re.fullmatch(r'\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?-x86_64-pc-windows-msvc', parts[0]) is not None)
        except (OSError, ValueError):
            return False

    def endpoints(self):
        root = self.codex_home / 'agents-servers'
        found = []
        # Only existing, bounded server-state files belonging to this home are considered.
        for state in sorted(root.glob('*/server-state.json'))[:ENDPOINT_LIMIT]:
            path = self.endpoint(state.parent.name)
            if path is not None:
                found.append(path)
        return found

    def _row(self, raw, live=None):
        sid = str(raw.get('id') or '')
        status = live.get('status') if live else None
        project = self.project(raw.get('cwd'))
        preview = _text(raw.get('preview') or raw.get('first_user_message') or raw.get('title'), 140)
        name = _text((live or {}).get('name') or raw.get('name'), 160)
        return {'agent': 'codex', 'id': sid, 'home': '.codex', 'cwd': str(raw.get('cwd') or ''),
                'title': name or preview[:60] or '(제목 없음)', 'named': bool(name),
                'project': project, 'account': '기기 Codex',
                'state': status.get('type') if isinstance(status, dict) else 'unknown',
                'phase': _phase(status), 'status': ','.join(_flags(status)),
                'loaded': live is not None, 'runtimeKnown': live is not None and _phase(status) != 'unknown',
                'startedAt': _epoch(raw.get('created_at')), 'updatedAt': _epoch(raw.get('updated_at')),
                'snippet': preview if name else '', 'readOnly': True, 'canRead': True,
                'runtimeReason': None if live else '저장된 기록입니다. 현재 실행 상태는 확인되지 않았습니다.',
                **_origin({**raw, **(live or {})})}

    def codex_rows(self, errors):
        rows = self.database_rows()
        live = {}
        overall = time.monotonic() + 12
        stored = {row['id']: row for row in rows}
        ambiguous = set()
        for path in self.endpoints():
            if time.monotonic() >= overall:
                errors.append('PC 서버 읽기 기한이 끝났습니다. 나머지는 저장된 기록을 표시합니다.')
                break
            try:
                with self.proxy_factory(self.binary, path, min(overall, time.monotonic() + 4)) as client:
                    loaded = client.call('thread/loaded/list', {'limit': LIMIT})
                    listed = client.call('thread/list', {'limit': LIMIT, 'archived': False, 'useStateDbOnly': True,
                                                       'sourceKinds': CODEX_SOURCES})
                if not isinstance(loaded, dict) or not isinstance(loaded.get('data'), list) or not isinstance(listed, dict) or not isinstance(listed.get('data'), list):
                    raise ValueError('PC 서버 목록 형식이 올바르지 않습니다.')
                loaded_ids = set(value for value in loaded['data'] if isinstance(value, str))
                for thread in listed['data']:
                    if (isinstance(thread, dict) and thread.get('id') in loaded_ids
                            and thread.get('id') in stored
                            and _norm(thread.get('cwd')) == _norm(stored[thread['id']].get('cwd'))):
                        if thread['id'] in live:
                            ambiguous.add(thread['id'])
                        live[thread['id']] = thread
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                errors.append('PC 프로젝트 서버 상태를 읽지 못했습니다. 저장된 기록을 표시합니다.')
                # A failed endpoint is never retried during this collection.
        output = []
        for row in rows:
            observed = None if row['id'] in ambiguous else live.get(row['id'])
            if observed and _norm(observed.get('cwd')) != _norm(row.get('cwd')):
                observed = None
            output.append(self._row(row, observed))
        return output

    def _transcripts(self):
        root = self.claude_home / 'projects'
        found = {}
        for path in root.glob('*/*.jsonl'):
            if not SID.fullmatch(path.stem) or path.is_symlink():
                continue
            try:
                path = _safe(path, root)
                found.setdefault(path.stem, []).append(path)
            except (OSError, ValueError):
                continue
            if len(found) > 2000:
                break
        return {sid: paths[0] for sid, paths in found.items() if len(paths) == 1}

    def _claude_executable(self, executable):
        if not isinstance(executable, str):
            return False
        try:
            path = _safe(executable, self.home)
            if not path.is_file():
                return False
            if path.name.casefold() == 'claude.exe':
                return True
            # A running Windows binary can retain its old image after an updater
            # renames the file. Accept that precise updater filename only in the
            # account's native CLI installation directory, never an arbitrary cwd.
            return (_norm(path.parent) == _norm(self.home / '.local/bin')
                    and re.fullmatch(r'claude\.exe\.old\.[1-9]\d{12}\.[1-9]\d{0,9}', path.name, re.IGNORECASE) is not None)
        except (OSError, ValueError):
            return False

    def _agent_executable(self, executable):
        if self._codex_executable(executable):
            return 'Codex'
        if (self._claude_executable(executable)
                and _norm(Path(_windows_path(executable)).parent) == _norm(self.home / '.local/bin')):
            return 'Claude Code'
        return None

    def _shell_executable(self, executable):
        if not isinstance(executable, str):
            return False
        # Only native installation locations, never a same-named cwd executable.
        roots = [(os.environ.get('SystemRoot'), 'system'), (os.environ.get('ProgramFiles'), 'programs')]
        for root, kind in roots:
            if not root:
                continue
            try:
                path = _safe(executable, root)
                relative = _norm(path.relative_to(Path(root).resolve()))
                if not path.is_file():
                    continue
                if kind == 'system' and relative in ('system32/cmd.exe', 'system32/windowspowershell/v1.0/powershell.exe'):
                    return True
                if kind == 'programs' and (relative in ('powershell/7/pwsh.exe', 'powershell/7-preview/pwsh.exe')
                        or re.fullmatch(r'windowsapps/microsoft\.powershell_\d+\.\d+\.\d+\.\d+_(?:x64|x86|arm64)__8wekyb3d8bbwe/pwsh\.exe', relative)):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _agent_ancestor(self, process, parents, deadline):
        chain, seen, child = [process], {process['pid']}, process
        name = None
        for unused in range(ANCESTRY_LIMIT):
            if time.monotonic() >= deadline:
                return None
            pid = parents.get(child['pid'])
            if type(pid) is not int or pid <= 0 or pid in seen:
                return None
            parent = self.process_reader(pid)
            if (not _same_process(parent, parent) or parent['pid'] != pid
                    or int(parent['start']) > int(child['start'])):
                return None  # A reused parent PID cannot prove who started the child.
            chain.append(parent)
            seen.add(pid)
            name = self._agent_executable(parent['exe'])
            if name:
                break
            if not self._shell_executable(parent['exe']):
                return None
            child = parent
        if not name:
            return None
        # Recheck every observed process after traversing the chain. No process
        # name, title, print flag, or historical PID alone establishes origin.
        for item in reversed(chain):
            if time.monotonic() >= deadline or not _same_process(item, self.process_reader(item['pid'])):
                return None
        return name

    def _claude_details(self, process, parents, deadline):
        if not _same_process(process, process) or time.monotonic() >= deadline:
            return {}
        flags = self.flags_reader(process, deadline)
        name = self._agent_ancestor(process, parents, deadline)
        if time.monotonic() >= deadline:
            return {}
        if not _same_process(process, self.process_reader(process['pid'])):
            return None  # An observed identity change also invalidates runtime state.
        result = {}
        if name:
            result.update(initiator='agent', initiatorEvidence='process_ancestry', initiatorName=name)
        if isinstance(flags, dict) and flags.get('print') is True:
            result['executionMode'] = 'one-shot'
            if flags.get('noSessionPersistence') is True:
                result['transcriptAvailability'] = 'disabled'
        return result

    def claude_rows(self, target_sid=None):
        records = {}
        for path in list((self.claude_home / 'sessions').glob('[0-9]*.json'))[:256]:
            try:
                row = _json(path, self.claude_home)
                if not isinstance(row, dict):
                    continue
                sid = row.get('sessionId')
                if not SID.fullmatch(sid or '') or row.get('kind') not in ('interactive', 'bg') or not path.stem.isdigit():
                    continue
                if target_sid is not None and sid != target_sid:
                    continue
                pid = int(path.stem)
                process = self.process_reader(pid)
                stamp = row.get('procStart')
                exact = bool(process and type(row.get('pid')) is int and row['pid'] == pid
                             and isinstance(stamp, str) and re.fullmatch(r'\d{17,20}', stamp)
                             and stamp == process.get('start')
                             and isinstance(row.get('pidDomain'), str)
                             and row['pidDomain'].casefold() == 'win32:' + self.hostname.casefold()
                             and self._claude_executable(process.get('exe')))
                if sid in records:
                    records[sid] = (row, False, None)  # Multiple owners stay visible, with unknown runtime.
                else:
                    records[sid] = (row, exact, process)
            except (OSError, ValueError, TypeError):
                continue
        paths = self._transcripts()
        selected = sorted(paths, key=lambda sid: paths[sid].stat().st_mtime, reverse=True)[:LIMIT]
        ids = list(dict.fromkeys(list(records) + selected))
        if target_sid is not None:
            ids = [sid for sid in ids if sid == target_sid]
        output = []
        deadline, probes, parents = time.monotonic() + CLAUDE_PROBE_SECONDS, 0, None
        for sid in ids[:LIMIT]:
            row, exact, process = records.get(sid, ({}, False, None))
            path = paths.get(sid)
            details = {}
            if exact and probes < CLAUDE_PROBE_LIMIT and time.monotonic() < deadline:
                probes += 1
                try:
                    if parents is None:
                        parents = self.parent_reader(deadline)
                    details = self._claude_details(process, parents, deadline)
                    if details is None:
                        exact, details = False, {}
                except (OSError, ValueError, TypeError):
                    pass
            cwd = row.get('cwd') or ''
            if not cwd and path:
                for entry in _head(path):
                    if isinstance(entry.get('cwd'), str):
                        cwd = entry['cwd']
                        break
            status = row.get('status')
            phase = ('working' if status == 'busy' else 'needs_input' if status == 'waiting' else 'idle' if status == 'idle' else 'unknown') if exact else 'unknown'
            project = self.project(cwd)
            output.append({'agent': 'claude', 'id': sid, 'sessionId': sid, 'home': '.claude', 'kind': 'background' if row.get('kind') == 'bg' else 'interactive',
                'title': _text(row.get('name'), 160) or '(Claude 대화)', 'cwd': cwd, 'project': project,
                'phase': phase, 'state': phase, 'status': status if exact else None, 'runtimeKnown': exact and phase != 'unknown',
                'startedAt': _epoch(row.get('startedAt')), 'updatedAt': int(path.stat().st_mtime) if path else None,
                'readOnly': True, 'canRead': path is not None or details.get('transcriptAvailability') == 'disabled',
                'snippet': '', **_origin(row), **details})
        return output

    def snapshot(self):
        errors = []
        try:
            codex = self.codex_rows(errors)
        except (OSError, ValueError, sqlite3.Error):
            codex = []
            errors.append('PC Codex 저장 목록을 읽지 못했습니다.')
        try:
            claude = self.claude_rows()
        except (OSError, ValueError):
            claude = []
            errors.append('PC Claude 저장 목록을 읽지 못했습니다.')
        return {'collectedAt': int(time.time()), 'codex': codex, 'claude': claude, 'tmux': [],
                'errors': errors, 'capabilities': dict(CAPABILITIES), 'platform': 'windows'}

    def read(self, agent, sid, home=None):
        if not SID.fullmatch(sid or '') or agent not in ('codex', 'claude'):
            raise ValueError('PC 대화 식별자가 올바르지 않습니다.')
        if agent == 'claude':
            path = self._transcripts().get(sid)
            row = next(iter(self.claude_rows(target_sid=sid)), {})
            if path is None:
                if row.get('transcriptAvailability') == 'disabled':
                    return {'messages': [], 'phase': row.get('phase', 'unknown'), 'runtimeKnown': row.get('runtimeKnown', False),
                            'activeFlags': [], 'updatedAt': row.get('updatedAt'), 'readOnly': True,
                            'historyNotice': dict(NOT_PERSISTED), 'executionMode': 'one-shot', 'transcriptAvailability': 'disabled'}
                raise ValueError('정확한 PC Claude 대화 기록을 찾지 못했습니다.')
            messages = _claude_messages(_tail(path))
            return {'messages': messages, 'phase': row.get('phase', 'unknown'), 'runtimeKnown': row.get('runtimeKnown', False),
                    'activeFlags': [], 'updatedAt': int(path.stat().st_mtime), 'readOnly': True,
                    **{key: row[key] for key in ('executionMode', 'transcriptAvailability') if key in row}}
        if home != '.codex':
            raise ValueError('등록된 PC Codex home만 읽을 수 있습니다.')
        if str(uuid.UUID(sid)) != sid:
            raise ValueError('PC Codex UUID 형식이 올바르지 않습니다.')
        rows = self.database_rows(sid)
        if len(rows) != 1:
            raise ValueError('정확한 PC Codex 대화 기록을 찾지 못했습니다.')
        row = rows[0]
        observed = []
        overall = time.monotonic() + 12
        for path in self.endpoints():
            if time.monotonic() >= overall:
                break
            try:
                with self.proxy_factory(self.binary, path, min(overall, time.monotonic() + 5)) as client:
                    loaded = client.call('thread/loaded/list', {'limit': LIMIT})
                    if not isinstance(loaded, dict) or not isinstance(loaded.get('data'), list) or sid not in loaded['data']:
                        continue
                    result = client.call('thread/read', {'threadId': sid, 'includeTurns': True})
                thread = result.get('thread') if isinstance(result, dict) else None
                if not isinstance(thread, dict) or thread.get('id') != sid or _norm(thread.get('cwd')) != _norm(row['cwd']):
                    raise ValueError('PC 서버 대화의 소유 경계가 다릅니다.')
                observed.append({'messages': _codex_messages(thread.get('turns') or []), 'phase': _phase(thread.get('status')),
                        'runtimeKnown': _phase(thread.get('status')) != 'unknown',
                        'activeFlags': _flags(thread.get('status')),
                        'updatedAt': _epoch(thread.get('updatedAt')), 'readOnly': True})
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                pass
        if len(observed) == 1:
            return observed[0]
        rollout = row.get('rollout_path')
        if not isinstance(rollout, str) or not rollout:
            raise ValueError('PC Codex 저장 대화 경로를 확인하지 못했습니다.')
        path = _safe(Path(rollout), self.codex_home)
        if path.suffix != '.jsonl' or path.relative_to(self.codex_home.resolve()).parts[0] != 'sessions':
            raise ValueError('PC Codex 저장 대화 경로가 올바르지 않습니다.')
        # The first session_meta line binds the file to this native identity.
        meta = next((entry.get('payload') for entry in _head(path) if entry.get('type') == 'session_meta'), None)
        if not isinstance(meta, dict) or meta.get('id') != sid or _norm(meta.get('cwd')) != _norm(row['cwd']):
            raise ValueError('PC 저장 대화의 세션 신원을 확인하지 못했습니다.')
        return {'messages': _rollout_messages(_tail(path)), 'phase': 'unknown', 'runtimeKnown': False,
                'activeFlags': [], 'updatedAt': _epoch(row['updated_at']), 'readOnly': True,
                'runtimeReason': '저장된 대화입니다. 현재 실행 상태는 확인되지 않았습니다.'}


def _head(path):
    with path.open('rb') as stream:
        raw = stream.read(65536)
    return _entries(raw.splitlines()[:30])


def _tail(path):
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - TAIL_BYTES))
        raw = stream.read(TAIL_BYTES)
    lines = raw.splitlines()
    return _entries(lines[1:] if size > TAIL_BYTES else lines)


def _entries(lines):
    entries = []
    for raw in lines[-2000:]:
        try:
            item = json.loads(raw)
            if isinstance(item, dict):
                entries.append(item)
        except (ValueError, UnicodeError):
            continue
    return entries


def _message(role, text, ts=None, **metadata):
    return {'role': role, 'text': _text(text), 'ts': _epoch(ts),
            **{key: value for key, value in metadata.items() if _metadata(value)}}


def _codex_messages(turns):
    result = []
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        for item in turn.get('items') if isinstance(turn.get('items'), list) else []:
            if not isinstance(item, dict):
                continue
            metadata = {'turnId': turn.get('id'), 'turnStatus': turn.get('status'), 'messageId': item.get('id')}
            if item.get('type') == 'userMessage':
                content = item.get('content') if isinstance(item.get('content'), list) else []
                text = '\n'.join(block.get('text', '') for block in content if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str))
                if text:
                    result.append(_message('user', text, turn.get('startedAt'), **metadata))
            elif item.get('type') == 'agentMessage' and item.get('text'):
                result.append(_message('assistant', item['text'], turn.get('completedAt'), phase=item.get('phase'), **metadata))
            elif item.get('type') == 'commandExecution':
                result.append(_message('tool', '$ ' + _text(item.get('command'), 160), toolKind='command', **metadata))
    return result[-MESSAGE_LIMIT:]


def _claude_messages(entries):
    result = []
    for entry in entries:
        message = entry.get('message')
        role = entry.get('type')
        if role not in ('user', 'assistant') or not isinstance(message, dict):
            continue
        content = message.get('content')
        text = content if isinstance(content, str) else '\n'.join(block.get('text', '') for block in content or [] if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)) if isinstance(content, list) else ''
        if text and not text.startswith(('<system-reminder>', '[Request interrupted by user')):
            result.append(_message(role, text, entry.get('timestamp'), messageId=entry.get('uuid')))
    return result[-MESSAGE_LIMIT:]


def _rollout_messages(entries):
    result = []
    turn = None
    for entry in entries:
        payload = entry.get('payload')
        if not isinstance(payload, dict):
            continue
        if entry.get('type') == 'turn_context':
            turn = payload.get('turn_id')
        if entry.get('type') == 'response_item' and payload.get('type') == 'message':
            role = payload.get('role')
            if role not in ('user', 'assistant'):
                continue
            content = payload.get('content') if isinstance(payload.get('content'), list) else []
            text = '\n'.join(block.get('text', '') for block in content
                             if isinstance(block, dict) and block.get('type') in ('input_text', 'output_text') and isinstance(block.get('text'), str))
            if text:
                result.append(_message(role, text, entry.get('timestamp'), turnId=turn, messageId=payload.get('id'), phase=payload.get('phase')))
    return result[-MESSAGE_LIMIT:]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        reader = WindowsCollector()
        if args in (['snapshot'], ['snapshot', '--full']):
            result = reader.snapshot()
        elif len(args) in (3, 4) and args[0] == 'read':
            result = reader.read(args[1], args[2], args[3] if len(args) == 4 else None)
        else:
            raise ValueError('지원하지 않는 PC 읽기 요청입니다.')
    except (OSError, ValueError, sqlite3.Error):
        result = {'error': 'PC 세션 읽기를 완료하지 못했습니다.', 'code': 'windows_read_unavailable',
                  'messages': [], 'phase': 'unknown', 'runtimeKnown': False, 'readOnly': True}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
