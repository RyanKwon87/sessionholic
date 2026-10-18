"""Finite per-host transfer helper. No listener, model proxy or credential copying."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import errno
import shutil
import signal
import socket
import sys
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from collector import RpcError
import instance

MAX_ARCHIVE = 128 * 1024 * 1024
ID = re.compile(r'[a-f0-9]{32}\Z')
ROUTE = re.compile(r'[a-f0-9]{64}\Z')
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
HOST = re.compile(r'[A-Za-z0-9_.-]{1,40}\Z')
# The source fields a staged handoff keeps; the same selector a remote attach sends.
SELECTOR = ('host', 'agent', 'id', 'home', 'cwd', 'kind', 'sessionId', 'phase')
CHECK_MESSAGES = [{'role': 'user', 'text': '점검용 인계 메시지입니다. 실제 대화를 읽지 않았습니다.'}]
# A board terminal's run kept in this host's own tmux server (never the user's), so a dropped
# connection or a sleeping host does not end it; the board attaches again by its name.
HOLD_SOCKET = 'sessionholic-hold'
HOLD_NAME = re.compile(r'sh-[0-9a-f]{16}\Z')
HOLD_OPTIONS = (('status', 'off'), ('prefix', 'None'), ('prefix2', 'None'), ('mouse', 'off'), ('escape-time', '10'),
                # The attached client draws on the board terminal's own screen, not an alternate
                # one: a run's last output stays there when it ends, and plain output scrolls
                # into the board's history. Mouse modes still pass to a run that asks for them.
                ('terminal-overrides', '*:smcup@:rmcup@'))
HOLD_TIMEOUT = 10
HOLD_STALE_ENVELOPE = 600  # an envelope this old was never run (hold-run removes it first)
# What tests leave on a host (sweep_leftovers): tmux socket files of these names, and Codex
# app-server daemons a test's Codex home (`<temporary folder>/tmp…/.codex…`) started.
TEST_SOCKET_PREFIXES = ('sb-test-', 'sb-browser-', 'sb-validation-')
LEFTOVER_PROCESS_AGE = 3600
LEFTOVER_SOCKET_AGE = 86400
# Set by `hold <name>` before a terminal step: its final exec then runs in that session.
_hold = None


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
    return private_dir(instance.remote_path('transfersDir', Path.home()))


def job(transfer_id):
    if not isinstance(transfer_id, str) or not ID.fullmatch(transfer_id):
        raise ValueError('이전 식별자가 올바르지 않습니다.')
    return private_dir(root() / transfer_id)


@contextmanager
def _folder_lock(folder, unsafe, busy):
    fd = os.open(str(folder / '.operation.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(unsafe)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(busy) from None
        yield folder
    finally:
        os.close(fd)


@contextmanager
def job_operation(transfer_id):
    """A cleanup must never unlink archives used by an active finite worker."""
    with _folder_lock(job(transfer_id), '이전 작업 잠금 파일이 안전하지 않습니다.',
                      '이전 파일을 사용 중입니다. 작업이 끝난 뒤 정리를 다시 확인해 주세요.'):
        yield


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


@contextmanager
def stage(name):
    """Tag a failure with the prepare step it came from, for the coordinator."""
    try:
        yield
    except Exception as exc:
        if getattr(exc, 'transfer_stage', None) is None:
            exc.transfer_stage = name
        raise


def discard_import(destination, transfer_id):
    """Remove this transfer's freshly imported copy when no session was bound to it."""
    path = destination / transfer_id
    try:
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            shutil.rmtree(path)
    except OSError:
        pass


