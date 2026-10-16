"""Bounded SSH transfer coordinator for registered machines and fresh workspaces."""
import base64
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import stat
from contextlib import contextmanager
import threading
import time
import zipfile

import instance
from launch import scrub_text
import settings

MAX_ARCHIVE = 128 * 1024 * 1024
# PowerShell ends a single-quoted string at any of these (not only '); doubled, each stays literal.
PS_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"
# The longest remote command a PC terminal is opened with (cmd.exe, a possible default shell,
# stops at 8191 characters); a longer menu would end without saying why.
WINDOWS_COMMAND_LIMIT = 8000
# What a PC takes over windows_rpc. An upload (`attachment`) stays one program on stdin: its
# base64 goes in literal lines of this length (Python 3.9's tokenizer grows its buffer a little
# per read of one long line), and its wait is the caller's (Chat.upload), as on a Mac.
WINDOWS_OPS = ('chat', 'control_preview', 'control_execute', 'terminal_plan', 'resume_preview', 'attachment')
WINDOWS_DATA_LINE = 4096
WINDOWS_UPLOAD_SECONDS = 120
BASE64 = re.compile(r'[A-Za-z0-9+/]*={0,2}\Z')
PROFILE_FAILURE_TTL = 120
RUNTIME_MODULES = ('collector.py', 'launch.py', 'transfer_native.py', 'transfer_workspace.py', 'transfer_worker.py',
                   'native_chat.py', 'attachments.py', 'managed_launch.py', 'settings.py', 'instance.py',
                   'agent_router.py', 'session_control.py', 'launcher_programs.py')
# Prepare stages before a native session is bound; a failure there started nothing.
PRE_BIND_STAGES = frozenset(('validate', 'import', 'build'))
UNKNOWN_PREPARE = ('대상 준비 결과를 확인하지 못했습니다. 이미 작업이 시작되었을 수 있으므로 '
                   '대상 기기의 작업 목록과 이전 기록을 확인해 주세요.')
UNKNOWN_HANDOFF = ('원격 기기의 인계 준비 결과를 확인하지 못했습니다. 새 작업이 이미 시작됐을 수 있으므로 '
                   '다시 실행하지 말고 그 기기의 작업 목록을 먼저 확인해 주세요.')
