"""Private board display names and read marks. Native titles and session identities never change."""
import copy
import hashlib
import json
import os
import re
import sys
import time
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


# Every store identifies every listed row on every snapshot (six times a row): a row's key
# is kept by the fields _identity reads. Bounded; emptied when full.
_ROW_FIELDS = ('host', 'agent', 'id', 'home', 'kind', 'sessionId')
_ROW_KEYS = {}
_ROW_KEYS_LIMIT = 8192


def _row_key(source):
    """_key(_identity(source)) of a listed row, or None when it cannot be identified."""
    try:
        memo = tuple(source.get(name) for name in _ROW_FIELDS)
        found = _ROW_KEYS.get(memo, False)
    except TypeError:
        # A field that cannot be a dictionary key: identified without the memo.
        memo, found = None, False
    if found is False:
        try:
            found = _key(_identity(source))
        except (GoalError, ValueError):
            found = None
        if memo is not None:
            if len(_ROW_KEYS) >= _ROW_KEYS_LIMIT:
                _ROW_KEYS.clear()
            _ROW_KEYS[memo] = found
    return found


def _lstat_key(path):
    """What changes when a store file is replaced (every write is a new file), rewritten in
    place, or has its owner, mode or links changed; None when it is missing or unreadable."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _title(value):
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > TITLE_LIMIT
            or any(unicodedata.category(char) in ('Cc', 'Cs') or '\u202a' <= char <= '\u202e'
                   or '\u2066' <= char <= '\u2069' for char in value)):
        raise GoalError('보드 표시 제목은 제어문자 없이 160자 이내로 입력해 주세요.', 'invalid_session_label')
    return scrub_text(value.strip()) or None


class _KeptRead:
    """A display store's parsed file, kept for decorate (which only looks) while the file is
    the same, so the list's poll does not parse and check an unchanged file again."""
    def state_key(self):
        """Changes whenever what decorate reads could have: the store file and its lock file."""
        return _lstat_key(self.path), _lstat_key(self.lock_path)

    def _read_cached(self):
        """_read, kept while _lstat_key of the file stays the same; the caller holds _storage.
        The key is taken before reading: a file replaced meanwhile is read again next time.
        Writers change what _read returns before writing it, so they never get this one."""
        key = _lstat_key(self.path)
        kept = getattr(self, '_kept', None)
        if key is not None and kept is not None and kept[0] == key:
            return kept[1]
        data = self._read()
        self._kept = (key, data) if key is not None and data is not None else None
        return data


class SessionLabels(_KeptRead, Goals):
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
    def _titles(source, title):
        original = source.get('title') or source.get('name') or '제목 없는 작업'
        original = scrub_text(original) if isinstance(original, str) else '제목 없는 작업'
        return {'titleAlias': title, 'originalTitle': original, 'displayTitle': title or original}

    @classmethod
    def _display(cls, source, title):
        return {'source': _identity(source), **cls._titles(source, title)}

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

    def decorate(self, snapshot, in_place=False):
        """`in_place`: the snapshot is the caller's own copy (Board.snapshot makes one) and its
        rows are decorated as they are instead of in a copy."""
        if not in_place:
            snapshot = copy.deepcopy(snapshot)
        try:
            with self._storage():
                data = self._read_cached()
        except GoalError as exc:
            return {**snapshot, 'sessionLabels': {'ok': False, **exc.payload}}
        for host in snapshot.get('hosts', []):
            for agent in ('codex', 'claude'):
                for row in (host.get('data') or {}).get(agent, []):
                    key = _row_key({**row, 'host': host['name'], 'agent': agent})
                    if key is None:
                        continue  # Unidentifiable rows stay visible, without an alias.
                    row.update(self._titles(row, data['labels'].get(key, {}).get('title')))
        return {**snapshot, 'sessionLabels': {'ok': True, 'revision': data['revision']}}


# ---------- read marks ----------
MAX_SEEN = 2000