def prepare(args, profiles, import_workspace):
    # validate/import/build failures start nothing; bind may create a native
    # session, so its failures and later ones stay unknown to the coordinator.
    from launch import build_transferred_launch
    from managed_launch import bind, ensure_app_server
    with stage('validate'):
        folder = job(args['transferId'])
        if (folder / 'launch.json').exists():
            error = ValueError('이미 준비한 이전입니다. 열린 터미널을 확인해 주세요.')
            error.transfer_stage = 'prepared'
            raise error
        profile = next((p for p in profiles() if p['id'] == args['target']['account'] and p['agent'] == args['target']['agent']), None)
        if not profile or not profile.get('available'):
            raise ValueError('대상 기기의 기존 계정을 사용할 수 없습니다.')
        destination = private_dir(instance.remote_path('workspacesDir', Path.home()))
    with stage('import'):
        imported = import_workspace(folder / 'incoming.tar.gz', destination, args['transferId'], args['sha256'])
    try:
        with stage('build'):
            metadata = {**args['metadata'], 'destinationCwd': imported['cwd'], 'projectRoot': imported['root'],
                        'attachmentsPathMap': imported.get('attachmentsPathMap', [])}
            spec = build_transferred_launch(args['source'], {**profile, 'host': args['target']['host']},
                                            args['messages'], folder, imported['cwd'], metadata)
            ensure_app_server(spec, profile)
    except Exception:
        discard_import(destination, args['transferId'])
        raise
    with stage('bind'):
        spec = bind(spec, profile, args['target']['host'])
    with stage('save'):
        save(folder / 'launch.json', spec)
        save(folder / 'transfer.json', metadata)
    return {'cwd': imported['cwd'], 'root': imported['root'], 'nativeSource': spec.get('nativeSource'), 'connectionMode': spec.get('connectionMode'), 'warnings': spec.get('warnings', []), 'summary': imported.get('summary', {})}


# Same-host handoff (another agent or account on this host). Like `prepare` for a transfer,
# `stage_handoff` validates, builds and binds here, so the board learns the new native
# identity before its terminal opens; the terminal's `handoff <token>` only execs it once.

def handoff_root():
    return private_dir(instance.remote_path('transfersDir', Path.home()) / 'handoffs')


def handoff_token(request_id):
    """One folder per board request, so a repeated request finds its earlier work."""
    if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
        raise ValueError('인계 요청 식별자가 올바르지 않습니다.')
    return hashlib.sha256(('handoff:' + request_id).encode()).hexdigest()[:32]


@contextmanager
def handoff_operation(token):
    if not isinstance(token, str) or not ID.fullmatch(token):
        raise ValueError('인계 식별자가 올바르지 않습니다.')
    with _folder_lock(private_dir(handoff_root() / token), '인계 작업 잠금 파일이 안전하지 않습니다.',
                      '같은 인계를 처리하고 있습니다. 잠시 뒤 터미널이나 작업 목록을 확인해 주세요.') as folder:
        yield folder


def handoff_request(args, check=False):
    """The source selector and target of a same-host handoff request, validated."""
    if (not isinstance(args, dict) or not isinstance(args.get('source'), dict)
            or not isinstance(args.get('target'), dict)):
        raise ValueError('인계 요청 형식이 올바르지 않습니다.')
    source, target = args['source'], args['target']
    selector = {key: source[key] for key in SELECTOR if source.get(key) is not None}
    if check:
        selector.setdefault('host', 'check')
        selector.setdefault('phase', 'idle')
    if (any(not isinstance(value, str) for value in selector.values())
            or not HOST.fullmatch(selector.get('host', ''))):
        raise ValueError('원본 작업 정보가 올바르지 않습니다.')
    if target.get('host', selector['host']) != selector['host']:
        raise ValueError('같은 기기 안의 인계만 이 기기에서 준비할 수 있습니다.')
    if target.get('agent') not in ('claude', 'codex') or not isinstance(target.get('account'), str):
        raise ValueError('대상 에이전트와 계정을 확인해 주세요.')
    return selector, {'host': selector['host'], 'agent': target['agent'], 'account': target['account']}


