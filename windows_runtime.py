"""Finite Windows native-session actions, preserving the owning PC's settings.

The board injects this module and windows_collector over SSH stdin. It starts no
server and accepts no arbitrary shell command, PID, binary or filesystem path.
Only metadata receipts and a local proof key are persisted; input is not logged.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import uuid

import windows_collector as wc

REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')
# A Claude background run's job id: what `claude --bg` prints, `claude agents` lists and
# `claude attach` takes (the session UUID is refused there).
CLAUDE_JOB = re.compile(r'[0-9a-z]{8}\Z')


class NotLoaded(ValueError):
    """No PC server has this stored session open: it may be resumed into its project's server."""
CONTROL_REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
PROOF_TTL = 45
ARCHIVE_EFFECT = '선택한 세션을 종료하고 대화 기록을 보관합니다. 하위 에이전트도 함께 종료·보관을 요청합니다.'
STORE_RELATIVE = '.local/state/sessionholic-windows'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _identity(source):
    if not isinstance(source, dict) or source.get('agent') not in ('codex', 'claude'):
        raise ValueError('PC 세션 정보가 올바르지 않습니다.')
    agent = source['agent']
    sid = source.get('id') or source.get('sessionId')
    if not isinstance(sid, str) or not wc.SID.fullmatch(sid):
        raise ValueError('PC 세션 ID가 올바르지 않습니다.')
    if agent == 'codex' and (str(uuid.UUID(sid)) != sid or source.get('home') != '.codex'):
        raise ValueError('등록된 PC Codex 계정을 확인해 주세요.')
    host = source.get('host')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', host):
        raise ValueError('PC 기기 식별자가 올바르지 않습니다.')
    return {'host': host, 'agent': agent, 'id': sid, 'home': '.codex' if agent == 'codex' else '.claude'}


def _private_acl(path):
    if os.name != 'nt':
        path.chmod(0o700)
        return
    # Owner and SYSTEM only, inherited by receipts created inside this directory.
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1, ctypes.byref(descriptor), None):
        raise OSError('PC 세션 작업 디렉터리 권한을 준비하지 못했습니다.')
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError('PC 세션 작업 디렉터리 권한을 적용하지 못했습니다.')
    finally:
        kernel.LocalFree(descriptor)


