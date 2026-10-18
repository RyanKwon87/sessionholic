"""Private text-history archives and a restart-safe retention journal.

No native process is stopped. Project files and attachments are never archived.
Native operations are supplied by the board's existing owning-host RPC.
"""
from contextlib import contextmanager
import copy
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

import history_native

DAY = 86400
MAX_RECORDS = 10000
MAX_STATE = 16 * 1024 * 1024
DEFAULT_POLICY = {'enabled': True, 'idleDays': 14, 'graceDays': 7}
KEY = re.compile('[0-9a-f]{64}')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def identity(source):
    if (not isinstance(source, dict) or source.get('agent') not in ('codex', 'claude')
            or not re.fullmatch('[A-Za-z0-9_.-]{1,40}', source.get('host') or '')
            or not isinstance(source.get('id'), str) or not source['id'] or len(source['id']) > 128
            or not isinstance(source.get('home'), (str, type(None)))):
        raise ValueError('세션 식별자가 올바르지 않습니다.')
    return {k: source.get(k) for k in ('host', 'agent', 'home', 'id', 'sessionId')}


def key(source):
    return hashlib.sha256(encode(identity(source))).hexdigest()


def _read(path, limit):
    if path.parent.resolve() != path.parent.absolute():
        raise ValueError('대화 보존 경로에 심볼릭 링크가 있습니다.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('대화 보존 파일의 종류·권한·크기를 확인해 주세요.')
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('대화 보존 파일이 한도를 넘습니다.')
    return payload


def _save(path, payload):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def summarize(messages):
    """An extractive summary: bounded verbatim evidence, with no invented completion claim."""
    users = [m for m in messages if m['role'] == 'user']
    answers = [m for m in messages if m['role'] == 'assistant']
    if not users or not answers:
        raise ValueError('요약할 요청과 답변을 모두 확인하지 못했습니다.')
    return {'method': 'extractive', 'purpose': users[0]['text'][:1600],
            'lastRequest': users[-1]['text'][:1600], 'lastAnswer': answers[-1]['text'][:4000],
            'messageCount': len(messages), 'note': '대화에서 발췌한 요약입니다. 작업 완료 여부는 원문으로 확인하세요.'}


class Histories:
    def __init__(self, state_dir, rpc, clock=time.time):
        self.root = Path(state_dir) / 'session-history'
        self.rpc, self.clock = rpc, clock
        self.lock, self.sweeping, self.stop = threading.RLock(), threading.Lock(), threading.Event()
        self.worker_enabled = False

    @contextmanager
    def storage(self):
        with self.lock:
            for path in reversed((self.root,) + tuple(self.root.parents)):
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    raise ValueError('대화 보존 경로가 안전하지 않습니다.')
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.stat().st_uid != os.getuid():
                raise ValueError('대화 보존 폴더의 소유자를 확인해 주세요.')
            self.root.chmod(0o700)
            fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise ValueError('대화 보존 잠금 파일을 확인해 주세요.')
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _load(self):
        try:
            data = json.loads(_read(self.root / 'journal.json', MAX_STATE))
        except FileNotFoundError:
            return {'version': 1, 'policy': dict(DEFAULT_POLICY), 'records': {}}
        if (not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('records'), dict)
                or len(data['records']) > MAX_RECORDS):
            raise ValueError('대화 보존 상태를 확인하지 못했습니다.')
        self._policy(data.get('policy'))
        for k, row in data['records'].items():
            if not KEY.fullmatch(k) or key(row.get('source')) != k:
                raise ValueError('대화 보존 세션 정보가 올바르지 않습니다.')
        return data

    @staticmethod
    def _policy(value):
        if (not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY)
                or type(value['enabled']) is not bool
                or any(type(value[k]) is not int or not 1 <= value[k] <= 365 for k in ('idleDays', 'graceDays'))):
            raise ValueError('보존 기간은 1~365일 사이로 지정해 주세요.')
        return dict(value)

    def _write(self, data):
        payload = encode(data)
        if len(payload) > MAX_STATE:
            raise ValueError('대화 보존 상태의 저장 한도에 도달했습니다.')
        _save(self.root / 'journal.json', payload)

    @staticmethod
    def _public(k, row):
        return {'key': k, **{name: copy.deepcopy(row.get(name)) for name in (
            'source', 'title', 'state', 'preserved', 'createdAt', 'deleteAfter', 'deletedAt',
            'messageCount', 'archiveBytes', 'sourceBytes', 'reclaimedBytes', 'reason', 'summary')}}

    def status(self):
        with self.storage():
            data = self._load()
            rows = [self._public(k, r) for k, r in data['records'].items()]
            return {'policy': data['policy'], 'records': sorted(rows, key=lambda r: r.get('createdAt') or 0, reverse=True),
                    'running': self.sweeping.locked(), 'workerEnabled': self.worker_enabled}

    def policy(self, value):
        value = self._policy(value)
        with self.storage():
            data = self._load()
            old = data['policy']
            data['policy'] = value
            # A policy change cannot shorten an already granted cancellation period.
            if old != value:
                for row in data['records'].values():
                    if row.get('state') == 'pending':
                        row['deleteAfter'] = max(row['deleteAfter'], self.clock() + value['graceDays'] * DAY)
            self._write(data)
        return self.status()

    def preserve(self, archive_key, value):
        if not isinstance(archive_key, str) or not KEY.fullmatch(archive_key) or type(value) is not bool:
            raise ValueError('보존 요청이 올바르지 않습니다.')
        with self.storage():
            data = self._load()
            row = data['records'].get(archive_key)
            if row is None:
                raise ValueError('보존할 대화를 찾지 못했습니다.')
            row['preserved'] = value
            if row.get('state') not in ('deleted', 'uncertain'):
                row['state'] = 'preserved' if value else ('pending' if row.get('canDelete') else 'archived')
                row['deleteAfter'] = self.clock() + data['policy']['graceDays'] * DAY if not value and row.get('canDelete') else None
            self._write(data)
        return self.status()

    def _archive(self, archive_key, expected=None):
        packed = _read(self.root / (archive_key + '.json.gz'), history_native.MAX_TEXT_BYTES)
        if expected and hashlib.sha256(packed).hexdigest() != expected:
            raise ValueError('보존 대화의 검증에 실패했습니다. 원본 기록을 유지합니다.')
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            raw = stream.read(history_native.MAX_TEXT_BYTES + 1)
        if len(raw) > history_native.MAX_TEXT_BYTES:
            raise ValueError('보존 대화 크기를 확인해 주세요.')
        value = json.loads(raw)
        if value.get('key') != archive_key or key(value.get('source')) != archive_key:
            raise ValueError('보존 대화의 세션 정보가 일치하지 않습니다.')
        return value

    def conversation(self, archive_key, offset=0):
        if not isinstance(archive_key, str) or not KEY.fullmatch(archive_key):
            raise ValueError('보존 대화 식별자가 올바르지 않습니다.')
        offset = int(offset)
        if offset < 0:
            raise ValueError('대화 위치가 올바르지 않습니다.')
        with self.storage():
            data = self._load()
            row = data['records'].get(archive_key)
            if not row or not row.get('archiveHash'):
                raise ValueError('보존 대화를 찾지 못했습니다.')
            result = self._archive(archive_key, row['archiveHash'])
        messages = result.pop('messages')
        return {**result, 'messages': messages[offset:offset + 100], 'total': len(messages),
                'nextOffset': offset + 100 if offset + 100 < len(messages) else None}

    def sweep(self, candidates, protected):
        if not self.sweeping.acquire(blocking=False):
            return
        try:
            # One board owns the journal. Remote calls are serialized and bounded per pass.
            with self.storage():
                data = self._load()
                if not data['policy']['enabled']:
                    return
                attempted = {k: r.get('attemptedAt', 0) for k, r in data['records'].items()}
            candidates = sorted(candidates, key=lambda s: attempted.get(key(s), 0))[:8]
            for source in candidates:
                if self.stop.is_set():
                    break
                try:
                    self._one(source, protected)
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    # Never write raw exceptions: native error text can include a private dialogue.
                    with self.storage():
                        data = self._load()
                        k = key(source)
                        row = data['records'].setdefault(k, {'source': source, 'title': source.get('title'), 'state': 'blocked'})
                        row.update(attemptedAt=self.clock(), reason='대화 또는 원본 상태를 확인하지 못해 정리를 미뤘습니다.')
                        self._write(data)
        finally:
            self.sweeping.release()

    def _one(self, source, protected):
        k, now = key(source), self.clock()
        with self.storage():
            data = self._load()
            policy, row = data['policy'], data['records'].get(k)
            if len(data['records']) >= MAX_RECORDS and row is None:
                return
            if row and (row.get('state') in ('deleted', 'uncertain') or row.get('preserved')):
                return
            family = (row or {}).get('family') or [source['id']]
            if any((source['host'], source['agent'], source.get('home'), sid) in protected for sid in family):
                if row:
                    row.update(state='waiting', deleteAfter=None, attemptedAt=now, reason='사용 중이거나 고정한 대화입니다.')
                    self._write(data)
                return
        export = self.rpc(source, {'action': 'export', 'source': source})
        family = export['family']
        if any((source['host'], source['agent'], source.get('home'), sid) in protected for sid in family):
            with self.storage():
                data = self._load()
                row = data['records'].get(k)
                if row is None:
                    row = {'source': source, 'title': source.get('title'), 'state': 'waiting'}
                    data['records'][k] = row
                row.update(state='waiting', deleteAfter=None, attemptedAt=now, reason='하위 대화를 사용 중이거나 고정했습니다.')
                self._write(data)
            return
        if export['lastActivityAt'] <= 0 or export.get('ownerTime', now) - export['lastActivityAt'] < policy['idleDays'] * DAY:
            # Reuse after the previous snapshot cancels its pending deletion.
            with self.storage():
                data = self._load()
                row = data['records'].get(k)
                if row is None:
                    row = {'source': source, 'title': source.get('title'), 'state': 'waiting'}
                    data['records'][k] = row
                if row.get('state') not in ('deleted', 'uncertain'):
                    row.update(state='waiting', deleteAfter=None, attemptedAt=now, reason='최근에 사용한 대화입니다.')
                    self._write(data)
            return
        messages = export['messages']
        if not messages or any(not isinstance(m, dict) or m.get('role') not in ('user', 'assistant')
                               or not isinstance(m.get('text'), str) for m in messages):
            raise ValueError('전체 대화를 확인하지 못했습니다.')
        # Defence in depth: even a remote helper must not smuggle attachment data into text.
        messages = [{**{field: m.get(field) for field in ('role', 'ts', 'messageId', 'threadId')},
                     'text': history_native.text(m['text'])} for m in messages]
        primary_id = source.get('sessionId') or source['id'] if source['agent'] == 'claude' else source['id']
        summary = summarize([m for m in messages if m.get('threadId') in (None, primary_id)])
        summary['messageCount'] = len(messages)
        with self.storage():
            data = self._load()
            row = data['records'].get(k)
            if not data['policy']['enabled'] or (row and (row.get('preserved') or row.get('state') in ('deleted', 'uncertain'))):
                return
            for sibling in data['records'].values():
                peer = sibling['source']
                if (sibling.get('preserved') and peer['host'] == source['host']
                        and peer['agent'] == source['agent'] and peer.get('home') == source.get('home')
                        and (peer['id'] in family or any(sid in family for sid in sibling.get('family') or []))):
                    if row:
                        row.update(state='waiting', deleteAfter=None, attemptedAt=now, reason='하위 대화를 보존하도록 지정했습니다.')
                        self._write(data)
                    return
            if not row or row.get('fingerprint') != export['fingerprint'] or row.get('state') == 'waiting':
                artifact = {'version': 1, 'key': k, 'source': identity(source), 'title': history_native.text(source.get('title')),
                            'messages': messages, 'summary': summary, 'createdAt': now}
                raw = encode(artifact)
                if len(raw) > history_native.MAX_TEXT_BYTES:
                    raise ValueError('전체 대화가 보존 한도를 넘습니다.')
                packed = gzip.compress(raw, mtime=0)
                archive_hash = hashlib.sha256(packed).hexdigest()
                _save(self.root / (k + '.json.gz'), packed)
                if self._archive(k, archive_hash) != artifact:
                    raise ValueError('대화 보존을 검증하지 못했습니다.')
                row = {'source': source, 'title': artifact['title'], 'state': 'pending', 'createdAt': now,
                       'deleteAfter': now + policy['graceDays'] * DAY, 'preserved': False,
                       'fingerprint': export['fingerprint'], 'archiveHash': archive_hash,
                       'messageCount': len(messages), 'archiveBytes': len(packed), 'sourceBytes': export.get('sourceBytes'),
                       'summary': summary, 'family': family}
                data['records'][k] = row
            row.update(attemptedAt=now, reason=export.get('reason'), canDelete=bool(export.get('canDelete')))
            if not export.get('canDelete'):
                row['state'] = 'archived'
            elif row.get('state') in ('archived', 'blocked'):
                row.update(state='pending', deleteAfter=now + policy['graceDays'] * DAY)
            self._write(data)
            if row['state'] != 'pending' or now < row['deleteAfter']:
                return
            self._archive(k, row['archiveHash'])
            # The current source was freshly exported above. Changes reset the full grace period.
            # Holding the journal lock orders a local preserve/cancel before or after this action.
            # Journal intent before dispatch: a lost response must never cause a replay.
            row.update(state='uncertain', reason='원본 기록 정리 결과를 확인 중입니다.')
            self._write(data)
            result = self.rpc(source, {'action': 'delete', 'source': source,
                                       'fingerprint': row['fingerprint'], 'family': family})
            if result.get('confirmed') is not True or set(result.get('family') or []) != set(family):
                row.update(state='uncertain', reason='원본 기록 정리 결과를 확인하지 못했습니다. 자동 재시도하지 않습니다.')
            else:
                row.update(state='deleted', deletedAt=now, reclaimedBytes=result.get('reclaimedBytes'), reason=None)
            self._write(data)

    def start(self, collect, interval=3600):
        self.worker_enabled = True
        def run():
            while not self.stop.wait(60 if not hasattr(self, '_started') else interval):
                self._started = True
                try:
                    candidates, protected = collect()
                    self.sweep(candidates, protected)
                except (OSError, ValueError, RuntimeError):
                    pass
        threading.Thread(target=run, name='session-history', daemon=True).start()