def handoff_profile(selector, target):
    """This host's target profile and its own boundary verdict; anything unclear refuses."""
    from launch import discover_profiles, source_environment
    import settings
    profile = next((p for p in discover_profiles()
                    if p['id'] == target['account'] and p['agent'] == target['agent']), None)
    if not profile or not profile.get('available'):
        raise ValueError((profile or {}).get('reason') or '이 기기의 대상 계정을 사용할 수 없습니다.')
    source_home = selector.get('home') or ('.claude' if selector.get('agent') == 'claude' else None)
    if selector.get('agent') == profile['agent'] and source_home == profile['home']:
        raise ValueError('같은 계정은 기존 세션 연결로 열어 주세요.')
    environment = source_environment(selector)
    if settings.crosses_boundary(profile['agent'], environment, profile.get('environment', 'default'),
                                 settings.load()):
        raise ValueError('원본과 대상의 실행 환경 경계가 다릅니다.')
    return profile, environment


def handoff_summary(spec, **extra):
    """What `handoff` runs: the binary name, folder and variable names, never values or the prompt."""
    argv = spec.get('argv') or []
    return {'ok': True, 'mode': 'handoff', 'binary': os.path.basename(argv[0]) if argv else None,
            'cwd': spec.get('cwd'), 'envKeys': sorted(spec.get('env') or {}),
            'connectionMode': spec.get('connectionMode') or 'direct', **extra}


def stage_handoff(args):
    from launch import build_launch
    from managed_launch import bind, ensure_app_server
    with stage('validate'):
        if not isinstance(args, dict):
            raise ValueError('인계 요청 형식이 올바르지 않습니다.')
        token = handoff_token(args.get('requestId'))
        route = args.get('routeKey')
        if not isinstance(route, str) or not ROUTE.fullmatch(route):
            raise ValueError('인계 경로 식별자가 올바르지 않습니다.')
        selector, target = handoff_request(args)
    with handoff_operation(token) as folder:
        with stage('validate'):
            if (folder / 'launch.json').exists() or (folder / 'started.json').exists():
                error = ValueError('이미 처리한 인계 요청입니다. 열린 터미널이나 이 기기의 작업 목록을 확인해 주세요.')
                error.transfer_stage = 'prepared'
                raise error
            profile, environment = handoff_profile(selector, target)
        with stage('build'):
            # The handoff document stays in this request's private folder on this host.
            spec = build_launch(selector, {**profile, 'host': selector['host']}, args.get('messages'), folder)
            if spec.get('mode') != 'handoff':
                raise ValueError('이 기기 안의 도구·계정 전환만 준비할 수 있습니다.')
            # The first input goes through the target home's app-server; start it before
            # anything is bound, so a refusal here still means nothing started.
            ensure_app_server(spec, profile)
        with stage('bind'):
            # Route-keyed like the board's own receipt: a later launch of the same route
            # reopens an unattached thread instead of sending the first prompt again.
            spec = bind(spec, profile, selector['host'],
                        receipt_path=handoff_root() / 'native-launches' / (route + '.json'))
        with stage('save'):
            save(folder / 'launch.json', {**spec, 'requestId': args['requestId'], 'source': selector,
                                          'target': target, 'sourceEnvironment': environment,
                                          'preparedAt': int(time.time())})
    return {'token': token, 'cwd': spec['cwd'], 'nativeSource': spec.get('nativeSource'),
            'connectionMode': spec.get('connectionMode'), 'warnings': spec.get('warnings', [])}


def check_handoff(args):
    """`handoff --check -`: validate and build like `stage_handoff`, but bind nothing, start
    nothing and keep no file. Prints what a prepared launch would run."""
    from launch import build_launch
    from managed_launch import app_server_running
    selector, target = handoff_request(args, check=True)
    profile, environment = handoff_profile(selector, target)
    messages = args.get('messages') or CHECK_MESSAGES
    with tempfile.TemporaryDirectory(prefix='handoff-check-') as scratch:
        spec = build_launch(selector, {**profile, 'host': selector['host']}, messages, Path(scratch) / 'state')
    codex = profile['agent'] == 'codex'
    extra = {'appServer': 'running' if app_server_running(profile) else 'start'} if codex else {}
    return handoff_summary({**spec, 'connectionMode': 'attach' if codex else None}, prepared=False,
                           agent=profile['agent'], profile=profile['id'], sourceEnvironment=environment,
                           firstInput='app-server' if codex else 'argument', **extra)


