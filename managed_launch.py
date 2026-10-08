"""Bind a board launch and its initial prompt to one durable native identity."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid


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
            queued = client.call('thread/queue/add', {'threadId': identity['id'],
                'clientUserMessageId': request_id, 'input': [{'type': 'text', 'text': prompt}]}).get('queuedSubmission', {})
            if queued.get('clientUserMessageId') != request_id or not queued.get('id'):
                raise ValueError('첫 메시지 접수 결과를 확인하지 못했습니다.')
            record.update(delivery='queued', queueId=queued['id']); _save(receipt_path, record)
            try:
                client.call('thread/queue/start', {'threadId': identity['id'], 'queuedSubmissionId': queued['id']})
            except NativeRejected:
                # Automatic dispatch can already have started this exact queue item.
                pass
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
