"""Authenticated board coordinator; clients can choose identities, never host paths."""
import base64
import hashlib
import json
from pathlib import Path
import re
import threading
import time

MAX_FILE = 20 * 1024 * 1024
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
STATE_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')


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
                'turnId': result.get('turnId'), 'queueId': result.get('queueId'),
                'reason': result.get('reason'),
                'attachments': Chat.attachment_history(result.get('attachments', []))}

    @staticmethod
    def queue_history(result):
        """Old/malformed workers must not report an authoritative empty queue."""
        unknown = {'available': False, 'complete': False, 'total': None, 'truncated': False,
                   'reason': '예약 대기열을 확인하지 못했습니다.'}
        state, rows = result.get('queueState'), result.get('queuedMessages')
        if (not isinstance(state, dict) or not isinstance(rows, list) or len(rows) > 50
                or not {'available', 'complete', 'total', 'truncated'} <= state.keys()
                or any(type(state.get(key)) is not bool for key in ('available', 'complete', 'truncated'))
                or (state.get('total') is not None and (type(state['total']) is not int or state['total'] < len(rows)))
                or (state['available'] and state.get('total') is None)
                or (not state['available'] and (rows or state['total'] is not None))
                or (state['complete'] and (not state['available'] or state['truncated'] or state['total'] != len(rows)))):
            return [], unknown
        out, total_bytes = [], 0
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('queueId'), str) or not row['queueId'] or len(row['queueId']) > 256
                    or (row.get('requestId') is not None and (not isinstance(row['requestId'], str) or len(row['requestId']) > 128))
                    or not isinstance(row.get('text'), str) or len(row['text']) > 32768
                    or not isinstance(row.get('attachments'), list)
                    or row.get('status') != 'queued' or row.get('confirmed') is not True
                    or row.get('readbackConfirmed') is not True
                    or (state['complete'] and (row.get('textTruncated') is True or row.get('contentAvailable') is not True))):
                return [], unknown
            total_bytes += len(row['text'].encode())
            if total_bytes > 256 * 1024:
                return [], unknown
            attached = []
            for item in row['attachments'][:40]:
                if not isinstance(item, dict):
                    continue
                if item.get('source') == 'sessionholic':
                    attached.extend(Chat.attachment_history([item]))
                elif item.get('source') == 'native' and item.get('kind') in ('image', 'file'):
                    image = item['kind'] == 'image'
                    attached.append({'id': None, 'name': '이미지' if image else '파일',
                                     'type': 'image/*' if image else 'application/octet-stream',
                                     'size': None, 'kind': item['kind'], 'source': 'native'})
            out.append({'queueId': row['queueId'], 'requestId': row.get('requestId'), 'text': row['text'],
                        'textTruncated': row.get('textTruncated') is True, 'contentAvailable': row.get('contentAvailable') is True,
                        'attachments': attached, 'status': 'queued', 'confirmed': True,
                        'readbackConfirmed': True, 'currentSnapshotConfirmed': True, 'acknowledged': row.get('acknowledged') is True})
        from launch import scrub_text
        reason = state.get('reason')
        return out, {key: state[key] for key in ('available', 'complete', 'total', 'truncated')} | {
            'reason': scrub_text(reason[:1000]) if isinstance(reason, str) else None}

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
        receipts = []
        for request_id in request_ids:
            receipt = next((receipt for receipt in result.get('receipts', [])
                            if isinstance(receipt, dict) and receipt.get('requestId') == request_id),
                           {'delivery': 'unknown', 'reason': '전송 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.'})
            receipts.append(self.receipt(receipt, request_id))
        from server import scrub_payload
        messages = scrub_payload(result.get('messages', []))
        queued, queue_state = self.queue_history(result)
        queued = scrub_payload(queued)
        for message in messages + queued:
            if isinstance(message, dict) and isinstance(message.get('attachments'), list):
                from launch import scrub_text
                message['attachments'] = [{**item, 'name': scrub_text(item['name'])}
                                          if isinstance(item, dict) and isinstance(item.get('name'), str) else item
                                          for item in message['attachments']]
        response = {'route': identity, 'phase': phase,
                    'capability': {'supported': bool(caps.get('canSend')),
                                   'attachments': bool(source.get('cwd')),
                                   'reason': caps.get('reason') or result.get('reason')},
                    'messages': messages,
                    'queuedMessages': queued,
                    'queueState': queue_state,
                    'receipts': receipts, 'maxAttachmentBytes': MAX_FILE,
                    'requiresNativeTerminal': result.get('requiresNativeTerminal', phase == 'needs_input')}
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
        try:
            result = self.rpc(host, 'send', source, requestId=request_id,
                              input=[{'type': 'text', 'text': text}] if text.strip() else [], attachmentIds=ids)
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