class Store:
    def __init__(self, home):
        self.home = Path(home)
        if (not isinstance(STORE_RELATIVE, str) or Path(STORE_RELATIVE).is_absolute()
                or '..' in Path(STORE_RELATIVE).parts or not STORE_RELATIVE):
            raise ValueError('PC 작업 상태 경로가 올바르지 않습니다.')
        self.root = self.home / STORE_RELATIVE

    def prepare(self):
        wc._safe(self.root, self.home)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        wc._safe(self.root, self.home)
        _private_acl(self.root)

    def path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', name):
            raise ValueError('PC 작업 기록 이름이 올바르지 않습니다.')
        return wc._safe(self.root / name, self.root)

    def read(self, name):
        path = self.path(name)
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise ValueError('PC 작업 기록이 안전하지 않습니다.')
            return wc._json(path, self.root)
        except FileNotFoundError:
            return None

    def save(self, name, value):
        target = self.path(name)
        temporary = self.path('.' + secrets.token_hex(16) + '.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(value, out, ensure_ascii=True, separators=(',', ':'))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self):
        self.prepare()
        fd = os.open(str(self.path('.operation.lock')), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise ValueError('PC 작업 잠금 파일이 안전하지 않습니다.')
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def secret(self):
        found = self.read('proof-key.json')
        if found is None:
            found = {'key': secrets.token_hex(32)}
            self.save('proof-key.json', found)
        if not isinstance(found, dict) or not re.fullmatch(r'[a-f0-9]{64}', found.get('key', '')):
            raise ValueError('PC 세션 확인 키를 읽지 못했습니다.')
        return bytes.fromhex(found['key'])


class NativeProxy(wc.Proxy):
    ALLOWED = wc.Proxy.ALLOWED | {'turn/start', 'turn/interrupt', 'thread/archive'}


def _loaded(client):
    """Bounded pagination: a truncated list never proves a thread is absent."""
    found, cursor, seen = set(), None, set()
    for unused in range(3):
        result = client.call('thread/loaded/list', {'limit': wc.LIMIT, **({'cursor': cursor} if cursor else {})})
        if not isinstance(result, dict) or not isinstance(result.get('data'), list) or any(not isinstance(sid, str) for sid in result['data']):
            raise ValueError('PC 실행 목록 형식이 올바르지 않습니다.')
        found.update(result['data'])
        cursor = result.get('nextCursor')
        if cursor is None:
            return found, True
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(cursor) > 4096:
            raise ValueError('PC 실행 목록 페이지를 확인하지 못했습니다.')
        seen.add(cursor)
    return found, False


def _state(thread):
    turns = thread.get('turns')
    if not isinstance(turns, list):
        raise ValueError('PC 세션 응답 형식이 올바르지 않습니다.')
    known = all(isinstance(t, dict) and t.get('status') in ('inProgress', 'completed', 'interrupted', 'failed') for t in turns)
    active = [t for t in turns if isinstance(t, dict) and t.get('status') == 'inProgress']
    phase = wc._phase(thread.get('status')) if known else 'unknown'
    if phase == 'idle' and active:
        phase = 'unknown'
    return {'phase': phase, 'activeTurnId': active[0].get('id') if len(active) == 1 else None,
            'activeFlags': wc._flags(thread.get('status')), 'runtimeKnown': phase != 'unknown',
            'turns': [{'id': t.get('id'), 'status': t.get('status')} for t in turns[-80:] if isinstance(t, dict)],
            'updatedAt': wc._epoch(thread.get('updatedAt'))}


def _remote(value):
    if isinstance(value, dict):
        return any(key in ('vendorRemote', 'remote') or _remote(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_remote(item) for item in value)
    return value in ('vendorRemote', 'remote') if isinstance(value, str) else False


def _capabilities(thread):
    state = _state(thread)
    remote = _remote(thread.get('source')) or _remote(thread.get('threadSource'))
    supported = thread.get('canAcceptDirectInput') is True and state['runtimeKnown'] and not remote
    return {'canRead': True, 'canSend': supported, 'canAttachImages': False,
            'canAnswerApprovals': False, 'canAnswerQuestions': False,
            'reason': None if supported else '이 PC 세션의 직접 입력 지원을 확인하지 못했습니다. 터미널에서 이어가세요.'}


def _receipt(identity, request_id, thread):
    for turn in reversed(thread.get('turns') or []):
        for item in turn.get('items') or []:
            if isinstance(item, dict) and item.get('type') == 'userMessage' and item.get('clientId') == request_id:
                delivery = turn.get('status')
                return {'source': identity, 'requestId': request_id,
                        'delivery': delivery if delivery in ('completed', 'interrupted', 'failed') else 'accepted',
                        'confirmed': True, 'readbackConfirmed': True, 'turnId': turn.get('id'), 'canRetry': False}
    return {'source': identity, 'requestId': request_id, 'delivery': 'unknown', 'confirmed': False,
            'readbackConfirmed': False, 'turnId': None, 'canRetry': False,
            'reason': 'PC 수신 결과를 확인하지 못했습니다. 같은 입력을 자동으로 다시 보내지 않습니다.'}


def _terminate_windows(expected):
    """Pin one process handle, recheck creation time and executable, then terminate."""
    if os.name != 'nt':
        raise ValueError('Windows 원본 프로세스에서만 종료할 수 있습니다.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x1000 | 0x100000 | 1, False, expected['pid'])
    if not handle:
        raise OSError('원본 프로세스를 열지 못했습니다.')
    try:
        values = [wintypes.FILETIME() for unused in range(4)]
        length, image = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in values]) or not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)):
            raise OSError('원본 프로세스 신원을 확인하지 못했습니다.')
        start = str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        if start != expected['start'] or wc._norm(image.value) != wc._norm(expected['exe']):
            return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 프로세스가 변경되어 종료하지 않았습니다.'}
        if not kernel.TerminateProcess(handle, 0):
            raise OSError('원본 프로세스 종료 요청을 확인하지 못했습니다.')
        confirmed = kernel.WaitForSingleObject(handle, 1500) == 0
        return {'applied': True, 'confirmed': confirmed, 'status': 'terminated' if confirmed else 'unconfirmed',
                'reason': None if confirmed else '종료 요청을 보냈지만 원본 종료를 확인하지 못했습니다.'}
    finally:
        kernel.CloseHandle(handle)