# Only this versioned helper is installed. Credentials, dotfile profiles and hooks are not copied.
# A call normally carries only the helper's SHA-256. When `worker-<sha>.pyz` is missing or
# corrupt the bootstrap answers {"needRuntime": true} without running anything, and the
# coordinator sends the full helper, which is verified and replaced atomically.
# A call runs the helper from its folder `worker-<sha>/`, unpacked once from the verified
# archive, where Python keeps the compiled modules (__pycache__): from the archive itself every
# call compiled each module again. The folder is written in a temporary folder and renamed into
# place, so two first calls racing never use half of one. The archive is hashed again only when
# its inode, size or mtime differs from those recorded in the folder (`.verified`) at the last
# check, and is still marked as used, at most hourly. Its path stays the helper's path (a
# terminal step runs the archive). A folder that cannot be prepared: the call runs from the
# archive as before. Other versions unused for a day are removed, a version's folder with its
# archive. The runtime folder (instance `remote.runtimeDir`, under the remote home) is filled
# in once at import.
_BOOTSTRAP = r'''
import base64,hashlib,io,json,os,pathlib,re,stat,sys,time
p=json.load(sys.stdin); sha=p['sha256']; limit=2*1024*1024
assert re.fullmatch('[0-9a-f]{64}',sha)
data=base64.b64decode(p['runtime'],validate=True) if 'runtime' in p else None
assert data is None or (len(data)<limit and hashlib.sha256(data).hexdigest()==sha)
root=pathlib.Path.home()/@RUNTIME_DIR@
for d in (root,)+tuple(root.parents):
 if d.exists(): assert not d.is_symlink() and d.is_dir()
root.mkdir(parents=True,exist_ok=True,mode=0o700)
assert root.stat().st_uid==os.getuid();root.chmod(0o700)
path=root/('worker-'+sha+'.pyz');unpacked=root/('worker-'+sha);stamp=unpacked/'.verified'
def mine(s,kind): return kind(s.st_mode) and s.st_uid==os.getuid()
def mark(s): return '%d %d %d'%(s.st_ino,s.st_size,s.st_mtime_ns)
def recorded():
 try:
  if not mine(os.lstat(str(unpacked)),stat.S_ISDIR): return None
  with os.fdopen(os.open(str(stamp),os.O_RDONLY|os.O_NOFOLLOW)) as f: return f.read(100)
 except (OSError,ValueError): return None
known=recorded()
def valid():
 # (its stat, its bytes when hashed now), or None when missing or not this helper.
 try: fd=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW)
 except OSError: return None
 with os.fdopen(fd,'rb') as f:
  s=os.fstat(f.fileno());raw=None
  if not mine(s,stat.S_ISREG): return None
  if mark(s)!=known:
   raw=f.read(limit+1)
   if hashlib.sha256(raw).hexdigest()!=sha: return None
  # Used: another version's sweep keeps it (a day), touched through the checked descriptor.
  if raw is not None or time.time()-s.st_mtime>3600:
   try: os.utime(f.fileno());s=os.fstat(f.fileno())
   except OSError: pass
  return s,raw
v=valid()
if v is None:
 if data is None:
  sys.stdout.write(json.dumps({'ok':False,'needRuntime':True,'sha256':sha}));sys.stdout.flush();sys.exit(0)
 tmp=root/('.worker-'+sha+'.'+os.urandom(6).hex()+'.tmp')
 fd=os.open(str(tmp),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 try:
  with os.fdopen(fd,'wb') as f: f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(str(tmp),str(path))
 finally:
  if os.path.lexists(str(tmp)): os.unlink(str(tmp))
 v=valid()
 assert v is not None
s,raw=v
try:
 old=time.time()-86400;listed=os.listdir(str(root))
 for n in listed:
  if n!=path.name and re.fullmatch(r'worker-[0-9a-f]{64}\.pyz|\.worker-[0-9a-f]{64}\.[0-9a-f]{12}\.tmp',n):
   e=os.lstat(str(root/n))
   if mine(e,stat.S_ISREG) and e.st_mtime<old: os.unlink(str(root/n))
 for n in listed:
  # A version's folder goes with its archive; one an unpacking left (.dir) after a day.
  if n!=unpacked.name and re.fullmatch(r'worker-[0-9a-f]{64}|\.worker-[0-9a-f]{64}\.[0-9a-f]{12}\.dir',n):
   e=os.lstat(str(root/n))
   if mine(e,stat.S_ISDIR) and (e.st_mtime<old if n[0]=='.' else not os.path.lexists(str(root/(n+'.pyz')))):
    import shutil;shutil.rmtree(str(root/n),ignore_errors=True)
except OSError: pass
def whole():
 import zipfile
 try:
  if not mine(os.lstat(str(unpacked)),stat.S_ISDIR): return False
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   for i in z.infolist():
    e=os.lstat(str(unpacked/i.filename))
    if not mine(e,stat.S_ISREG) or e.st_size!=i.file_size: return False
 except OSError: return False
 return True
def drop(q):
 import shutil
 if mine(os.lstat(str(q)),stat.S_ISDIR): shutil.rmtree(str(q),ignore_errors=True)
 else: os.unlink(str(q))
def unpack():
 import zipfile
 tmp=root/('.worker-'+sha+'.'+os.urandom(6).hex()+'.dir');os.mkdir(str(tmp),0o700)
 try:
  with zipfile.ZipFile(io.BytesIO(raw)) as z:
   for i in z.infolist():
    assert re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',i.filename)
    fd=os.open(str(tmp/i.filename),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(z.read(i));f.flush();os.fsync(f.fileno())
  try: os.replace(str(tmp),str(unpacked))
  except OSError:
   if whole(): return
   # Not whole (or not this user's folder): moved aside, never used.
   aside=root/('.worker-'+sha+'.'+os.urandom(6).hex()+'.dir');os.replace(str(unpacked),str(aside))
   try: os.replace(str(tmp),str(unpacked))
   finally: drop(aside)
 finally:
  if os.path.lexists(str(tmp)): drop(tmp)
here=None
try:
 if raw is not None and not whole(): unpack()
 if mark(s)!=known:
  t=unpacked/('.verified.'+os.urandom(6).hex())
  try:
   with os.fdopen(os.open(str(t),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'w') as f: f.write(mark(s))
   os.replace(str(t),str(stamp))
  finally:
   if os.path.lexists(str(t)): os.unlink(str(t))
 blob=(unpacked/'instance.json').read_bytes();here=unpacked
except Exception: pass
sys.argv=[str(path)];sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(p['request']).encode()))
if here is None:
 import runpy;sys.path.insert(0,str(path));runpy.run_path(str(path),run_name='__main__')
else:
 # macOS's python3 writes no compiled modules by default: in this folder (this user's, private) it does.
 sys.dont_write_bytecode=False
 # The board's names (instance.json), never this host's own instance file or environment.
 import importlib.machinery,types
 m=types.ModuleType('instance');m.__file__=str(here/'instance.py');m.INJECTED=blob;sys.modules['instance']=m
 sys.path.insert(0,str(here));importlib.machinery.SourceFileLoader('instance',m.__file__).exec_module(m)
 import transfer_worker;transfer_worker.main()
'''
BOOTSTRAP = _BOOTSTRAP.replace('@RUNTIME_DIR@', repr(instance.current()['remote']['runtimeDir']), 1)


class WorkerError(RuntimeError):
    """The helper ran to completion and explicitly reported this failure.

    Transport failures (SSH, timeouts, crashes, unreadable replies) are plain
    RuntimeError: their remote effect is unknown. `stage` is the helper's
    reported step, when it gives one.
    """
    def __init__(self, message, stage=None):
        super().__init__(message)
        self.stage = stage


def _save(path, value):
    from server import private_json
    private_json(path, value)


def ps_quote(value):
    """A PowerShell single-quoted string holding exactly `value`."""
    return "'" + "".join(c + c if c in PS_SINGLE_QUOTES else c for c in value) + "'"


def windows_literal_argument(value):
    """Whether Windows PowerShell 5.1 passes `value` to a program unchanged: it drops a double
    quote, and a quoted argument ending in a backslash would escape its closing quote."""
    return '"' not in value and not (any(c.isspace() for c in value) and value.endswith('\\'))


def windows_encoded_command(script):
    return 'powershell.exe -NoProfile -EncodedCommand ' + base64.b64encode(script.encode('utf-16le')).decode('ascii')