class SessionSeen(_KeptRead, Goals):
    """The private-file, flock and atomic-write contract of the goal store, for one small
    file. `since` is when this board started keeping it: a session that last changed
    before then is not called unread (its first deploy lights nothing up)."""
    NAME, ROW_FIELD, SNAPSHOT_FIELD, CODE = 'session-seen', 'seenAt', 'sessionSeen', 'session_seen'
    WHAT = '읽음 기록'

    def __init__(self, state_dir, clock=time.time):
        super().__init__(state_dir)
        self.path = self.directory / (self.NAME + '.json')
        self.lock_path = self.directory / (self.NAME + '.lock')
        self.clock = clock

    def _unavailable(self):
        return GoalError(self.WHAT + '을 안전하게 읽거나 저장하지 못했습니다.', self.CODE + '_unavailable', 503)

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, 'rb') as stream:
                if self._private_file(stream.fileno()).st_size > MAX_BYTES:
                    raise ValueError('file size')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('file size')
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {'version', 'since', 'seen'}
                    or data['version'] != 1 or type(data['since']) is not int
                    or not isinstance(data['seen'], dict) or len(data['seen']) > MAX_SEEN):
                raise ValueError('store')
            for key, row in data['seen'].items():
                if (not isinstance(row, dict) or set(row) != {'source', 'at'} or type(row['at']) is not int
                        or _identity(row['source']) != row['source'] or _key(row['source']) != key):
                    raise ValueError('row')
            return data
        except (GoalError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise self._unavailable() from None

    def mark(self, source, at):
        """The session was looked at as of its update time `at` (never moves back).
        Returns ({'seenAt'}, whether anything was written)."""
        if type(at) is not int or at < 0:
            raise GoalError('기록할 시점이 올바르지 않습니다.', 'invalid_' + self.CODE)
        identity = _identity(source)
        key = _key(identity)
        with self._storage(write=True):
            data = self._read() or {'version': 1, 'since': int(self.clock()), 'seen': {}}
            row = data['seen'].get(key)
            if row and row['at'] >= at:
                return {'seenAt': row['at']}, False
            data['seen'][key] = {'source': identity, 'at': at}
            if len(data['seen']) > MAX_SEEN:
                oldest = sorted(data['seen'], key=lambda item: data['seen'][item]['at'])
                for stale in oldest[:len(data['seen']) - MAX_SEEN]:
                    del data['seen'][stale]
            self._write(data)
            return {'seenAt': at}, True

    def unmark(self, source):
        """Forget the session's mark. Returns whether anything was written."""
        key = _key(_identity(source))
        with self._storage(write=True):
            data = self._read()
            if not data or key not in data['seen']:
                return False
            del data['seen'][key]
            self._write(data)
            return True

    def decorate(self, snapshot, in_place=False):
        """Each row's `seenAt` (None when never looked at here) and the board's `since`.
        `in_place` as for SessionLabels.decorate."""
        if not in_place:
            snapshot = copy.deepcopy(snapshot)
        try:
            with self._storage():
                data = self._read_cached()
            if data is None:
                # The first read starts the record: what changed before now counts as read.
                with self._storage(write=True):
                    data = self._read()
                    if data is None:
                        data = {'version': 1, 'since': int(self.clock()), 'seen': {}}
                        self._write(data)
            self.warned = False
        except GoalError as exc:
            if not getattr(self, 'warned', False):
                self.warned = True
                sys.stderr.write('%s을 읽지 못했습니다 (%s). 이 표시를 끈 채로 둡니다.\n' % (self.WHAT, exc.payload.get('code')))
            return {**snapshot, self.SNAPSHOT_FIELD: {'ok': False, **exc.payload}}
        for host in snapshot.get('hosts', []):
            for agent in ('codex', 'claude'):
                for row in (host.get('data') or {}).get(agent, []):
                    key = _row_key({**row, 'host': host['name'], 'agent': agent})
                    if key is None:
                        continue
                    found = data['seen'].get(key)
                    row[self.ROW_FIELD] = found['at'] if found else None
        return {**snapshot, self.SNAPSHOT_FIELD: {'ok': True, 'since': data['since']}}


class SessionTidied(SessionSeen):
    """When a session was put away from the list: ended from the board, or 목록에서 정리.
    The list counts it as old until it changes again (the screen allows the shutdown's own
    writes a short grace). Display only: the native session and its history stay as they are."""
    NAME, ROW_FIELD, SNAPSHOT_FIELD, CODE = 'session-tidied', 'tidiedAt', 'sessionTidied', 'session_tidied'
    WHAT = '목록 정리 기록'


class SessionRemoved(SessionSeen):
    """When a session was removed from the list (목록에서 지우기). Unlike a tidy it stays away
    after it changes again; the screen includes it only when explicitly requested.
    Display only: the native session and its history stay as they are."""
    NAME, ROW_FIELD, SNAPSHOT_FIELD, CODE = 'session-removed', 'removedAt', 'sessionRemoved', 'session_removed'
    WHAT = '목록에서 지운 기록'


class SessionOpened(SessionSeen):
    """When a session's conversation was last opened on this board (any device): the list
    orders its older sessions by it."""
    NAME, ROW_FIELD, SNAPSHOT_FIELD, CODE = 'session-opened', 'openedAt', 'sessionOpened', 'session_opened'
    WHAT = '대화를 연 기록'


class SessionPinned(SessionSeen):
    """Board favorites, shared by devices without modifying native session metadata."""
    NAME, ROW_FIELD, SNAPSHOT_FIELD, CODE = 'session-pinned', 'pinnedAt', 'sessionPinned', 'session_pinned'
    WHAT = '상단 고정 기록'


class BoardPreferences(_KeptRead, Goals):
    """Small shared UI preferences; never credentials, drafts or native settings."""
    NAME, FIELD, MAX_FILE = 'board-preferences', 'quickReplies', 16384
    CONFLICT = ('다른 기기에서 문구를 바꿨습니다. 최신 문구를 확인한 뒤 다시 저장해 주세요.', 'preferences_conflict')

    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.path = self.directory / (self.NAME + '.json')
        self.lock_path = self.directory / (self.NAME + '.lock')

    def _unavailable(self):
        return GoalError('빠른 답장 설정을 읽거나 저장하지 못했습니다. 편집 내용은 유지됩니다.',
                         'preferences_unavailable', 503)

    @staticmethod
    def rows(value):
        if (not isinstance(value, list) or len(value) > 12 or any(
                not isinstance(row, str) or not row.strip() or len(row) > 200 or
                any(unicodedata.category(char) in ('Cc', 'Cs') for char in row) for row in value)):
            raise GoalError('빠른 답장은 한 줄 200자, 최대 12개까지 저장할 수 있습니다.', 'invalid_quick_replies')
        return [row.strip() for row in value]

    def _valid(self, value):
        """The stored form of `value`, or a GoalError."""
        return self.rows(value)

    @staticmethod
    def _empty():
        """The value before anything is saved: None keeps each browser's own phrases."""
        return None

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, self.FIELD: self._empty()}
        try:
            with os.fdopen(fd, 'rb') as stream:
                if self._private_file(stream.fileno()).st_size > self.MAX_FILE:
                    raise ValueError('size')
                data = json.loads(stream.read(self.MAX_FILE + 1))
            if (not isinstance(data, dict) or set(data) != {'version', 'revision', self.FIELD} or
                    type(data['version']) is not int or data['version'] != 1 or
                    type(data['revision']) is not int or data['revision'] < 0 or
                    data[self.FIELD] != self._empty() and self._valid(data[self.FIELD]) != data[self.FIELD]):
                raise ValueError('format')
            return data
        except (GoalError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise self._unavailable() from None

    def snapshot(self):
        with self._storage():
            data = self._read()
            return {'ok': True, 'revision': data['revision'], self.FIELD: data[self.FIELD]}

    def _set(self, value, revision):
        value = self._valid(value)
        if type(revision) is not int or revision < 0:
            raise GoalError('저장할 설정 버전을 다시 확인해 주세요.', 'invalid_preferences_revision')
        with self._storage(write=True):
            data = self._read()
            if data[self.FIELD] != value:
                if data['revision'] != revision:
                    raise GoalError(*self.CONFLICT, 409)
                data.update({self.FIELD: value, 'revision': data['revision'] + 1})
                if len(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()) + 1 > self.MAX_FILE:
                    raise GoalError('저장할 내용이 너무 큽니다. 일부를 줄인 뒤 다시 저장해 주세요.', 'preferences_too_large')
                self._write(data)
            return {'ok': True, 'revision': data['revision'], self.FIELD: data[self.FIELD]}

    def set_quick_replies(self, rows, revision):
        return self._set(rows, revision)


PROMPT_LIMIT, PROMPT_TITLE_LIMIT, PROMPT_BODY_LIMIT = 20, 40, 4000
# The board takes a message that opens like "/model" for one of its own commands, never sent.
BOARD_COMMAND = re.compile(r'/[a-z]+(?:\s|$)')


class SavedPrompts(BoardPreferences):
    """Saved prompts: a short title and a multi-line body each, shared by the board's devices.
    The list's poll carries only the revision (summary); a page reads the bodies when it changes.
    The file stays under the 64 KiB a JSON request may be, so a list that fits is also one a page
    can send."""
    NAME, FIELD, MAX_FILE = 'board-prompts', 'prompts', 61440
    CONFLICT = ('다른 기기에서 저장 프롬프트를 바꿨습니다. 최신 프롬프트를 확인한 뒤 다시 저장해 주세요.', 'prompts_conflict')
    MODES = ('send', 'insert')

    def _unavailable(self):
        return GoalError('저장 프롬프트를 읽거나 저장하지 못했습니다. 편집 내용은 유지됩니다.', 'prompts_unavailable', 503)

    @staticmethod
    def _empty():
        return []

    @staticmethod
    def _bad(message):
        return GoalError(message, 'invalid_prompts')

    def _valid(self, value):
        if not isinstance(value, list) or len(value) > PROMPT_LIMIT:
            raise self._bad(f'저장 프롬프트는 {PROMPT_LIMIT}개까지 저장할 수 있습니다.')
        rows = []
        for number, row in enumerate(value, 1):
            if not isinstance(row, dict) or set(row) != {'title', 'body', 'mode'} or row['mode'] not in self.MODES:
                raise self._bad(f'{number}번째 프롬프트의 형식을 확인해 주세요.')
            title, body = row['title'], row['body']
            if (not isinstance(title, str) or not title.strip() or len(title) > PROMPT_TITLE_LIMIT
                    or any(unicodedata.category(char) in ('Cc', 'Cs') or '\u202a' <= char <= '\u202e'
                           or '\u2066' <= char <= '\u2069' for char in title)):
                raise self._bad(f'{number}번째 프롬프트의 제목은 제어문자 없이 {PROMPT_TITLE_LIMIT}자 이내로 적어 주세요.')
            # Line breaks and tabs are the body's own; any other control character is not text.
            if (not isinstance(body, str) or not body.strip() or len(body) > PROMPT_BODY_LIMIT
                    or any(unicodedata.category(char) == 'Cs' or unicodedata.category(char) == 'Cc'
                           and char not in '\n\t' for char in body)):
                raise self._bad(f'{number}번째 프롬프트의 본문은 제어문자 없이 {PROMPT_BODY_LIMIT}자 이내로 적어 주세요.')
            if BOARD_COMMAND.match(body.strip()):
                raise self._bad(f'{number}번째 프롬프트의 본문이 /영문으로 시작해 보드 명령으로 처리됩니다. 앞에 다른 글자를 넣어 주세요.')
            rows.append({'title': title.strip(), 'body': body.strip(), 'mode': row['mode']})
        return rows

    def summary(self):
        """What the list's poll carries: whether the prompts read, and their revision."""
        with self._storage():
            return {'ok': True, 'revision': self._read_cached()['revision']}

    def set_prompts(self, prompts, revision):
        return self._set(prompts, revision)
