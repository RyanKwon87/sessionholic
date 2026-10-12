"""Private board display names. Native titles and session identities never change."""
import copy
import hashlib
import json
import os
import re
import unicodedata

from goals import GoalError, Goals, MAX_BYTES, _sessions
from launch import scrub_text
import settings

TITLE_LIMIT = 160
MAX_LABELS = 2000


def reference(source):
    if not isinstance(source, dict) or source.get('agent') not in ('codex', 'claude'):
        raise GoalError('제목을 바꿀 세션을 다시 선택해 주세요.', 'invalid_session_label')
    value = {key: source.get(key) for key in ('host', 'agent', 'id', 'home')}
    if not value['id'] and value['agent'] == 'claude' and source.get('kind') == 'interactive':
        value['id'] = source.get('sessionId')
    result = _sessions([value])[0]
    result.pop('point')
    return result


def _identity(source):
    identity = reference(source)
    # A native Claude worker id and its transcript UUID can differ. A reused
    # worker id must never inherit another transcript's display name.
    if identity['agent'] == 'claude' and source.get('sessionId'):
        sid = source['sessionId']
        if not isinstance(sid, str) or not re.fullmatch(r'[0-9A-Za-z-]{8,64}', sid):
            raise ValueError('세션의 대화 식별자를 확인해 주세요.')
        identity['sessionId'] = sid
    return identity


def _key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _title(value):
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > TITLE_LIMIT
            or any(unicodedata.category(char) in ('Cc', 'Cs') or '\u202a' <= char <= '\u202e'
                   or '\u2066' <= char <= '\u2069' for char in value)):
        raise GoalError('보드 표시 제목은 제어문자 없이 160자 이내로 입력해 주세요.', 'invalid_session_label')
    return scrub_text(value.strip()) or None


class SessionLabels(Goals):
    """Reuse the existing private-file, flock and atomic-write contract only.

    No goal, native process, conversation, SSH or external service is consulted.
    Storage is lazy so an unavailable alias file cannot stop the board server.
    """
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.path = self.directory / 'session-labels.json'
        self.lock_path = self.directory / 'session-labels.lock'

    @staticmethod
    def _unavailable():
        return GoalError('보드 표시 제목을 안전하게 읽거나 저장하지 못했습니다. 기존 제목은 유지됩니다.',
                         'session_labels_unavailable', 503)

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, 'labels': {}}
        try:
            with os.fdopen(fd, 'rb') as stream:
                if self._private_file(stream.fileno()).st_size > MAX_BYTES:
                    raise ValueError('file size')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('file size')
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'revision', 'labels'}
                    or type(data['version']) is not int or data['version'] != 1
                    or type(data['revision']) is not int or data['revision'] < 0
                    or not isinstance(data['labels'], dict) or len(data['labels']) > MAX_LABELS):
                raise ValueError('store')
            for key, label in data['labels'].items():
                if (not isinstance(label, dict) or set(label) != {'source', 'title'}
                        or _identity(label['source']) != label['source'] or _key(label['source']) != key
                        or not label['title'] or _title(label['title']) != label['title']):
                    raise ValueError('label')
            return data
        except (GoalError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise self._unavailable() from None

    @staticmethod
    def _display(source, title):
        original = source.get('title') or source.get('name') or '제목 없는 작업'
        original = scrub_text(original) if isinstance(original, str) else '제목 없는 작업'
        return {'source': _identity(source), 'titleAlias': title, 'originalTitle': original,
                'displayTitle': title or original}

    def set(self, source, title):
        identity, title = _identity(source), _title(title)
        key = _key(identity)
        with self._storage(write=True):
            data = self._read()
            old = data['labels'].get(key)
            if (old or {}).get('title') != title:
                if title is None:
                    data['labels'].pop(key, None)
                else:
                    if old is None and len(data['labels']) >= MAX_LABELS:
                        raise GoalError('보드 표시 제목의 저장 한도에 도달했습니다.', 'session_labels_full', 409)
                    data['labels'][key] = {'source': identity, 'title': title}
                data['revision'] += 1
                self._write(data)
            return {**self._display(source, title), 'revision': data['revision']}

    def decorate(self, snapshot):
        snapshot = copy.deepcopy(snapshot)
        try:
            with self._storage():
                data = self._read()
        except GoalError as exc:
            return {**snapshot, 'sessionLabels': {'ok': False, **exc.payload}}
        for host in snapshot.get('hosts', []):
            for agent in ('codex', 'claude'):
                for row in (host.get('data') or {}).get(agent, []):
                    try:
                        source = {**row, 'host': host['name'], 'agent': agent}
                        alias = data['labels'].get(_key(_identity(source)), {}).get('title')
                        row.update({key: value for key, value in self._display(source, alias).items() if key != 'source'})
                    except (GoalError, ValueError):
                        continue  # Unidentifiable rows stay visible, without an alias.
        return {**snapshot, 'sessionLabels': {'ok': True, 'revision': data['revision']}}
