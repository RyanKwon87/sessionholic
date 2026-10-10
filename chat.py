"""Authenticated board coordinator; clients can choose identities, never host paths."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

MAX_FILE = 20 * 1024 * 1024
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
STATE_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')
CLAUDE_BLOCKED = ('Claude 터미널에 승인·선택 메뉴가 떠 있어 보내지 않았습니다. '
                  '입력이 메뉴 선택으로 처리될 수 있으니 터미널에서 먼저 선택해 주세요.')
# A numbered option under the selection cursor ("❯ 1. Yes") marks a permission or
# question menu, where Enter would confirm the highlighted choice.
CLAUDE_MENU = re.compile(r'^\s*[│|]?\s*[❯›>]\s*\d+\.\s', re.M)
CLAUDE_EXTERNAL = ('이 Claude 세션은 보드 밖 터미널에서 실행 중이라 웹에서 입력할 수 없습니다. '
                   '그 터미널에서 입력하거나 백그라운드 세션(claude --bg)으로 실행해 주세요.')
CLAUDE_READY_SECONDS = 12
CLAUDE_PROBE = 160


class SendRejected(ValueError):
    """Validation failed before the native send boundary was entered."""
    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.status = status


class Chat:
    def __init__(self, board, workflow, state_dir):
        self.board, self.workflow = board, workflow
        self.root = Path(state_dir) / 'chat'
        self.lock = threading.RLock()
        self.cache = {}
        self.cache_epoch = 0
        self.read_sequence = 0
        self.upload_gate = threading.BoundedSemaphore(2)
        self.claude_lock = threading.Lock()

    @staticmethod
    def identity(source):
        return {k: source.get(k) for k in ('host', 'agent', 'home', 'id')}

    def source(self, ref):
        if not isinstance(ref, dict):
            raise ValueError('작업을 선택해 주세요.')
        # Ordinary messages stay on the selected source even after a transfer.
        # A fresh destination is accepted only from server-written nativeSource metadata.
        try:
            source = self.board.source(ref)
        except ValueError:
            source = None
            for terminal in self.workflow.terminals.list():
                native = terminal.get('nativeSource') or terminal.get('metadata', {}).get('nativeSource')
                if isinstance(native, dict) and self.identity(native) == self.identity(ref or {}):
                    source = {**native, 'title': terminal.get('title', '이어가는 작업')}
                    break
            if source is None:
                raise
        host = next(h for h in self.board.hosts if h['name'] == source['host'])
        if not self.board.state[host['name']]['ok']:
            raise RuntimeError('기기 연결을 다시 확인한 뒤 메시지를 보내 주세요.')
        return source, host

    def rpc(self, host, action, source, **kwargs):
        return self.workflow.transfers.rpc(host, 'chat', {'action': action, 'source': source, **kwargs}, timeout=20)

    @staticmethod
    def attachment_history(items):
        from launch import scrub_text
        from native_chat import _manifest
        return [{**item, 'name': scrub_text(item['name'])} for item in _manifest(items)]

    @staticmethod
    def receipt(result, request_id):
        status = result.get('delivery') or result.get('status') or 'unknown'
        if status == 'rejected': status = 'failed'
        return {'requestId': request_id, 'status': status,
                'confirmed': result.get('confirmed', False),
                'readbackConfirmed': result.get('readbackConfirmed', False),
                'currentSnapshotConfirmed': result.get('currentSnapshotConfirmed', result.get('readbackConfirmed', False)),
                'acknowledged': result.get('acknowledged', False),
                'turnId': result.get('turnId'),
                'reason': result.get('reason'),
                'attachments': Chat.attachment_history(result.get('attachments', []))}

    def state(self, data):
        source, host = self.source(data.get('source'))
        identity = self.identity(source)
        key = json.dumps(identity, sort_keys=True)
        request_id = data.get('requestId')
        if request_id is not None and (not isinstance(request_id, str) or not STATE_REQUEST.fullmatch(request_id)):
            raise ValueError('메시지 요청 식별자가 올바르지 않습니다.')
        additional = data.get('requestIds', [])
        if (not isinstance(additional, list) or len(additional) > 20
                or any(not isinstance(identifier, str) or not STATE_REQUEST.fullmatch(identifier) for identifier in additional)):
            raise ValueError('한 번에 확인할 메시지는 올바른 식별자로 20개까지 선택할 수 있습니다.')
        request_ids = list(dict.fromkeys(([request_id] if request_id else []) + additional))
        if len(request_ids) > 20:
            raise ValueError('한 번에 확인할 메시지는 20개까지 선택할 수 있습니다.')
        with self.lock:
            cached = self.cache.get(key)
            if (not request_ids and cached and cached[3] == source.get('cwd')
                    and time.monotonic() - cached[0] < 2):
                return cached[1]
            self.read_sequence += 1
            sequence, epoch = self.read_sequence, self.cache_epoch
        kwargs = {'requestId': request_id} if request_id else {}
        if additional:
            kwargs['requestIds'] = additional
        result = self.rpc(host, 'read', source, **kwargs)
        caps = result.get('capabilities', {})
        phase = result.get('phase', 'unavailable')
        if phase in ('unknown', 'notLoaded'): phase = 'unavailable'
        from server import scrub_payload
        messages = scrub_payload(result.get('messages', []))
        claude = source.get('agent') == 'claude'
        receipts = []
        for request_id in request_ids:
            if claude:
                receipt = self._claude_receipt(source, request_id, messages)
            else:
                receipt = next((receipt for receipt in result.get('receipts', [])
                                if isinstance(receipt, dict) and receipt.get('requestId') == request_id),
                               {'delivery': 'unknown', 'reason': '전송 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.'})
            receipts.append(self.receipt(receipt, request_id))
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get('attachments'), list):
                from launch import scrub_text
                message['attachments'] = [{**item, 'name': scrub_text(item['name'])}
                                          if isinstance(item, dict) and isinstance(item.get('name'), str) else item
                                          for item in message['attachments']]
        if claude:
            supported, reason = self._claude_capability(source, phase)
        else:
            supported, reason = bool(caps.get('canSend')), caps.get('reason') or result.get('reason')
        response = {'route': identity, 'phase': phase,
                    'capability': {'supported': supported,
                                   'attachments': bool(source.get('cwd')),
                                   'reason': None if supported else reason},
                    'messages': messages,
                    'receipts': receipts, 'maxAttachmentBytes': MAX_FILE,
                    'requiresNativeTerminal': phase == 'needs_input' if claude else
                    result.get('requiresNativeTerminal', phase == 'needs_input')}
        with self.lock:
            cached = self.cache.get(key)
            # Receipt reads are request-specific. A send or a newer read must
            # also prevent an earlier in-flight snapshot from repopulating cache.
            if (not request_ids and epoch == self.cache_epoch
                    and (cached is None or sequence > cached[2])):
                if key not in self.cache and len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (time.monotonic(), response, sequence, source.get('cwd'))
        return response

    # ---------- Claude: input through a board-owned terminal ----------
    @staticmethod
    def _same_claude(native, source):
        if not isinstance(native, dict) or native.get('agent') != 'claude' or native.get('host') != source.get('host'):
            return False
        ids = {source.get('id'), source.get('sessionId')} - {None}
        return native.get('id') in ids or native.get('sessionId') in ids

    def _claude_terminal(self, source):
        for terminal in self.workflow.terminals.list():
            native = terminal.get('nativeSource') or terminal.get('metadata', {}).get('nativeSource')
            if terminal.get('alive') and not terminal.get('closed') and self._same_claude(native, source):
                return terminal
        return None

    def _claude_capability(self, source, phase):
        # A session waiting for the user can still take a reply; a menu on its
        # screen is checked at send time, right before anything is pasted.
        try:
            if self._claude_terminal(source):
                return True, None
        except (RuntimeError, OSError):
            return False, '보드 터미널 상태를 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.'
        if source.get('kind') == 'background':
            return True, None
        return False, CLAUDE_EXTERNAL

    def _open_claude_terminal(self, ref, source, request_id):
        """Attach the exact background session in a board terminal, never a new session."""
        plan = self.workflow.plan({'source': ref, 'target': {
            'host': source['host'], 'agent': 'claude', 'account': 'claude:default'}})
        if not plan.get('allowed'):
            raise SendRejected(plan.get('reason') or CLAUDE_EXTERNAL, 409)
        launch_id = 'chat_' + hashlib.sha256(request_id.encode()).hexdigest()[:40]
        terminal = self.workflow.launch(plan['id'], launch_id)['terminal']
        # The attach UI redraws while it loads; paste only after it settles.
        deadline, after, seen, quiet = time.monotonic() + CLAUDE_READY_SECONDS, 0, False, time.monotonic()
        while time.monotonic() < deadline:
            chunk = self.workflow.terminals.read(terminal['id'], after, 0.25)
            if chunk['after'] != after:
                after, seen, quiet = chunk['after'], True, time.monotonic()
            elif seen and time.monotonic() - quiet >= 0.8:
                return terminal
            if not chunk.get('alive', True):
                break
        if not seen:
            raise SendRejected('Claude 터미널이 준비되지 않았습니다. 터미널을 열어 상태를 확인해 주세요.', 409)
        return terminal

    def _claude_record_path(self, source, request_id):
        folder = self.root / 'claude-sends'
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('메시지 전송 기록 경로가 안전하지 않습니다.')
        key = hashlib.sha256(json.dumps([self.identity(source), request_id], sort_keys=True).encode()).hexdigest()
        return folder / (key + '.json')

    @staticmethod
    def _probe(text):
        # A hash of the collapsed opening text; the message body itself is never stored.
        collapsed = ' '.join(str(text or '').split())[:CLAUDE_PROBE]
        return collapsed, hashlib.sha256(collapsed.encode()).hexdigest()

    def _claude_read_record(self, source, request_id):
        try:
            path = self._claude_record_path(source, request_id)
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        except (OSError, ValueError):
            return None
        with os.fdopen(fd, 'rb') as file:
            try:
                record = json.loads(file.read(16384))
            except ValueError:
                return None
        return record if isinstance(record, dict) else None

    def _claude_receipt(self, source, request_id, messages):
        record = self._claude_read_record(source, request_id)
        if not record:
            return {'delivery': 'unknown', 'reason': '전송 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.'}
        receipt = dict(record.get('receipt') or {})
        probe, length = record.get('probe'), record.get('probeLength')
        if receipt.get('acknowledged') and isinstance(probe, str) and isinstance(length, int):
            for message in messages:
                if (isinstance(message, dict) and message.get('role') == 'user'
                        and (message.get('ts') or 0) >= record.get('sentAt', 0) - 5):
                    collapsed = ' '.join(str(message.get('text') or '').split())[:length]
                    if hashlib.sha256(collapsed.encode()).hexdigest() == probe:
                        receipt.update(readbackConfirmed=True, currentSnapshotConfirmed=True,
                                       reason=None)
                        break
        return receipt

    def _send_claude(self, ref, source, host, request_id, text, ids, manifest):
        if not self.claude_lock.acquire(timeout=30):
            raise SendRejected('다른 메시지를 전달하고 있습니다. 잠시 뒤 다시 보내 주세요.', 409)
        try:
            previous = self._claude_read_record(source, request_id)
            if previous:
                return {**(previous.get('receipt') or {}), 'attachments': manifest}
            try:
                current = self.rpc(host, 'capabilities', source)
            except (RuntimeError, OSError):
                raise SendRejected('Claude 세션 상태를 확인하지 못해 보내지 않았습니다. 다시 시도해 주세요.', 409) from None
            if current.get('phase') == 'done' and source.get('kind') != 'background':
                raise SendRejected(CLAUDE_EXTERNAL, 409)
            try:
                terminal = self._claude_terminal(source)
            except (RuntimeError, OSError):
                raise SendRejected('보드 터미널 상태를 확인하지 못해 보내지 않았습니다. 잠시 뒤 다시 시도해 주세요.', 409) from None
            if terminal is None:
                if source.get('kind') != 'background':
                    raise SendRejected(CLAUDE_EXTERNAL, 409)
                try:
                    terminal = self._open_claude_terminal(ref, source, request_id)
                except SendRejected:
                    raise
                except (ValueError, RuntimeError, OSError) as exc:
                    raise SendRejected(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else
                                       'Claude 터미널을 열지 못했습니다.', 409) from None
            try:
                screen = self.workflow.terminals.capture(terminal['id'])
            except (RuntimeError, ValueError, KeyError, OSError):
                raise SendRejected('Claude 터미널 화면을 확인하지 못해 보내지 않았습니다. 다시 시도해 주세요.', 409) from None
            if CLAUDE_MENU.search(screen):
                raise SendRejected(CLAUDE_BLOCKED, 409)
            lines = [text.strip()] if text.strip() else []
            for aid in ids:
                record = json.loads((self.root / 'attachments' / (aid + '.json')).read_text())
                lines.append('첨부 파일: ' + record['attachment']['path'])
            body = '\n'.join(lines)
            probe, digest = self._probe(body)
            path = self._claude_record_path(source, request_id)
            base = {'source': self.identity(source), 'requestId': request_id, 'turnId': None,
                    'canRetry': False, 'terminalId': terminal['id']}
            unknown = {**base, 'delivery': 'unknown', 'confirmed': False, 'readbackConfirmed': False,
                       'acknowledged': False,
                       'reason': '전달 결과를 확인하지 못했습니다. 같은 입력을 자동으로 다시 보내지 않습니다.'}
            # Reserve the request before any byte reaches the CLI (at-most-once).
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                saved = self._claude_read_record(source, request_id) or {}
                return {**(saved.get('receipt') or unknown), 'attachments': manifest}
            record = {'requestId': request_id, 'sentAt': time.time(), 'probe': digest,
                      'probeLength': len(probe), 'receipt': unknown}
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(record, file)
            from terminal import InputNotSent
            try:
                self.workflow.terminals.paste(terminal['id'], body, submit=True)
                receipt = {**base, 'delivery': 'accepted', 'confirmed': True, 'acknowledged': True,
                           'readbackConfirmed': False,
                           'reason': 'Claude 터미널에 입력했습니다. 대화 기록 반영은 아직 확인되지 않았습니다.'}
            except InputNotSent as exc:
                path.unlink(missing_ok=True)
                raise SendRejected(str(exc), 409) from None
            except (RuntimeError, ValueError, OSError):
                receipt = unknown
            from server import private_json
            try:
                private_json(path, {**record, 'receipt': receipt})
            except (OSError, ValueError):
                receipt = {**receipt, 'ledgerPending': True}
            return {**receipt, 'attachments': manifest}
        finally:
            self.claude_lock.release()

    def upload(self, ref, name, content):
        source, host = self.source(ref)
        if not isinstance(name, str) or len(name.encode()) > 512:
            raise ValueError('파일 이름이 너무 깁니다.')
        if not content or len(content) > MAX_FILE:
            raise ValueError('첨부 파일은 1바이트부터 20MiB까지 보낼 수 있습니다.')
        if not self.upload_gate.acquire(blocking=False):
            raise RuntimeError('다른 파일을 전송 중입니다. 완료 뒤 다시 선택해 주세요.')
        try:
            # Fresh native read supplies actual cwd; snapshot cwd cannot drift unnoticed.
            current = self.rpc(host, 'read', source)
            cwd = current.get('cwd')
            if not cwd or cwd != source.get('cwd'):
                raise ValueError('실제 세션의 작업 폴더를 확인하지 못했습니다. 목록을 새로 읽어 주세요.')
            attachment = self.workflow.transfers.rpc(host, 'attachment', {
                'cwd': cwd, 'name': name, 'data': base64.b64encode(content).decode('ascii'),
                'sha256': hashlib.sha256(content).hexdigest()}, timeout=60)
            from server import private_json
            record = {'source': self.identity(source), 'cwd': cwd, 'attachment': attachment}
            private_json(self.root / 'attachments' / (attachment['id'] + '.json'), record)
            return {'attachment': {**attachment, 'type': attachment.get('mime', 'application/octet-stream')}}
        finally:
            self.upload_gate.release()

    def _prepare_send(self, data):
        source, host = self.source(data.get('source'))
        request_id, text = data.get('requestId'), data.get('text', '')
        if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise ValueError('메시지 요청 식별자가 필요합니다.')
        if not isinstance(text, str) or len(text.encode()) > 32768:
            raise ValueError('메시지는 32KiB까지 보낼 수 있습니다.')
        ids = data.get('attachments', [])
        if not isinstance(ids, list) or len(ids) > 8 or len(set(str(i) for i in ids)) != len(ids):
            raise ValueError('한 메시지에는 첨부 파일을 8개까지 보낼 수 있습니다.')
        if not text.strip() and not ids:
            raise ValueError('메시지나 첨부 파일을 추가해 주세요.')
        manifest = []
        for aid in ids:
            if not isinstance(aid, str) or not re.fullmatch('[a-f0-9]{32}', aid):
                raise ValueError('첨부 식별자가 올바르지 않습니다.')
            path = self.root / 'attachments' / (aid + '.json')
            if path.is_symlink() or not path.is_file():
                raise ValueError('첨부 기록을 확인하지 못했습니다. 다시 선택해 주세요.')
            record = json.loads(path.read_text())
            if record.get('source') != self.identity(source) or record.get('cwd') != source.get('cwd'):
                raise ValueError('다른 세션에 올린 파일은 이 메시지에 첨부할 수 없습니다.')
            item = record['attachment']
            manifest.append({'id': item['id'], 'name': item['name'], 'type': item['mime'],
                             'size': item['size'], 'kind': 'image' if item['mime'].startswith('image/') else 'file', 'source': 'sessionholic'})
        return source, host, request_id, text, ids, self.attachment_history(manifest)

    def send(self, data):
        try:
            source, host, request_id, text, ids, manifest = self._prepare_send(data)
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '메시지와 첨부 정보를 다시 확인해 주세요.'
            raise SendRejected(reason, 409 if isinstance(exc, RuntimeError) else 400) from None
        if source.get('agent') == 'claude':
            result = self._send_claude(data.get('source'), source, host, request_id, text, ids, manifest)
        else:
            from transfer import WorkerError
            try:
                result = self.rpc(host, 'send', source, requestId=request_id,
                                  input=[{'type': 'text', 'text': text}] if text.strip() else [], attachmentIds=ids)
            except WorkerError as exc:
                # The helper finished and reported a refusal; native_chat keeps every
                # failure after turn/start inside its receipt, so nothing was sent.
                raise SendRejected(str(exc) or '메시지를 보내지 못했습니다. 초안은 유지됩니다.', 409) from None
            except (RuntimeError, OSError):
                # A broken SSH/HTTP response is not evidence that submission failed.
                result = {'delivery': 'unknown', 'reason': '전송 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.'}
        if manifest and not result.get('attachments'):
            result = {**result, 'attachments': manifest}
        receipt = self.receipt(result, request_id)
        with self.lock:
            self.cache_epoch += 1
            self.cache.pop(json.dumps(self.identity(source), sort_keys=True), None)
        return {'status': receipt['status'], 'receipt': receipt}