def windows_launcher_script(launcher):
    """A PC's 새 세션: a numbered menu the board shows as buttons (`  1. 이름` rows, then
    `선택:`; one choice runs at once), then the chosen command in its folder. Only those
    literal values run; the script travels base64-encoded, so nothing in it is parsed again
    on the way."""
    quote = ps_quote
    rows = ['@{Label=%s; Exe=%s; Args=@(%s); Cwd=%s}' % (
        quote(choice['label']), quote(choice['command'][0]), ', '.join(quote(arg) for arg in choice['command'][1:]),
        quote(choice.get('cwd') or '')) for choice in launcher['choices']]
    run = ("if ($c.Cwd) { Set-Location -LiteralPath $c.Cwd } else { Set-Location -LiteralPath $env:USERPROFILE }; "
           "& $c.Exe @($c.Args); exit $LASTEXITCODE")
    if len(rows) == 1:
        return "$ErrorActionPreference='Stop'; $c=" + rows[0] + "; " + run
    return ("$ErrorActionPreference='Stop'; $choices=@(" + ', '.join(rows) + "); "
            "Write-Host " + quote(launcher['label']) + "; "
            "for ($i=0; $i -lt $choices.Count; $i++) { Write-Host ('  {0}. {1}' -f ($i+1), $choices[$i].Label) }; "
            "Write-Host '  q. 닫기'; $n=0; "
            "while ($true) { $pick=Read-Host '선택'; if ($pick -eq 'q') { exit 0 }; "
            "if ([int]::TryParse($pick, [ref]$n) -and $n -ge 1 -and $n -le $choices.Count) { break }; "
            "Write-Host '번호를 다시 골라 주세요.' }; $c=$choices[$n-1]; " + run)


