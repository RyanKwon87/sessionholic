"""Finite per-host transfer helper. No listener, model proxy or credential copying."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import sys
import stat
from contextlib import contextmanager
from collector import RpcError

MAX_ARCHIVE = 128 * 1024 * 1024
ID = re.compile(r'[a-f0-9]{32}\Z')


def private_dir(path):
    path = Path(path)
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ValueError('이전 저장 경로가 안전하지 않습니다.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError('이전 저장 경로의 소유자를 확인해 주세요.')
    path.chmod(0o700)
    return path


def root():
    return private_dir(Path.home() / '.local/state/sessionholic-transfers')


def job(transfer_id):
    if not isinstance(transfer_id, str) or not ID.fullmatch(transfer_id):
        raise ValueError('이전 식별자가 올바르지 않습니다.')
    return private_dir(root() / transfer_id)


@contextmanager
def job_operation(transfer_id):
    """A cleanup must never unlink archives used by an active finite worker."""
    folder = job(transfer_id)
    fd = os.open(str(folder / '.operation.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('이전 작업 잠금 파일이 안전하지 않습니다.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('이전 파일을 사용 중입니다. 작업이 끝난 뒤 정리를 다시 확인해 주세요.') from None
        yield
    finally:
        os.close(fd)


def cleanup_archives(folder):
    """Remove only this helper's archive names; preserve workspaces and receipts."""
    removed = 0
    for directory, pattern in ((folder, r'(?:incoming\.tar\.gz|incoming\.[a-f0-9]{12}\.tmp)'),
                               (folder / 'export', r'workspace-[a-f0-9]{32}\.tar\.gz')):
        if not os.path.lexists(directory):
            continue
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('이전 임시 저장 폴더가 안전하지 않습니다.')
            for name in os.listdir(fd):
                if not re.fullmatch(pattern, name):
                    continue
                item = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
                    raise ValueError('이전 임시 파일의 종류 또는 권한이 안전하지 않습니다.')
                os.unlink(name, dir_fd=fd)
                removed += 1
        finally:
            os.close(fd)
    return {'ok': True, 'removed': removed}


def save(path, value):
    tmp = path.with_name(path.name + '.' + secrets.token_hex(6))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists(): tmp.unlink()


def read_private(path):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('이전 기록의 권한을 확인해 주세요.')
    return json.loads(path.read_text())


def source_ready(source, expected=None):
    from transfer_native import source_state
    state = source_state(source)
    if not state.get('safeToTransfer'):
        raise ValueError(state.get('reason') or '원본 응답이 멈춘 것을 확인하지 못했습니다.')
    if expected is not None and state.get('revision') != expected:
        raise ValueError('원본 대화 상태가 바뀌었습니다. 새로 검토해 주세요.')
    return state


def _rpc(request):
    from transfer_native import profiles, source_state, interrupt_source
    from transfer_workspace import inspect_workspace, export_workspace, import_workspace
    op, args = request.get('op'), request.get('args', {})
    if op == 'chat':
        from native_chat import handle
        return handle(args)
    if op == 'attachment':
        from attachments import receive
        return receive(args)
    if op == 'profiles':
        return {'profiles': profiles(), 'home': str(Path.home())}
    if op == 'state': return source_state(args['source'])
    if op == 'interrupt': return interrupt_source(args['source'])
    if op == 'inspect':
        from launch import source_environment
        result = inspect_workspace(args['source']['cwd'])
        result['sourceEnvironment'] = source_environment(args['source'])
        return result
    if op == 'export':
        folder = job(args['transferId'])
        ready = source_ready(args['source'], args.get('revision'))
        result = export_workspace(args['source']['cwd'], folder / 'export')
        from launch import source_environment
        result['summary']['sourceEnvironment'] = source_environment(args['source'])
        source_ready(args['source'], ready.get('revision'))
        archive = Path(result['archivePath'])
        if archive.stat().st_size > MAX_ARCHIVE: raise ValueError('이전 파일 묶음이 128MiB를 넘습니다.')
        save(folder / 'export.json', result)
        return result
    if op == 'prepare':
        from launch import build_transferred_launch
        folder = job(args['transferId'])
        if (folder / 'launch.json').exists():
            raise ValueError('이미 준비한 이전입니다. 열린 터미널을 확인해 주세요.')
        profile = next((p for p in profiles() if p['id'] == args['target']['account'] and p['agent'] == args['target']['agent']), None)
        if not profile or not profile.get('available'):
            raise ValueError('대상 기기의 기존 계정을 사용할 수 없습니다.')
        destination = private_dir(Path.home() / '.local/share/sessionholic/workspaces')
        imported = import_workspace(folder / 'incoming.tar.gz', destination, args['transferId'], args['sha256'])
        metadata = {**args['metadata'], 'destinationCwd': imported['cwd'], 'projectRoot': imported['root'],
                    'attachmentsPathMap': imported.get('attachmentsPathMap', [])}
        spec = build_transferred_launch(args['source'], {**profile, 'host': args['target']['host']},
                                        args['messages'], folder, imported['cwd'], metadata)
        from managed_launch import bind
        spec = bind(spec, profile, args['target']['host'])
        save(folder / 'launch.json', spec)
        save(folder / 'transfer.json', metadata)
        return {'cwd': imported['cwd'], 'root': imported['root'], 'nativeSource': spec.get('nativeSource'), 'connectionMode': spec.get('connectionMode'), 'warnings': spec.get('warnings', []), 'summary': imported.get('summary', {})}
    if op == 'cleanup':
        folder = job(args['transferId'])
        return cleanup_archives(folder)
    if op == 'status':
        folder = job(args['transferId'])
        return {'prepared': (folder / 'launch.json').exists(), 'started': (folder / 'started.json').exists()}
    raise ValueError('지원하지 않는 이전 작업입니다.')