class WindowsRuntime:
    def __init__(self, home=None, reader=None, proxy_factory=NativeProxy, terminate_process=_terminate_windows):
        self.reader = reader or wc.WindowsCollector(home)
        self.home, self.proxy_factory = self.reader.home, proxy_factory
        self.store = Store(self.home)
        self.terminate_process = terminate_process

    def _thread(self, client, identity, cwd):
        loaded, complete = _loaded(client)
        if identity['id'] not in loaded:
            raise ValueError('PC 원본 세션이 현재 서버에 열려 있지 않습니다.')
        result = client.call('thread/read', {'threadId': identity['id'], 'includeTurns': True})
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != identity['id'] or wc._norm(thread.get('cwd')) != wc._norm(cwd):
            raise ValueError('PC 원본 세션의 작업 폴더가 변경되었습니다.')
        _state(thread)
        return thread

    def _codex(self, source, deadline):
        identity = _identity(source)
        rows = self.reader.database_rows(identity['id'])
        if len(rows) != 1 or rows[0].get('archived') or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(rows[0]['cwd']):
            raise ValueError('PC 원본 세션과 작업 폴더를 확인하지 못했습니다.')
        matches, failures = [], False
        for path in self.reader.endpoints():
            if time.monotonic() >= deadline:
                raise TimeoutError('PC 세션 소유 서버 확인 시간이 초과되었습니다.')
            try:
                with self.proxy_factory(self.reader.binary, path, min(deadline, time.monotonic() + 4)) as client:
                    loaded, complete = _loaded(client)
                    if not complete:
                        raise ValueError('PC 서버 전체 실행 목록을 확인하지 못했습니다.')
                    if identity['id'] not in loaded:
                        continue
                    thread = self._thread(client, identity, rows[0]['cwd'])
                state = wc._json(path.parent / 'server-state.json', self.reader.codex_home)
                process = self.reader.process_reader(state.get('server_pid'))
                if not process:
                    raise ValueError('PC 원본 서버 프로세스를 확인하지 못했습니다.')
                matches.append({'source': identity, 'path': str(path), 'cwd': rows[0]['cwd'], 'thread': thread,
                                'server': {'pid': process['pid'], 'start': process['start'], 'exe': wc._norm(process['exe'])}})
            except (OSError, ValueError, TimeoutError):
                failures = True
        if not matches and not failures:
            raise NotLoaded('PC 원본 세션이 현재 서버에 열려 있지 않습니다.')
        if len(matches) != 1 or failures:
            raise ValueError('PC 세션의 정확한 소유 서버를 확인하지 못했습니다.')
        return matches[0]

    def _project_endpoint(self, cwd):
        """The running project server a stored session under ~/Projects resumes into: the one
        named after its Projects folder (any case). With no server of that name, the only
        `<folder>-…` server that no Projects folder of its own name owns (a project whose
        folder name differs). None otherwise, or when that server is not running."""
        root = wc._windows_path(self.reader.home / 'Projects').rstrip('/') + '/'
        path = wc._windows_path(cwd).rstrip('/')
        if not path.casefold().startswith(root.casefold()):
            return None
        folder = path[len(root):].split('/')[0].casefold()
        try:
            names = sorted(entry.name for entry in (self.reader.codex_home / 'agents-servers').iterdir()
                           if entry.is_dir())[:256]
        except OSError:
            return None
        named = [name for name in names if name.casefold() == folder]
        if not named:
            named = [name for name in names if name.casefold().startswith(folder + '-')
                     and not (self.reader.home / 'Projects' / name).exists()]
        return self.reader.endpoint(named[0]) if len(named) == 1 else None

    def _codex_resume(self, source):
        identity = _identity(source)
        rows = self.reader.database_rows(identity['id'])
        if len(rows) != 1 or rows[0].get('archived') or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(rows[0]['cwd']):
            raise ValueError('PC 저장 기록과 작업 폴더를 확인하지 못했습니다.')
        cwd = rows[0]['cwd']
        if not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_dir() or not self.reader.binary.is_file():
            raise ValueError('PC 원본 작업 폴더나 Codex 실행 파일을 확인하지 못했습니다.')
        # Two writers on one thread would interleave its record.
        if self.reader.writer_owners({identity['id']}):
            raise ValueError('이 기록은 PC에서 다른 Codex 실행(codex exec 등)이 쓰고 있습니다. 그 실행이 끝난 뒤 이어가 주세요.')
        endpoint = self._project_endpoint(cwd)
        if endpoint is None:
            # An SSH session is elevated: without its project's own (non-elevated) server, Codex
            # would run an embedded runtime in that session instead.
            raise ValueError('이 기록의 프로젝트 Codex 서버가 PC에서 꺼져 있습니다. 새 세션으로 그 프로젝트를 열어 서버를 켠 뒤 이어갈 수 있습니다.')
        argv = [str(self.reader.binary), 'resume', identity['id'], '--remote', 'unix://' + Path(endpoint).as_posix(), '--cd', cwd]
        return {'argv': argv, 'cwd': cwd, 'env': {'CODEX_HOME': str(self.reader.codex_home)},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': source.get('kind')}, 'connectionMode': 'attach', 'resumed': True}

    def _claude_running(self, sid):
        """Whether a live Claude process on this PC owns the session (its registry record).
        A record that cannot be read while its Claude process lives may be this one: unknown."""
        for path in sorted((self.reader.claude_home / 'sessions').glob('[0-9]*.json'))[:256]:
            if not path.stem.isdigit():
                continue
            try:
                row = wc._json(path, self.reader.claude_home)
            except (OSError, ValueError, TypeError):
                process = self.reader.process_reader(int(path.stem))
                if process and self.reader._claude_executable(process.get('exe')):
                    raise ValueError('PC Claude 실행 기록을 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요.')
                continue
            if not isinstance(row, dict) or row.get('sessionId') != sid:
                continue
            process = self.reader.process_reader(int(path.stem))
            if process and (not isinstance(row.get('procStart'), str) or row.get('procStart') == process.get('start')):
                return True
        return False

    def _claude_resume(self, source):
        """A stored Claude conversation no PC process runs, continued in a board terminal."""
        identity = _identity(source)
        if self._claude_running(identity['id']):
            raise ValueError('이 Claude 세션은 PC 터미널에서 실행 중입니다. 그 터미널에서 이어가세요.')
        path = self.reader._transcripts().get(identity['id'])
        if path is None:
            raise ValueError('PC Claude 기록을 확인하지 못했습니다.')
        cwd = next((entry['cwd'] for entry in wc._head(path) if isinstance(entry.get('cwd'), str)), '')
        if source.get('cwd') and wc._norm(source['cwd']) != wc._norm(cwd):
            raise ValueError('PC Claude 기록의 작업 폴더가 바뀌었습니다.')
        binary = self.reader.home / '.local/bin/claude.exe'
        if not cwd or not Path(wc._windows_path(cwd)).is_dir() or not binary.is_file():
            raise ValueError('PC 원본 작업 폴더나 Claude 실행 파일을 확인하지 못했습니다.')
        return {'argv': [str(binary), '--resume', identity['id']], 'cwd': cwd, 'env': {},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': 'interactive'}, 'connectionMode': 'resume', 'resumed': True}

    def _claude(self, source):
        identity = _identity(source)
        matches = []
        for path in sorted((self.reader.claude_home / 'sessions').glob('[0-9]*.json'))[:256]:
            try:
                row = wc._json(path, self.reader.claude_home)
                if row.get('sessionId') != identity['id']:
                    continue
                pid = int(path.stem)
                process = self.reader.process_reader(pid)
                if (not process or row.get('pid') != pid or type(row.get('pid')) is not int
                        or row.get('procStart') != process.get('start')
                        or not re.fullmatch(r'\d{17,20}', row.get('procStart', ''))
                        or str(row.get('pidDomain', '')).casefold() != 'win32:' + self.reader.hostname.casefold()
                        or row.get('kind') not in ('interactive', 'bg')
                        or not self.reader._claude_executable(process.get('exe'))
                        or source.get('cwd') and wc._norm(source['cwd']) != wc._norm(row.get('cwd'))):
                    raise ValueError('PC Claude 프로세스의 신원이 변경되었습니다.')
                matches.append({'row': row, 'process': process, 'source': identity, 'registry': str(path)})
            except (OSError, TypeError):
                raise ValueError('PC Claude 실행 기록을 확인하지 못했습니다.') from None
        if len(matches) != 1:
            raise ValueError('PC Claude 실행의 정확한 소유 프로세스를 확인하지 못했습니다.')
        return matches[0]

    def chat(self, args):
        source, action = args.get('source'), args.get('action')
        identity = _identity(source)
        if action not in ('read', 'capabilities', 'send', 'receipt'):
            raise ValueError('지원하지 않는 PC 대화 요청입니다.')
        if identity['agent'] != 'codex':
            if action not in ('read', 'capabilities'):
                raise ValueError('Claude 입력은 정확한 세션의 보드 터미널을 통해 보냅니다.')
            result = self.reader.read('claude', identity['id'])
            return {**result, 'capabilities': {'canSend': False, 'canRead': True}}
        if action in ('send', 'receipt') and not REQUEST.fullmatch(args.get('requestId', '')):
            raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
        try:
            observed = self._codex(source, time.monotonic() + 12)
        except (OSError, ValueError, TimeoutError):
            if action not in ('read', 'capabilities'):
                raise
            return {**self.reader.read('codex', identity['id'], '.codex'),
                    'capabilities': {'canSend': False, 'canRead': True, 'reason': '현재 실행 중인 PC 원본 서버를 확인하지 못했습니다.'}}
        thread = observed['thread']
        if action == 'send':
            return self._send(args, observed)
        supplied_ids = args.get('requestIds') or []
        if not isinstance(supplied_ids, list) or len(supplied_ids) > 32:
            raise ValueError('PC 메시지 확인 목록이 올바르지 않습니다.')
        request_ids = list(supplied_ids)
        if args.get('requestId') is not None:
            request_ids.insert(0, args['requestId'])
        if any(not isinstance(value, str) or not REQUEST.fullmatch(value) for value in request_ids):
            raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
        request_ids = list(dict.fromkeys(request_ids))
        if action == 'receipt':
            request_ids = [args['requestId']]
        receipts = []
        for request_id in request_ids:
            if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
                raise ValueError('PC 메시지 요청 식별자가 올바르지 않습니다.')
            receipt = _receipt(identity, request_id, thread)
            if not receipt['confirmed'] and self.store.root.exists():
                saved = self.store.read('chat-' + _digest([identity, request_id]) + '.json')
                if saved and saved.get('result'):
                    receipt = saved['result']
            receipts.append(receipt)
        capabilities = {**_capabilities(thread), 'canLaunch': True}
        messages, older = wc._recent_window(wc._codex_rows(thread['turns']))
        result = {**_state(thread), 'messages': messages, 'olderOmitted': older, 'cwd': observed['cwd'],
                  'capabilities': capabilities, 'readOnly': not capabilities['canSend'], 'receipts': receipts}
        if action == 'receipt':
            result.update(receipts[0])
        return result

    def _send(self, args, observed):
        identity, request_id = observed['source'], args['requestId']
        inputs = args.get('input')
        if (not isinstance(inputs, list) or not 1 <= len(inputs) <= 16 or args.get('attachmentIds')
                or any(not isinstance(item, dict) or set(item) != {'type', 'text'} or item.get('type') != 'text'
                       or not isinstance(item.get('text'), str) for item in inputs)
                or not any(item['text'].strip() for item in inputs)
                or sum(len(item['text'].encode()) for item in inputs) > 32768):
            raise ValueError('PC 입력은 32 KiB 이하의 텍스트를 지원합니다.')
        digest, name = _digest(inputs), 'chat-' + _digest([identity, request_id]) + '.json'
        with self.store.lock():
            prior = self.store.read(name)
            if prior:
                if prior.get('digest') != digest:
                    raise ValueError('같은 요청 ID로 다른 메시지를 보낼 수 없습니다.')
                return prior['result']
            with self.proxy_factory(self.reader.binary, observed['path'], time.monotonic() + 10) as client:
                self._verify_endpoint(observed)
                thread = self._thread(client, identity, observed['cwd'])
                before = _receipt(identity, request_id, thread)
                if before['confirmed']:
                    return before
                caps = _capabilities(thread)
                if not caps['canSend']:
                    return {**before, 'delivery': 'rejected', 'reason': caps['reason']}
                self.store.save(name, {'digest': digest, 'result': before})
                result = before
                try:
                    response = client.call('turn/start', {'threadId': identity['id'], 'input': inputs, 'clientUserMessageId': request_id})
                    turn = response.get('turn') if isinstance(response, dict) else None
                    if isinstance(turn, dict) and isinstance(turn.get('id'), str):
                        result = {**before, 'delivery': 'accepted', 'confirmed': True, 'acknowledged': True, 'turnId': turn['id'], 'reason': None}
                    after = self._thread(client, identity, observed['cwd'])
                    confirmed = _receipt(identity, request_id, after)
                    if confirmed['confirmed']:
                        result = confirmed
                except (OSError, ValueError, TimeoutError):
                    pass
                self.store.save(name, {'digest': digest, 'result': result})
                return result

    def _verify_endpoint(self, observed):
        path = Path(observed['path'])
        current = self.reader.endpoint(path.parent.name)
        state = wc._json(path.parent / 'server-state.json', self.reader.codex_home)
        process = self.reader.process_reader(state.get('server_pid'))
        key = {'pid': process['pid'], 'start': process['start'], 'exe': wc._norm(process['exe'])} if process else None
        if current is None or wc._norm(current) != wc._norm(path) or key != observed['server']:
            raise ValueError('PC 원본 서버가 변경되었습니다. 다시 확인해 주세요.')

    def _observe(self, source):
        identity = _identity(source)
        capabilities = {name: {'supported': False, 'method': None, 'reason': '원본 실행 상태를 확인하지 못했습니다.'} for name in ('interrupt', 'terminate')}
        try:
            if identity['agent'] == 'codex':
                observed = self._codex(source, time.monotonic() + 12)
                state = _state(observed['thread'])
                can_interrupt = state['activeTurnId'] is not None and state['phase'] in ('working', 'needs_input')
                can_end = state['runtimeKnown']
                capabilities = {
                    'interrupt': {'supported': can_interrupt, 'method': 'codex-turn-interrupt', 'reason': None if can_interrupt else '진행 중인 응답이 없습니다.'},
                    'terminate': {'supported': can_end, 'method': 'codex-thread-archive', 'reason': None if can_end else 'PC 실행 상태를 확인하지 못했습니다.',
                                  'effect': ARCHIVE_EFFECT, 'scope': 'session-and-descendants', 'scopeReadback': 'selected-session'}}
                target = {'identity': identity, 'path': observed['path'], 'cwd': wc._norm(observed['cwd']),
                          'server': observed['server'], 'turnId': state['activeTurnId'], 'phase': state['phase']}
                return {**observed, 'target': target, 'phase': state['phase'], 'capabilities': capabilities}
            observed = self._claude(source)
            row = observed['row']
            phase = {'busy': 'working', 'idle': 'idle', 'waiting': 'needs_input'}.get(row.get('status'), 'unknown')
            capabilities['interrupt']['reason'] = '이 Claude 실행 방식은 응답만 중단하는 제어를 제공하지 않습니다.'
            capabilities['terminate'] = {'supported': True, 'method': 'claude-cli-process', 'reason': None,
                'effect': '선택한 Claude CLI 프로세스를 종료합니다. 대화 기록은 유지되며 별도 하위 도구 프로세스의 종료는 확인하지 않습니다.',
                'scope': 'process'}
            target = {'identity': identity, 'process': observed['process'], 'registry': observed['registry'], 'kind': row['kind'], 'cwd': wc._norm(row.get('cwd'))}
            return {**observed, 'target': target, 'phase': phase, 'capabilities': capabilities}
        except (OSError, ValueError, TimeoutError):
            return {'source': identity, 'target': None, 'phase': 'unknown', 'capabilities': capabilities}

    def _view(self, observed):
        payload = {'source': observed['source'], 'revision': _digest(observed['target']),
                   'expiresAt': int(time.time()) + PROOF_TTL, 'nonce': secrets.token_hex(12)}
        data = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        proof = data + '.' + hmac.new(self.store.secret(), data.encode(), hashlib.sha256).hexdigest()
        return {**{key: payload[key] for key in ('source', 'revision', 'expiresAt')}, 'phase': observed['phase'],
                'capabilities': observed['capabilities'], 'targetProof': proof}

    def preview(self, source):
        observed = self._observe(source)
        with self.store.lock():
            return self._view(observed)

    def execute(self, args):
        source, action, request_id, proof = args.get('source'), args.get('action'), args.get('requestId'), args.get('targetProof')
        identity = _identity(source)
        if action not in ('interrupt', 'terminate') or not isinstance(request_id, str) or not CONTROL_REQUEST.fullmatch(request_id):
            raise ValueError('PC 세션 제어 요청이 올바르지 않습니다.')
        if not isinstance(proof, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,3900}\.[a-f0-9]{64}', proof):
            raise ValueError('PC 세션 확인 정보가 올바르지 않습니다.')
        digest, name = _digest([identity, action, proof]), 'control-' + _digest(request_id) + '.json'
        with self.store.lock():
            prior = self.store.read(name)
            if prior:
                if prior.get('digest') != digest:
                    raise ValueError('같은 요청 ID로 다른 세션을 제어할 수 없습니다.')
                return {**prior['result'], 'replayed': True}
            data, signature = proof.split('.')
            if not hmac.compare_digest(signature, hmac.new(self.store.secret(), data.encode(), hashlib.sha256).hexdigest()):
                raise ValueError('PC 세션 확인 정보가 변경되었습니다.')
            payload = json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))
            if payload.get('source') != identity or type(payload.get('expiresAt')) is not int or not time.time() <= payload['expiresAt'] <= time.time() + PROOF_TTL + 2:
                raise ValueError('PC 세션 확인이 만료되었습니다. 다시 확인해 주세요.')
            proof_name = 'used-' + _digest(proof) + '.json'
            used = self.store.read(proof_name)
            if used:
                if used['result'].get('action') != action:
                    raise ValueError('이미 사용한 세션 확인 정보입니다.')
                return {**used['result'], 'replayed': True}
            observed = self._observe(source)
            result = {'source': identity, 'action': action, 'requestId': request_id, 'confirmed': False, 'applied': False, 'status': 'rejected'}
            if payload.get('revision') != _digest(observed['target']):
                return {**result, 'status': 'stale', 'reason': '원본 실행이 변경되었습니다. 다시 확인해 주세요.', 'preview': self._view(observed)}
            cap = observed['capabilities'][action]
            if not cap['supported']:
                return {**result, 'reason': cap['reason']}
            pending = {**result, 'applied': None, 'status': 'unconfirmed', 'reason': 'PC 실행 결과를 확인하지 못했습니다. 자동 재실행하지 않습니다.'}
            self.store.save(name, {'digest': digest, 'result': pending})
            self.store.save(proof_name, {'result': pending})
            try:
                details = self._apply(observed, action)
                result.update(details)
            except (OSError, ValueError, TimeoutError):
                result = pending
            self.store.save(name, {'digest': digest, 'result': result})
            self.store.save(proof_name, {'result': result})
            return result

    def _apply(self, observed, action):
        identity = observed['source']
        if identity['agent'] == 'claude':
            fresh = self._claude({**identity, 'cwd': observed['row'].get('cwd')})
            if fresh['process'] != observed['process']:
                return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 프로세스가 변경되어 종료하지 않았습니다.'}
            return {**self.terminate_process(fresh['process']), 'scope': 'process',
                    'effect': observed['capabilities']['terminate']['effect'], 'descendantsConfirmed': False}
        self._verify_endpoint(observed)
        with self.proxy_factory(self.reader.binary, observed['path'], time.monotonic() + 10) as client:
            fresh = self._thread(client, identity, observed['cwd'])
            state = _state(fresh)
            if state['activeTurnId'] != observed['target']['turnId'] or state['phase'] != observed['phase']:
                return {'applied': False, 'confirmed': False, 'status': 'stale', 'reason': '원본 응답이 변경되어 제어하지 않았습니다.'}
            if action == 'interrupt':
                turn = state['activeTurnId']
                client.call('turn/interrupt', {'threadId': identity['id'], 'turnId': turn})
                after = self._thread(client, identity, observed['cwd'])
                exact = next((t for t in after['turns'] if t.get('id') == turn), None)
                confirmed = bool(exact and exact.get('status') in ('interrupted', 'completed', 'failed'))
            else:
                client.call('thread/archive', {'threadId': identity['id']})
                loaded, complete = _loaded(client)
                rows = self.reader.database_rows(identity['id'])
                confirmed = (complete and identity['id'] not in loaded and len(rows) == 1
                             and rows[0].get('archived') == 1 and wc._norm(rows[0].get('cwd')) == wc._norm(observed['cwd']))
            return {'applied': True, 'confirmed': confirmed, 'status': ('interrupted' if action == 'interrupt' else 'terminated') if confirmed else 'unconfirmed',
                    'reason': None if confirmed else 'PC 요청을 보냈지만 원본 상태 변경을 확인하지 못했습니다.',
                    **({'effect': ARCHIVE_EFFECT, 'scope': 'session-and-descendants', 'scopeReadback': 'selected-session',
                        'descendantsConfirmed': False} if action == 'terminate' else {})}

    def terminal_plan(self, source):
        identity = _identity(source)
        if identity['agent'] == 'codex':
            try:
                observed = self._codex(source, time.monotonic() + 12)
            except NotLoaded:
                return self._codex_resume(source)
            self._verify_endpoint(observed)
            argv = [str(self.reader.binary), 'resume', identity['id'], '--remote', 'unix://' + Path(observed['path']).as_posix(), '--cd', observed['cwd']]
            cwd = observed['cwd']
        else:
            # No process runs it (a background run's record may outlive it): it continues here.
            if not self._claude_running(identity['id']):
                return self._claude_resume(source)
            observed = self._claude(source)
            if observed['row'].get('kind') != 'bg':
                raise ValueError('이 Claude 세션은 원래 대화형 터미널에서 실행 중입니다. 정확한 연결을 지원하는 background 세션만 열 수 있습니다.')
            # The verified owner's registry record names its job. Closing the board terminal
            # ends only this attach client; the run keeps going.
            job = observed['row'].get('jobId')
            if not isinstance(job, str) or not CLAUDE_JOB.fullmatch(job):
                raise ValueError('이 PC Claude background 세션의 터미널 연결 식별자를 확인하지 못했습니다. 원본 PC 터미널에서 이어가세요.')
            binary = self.reader.home / '.local/bin/claude.exe'
            if not binary.is_file():
                raise ValueError('PC Claude 실행 파일을 확인하지 못했습니다.')
            argv, cwd = [str(binary), 'attach', job], observed['row'].get('cwd')
        if not isinstance(cwd, str) or not Path(wc._windows_path(cwd)).is_dir():
            raise ValueError('PC 원본 작업 폴더를 확인하지 못했습니다.')
        return {'argv': argv, 'cwd': cwd, 'env': {'CODEX_HOME': str(self.reader.codex_home)} if identity['agent'] == 'codex' else {},
                'nativeSource': {**identity, 'cwd': cwd, 'kind': source.get('kind')}, 'connectionMode': 'attach'}

    def resume_preview(self, source):
        identity = _identity(source)
        try:
            if identity['agent'] != 'codex':
                self._claude_resume(source)
            else:
                try:
                    self._codex(source, time.monotonic() + 12)
                except NotLoaded:
                    self._codex_resume(source)
            return {'source': identity, 'allowed': True, 'blockedByBoard': False, 'reason': None}
        except (OSError, ValueError, TimeoutError) as exc:
            return {'source': identity, 'allowed': False, 'blockedByBoard': False,
                    'reason': str(exc) or '이 PC 기록을 다시 열 수 없습니다.'}


def handle(request):
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('PC 작업 요청 형식이 올바르지 않습니다.')
    runtime, args = WindowsRuntime(), request['args']
    op = request.get('op')
    if op == 'chat':
        return runtime.chat(args)
    if op == 'control_preview':
        return runtime.preview(args.get('source'))
    if op == 'control_execute':
        return runtime.execute(args)
    if op == 'terminal_plan':
        return runtime.terminal_plan(args.get('source'))
    if op == 'resume_preview':
        return runtime.resume_preview(args.get('source'))
    raise ValueError('지원하지 않는 PC 작업 요청입니다.')