class Transfers:
    def __init__(self, hosts, state_dir):
        self.hosts = {h['name']: h for h in hosts}
        self.base_dir = Path(state_dir)
        self.state_dir = self.base_dir / 'transfers'
        self.runtime_paths = {}
        # host name -> whether this process's helper SHA is known to be installed there
        self.runtime_installed = {}
        self.profile_cache = {}
        self.launcher_cache = {}
        # host name -> the tmux its helper keeps runs in (reported with profiles), or None
        self.hold_support = {}
        # This board's mark in the names of the runs it holds, so its sweep never takes another
        # board's (a development board, a canary) on the same host.
        self.hold_tag = hashlib.sha256(str(self.base_dir.resolve()).encode()).hexdigest()[:8]
        self.hold_waiting = self.base_dir / 'hold-waiting'
        self.profile_failures = {}
        # Hosts whose expired account list is being read again in the background.
        self.profile_reading = set()
        self.lock = threading.RLock()
        self._runtime = None

    def runtime(self):
        if self._runtime is None:
            root = Path(__file__).resolve().parent
            output = io.BytesIO()
            # Fixed timestamps and attributes: the same sources yield the same SHA,
            # so restarts reuse the installed helper instead of adding versions.
            # The helper carries the board's names as `instance.json`; remote hosts never
            # read their own instance file.
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, data in [(name, (root / name).read_bytes()) for name in RUNTIME_MODULES] + [
                        (instance.FILENAME, instance.remote_blob().encode('utf-8')),
                        ('__main__.py', b'from transfer_worker import main\nmain()\n')]:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    z.writestr(info, data)
            data = output.getvalue()
            self._runtime = {'runtime': base64.b64encode(data).decode('ascii'), 'sha256': hashlib.sha256(data).hexdigest()}
        return self._runtime

    def control_options(self):
        """Reuse one SSH connection per host; the socket folder rule is shared with collection."""
        from server import prepare_ssh_control_dir, ssh_control_options
        prepare_ssh_control_dir(self.base_dir)
        return ssh_control_options(self.base_dir)

    @staticmethod
    def terminal_options():
        """A terminal holds its own SSH connection that outlasts about two minutes of silence.

        On the shared connection a terminal lived and died with collection's 15 s keepalive:
        a laptop lid closed for 20 s ended every terminal of that host at once."""
        return ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'ServerAliveInterval=15',
                '-o', 'ServerAliveCountMax=8', '-o', 'ControlMaster=no', '-o', 'ControlPath=none']

    def command(self, host, args, tty=False):
        if host.get('platform') == 'windows':
            from server import WINDOWS_READ_ONLY
            raise ValueError(WINDOWS_READ_ONLY)
        python = host.get('python') or '/usr/bin/python3'
        if host.get('local'):
            return [sys.executable] + args
        command = 'exec ' + shlex.join([python] + args)
        if tty:
            return self._ssh_tty(host, command)
        return [shutil.which('ssh') or '/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2'] + self.control_options() + ['--', host['ssh'], command]

    def terminal(self, host, args):
        """A board terminal's command for `args` (['-I', helper, step...]) and its hold name.

        On a Mac whose helper keeps runs in its own tmux, the step runs there under a new
        name (`hold`), and the command is this board's reconnect loop (terminal.py
        --hold-loop): a dropped connection or a sleeping host does not end the run, and the
        board attaches again (`reattach`, which never starts anything). Elsewhere it is the
        plain SSH command and no name."""
        from transfer_worker import HOLD_SOCKET
        with self.lock:
            tmux = self.hold_support.get(host['name'])
        if host.get('local') or host.get('platform') == 'windows' or not tmux:
            return self.command(host, args, tty=True), None
        if len(args) < 3 or args[0] != '-I':
            raise ValueError('기기 도우미 명령이 올바르지 않습니다.')
        name = 'sh-' + self.hold_tag + secrets.token_hex(4)
        first = self.command(host, ['-I', args[1], 'hold', name] + list(args[2:]), tty=True)
        # Attaching again needs only that host's tmux: never a helper file a later deploy removes.
        # The exact target is quoted by hand: shlex leaves "=name" bare, and zsh (a Mac's login
        # shell) reads a word starting with "=" as a command path.
        again = self._ssh_tty(host, 'exec ' + shlex.join([tmux, '-L', HOLD_SOCKET, '-f', '/dev/null',
                                                          'attach-session', '-t']) + " '=" + name + "'")
        loop = [sys.executable, '-I', str(Path(__file__).resolve().with_name('terminal.py')), '--hold-loop',
                json.dumps({'first': first, 'again': again, 'wait': str(self.hold_waiting / name)})]
        return loop, name

    def holds_mine(self, name):
        """Whether a hold on a host is one this board made (its sweep ends no other's)."""
        return isinstance(name, str) and name.startswith('sh-' + self.hold_tag)

    def _ssh_tty(self, host, remote):
        return [shutil.which('ssh') or '/usr/bin/ssh', '-tt'] + self.terminal_options() + ['--', host['ssh'], remote]

    def _exchange(self, host, request, timeout, full):
        """One bootstrap run; `full` adds the helper archive, otherwise only its SHA-256 goes."""
        runtime = self.runtime()
        payload = {'sha256': runtime['sha256'], 'request': request}
        if full:
            payload['runtime'] = runtime['runtime']
        import collector
        try:
            result = subprocess.run(self.command(host, ['-I', '-c', BOOTSTRAP]),
                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, env=collector.tool_env())
        except subprocess.TimeoutExpired:
            raise RuntimeError('기기 응답 시간이 초과됐습니다. 이전 상태를 확인한 뒤 다시 시도해 주세요.') from None
        if result.returncode == 255 and not host.get('local'):
            raise RuntimeError('기기의 이전 도우미에 연결하지 못했습니다. SSH 연결을 확인해 주세요.')
        if result.returncode:
            lines = result.stderr.decode('utf-8', 'replace').strip().splitlines()
            detail = ''.join(c if c.isprintable() else ' ' for c in lines[-1]).strip() if lines else ''
            raise RuntimeError('원격 실행 오류: ' + (scrub_text(detail)[:300] or '종료 코드 %d' % result.returncode))
        try: data = json.loads(result.stdout)
        except (ValueError, UnicodeError): raise RuntimeError('기기 응답을 읽지 못했습니다.') from None
        if not isinstance(data, dict): raise RuntimeError('기기 응답을 읽지 못했습니다.')
        return data

    def _needs_runtime(self, data):
        return data.get('needRuntime') is True and data.get('sha256') == self.runtime()['sha256']

    def windows_rpc(self, host, op, args, timeout):
        """Send reviewed code and one request on stdin; user input never enters argv.

        An upload's file (`data`, base64) is kept out of the request's JSON: checked to be
        base64 alone, it follows the modules as a list of short literal lines."""
        from server import WINDOWS_READ_ONLY, collector_command, SESSION_ID, HOME_KEY
        if op not in WINDOWS_OPS:
            raise ValueError(WINDOWS_READ_ONLY)
        source = args.get('source') or {}
        agent, sid, home = source.get('agent'), source.get('id'), source.get('home')
        if (agent not in ('codex', 'claude') or not isinstance(sid, str) or not SESSION_ID.fullmatch(sid)
                or agent == 'codex' and (not isinstance(home, str) or not HOME_KEY.fullmatch(home))):
            raise ValueError('Windows 세션 정보를 확인해 주세요.')
        if op == 'chat' and args.get('action') not in ('read', 'capabilities', 'send', 'receipt'):
            raise ValueError('지원하지 않는 Windows 대화 요청입니다.')
        data = None
        if op == 'attachment':
            from attachments import MAX_FILE_BYTES
            data = args.get('data')
            if (not isinstance(data, str) or len(data) % 4 or len(data) > ((MAX_FILE_BYTES + 2) // 3) * 4
                    or not BASE64.fullmatch(data) or any(not isinstance(args.get(key), str) for key in ('cwd', 'name', 'sha256'))):
                raise ValueError('PC에 보낼 첨부 파일 정보가 올바르지 않습니다.')
            args = {key: value for key, value in args.items() if key != 'data'}
        args = {**args, 'source': {**source, 'host': host['name']}}
        root = Path(__file__).resolve().parent
        program = 'import types,sys,json\n'
        for name in ('windows_collector', 'windows_attachments', 'windows_runtime'):
            program += ("m=types.ModuleType(" + repr(name) + ");sys.modules[" + repr(name) + "]=m\nexec("
                        + ascii((root / (name + '.py')).read_text(encoding='utf-8')) + ",m.__dict__)\n")
        program += 'm.STORE_RELATIVE=' + ascii(instance.current()['remote']['chatLedgerDir'] + '/windows') + '\n'
        program += ("sys.modules['windows_attachments'].DIRECTORY="
                    + ascii(instance.current()['remote']['attachmentsDir']) + '\n')
        if data is not None:
            program += 'DATA=[\n' + ''.join("'" + data[start:start + WINDOWS_DATA_LINE] + "',\n"
                                              for start in range(0, len(data), WINDOWS_DATA_LINE)) + ']\n'
        program += ('try:\n result=m.handle(json.loads(' + ascii(json.dumps({'op': op, 'args': args})) + ')'
                    + (',DATA' if data is not None else '') + ')\n'
                    ' print(json.dumps({"ok":True,"result":result},ensure_ascii=True))\n'
                    'except ValueError as exc:\n print(json.dumps({"ok":False,"error":str(exc)},ensure_ascii=True))\n'
                    'except Exception:\n print(json.dumps({"ok":False,"unknown":True}))\n')
        try:
            done = subprocess.run(collector_command(host, [], self.base_dir), input=program.encode(),
                                  capture_output=True,
                                  timeout=min(timeout, WINDOWS_UPLOAD_SECONDS if data is not None else 30))
        except subprocess.TimeoutExpired:
            raise RuntimeError('PC 응답을 확인하지 못했습니다. 요청은 자동으로 다시 보내지 않습니다.') from None
        if done.returncode:
            raise RuntimeError('PC 연결 결과를 확인하지 못했습니다. 현재 상태를 다시 확인해 주세요.')
        try:
            data = json.loads(done.stdout)
        except (ValueError, UnicodeError):
            raise RuntimeError('PC 응답 형식이 올바르지 않습니다.') from None
        if not isinstance(data, dict) or data.get('unknown'):
            raise RuntimeError('PC 요청 결과를 확인하지 못했습니다.')
        if data.get('ok') is not True:
            raise WorkerError(scrub_text(data.get('error') or 'PC 요청을 실행하지 못했습니다.')[:1000])
        if not isinstance(data.get('result'), dict):
            raise RuntimeError('PC 응답 형식이 올바르지 않습니다.')
        return data['result']

    def windows_terminal(self, host, spec):
        """A literal PowerShell command in the Mini-owned SSH terminal."""
        from pathlib import PureWindowsPath
        argv, cwd, env = spec.get('argv'), spec.get('cwd'), spec.get('env') or {}
        if (not isinstance(argv, list) or not argv or len(argv) > 32
                or any(not isinstance(v, str) or not v or len(v) > 4096 or any(ord(c) < 32 for c in v) for v in argv)
                or not PureWindowsPath(argv[0]).is_absolute() or not isinstance(cwd, str)
                or not PureWindowsPath(cwd).is_absolute() or any(ord(c) < 32 for c in cwd)
                or not isinstance(env, dict) or set(env) - {'CODEX_HOME'}
                or any(not isinstance(v, str) or not PureWindowsPath(v).is_absolute()
                       or any(ord(c) < 32 for c in v) for v in env.values())):
            raise ValueError('PC 터미널 연결 정보가 올바르지 않습니다.')
        if any(not windows_literal_argument(value) for value in argv[1:]):
            raise ValueError('PC 터미널 연결 정보가 올바르지 않습니다.')
        quote = ps_quote
        script = "$ErrorActionPreference='Stop'; Set-Location -LiteralPath " + quote(cwd) + '; '
        for key, value in env.items():
            script += '$env:' + key + '=' + quote(value) + '; '
        script += '& ' + ' '.join(quote(value) for value in argv) + '; exit $LASTEXITCODE'
        return self._windows_ssh(host, script)

    def _windows_ssh(self, host, script):
        command = windows_encoded_command(script)
        if len(command) > WINDOWS_COMMAND_LIMIT:
            raise ValueError('PC 터미널 명령이 너무 깁니다.')
        return [shutil.which('ssh') or '/usr/bin/ssh', '-tt'] + self.terminal_options() + ['--', host['ssh'], command]

    def windows_launcher(self, host, launcher):
        """A PC's 새 세션 (the host settings' `launcher` choices, `windows_launcher_script`)."""
        return self._windows_ssh(host, windows_launcher_script(launcher))

    def rpc(self, host, op, args=None, timeout=30):
        if host.get('platform') == 'windows':
            return self.windows_rpc(host, op, args or {}, timeout)
        request = {'op': op, 'args': args or {}}
        name = host['name']
        with self.lock:
            # True: this helper version is confirmed on the host. False: the host
            # reported it missing and no full copy has been confirmed since.
            # Unknown hosts are asked with the SHA first, which a restart usually finds installed.
            missing = self.runtime_installed.get(name) is False
        data = self._exchange(host, request, timeout, full=missing)
        if not missing and self._needs_runtime(data):
            with self.lock:
                self.runtime_installed[name] = False
            data = self._exchange(host, request, timeout, full=True)
        if self._needs_runtime(data):
            raise RuntimeError('기기에 이전 도우미를 설치하지 못했습니다. 기기 상태를 확인해 주세요.')
        with self.lock:
            self.runtime_installed[name] = True
        if not data.get('ok'):
            stage = data.get('stage')
            raise WorkerError(scrub_text(data.get('error') or '기기의 이전 준비에 실패했습니다.')[:1000],
                              stage if isinstance(stage, str) else None)
        runtime = data.get('runtime', '')
        if not runtime.startswith('/') or not runtime.endswith('.pyz') or any(ord(c)<32 for c in runtime):
            raise RuntimeError('기기 도우미 경로가 올바르지 않습니다.')
        self.runtime_paths[host['name']] = runtime
        return data['result']

    def profiles(self, host, refresh=False):
        with self.lock:
            cached = self.profile_cache.get(host['name'])
            failure = self.profile_failures.get(host['name'])
            # A current list stands, though a forced read (a plan's) failed since: that failure
            # must not hide it.
            if not refresh and cached and time.monotonic() - cached[0] < 120:
                return cached[1]
            # A failed probe is not retried automatically, but only for a bounded time.
            if not refresh and failure and time.monotonic() - failure[0] < PROFILE_FAILURE_TTL:
                raise RuntimeError(failure[1])
            # An expired list is answered at once (a request never waits for the host's helper);
            # one background read per host renews it, and its failure is recorded as above.
            if not refresh and cached:
                if host['name'] not in self.profile_reading:
                    self.profile_reading.add(host['name'])
                    try:
                        threading.Thread(target=self._renew_profiles, args=(host,), daemon=True,
                                         name='profiles').start()
                    except RuntimeError:
                        self.profile_reading.discard(host['name'])
                return cached[1]
        return self._read_profiles(host)

    def _renew_profiles(self, host):
        try:
            self._read_profiles(host)
        except (RuntimeError, OSError, ValueError):
            pass  # Kept as this host's failure; the next request reports it.
        finally:
            with self.lock:
                self.profile_reading.discard(host['name'])

    def _read_profiles(self, host):
        try:
            data = self.rpc(host, 'profiles', timeout=12)
            if not isinstance(data, dict) or not isinstance(data.get('profiles'), list):
                raise RuntimeError('기기의 계정 목록을 읽지 못했습니다.')
            result = data['profiles']
        except (RuntimeError, OSError, ValueError) as exc:
            with self.lock: self.profile_failures[host['name']] = (time.monotonic(), str(exc))
            raise
        with self.lock:
            self.profile_cache[host['name']] = (time.monotonic(), result)
            self.launcher_cache[host['name']] = data.get('launcher') if isinstance(data.get('launcher'), dict) else None
            tmux = data.get('holdTmux')
            held = (data.get('holds') is True and isinstance(tmux, str) and tmux.startswith('/') and len(tmux) < 512
                    and not any(c.isspace() or ord(c) < 32 for c in tmux))
            self.hold_support[host['name']] = tmux if held else None
            self.profile_failures.pop(host['name'], None)
        return result

    def launcher(self, host, refresh=False):
        """The host's "새 세션" status, read with its profiles (the same helper request)."""
        self.profiles(host, refresh=refresh)
        with self.lock:
            status = self.launcher_cache.get(host['name'])
        if not status:
            return {'available': False, 'label': None, 'reason': '이 기기의 새 세션 설정을 확인하지 못했습니다.'}
        return {'available': status.get('available') is True,
                'label': status['label'] if isinstance(status.get('label'), str) else None,
                'reason': status['reason'] if isinstance(status.get('reason'), str) else None}

    def preview(self, source, target, source_host, target_host):
        status = self.rpc(source_host, 'state', {'source': source}, timeout=12)
        if not status.get('safeToTransfer') and not status.get('canInterrupt'):
            raise ValueError(status.get('reason') or '원본 응답을 안전하게 중단할 수 없습니다.')
        workspace = self.rpc(source_host, 'inspect', {'source': source}, timeout=45)
        return {'sourceHostLabel': source_host['label'], 'targetHostLabel': target_host['label'],
                'workspace': workspace, 'requiresInterrupt': not status.get('safeToTransfer'),
                'destinationNote': '대상 기기의 별도 작업 폴더에 복사합니다. 원본 파일과 기록은 유지됩니다.'}

    def _copy(self, source_host, target_host, transfer_id, expected, folder):
        import collector
        archive = folder / 'workspace.tar.gz'
        source_args = ['-I', self.runtime_paths[source_host['name']], 'send', transfer_id]
        # Stream to a private file, bounding by the helper's verified 128MiB limit.
        fd = os.open(str(archive), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as out:
                process = subprocess.Popen(self.command(source_host, source_args), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=collector.tool_env())
                timer = threading.Timer(180, process.kill); timer.daemon = True; timer.start()
                try:
                    total = 0
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk: break
                        total += len(chunk)
                        if total > MAX_ARCHIVE:
                            process.kill()
                            raise RuntimeError('원본 파일 묶음이 허용 크기를 넘었습니다.')
                        out.write(chunk)
                    if process.wait(timeout=5): raise RuntimeError('원본 파일 묶음을 받지 못했습니다.')
                finally:
                    timer.cancel(); process.stdout.close()
                    if process.poll() is None: process.kill()
                    process.wait(timeout=5)
            digest = hashlib.sha256()
            with archive.open('rb') as inp:
                for chunk in iter(lambda: inp.read(1024*1024), b''): digest.update(chunk)
            if digest.hexdigest() != expected: raise RuntimeError('전송한 원본 파일의 검증에 실패했습니다.')
            args = ['-I', self.runtime_paths[target_host['name']], 'receive', transfer_id, expected]
            with archive.open('rb') as inp:
                result = subprocess.run(self.command(target_host, args), stdin=inp, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=180, env=collector.tool_env())
            if result.returncode: raise RuntimeError('대상 기기로 파일을 전송하지 못했습니다.')
            if not json.loads(result.stdout).get('ok'): raise RuntimeError('대상 파일 검증에 실패했습니다.')
        except subprocess.TimeoutExpired:
            raise RuntimeError('파일 전송 시간이 초과됐습니다. 원본 파일은 유지됩니다.') from None
        finally:
            if archive.exists(): archive.unlink()

    @contextmanager
    def coordinator_operation(self, transfer_id, wait=False):
        if not isinstance(transfer_id, str) or not re.fullmatch('[a-f0-9]{32}', transfer_id):
            raise ValueError('이전 식별자가 올바르지 않습니다.')
        directory = settings.safe_path(self.state_dir, directory=True, allow_missing=True)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(directory / (transfer_id + '.coordinator.lock')), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('이전 정리 잠금 파일이 안전하지 않습니다.')
            deadline = time.monotonic() + (40 if wait else 0)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait or time.monotonic() >= deadline:
                        raise RuntimeError('이전 작업이 진행 중입니다. 작업이 끝난 뒤 정리를 다시 확인해 주세요.') from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def execute(self, source, target, source_host, target_host, profile, request_id, read_messages):
        transfer_id = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        with self.coordinator_operation(transfer_id):
            return self._execute(source, target, source_host, target_host, profile, request_id, read_messages)

    def _execute(self, source, target, source_host, target_host, profile, request_id, read_messages):
        # Durable request identity prevents a lost HTTP response from starting a second model.
        transfer_id = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        folder = self.state_dir / transfer_id
        if folder.exists():
            raise ValueError('이미 처리한 이전 요청입니다. 열린 터미널이나 이전 기록을 확인해 주세요.')
        folder.mkdir(mode=0o700, parents=True)
        record = {'id': transfer_id, 'status': 'stopping', 'source': {k:source.get(k) for k in ('host','agent','home','id','cwd')},
                  'target': target, 'startedAt': int(time.time()), 'sourceStopped': False,
                  'targetPreparation': 'not_started', 'archiveCleanupHosts': [],
                  'archiveCleanupPending': False}
        def status(value):
            record['status'] = value; record['updatedAt'] = int(time.time()); _save(folder/'transfer.json', record)
        status('stopping')
        try:
            # Re-resolve the target before interrupting any source turn.
            available = self.profiles(target_host, refresh=True)
            actual = next((p for p in available if p['id']==target['account'] and p['agent']==target['agent'] and p.get('available')), None)
            if not actual: raise ValueError('대상 기기의 기존 계정을 사용할 수 없습니다.')
            if actual.get('environment', 'default') != profile.get('environment', 'default'):
                raise ValueError('대상 실행 환경이 바뀌었습니다. 실행 설정을 다시 확인해 주세요.')
            stopped = self.rpc(source_host, 'interrupt', {'source': source}, timeout=20)
            if not stopped.get('confirmed') or not stopped.get('safeToTransfer'):
                raise ValueError(stopped.get('reason') or '원본 응답이 멈추지 않아 이전을 중단했습니다.')
            record['sourceStopped'] = True
            source = {**source, 'phase': stopped.get('phase', 'idle')}
            messages = read_messages()
            if not messages: raise ValueError('최근 대화를 읽지 못해 이전을 중단했습니다.')
            record['archiveCleanupHosts'] = [source_host['name']]
            record['archiveCleanupPending'] = True
            status('packing')
            exported = self.rpc(source_host, 'export', {'source': source, 'transferId': transfer_id,
                                'revision': stopped.get('revision')}, timeout=180)
            record['archiveCleanupHosts'].append(target_host['name'])
            status('copying')
            self._copy(source_host, target_host, transfer_id, exported['sha256'], folder)
            status('preparing')
            source_environment = exported.get('summary', {}).get('sourceEnvironment')
            if not isinstance(source_environment, str) or not settings.GROUP.fullmatch(source_environment):
                raise ValueError('원본 작업의 실행 환경을 확인하지 못했습니다.')
            config = settings.load()
            if settings.crosses_boundary(target['agent'], source_environment, actual.get('environment', 'default'), config):
                raise ValueError('원본과 대상의 실행 환경 경계가 다릅니다.')
            # The preview-only size listing stays out of the handoff record.
            workspace = {k: v for k, v in exported.get('summary', {}).items() if k != 'largestFiles'}
            metadata = {'transferId': transfer_id, 'sourceHost':source_host['name'], 'targetHost':target_host['name'],
                        'sourceCwd': source['cwd'], 'sourceStopped': True, 'originalHead': exported.get('summary',{}).get('head'),
                        'sourceEnvironment': source_environment,
                        'archiveSha256':exported['sha256'], 'workspace':workspace}
            legacy = config['legacy'].get('sourcePersonal')
            if legacy:
                # Also the older boolean, so a previous release can still read this record.
                metadata['sourcePersonal'] = (settings.boundary_group(source_environment, config)
                                              == settings.boundary_group(legacy['true'], config))
            # prepare can materialize the initial native input, before TUI exec.
            # Validate files first, then read native state at the last boundary.
            workspace_now = self.rpc(source_host, 'inspect', {'source':source}, timeout=45)
            if workspace_now.get('fingerprint') != exported.get('summary', {}).get('fingerprint'):
                raise ValueError('복사 중 원본 파일이 바뀌었습니다. 대상 실행을 시작하지 않았습니다.')
            current = self.rpc(source_host, 'state', {'source':source}, timeout=12)
            if not current.get('safeToTransfer') or current.get('revision') != stopped.get('revision'):
                raise ValueError('복사 중 원본 작업이 다시 바뀌었습니다. 대상 실행을 시작하지 않았습니다.')
            record['targetPreparation'] = 'unknown'
            status('preparing')
            try:
                prepared = self.rpc(target_host, 'prepare', {'source': source, 'target': target, 'messages': messages,
                                'transferId':transfer_id, 'sha256':exported['sha256'], 'metadata':metadata}, timeout=180)
            except WorkerError as exc:
                if exc.stage not in PRE_BIND_STAGES:
                    raise RuntimeError(UNKNOWN_PREPARE) from None
                # The helper reported a failure before binding any native session:
                # keep its reason and let the target archive be cleaned now.
                record['targetPreparation'] = 'failed'
                raise
            except (OSError, RuntimeError, ValueError):
                raise RuntimeError(UNKNOWN_PREPARE) from None
            record['targetPreparation'] = 'prepared'
            record.update(destinationCwd=prepared['cwd'], sourceRevision=stopped.get('revision'))
            status('ready')
            self._cleanup_record(record, folder / 'transfer.json')
            import collector
            argv, hold = self.terminal(target_host, ['-I', self.runtime_paths[target_host['name']], 'exec', transfer_id])
            return {'argv':argv, 'holdName':hold,
                    'cwd':str(Path.home()), 'env':collector.tool_env(), 'transferId':transfer_id,
                    'destinationCwd':prepared['cwd'], 'nativeSource':prepared.get('nativeSource'), 'connectionMode':prepared.get('connectionMode'), 'warnings':prepared.get('warnings',[])}
        except Exception as exc:
            record['error'] = scrub_text(str(exc))[:1000]; status('failed')
            # An unknown prepare may still be reading its incoming archive.
            # The target remains pending until an explicit status-confirmed retry.
            self._cleanup_record(record, folder / 'transfer.json')
            raise

    def _cleanup_record(self, record, path, explicit=False):
        names = record.get('archiveCleanupHosts', [])
        pending, deferred = [], []
        for name in names:
            if name not in self.hosts:
                pending.append(name)
                continue
            if name == record['target']['host'] and record.get('targetPreparation') == 'unknown':
                if not explicit:
                    pending.append(name); deferred.append(name)
                    continue
                # status takes the helper's per-transfer lock, so a successful reply
                # means no prepare is still reading the archive. Cleanup removes only
                # archives; an imported folder or bound native session is preserved.
                try:
                    state = self.rpc(self.hosts[name], 'status', {'transferId': record['id']}, timeout=12)
                    if (not isinstance(state, dict) or type(state.get('prepared')) is not bool
                            or type(state.get('started')) is not bool or (state['started'] and not state['prepared'])):
                        pending.append(name); deferred.append(name)
                        continue
                except (OSError, RuntimeError, ValueError):
                    pending.append(name); deferred.append(name)
                    continue
            try:
                result = self.rpc(self.hosts[name], 'cleanup', {'transferId': record['id']}, timeout=12)
                if not isinstance(result, dict) or result.get('ok') is not True:
                    pending.append(name)
            except (OSError, RuntimeError, ValueError):
                pending.append(name)
        record.update(archiveCleanupHosts=pending, archiveCleanupPending=bool(pending))
        _save(path, record)
        return {'id': record['id'], 'archiveCleanupPending': bool(pending),
                'pendingHosts': pending, 'deferredHosts': deferred}

    def cleanup_pending(self, transfer_id):
        with self.coordinator_operation(transfer_id):
            return self._cleanup_pending(transfer_id)

    def _cleanup_pending(self, transfer_id):
        if not isinstance(transfer_id, str) or not re.fullmatch('[a-f0-9]{32}', transfer_id):
            raise ValueError('이전 식별자가 올바르지 않습니다.')
        path = settings.safe_path(self.state_dir / transfer_id / 'transfer.json', private=True)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('이전 기록이 허용 크기를 넘습니다.')
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get('id') != transfer_id
                or not isinstance(record.get('source'), dict) or not isinstance(record.get('target'), dict)):
            raise ValueError('이전 기록 형식이 올바르지 않습니다.')
        if record.get('status') not in ('stopping', 'packing', 'copying', 'preparing', 'failed', 'ready', 'started'):
            raise ValueError('이전 기록의 진행 상태가 올바르지 않습니다.')
        hosts = [record['source'].get('host'), record['target'].get('host')]
        if any(not isinstance(name, str) or name not in self.hosts for name in hosts):
            raise ValueError('이전 기록의 기기가 현재 등록된 기기와 다릅니다.')
        names = record.get('archiveCleanupHosts')
        if names is None:
            names = hosts if record.get('sourceStopped') else []
            record['archiveCleanupHosts'] = names
        if not isinstance(names, list) or any(name not in hosts for name in names) or len(set(names)) != len(names):
            raise ValueError('이전 임시 파일 정리 기록이 올바르지 않습니다.')
        archive = path.parent / 'workspace.tar.gz'
        if os.path.lexists(archive):
            settings.safe_path(archive, private=True)
            archive.unlink()
        return self._cleanup_record(record, path, explicit=True)

    def mark_started(self, transfer_id, terminal_id):
        with self.coordinator_operation(transfer_id, wait=True):
            return self._mark_started(transfer_id, terminal_id)

    def _mark_started(self, transfer_id, terminal_id):
        path = self.state_dir / transfer_id / 'transfer.json'
        record = json.loads(path.read_text()); record.update(status='started', terminalId=terminal_id, updatedAt=int(time.time()))
        _save(path, record)