def rpc(request):
    if request.get('op') in ('export', 'prepare', 'cleanup', 'status'):
        with job_operation(request['args']['transferId']):
            return _rpc(request)
    return _rpc(request)


def attach(source):
    """Attach the exact existing native session using this host's environment."""
    from launch import build_launch, discover_profiles
    from transfer_native import _identity
    identity = _identity(source)
    identifier = 'claude:default' if identity['agent'] == 'claude' else 'codex:' + identity['home']
    profile = next((p for p in discover_profiles() if p['id'] == identifier and p.get('available')), None)
    if profile is None:
        raise ValueError('원격 원본 실행 프로필을 확인하지 못했습니다.')
    normalized = {**source, **identity}
    spec = build_launch(normalized, {**profile, 'host': normalized.get('host')}, [],
                        Path.home() / '.local/state/sessionholic')
    if spec['mode'] != 'attach':
        raise ValueError('원격 기존 세션 연결만 지원합니다.')
    os.chdir(spec['cwd'])
    os.execve(spec['argv'][0], spec['argv'], spec['env'])


def _main():
    if len(sys.argv) == 3 and sys.argv[1] == 'attach':
        try:
            attach(json.loads(sys.argv[2]))
        except (ValueError, OSError, KeyError):
            # PTY failures must not print input, environment or a traceback.
            print('원격 원본 터미널을 연결하지 못했습니다. 기기 설정을 확인해 주세요.', file=sys.stderr)
            raise SystemExit(1)
        return
    if len(sys.argv) > 1:
        op, transfer_id = sys.argv[1:3]
        folder = job(transfer_id)
        if op == 'send':
            record = read_private(folder / 'export.json')
            path = Path(record['archivePath'])
            if path.is_symlink() or folder not in path.parents or path.stat().st_size > MAX_ARCHIVE:
                raise ValueError('이전 묶음 경로가 올바르지 않습니다.')
            with path.open('rb') as inp:
                while True:
                    chunk = inp.read(1024 * 1024)
                    if not chunk: break
                    sys.stdout.buffer.write(chunk)
            return
        if op == 'receive':
            expected = sys.argv[3]
            if not re.fullmatch('[a-f0-9]{64}', expected): raise ValueError('검증 해시가 올바르지 않습니다.')
            temporary = folder / ('incoming.' + secrets.token_hex(6) + '.tmp')
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            digest, total = hashlib.sha256(), 0
            try:
                with os.fdopen(fd, 'wb') as out:
                    while True:
                        chunk = sys.stdin.buffer.read(1024 * 1024)
                        if not chunk: break
                        total += len(chunk)
                        if total > MAX_ARCHIVE: raise ValueError('이전 묶음이 허용 크기를 넘습니다.')
                        digest.update(chunk); out.write(chunk)
                    out.flush(); os.fsync(out.fileno())
                if digest.hexdigest() != expected: raise ValueError('전송한 파일 검증에 실패했습니다.')
                destination = folder / 'incoming.tar.gz'
                if destination.exists(): raise ValueError('같은 이전의 수신 파일이 이미 있습니다.')
                os.replace(str(temporary), str(destination))
            finally:
                if temporary.exists(): temporary.unlink()
            print(json.dumps({'ok': True, 'bytes': total}))
            return
        if op == 'exec':
            spec = read_private(folder / 'launch.json')
            marker = os.open(str(folder / 'started.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(marker, 'w') as out: json.dump({'started': True}, out)
            from managed_launch import mark_attached
            mark_attached(spec, 'transfer-' + transfer_id)
            os.chdir(spec['cwd'])
            os.execve(spec['argv'][0], spec['argv'], spec['env'])
        raise ValueError('지원하지 않는 이전 명령입니다.')
    try:
        request = json.loads(sys.stdin.buffer.read(30 * 1024 * 1024 + 1))
        result = rpc(request)
        print(json.dumps({'ok': True, 'result': result, 'runtime': sys.argv[0]}, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, KeyError, RpcError) as exc:
        from launch import scrub_text
        print(json.dumps({'ok': False, 'error': scrub_text(str(exc))[:1000]}, ensure_ascii=False))


def main():
    if len(sys.argv) > 2 and sys.argv[1] in ('send', 'receive', 'exec'):
        with job_operation(sys.argv[2]):
            return _main()
    return _main()


if __name__ == '__main__': main()
