"""Bounded SSH transfer coordinator for registered machines and fresh workspaces."""
import base64
import hashlib
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import stat
from contextlib import contextmanager
import threading
import time
import zipfile

from launch import scrub_text
import settings

MAX_ARCHIVE = 128 * 1024 * 1024
# Only this versioned helper is installed. Credentials, dotfile profiles and hooks are not copied.
BOOTSTRAP = r'''
import base64,hashlib,io,json,os,pathlib,runpy,sys
p=json.load(sys.stdin); data=base64.b64decode(p['runtime'],validate=True)
assert len(data)<2*1024*1024 and hashlib.sha256(data).hexdigest()==p['sha256']
root=pathlib.Path.home()/'.local/state/sessionholic-transfer-runtime'
for d in (root,)+tuple(root.parents):
 if d.exists(): assert not d.is_symlink() and d.is_dir()
root.mkdir(parents=True,exist_ok=True,mode=0o700)
assert root.stat().st_uid==os.getuid();root.chmod(0o700)
path=root/('worker-'+p['sha256']+'.pyz')
if path.exists():
 assert not path.is_symlink() and path.stat().st_uid==os.getuid()
 assert hashlib.sha256(path.read_bytes()).hexdigest()==p['sha256']
else:
 fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data)
sys.argv=[str(path)];sys.path.insert(0,str(path));sys.stdin=io.TextIOWrapper(io.BytesIO(json.dumps(p['request']).encode()))
runpy.run_path(str(path),run_name='__main__')
'''


def _save(path, value):
    from server import private_json
    private_json(path, value)


class Transfers:
    def __init__(self, hosts, state_dir):
        self.hosts = {h['name']: h for h in hosts}
        self.state_dir = Path(state_dir) / 'transfers'
        self.runtime_paths = {}
        self.profile_cache = {}
        self.profile_failures = {}
        self.lock = threading.RLock()
        self._runtime = None

    def runtime(self):
        if self._runtime is None:
            root = Path(__file__).resolve().parent
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
                for name in ('collector.py', 'launch.py', 'transfer_native.py', 'transfer_workspace.py', 'transfer_worker.py', 'native_chat.py', 'attachments.py', 'managed_launch.py', 'settings.py'):
                    z.writestr(name, (root / name).read_bytes())
                z.writestr('__main__.py', 'from transfer_worker import main\nmain()\n')
            data = output.getvalue()
            self._runtime = {'runtime': base64.b64encode(data).decode('ascii'), 'sha256': hashlib.sha256(data).hexdigest()}
        return self._runtime

    @staticmethod
    def command(host, args, tty=False):
        python = host.get('python') or '/usr/bin/python3'
        if host.get('local'):
            return [sys.executable] + args
        command = 'exec ' + shlex.join([python] + args)
        return [shutil.which('ssh') or '/usr/bin/ssh'] + (['-tt'] if tty else []) + ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6',
                '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', '--', host['ssh'], command]

    def rpc(self, host, op, args=None, timeout=30):
        payload = {**self.runtime(), 'request': {'op': op, 'args': args or {}}}
        import collector
        try:
            result = subprocess.run(self.command(host, ['-I', '-c', BOOTSTRAP]),
                input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, env=collector.tool_env())
        except subprocess.TimeoutExpired:
            raise RuntimeError('기기 응답 시간이 초과됐습니다. 이전 상태를 확인한 뒤 다시 시도해 주세요.') from None
        if result.returncode:
            raise RuntimeError('기기의 이전 도우미를 실행하지 못했습니다. SSH 연결을 확인해 주세요.')
        try: data = json.loads(result.stdout)
        except (ValueError, UnicodeError): raise RuntimeError('기기 응답을 읽지 못했습니다.') from None
        if not data.get('ok'): raise RuntimeError(data.get('error') or '기기의 이전 준비에 실패했습니다.')
        runtime = data.get('runtime', '')
        if not runtime.startswith('/') or not runtime.endswith('.pyz') or any(ord(c)<32 for c in runtime):
            raise RuntimeError('기기 도우미 경로가 올바르지 않습니다.')
        self.runtime_paths[host['name']] = runtime
        return data['result']

    def profiles(self, host, refresh=False):
        with self.lock:
            cached = self.profile_cache.get(host['name'])
            if not refresh and host['name'] in self.profile_failures:
                raise RuntimeError(self.profile_failures[host['name']])
            if not refresh and cached and time.monotonic() - cached[0] < 120:
                return cached[1]
        try:
            result = self.rpc(host, 'profiles', timeout=12)['profiles']
        except (RuntimeError, OSError, ValueError) as exc:
            with self.lock: self.profile_failures[host['name']] = str(exc)
            raise
        with self.lock:
            self.profile_cache[host['name']] = (time.monotonic(), result)
            self.profile_failures.pop(host['name'], None)
        return result

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
            if source_environment != actual.get('environment', 'default'):
                raise ValueError('원본과 대상의 실행 환경 경계가 다릅니다.')
            metadata = {'transferId': transfer_id, 'sourceHost':source_host['name'], 'targetHost':target_host['name'],
                        'sourceCwd': source['cwd'], 'sourceStopped': True, 'originalHead': exported.get('summary',{}).get('head'),
                        'sourceEnvironment': source_environment,
                        'archiveSha256':exported['sha256'], 'workspace':exported.get('summary',{})}
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
            except (OSError, RuntimeError, ValueError):
                raise RuntimeError('대상 준비 결과를 확인하지 못했습니다. 이미 작업이 시작되었을 수 있으므로 대상 기기의 작업 목록과 이전 기록을 확인해 주세요.') from None
            record['targetPreparation'] = 'prepared'
            record.update(destinationCwd=prepared['cwd'], sourceRevision=stopped.get('revision'))
            status('ready')
            self._cleanup_record(record, folder / 'transfer.json')
            import collector
            return {'argv':self.command(target_host, ['-I', self.runtime_paths[target_host['name']], 'exec', transfer_id], tty=True),
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
                try:
                    state = self.rpc(self.hosts[name], 'status', {'transferId': record['id']}, timeout=12)
                    if not isinstance(state, dict) or state.get('prepared') is not True:
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
