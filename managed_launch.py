"""Bind a board launch and its initial prompt to one durable native identity."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

APP_SERVER_START_SECONDS = 60
APP_SERVER_WAIT_SECONDS = 10
# A failed start waits this long for a daemon another launch is starting.
APP_SERVER_RACE_SECONDS = 5
APP_SERVER_FAILED = ('이 기기에서 대상 Codex 계정의 앱 서버를 시작하지 못했습니다. '
                     '그 계정으로 Codex를 한 번 실행한 뒤 다시 시도해 주세요.')


def _save(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('실행 기록 경로는 링크를 사용할 수 없습니다.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_uid != os.getuid():
        raise ValueError('실행 기록 소유자를 확인해 주세요.')
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False); out.flush(); os.fsync(out.fileno())
        os.replace(str(temp), str(path))
    finally:
        temp.unlink(missing_ok=True)


def _resume(spec, identity, socket, receipt_path):
    return {**spec, 'nativeSource': identity, 'connectionMode': 'attach',
            'nativeReceiptPath': str(receipt_path),
            'argv': [spec['argv'][0], 'resume', identity['id'], '--remote',
                     'unix://' + str(socket), '--cd', spec['cwd']]}


def app_server_running(profile, home=None):
    """Whether this Codex home's app-server control socket is there and safe to use."""
    from transfer_native import _socket_path
    try:
        _socket_path({'home': profile['home']}, home)
        return True
    except ValueError:
        return False


def ensure_app_server(spec, profile, home=None):
    """A Codex target's app-server, started the way the account's own CLI starts it.

    The first input goes through this socket, and a home unused for a while has no daemon.
    `codex app-server daemon start` starts one (and does nothing when one runs) with this
    launch's own variables, so the daemon stays inside the terminal's environment boundary.
    Nothing is sent to a model here. Returns True when this call started it."""
    if profile.get('agent') != 'codex' or app_server_running(profile, home):
        return False
    import launch
    argv, env = spec.get('argv') or [], spec.get('env') or {}
    expected = (launch._home(home) / profile['home']).resolve()
    if (not argv or not os.path.isabs(argv[0]) or not os.access(argv[0], os.X_OK)
            or not env.get('CODEX_HOME') or Path(env['CODEX_HOME']).resolve() != expected):
        raise ValueError(APP_SERVER_FAILED)
    # Later sessions of this account (from the user's own launcher too) join this daemon and
    # run their tools in its variables: a plain launch environment also keeps the user's SSH
    # agent, as their terminal does; one with isolation variables (personal Git·SSH) never.
    import settings
    if 'SSH_AUTH_SOCK' in os.environ and settings.ENV_KEYS.isdisjoint(env):
        env = {**env, 'SSH_AUTH_SOCK': os.environ['SSH_AUTH_SOCK']}
    try:
        result = subprocess.run([argv[0], 'app-server', 'daemon', 'start'], env=env, cwd=str(expected),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=APP_SERVER_START_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    deadline = time.monotonic() + APP_SERVER_WAIT_SECONDS
    if result is not None and result.returncode != 0:
        # Another launch may be starting the same account's daemon: its socket, if it comes.
        deadline = min(deadline, time.monotonic() + APP_SERVER_RACE_SECONDS)
    while not app_server_running(profile, home):
        if result is None or time.monotonic() >= deadline:
            raise ValueError(APP_SERVER_FAILED)
        time.sleep(0.2)
    return True


def mark_attached(spec, terminal_id):
    if spec.get('nativeReceiptPath'):
        path = Path(spec['nativeReceiptPath'])
        record = json.loads(path.read_text())
        if record.get('delivery') != 'unknown':
            record['terminalId'] = terminal_id
            _save(path, record)


def bind(spec, profile, host, *, home=None, receipt_path=None):
    if spec.get('mode') not in ('handoff', 'transfer'):
        return spec
    spec = dict(spec)
    agent, key = profile['agent'], profile['home']
    identity = {'host': host, 'agent': agent, 'home': key, 'cwd': spec['cwd']}
    if agent != 'codex':
        identity.update(id=str(uuid.uuid4()), kind='interactive')
        identity['sessionId'] = identity['id']
        spec.update(nativeSource=identity, argv=[spec['argv'][0], '--session-id', identity['id'], spec['argv'][-1]])
        return spec
    from transfer_native import _socket_path
    from native_chat import _client, NativeRejected
    from collector import RpcError
    selector = {**identity, 'id': str(uuid.uuid4())}
    ensure_app_server(spec, profile, home)
    socket = _socket_path(selector, home)
    receipt_path = Path(receipt_path or (spec['handoffPath'] + '.native.json'))
    prompt = spec['argv'][-1]
    # A connection failure never sends the first prompt twice. An orphaned launch
    # can only reopen its recorded thread; a successful TUI gets a new generation.
    if receipt_path.exists():
        if receipt_path.is_symlink() or receipt_path.stat().st_uid != os.getuid() or receipt_path.stat().st_mode & 0o077:
            raise ValueError('실행 기록의 권한을 확인해 주세요.')
        prior = json.loads(receipt_path.read_text())
        if not prior.get('terminalId'):
            native = prior['nativeSource']
            if any(native.get(k) != v for k, v in identity.items()):
                raise ValueError('미확인 실행 대상이 다릅니다. 원래 실행 기록을 확인해 주세요.')
            return _resume(spec, native, socket, receipt_path)
    request_id = 'handoff-' + uuid.uuid4().hex
    with _client(selector, home, time.monotonic() + 15) as client:
        # legacy is supported by both installed versions and the existing reader.
        thread = client.call('thread/start', {'cwd': spec['cwd'], 'historyMode': 'legacy'}).get('thread', {})
        identity['id'] = str(uuid.UUID(thread.get('id', '')))
        if thread.get('cwd') != spec['cwd']:
            raise ValueError('새 세션의 작업 경로가 일치하지 않아 실행을 중단했습니다.')
        record = {'nativeSource': identity, 'requestId': request_id, 'delivery': 'unknown',
                  'promptSha256': hashlib.sha256(prompt.encode()).hexdigest()}
        _save(receipt_path, record)
        # Empty threads cannot yet be resumed by these CLI versions. Materialize
        # the already-authorized handoff prompt natively before attaching the TUI.
        try:
            # turn/start submits the prompt directly; there is no board-side queue.
            started = client.call('turn/start', {'threadId': identity['id'],
                'clientUserMessageId': request_id, 'input': [{'type': 'text', 'text': prompt}]}).get('turn', {})
            if not started.get('id'):
                raise ValueError('첫 메시지 접수 결과를 확인하지 못했습니다.')
            record.update(delivery='accepted', turnId=started['id']); _save(receipt_path, record)
            # Wait for a resumable history without resubmitting the prompt.
            # Empty-thread reads can briefly reject before the first user item.
            deadline = time.monotonic() + 2
            while True:
                try:
                    visible = client.call('thread/read', {'threadId': identity['id'], 'includeTurns': True}).get('thread', {})
                    if visible.get('id') == identity['id'] and visible.get('turns'):
                        record['materialized'] = True
                        break
                except NativeRejected as exc:
                    if exc.code != -32600:
                        break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            _save(receipt_path, record)
        except (OSError, RuntimeError, ValueError, RpcError):
            # Keep the UUID and unknown receipt. Never start another model here.
            record['delivery'] = 'unknown'; _save(receipt_path, record)
    return _resume(spec, identity, socket, receipt_path)