def _prepared_launch(folder):
    path = folder / 'launch.json'
    if not path.exists():
        if (folder / 'started.json').exists():
            raise ValueError('이미 실행한 인계입니다. 보드의 작업 목록에서 새 작업을 확인해 주세요.')
        raise ValueError('준비된 인계를 찾지 못했습니다. 보드에서 다시 실행해 주세요.')
    spec = read_private(path)
    argv, cwd, env = spec.get('argv'), spec.get('cwd'), spec.get('env')
    if (not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv)
            or not os.path.isabs(argv[0]) or not isinstance(cwd, str) or not os.path.isabs(cwd)
            or not isinstance(env, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())):
        raise ValueError('준비된 인계 기록이 올바르지 않습니다.')
    return path, spec


def run_handoff(token, check=False):
    """The terminal step: read the prepared launch, remove it and exec it on this host, once."""
    with handoff_operation(token) as folder:
        path, spec = _prepared_launch(folder)
        if check:
            return handoff_summary(spec, prepared=True, agent=(spec.get('target') or {}).get('agent'),
                                   profile=(spec.get('target') or {}).get('account'),
                                   sourceEnvironment=spec.get('sourceEnvironment'),
                                   firstInput='app-server' if spec.get('connectionMode') == 'attach' else 'argument')
        if not os.path.isdir(spec['cwd']):
            raise ValueError('작업 폴더가 이 기기에 없습니다.')
        if not os.access(spec['argv'][0], os.X_OK):
            raise ValueError('대상 CLI 실행 파일을 찾지 못했습니다.')
        marker = os.open(str(folder / 'started.json'), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(marker, 'w') as out:
            json.dump({'startedAt': int(time.time())}, out)
        path.unlink()
        from managed_launch import mark_attached
        mark_attached(spec, 'handoff-' + token)
    os.chdir(spec['cwd'])
    _execve(spec['argv'], spec['env'])


def handoff_main(args):
    check = '--check' in args
    rest = [item for item in args if item != '--check']
    try:
        if len(rest) != 1:
            raise ValueError('사용법: handoff [--check] <인계 식별자> 또는 handoff --check - (표준 입력에 요청 JSON)')
        if check and rest[0] == '-':
            try:
                request = json.loads(sys.stdin.read(1024 * 1024))
            except ValueError:
                raise ValueError('점검 요청 JSON을 읽지 못했습니다.') from None
            print(json.dumps(check_handoff(request), ensure_ascii=False, indent=2))
        elif check:
            print(json.dumps(run_handoff(rest[0], check=True), ensure_ascii=False, indent=2))
        else:
            run_handoff(rest[0])
    except (ValueError, OSError, KeyError, RuntimeError, RpcError) as exc:
        # A terminal shows this line: no input, environment value or traceback.
        from launch import scrub_text
        detail = scrub_text(str(exc))[:300] if isinstance(exc, ValueError) else '기기 상태를 확인해 주세요.'
        print(('인계 점검에 실패했습니다. ' if check else '원격 인계를 시작하지 못했습니다. ') + detail, file=sys.stderr)
        raise SystemExit(1)


def _rpc(request):
    from transfer_native import profiles, source_state, interrupt_source
    from transfer_workspace import inspect_workspace, export_workspace, import_workspace
    op, args = request.get('op'), request.get('args', {})
    if op == 'session_history':
        from history_native import handle
        return handle(args)
    if op == 'chat':
        from native_chat import handle
        return handle(args)
    if op == 'attachment':
        from attachments import receive
        return receive(args)
    if op in ('control_preview', 'control_execute'):
        from session_control import SessionControl
        control = SessionControl()
        if op == 'control_preview':
            return control.preview(args['source'])
        return control.execute(args['source'], args['action'], args.get('targetProof'), args.get('requestId'))
    if op == 'profiles':
        from launch import new_session_status
        tmux = _tmux()
        # The board attaches again with this tmux directly, never through a helper file
        # that a later deploy may remove.
        return {'profiles': profiles(), 'home': str(Path.home()), 'launcher': new_session_status(),
                'holds': bool(tmux), 'holdTmux': tmux or None}
    if op == 'resume_preview':
        from launch import resume_preview
        return resume_preview(args['source'])
    if op == 'launcher_programs':
        from launcher_programs import launcher_programs
        return launcher_programs(args)
    if op == 'hold_list':
        return hold_list()
    if op == 'hold_kill':
        return hold_kill(args.get('name'))
    if op == 'leftovers':
        return sweep_leftovers()
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
        return prepare(args, profiles, import_workspace)
    if op == 'stage_handoff':
        return stage_handoff(args)
    if op == 'start_app_server':
        from launch import start_app_server
        return start_app_server(args.get('home'))
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
                        instance.remote_path('attachStateDir', Path.home()))
    if spec['mode'] not in ('attach', 'resume'):
        raise ValueError('원격 기존 세션 연결만 지원합니다.')
    os.chdir(spec['cwd'])
    _execve(spec['argv'], spec['env'])


# ---------- runs kept in this host's tmux ----------

def _tmux():
    import collector
    return collector.which('tmux')


def _hold_command(tmux, *args):
    return [tmux, '-L', HOLD_SOCKET, '-f', '/dev/null', *args]


def _hold_name(name):
    if not isinstance(name, str) or not HOLD_NAME.fullmatch(name):
        raise ValueError('실행 보관 이름이 올바르지 않습니다.')
    return name


def _client_env():
    """The attaching client's environment: this SSH session's own, never a tmux pane's."""
    return {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}


def _hold_run(tmux, *args, **kwargs):
    """One call to the hold server, bounded; a call that does not answer counts as failed."""
    try:
        return subprocess.run(_hold_command(tmux, *args), timeout=HOLD_TIMEOUT, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, '', '')


def _hold_alive(tmux, name):
    return _hold_run(tmux, 'has-session', '-t', '=' + name,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _hold_server(tmux):
    return _hold_run(tmux, 'list-sessions', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _execve(argv, env):
    """A terminal step's final exec: inside its hold session when the board asked for one and
    this host has tmux, else in this SSH session as before."""
    tmux = _tmux() if _hold is not None else None
    if tmux:
        hold_start(tmux, _hold, argv, env)
    os.execve(argv[0], argv, env)


def hold_start(tmux, name, argv, env):
    """Start the run in a detached session of the hold server (argv and environment through a
    private one-use envelope, never tmux's argv), then attach this SSH session to it."""
    name = _hold_name(name)
    if not _hold_alive(tmux, name):
        envelope = private_dir(root() / 'holds') / (name + '.json')
        save(envelope, {'argv': list(argv), 'env': dict(env), 'cwd': os.getcwd()})
        try:
            columns, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            columns, rows = 80, 24
        options = []
        if not _hold_server(tmux):
            # Server options, set once by the session that starts the server.
            for option, value in HOLD_OPTIONS:
                options += [';', 'set-option', '-g', option, value]
            options += [';', 'set-option', '-ga', 'terminal-features', ',xterm-256color:RGB']
        started = _hold_run(tmux, 'new-session', '-d', '-s', name, '-x', str(columns), '-y', str(rows),
                            sys.executable, '-I', sys.argv[0], 'hold-run', str(envelope), *options,
                            env=_client_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if started.returncode != 0 and not _hold_alive(tmux, name):
            if envelope.exists():
                # The run never started there: run it here as before rather than not at all.
                envelope.unlink()
                os.execve(argv[0], argv, env)
            # It started there (its envelope is used) and already ended: never a second run.
            print('[하던 일] 실행이 바로 끝났습니다.', flush=True)
            raise SystemExit(1)
    os.execve(tmux, _hold_command(tmux, 'attach-session', '-t', '=' + name), _client_env())


def hold_run(path):
    """Inside the hold session: the run itself, from its envelope (removed before it starts)."""
    path = Path(path)
    if path.parent != root() / 'holds' or not HOLD_NAME.fullmatch(path.stem):
        raise ValueError('실행 보관 경로가 올바르지 않습니다.')
    data = read_private(path)
    path.unlink()
    os.chdir(data['cwd'])
    env = {key: value for key, value in data['env'].items() if key not in ('TMUX', 'TMUX_PANE')}
    # The terminal the run now draws on is this tmux pane.
    if os.environ.get('TERM'):
        env['TERM'] = os.environ['TERM']
    os.execve(data['argv'][0], data['argv'], env)


def hold_attach(name):
    """Attach again after a dropped connection: never starts anything."""
    name = _hold_name(name)
    tmux = _tmux()
    if not tmux or not _hold_alive(tmux, name):
        print('[하던 일] 이 실행은 이미 끝났습니다.', flush=True)
        raise SystemExit(0)
    os.execve(tmux, _hold_command(tmux, 'attach-session', '-t', '=' + name), _client_env())


def hold_list():
    """The hold sessions on this host, by name and age in seconds (this host's clock)."""
    tmux = _tmux()
    if not tmux:
        return {'holds': []}
    # An envelope left behind (its run never started) holds an environment: removed when old.
    folder = root() / 'holds'
    for envelope in folder.glob('*.json') if folder.is_dir() else []:
        try:
            if time.time() - envelope.lstat().st_mtime > HOLD_STALE_ENVELOPE:
                envelope.unlink()
        except OSError:
            continue
    listed = _hold_run(tmux, 'list-sessions', '-F', '#{session_name} #{session_created}', capture_output=True, text=True)
    if listed.returncode != 0:
        return {'holds': []}  # No hold server running.
    now, holds = int(time.time()), []
    for line in listed.stdout.splitlines():
        name, _, created = line.partition(' ')
        if HOLD_NAME.fullmatch(name) and created.isdigit():
            holds.append({'name': name, 'age': max(0, now - int(created))})
    return {'holds': holds}


def _user_temp():
    """This user's temporary folder, resolved (an SSH session's TMPDIR may not name it)."""
    try:
        path = subprocess.run(['getconf', 'DARWIN_USER_TEMP_DIR'], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.realpath(path).rstrip('/') if path.startswith('/') else None


def _elapsed(text):
    """Seconds of a `ps` elapsed time, `[[dd-]hh:]mm:ss`."""
    days, _, clock = text.rpartition('-')
    parts = [int(part) for part in clock.split(':')]
    if not 2 <= len(parts) <= 3:
        raise ValueError('elapsed')
    hours, minutes, seconds = ([0] + parts)[-3:]
    return int(days or 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def sweep_leftovers(listing=None, temp=None, sockets=None, now=None, kill=os.kill):
    """End what tests left on this host and say how much: Codex app-server daemons (a daemon
    has no parent process, whoever started it) running for an hour or more from a Codex home
    right inside a temporary folder a test made (`<this user's temporary folder>/tmp…/.codex…`),
    and socket files of ended test tmux servers (tmux keeps them) a day old. Nothing else
    matches: a user's own Codex homes are not in the temporary folder, and the board's and
    the user's tmux servers have other names."""
    ended = removed = 0
    temp = _user_temp() if temp is None else temp
    if temp:
        if listing is None:
            try:
                listing = subprocess.run(['ps', '-axwwo', 'pid=,ppid=,uid=,etime=,args='], capture_output=True,
                                         text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                listing = ''
        for line in listing.splitlines():
            fields = line.split(None, 5)
            if len(fields) < 6:
                continue
            pid, ppid, uid, elapsed, program, rest = fields
            home = program[len(temp) + 1:].split('/packages/app-server-daemon/', 1)[0].split('/')
            if (not pid.isdigit() or ppid != '1' or uid != str(os.getuid()) or not program.startswith(temp + '/')
                    or len(home) != 2 or not home[0].startswith('tmp') or not home[1].startswith('.codex')
                    or '/packages/app-server-daemon/' not in program or not program.endswith('/bin/codex')
                    or rest.split(' ', 1)[0] != 'app-server'):
                continue
            try:
                if _elapsed(elapsed) < LEFTOVER_PROCESS_AGE:
                    continue
                kill(int(pid), signal.SIGTERM)
                ended += 1
            except (ValueError, OSError):
                continue
    folder = Path(sockets) if sockets is not None else Path(os.environ.get('TMUX_TMPDIR') or '/tmp') / ('tmux-%d' % os.getuid())
    now = time.time() if now is None else now
    try:
        paths = [path for path in folder.iterdir() if path.name.startswith(TEST_SOCKET_PREFIXES)]
    except OSError:
        paths = []
    for path in paths:
        try:
            info = path.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or now - info.st_mtime < LEFTOVER_SOCKET_AGE):
                continue
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(str(path))
                continue  # A server still answers there: not left over.
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    continue  # Only a refused connection says no server is there.
            finally:
                probe.close()
            path.unlink()
            removed += 1
        except OSError:
            continue
    return {'processes': ended, 'sockets': removed}


def hold_kill(name):
    name = _hold_name(name)
    tmux = _tmux()
    if not tmux or not _hold_alive(tmux, name):
        return {'killed': False}
    done = _hold_run(tmux, 'kill-session', '-t', '=' + name, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'killed': done.returncode == 0}


def new_session_main(preset=None):
    """The terminal step of "새 세션": run this host's own launcher, with its own settings (and
    the chosen preset, see launch.LAUNCH_PRESETS)."""
    from launch import new_session
    try:
        spec = new_session(preset=preset)
        os.chdir(spec['cwd'])
        _execve(spec['argv'], spec['env'])
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else '실행 명령을 시작하지 못했습니다.'
        print('새 세션을 열지 못했습니다. ' + message, file=sys.stderr)
        raise SystemExit(1)


def _main():
    if len(sys.argv) == 3 and sys.argv[1] == 'hold-run':
        try:
            hold_run(sys.argv[2])
        except (ValueError, OSError, KeyError):
            print('보관한 실행을 시작하지 못했습니다.', file=sys.stderr)
            raise SystemExit(1)
    if len(sys.argv) == 3 and sys.argv[1] == 'reattach':
        return hold_attach(sys.argv[2])
    if len(sys.argv) >= 3 and sys.argv[1] == 'handoff':
        return handoff_main(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == 'new-session':
        if len(sys.argv) == 2:
            return new_session_main()
        if len(sys.argv) == 4 and sys.argv[2] == '--preset':
            return new_session_main(sys.argv[3])
        print('새 세션을 열지 못했습니다. 실행 요청 형식이 올바르지 않습니다.', file=sys.stderr)
        raise SystemExit(1)
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
            _execve(spec['argv'], spec['env'])
        raise ValueError('지원하지 않는 이전 명령입니다.')
    try:
        request = json.loads(sys.stdin.buffer.read(30 * 1024 * 1024 + 1))
        result = rpc(request)
        print(json.dumps({'ok': True, 'result': result, 'runtime': sys.argv[0]}, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, KeyError, RpcError) as exc:
        from launch import scrub_text
        reply = {'ok': False, 'error': scrub_text(str(exc))[:1000]}
        if isinstance(getattr(exc, 'transfer_stage', None), str):
            reply['stage'] = exc.transfer_stage
        print(json.dumps(reply, ensure_ascii=False))


def main():
    global _hold
    if len(sys.argv) > 3 and sys.argv[1] == 'hold':
        # `hold <name> <terminal step...>`: the step runs as usual, its final exec in that session.
        _hold = _hold_name(sys.argv[2])
        sys.argv = [sys.argv[0]] + sys.argv[3:]
    if len(sys.argv) > 2 and sys.argv[1] in ('send', 'receive', 'exec'):
        with job_operation(sys.argv[2]):
            return _main()
    return _main()


if __name__ == '__main__': main()
