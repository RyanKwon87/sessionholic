"""Private automatic work derived from observed sessions, with no native I/O.

Only exact session identities and explicit parent/handoff evidence join work.
Extracted text is an excerpt or an explicit report, never a model conclusion.
A finished answer is a result; only a structured completion with criteria and
supporting evidence completes work. User corrections survive every refresh.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading

from goals import (Goals, GoalError, FIELDS, TEXT_LIMITS, STATUSES, HISTORY_LIMIT,
                   MAX_BYTES, MAX_GOALS, _sessions, _text, _actor, _timestamp)
import settings

MAX_SESSIONS = 8000
ACTIVITIES = ('attention', 'active', 'result', 'waiting', 'recent')
IDENTITY = ('host', 'agent', 'home', 'id')
LABELS = {
    '제목': 'title', '작업명': 'title', 'title': 'title',
    '현재': 'current', '진행 상황': 'current', '현재 작업': 'current', 'current': 'current',
    '다음': 'next', '다음 단계': 'next', '다음 행동': 'next', 'next': 'next', 'next steps': 'next',
    '대기 이유': 'waitingReason', '대기 사유': 'waitingReason', 'waiting reason': 'waitingReason',
    '완료 조건': 'completionCriteria', '완료 기준': 'completionCriteria', 'completion criteria': 'completionCriteria',
    '완료 근거': 'evidence', '검증 결과': 'evidence', 'evidence': 'evidence',
}


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _ref(value):
    if not isinstance(value, dict):
        raise GoalError('세션 정보를 확인해 주세요.')
    if (value.get('agent') == 'claude' and value.get('kind') == 'interactive' and not value.get('id')
            and isinstance(value.get('sessionId'), str) and re.fullmatch(r'[0-9A-Za-z-]{8,64}', value['sessionId'])):
        value = {**value, 'id': value['sessionId']}
    return _sessions([{key: value.get(key) for key in ('host', 'agent', 'id', 'home')}])[0]


def _key(value):
    return json.dumps([value[key] for key in IDENTITY], ensure_ascii=False, separators=(',', ':'))


def _excerpt(value, limit=2000):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if ord(c) >= 32 or c in '\n\t').strip()[:limit]


def _title(value):
    text = _excerpt(value, 4000)
    line = next((line.strip(' #*-\t') for line in text.splitlines() if line.strip()), '')
    return line[:TEXT_LIMITS['title']]


def _message_id(message):
    explicit = message.get('id') or message.get('messageId')
    return _excerpt(str(explicit), 200) if explicit is not None else _digest(message)[:24]


def _question(text):
    """A limited text cue, explicitly marked as inference by the caller."""
    line = next((line.strip(' #*-\t') for line in reversed(text.splitlines()) if line.strip()), '')
    if len(line) > 600:
        return ''
    if re.search(r'(?:알려|선택해|선택하여|답변해|승인해)\s*주세요[.!]?$', line):
        return line
    if line.endswith(('?', '？')) and re.search(
            r'(?:할까요|될까요|괜찮|원하시|어떤|어떻게|무엇|어느|would you|could you|can you|which|what should)', line, re.I):
        return line
    return ''


def _labels(text):
    """Read labeled lines/sections, ignoring code blocks and quoted content."""
    found, active, lines, fenced = {}, None, [], False
    def flush():
        if active and lines:
            found[active] = _excerpt('\n'.join(lines), TEXT_LIMITS[active])
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith(('```', '~~~')):
            flush(); active, lines, fenced = None, [], not fenced
            continue
        if fenced or stripped.startswith('>'):
            continue
        cleaned = stripped.strip('#*- \t')
        pair = re.match(r'^([^:：]{1,40})\s*[:：]\s*(.*)$', cleaned)
        label = (pair.group(1) if pair else cleaned).strip(' *').lower()
        field = LABELS.get(label)
        if field:
            flush(); active, lines = field, []
            if pair and pair.group(2).strip(' *'):
                lines.append(pair.group(2).strip(' *'))
        elif active and stripped:
            if stripped.startswith('#'):
                flush(); active, lines = None, []
            else:
                lines.append(stripped)
        elif active:
            flush(); active, lines = None, []
    flush()
    return found


class AutomaticWork(Goals):
    """Thread/process safe evidence store. Network collection belongs to the caller."""
    def __init__(self, state_dir):
        self.directory = Path(state_dir)
        self.path = self.directory / 'automatic-work.json'
        self.lock_path = self.directory / 'automatic-work.lock'
        self.lock = threading.Lock()

    @staticmethod
    def _unavailable():
        return GoalError('자동 작업 기록을 안전하게 읽거나 저장하지 못했습니다. 기존 기록은 유지됩니다.',
                         'automatic_work_unavailable', 503)

    @staticmethod
    def _empty():
        return {'version': 1, 'revision': 0, 'goals': [], 'sessions': {}}

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return self._empty()
        try:
            with os.fdopen(fd, 'rb') as stream:
                if self._private_file(stream.fileno()).st_size > MAX_BYTES:
                    raise ValueError('size')
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('size')
            data = json.loads(raw)
            self._validate(data)
            return data
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError, GoalError):
            raise self._unavailable() from None

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or set(data) != {'version', 'revision', 'goals', 'sessions'}
                or type(data['version']) is not int or data['version'] != 1 or type(data['revision']) is not int or data['revision'] < 0
                or not isinstance(data['goals'], list) or len(data['goals']) > MAX_GOALS
                or not isinstance(data['sessions'], dict) or len(data['sessions']) > MAX_SESSIONS):
            raise ValueError('store')
        ids = set()
        for goal in data['goals']:
            if (not isinstance(goal, dict) or not re.fullmatch(r'[a-f0-9]{32}', goal.get('id', ''))
                    or goal['id'] in ids or goal.get('automatic') is not True
                    or type(goal.get('revision')) is not int or goal['revision'] < 1
                    or goal.get('status') not in STATUSES or goal.get('activity') not in ACTIVITIES
                    or not isinstance(goal.get('automation'), dict)
                    or not isinstance(goal['automation'].get('fieldEvidence'), dict)
                    or not isinstance(goal['automation'].get('overrides'), list)
                    or any(field not in FIELDS for field in goal['automation']['overrides'])
                    or not isinstance(goal.get('connection'), dict)
                    or goal['connection'].get('state') not in ('connected', 'offline', 'mixed', 'unknown')
                    or type(goal['connection'].get('lastKnown')) is not bool
                    or not isinstance(goal.get('history'), list) or not 1 <= len(goal['history']) <= HISTORY_LIMIT
                    or type(goal.get('historyCount')) is not int or goal['historyCount'] < len(goal['history'])
                    or not isinstance(goal.get('latestResult'), str) or len(goal['latestResult']) > 2000):
                raise ValueError('goal')
            ids.add(goal['id'])
            for field in ('createdAt', 'updatedAt', 'lastActivityAt'):
                _timestamp(goal[field])
            if goal.get('resultAt') is not None:
                _timestamp(goal['resultAt'])
            for event in goal['history']:
                _timestamp(event['at']); _actor(event['actor'])
                _text(event['reason'], 2000, required=True)
                if (event['fromStatus'] not in (None,) + STATUSES or event['toStatus'] not in STATUSES
                        or not isinstance(event['fields'], list) or any(field not in FIELDS for field in event['fields'])):
                    raise ValueError('history')
            for field, limit in TEXT_LIMITS.items():
                _text(goal[field], limit, required=field == 'title')
            for field in ('pinned', 'archived'):
                if type(goal[field]) is not bool:
                    raise ValueError('flag')
            _sessions(goal['sessions'])
        for key, node in data['sessions'].items():
            if (not isinstance(node, dict) or _key(_ref(node['ref'])) != key or node['goalId'] not in ids
                    or not isinstance(node.get('seen'), list) or len(node['seen']) > 1024
                    or any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in node['seen'])
                    or not isinstance(node.get('segment'), str) or len(node['segment']) > 200
                    or type(node.get('manualGroup')) is not bool
                    or type(node.get('firstRequestSeen')) is not bool
                    or not (node.get('online') is None or type(node['online']) is bool)
                    or not isinstance(node.get('phase'), str)
                    or any(not isinstance(node.get(field), str) for field in
                           ('conversationDigest', 'lastMessageId', 'lastAssistant', 'lastAssistantId', 'lastRole'))
                    or not (node.get('parent') is None or isinstance(node['parent'], str))):
                raise ValueError('session')

    def _write(self, data):
        # Goals' writer already supplies fsync + atomic replace and 0600; use a
        # distinct filename/temporary prefix so both stores remain independent.
        raw = (json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > MAX_BYTES:
            raise GoalError('자동 작업 저장 한도에 도달했습니다.', 'automatic_work_full', 409)
        settings.safe_path(self.path, private=True, allow_missing=True)
        temporary = self.path.with_name('.automatic-work-' + secrets.token_hex(12) + '.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public(data):
        return {'revision': data['revision'], 'goals': copy.deepcopy(data['goals'])}

    def snapshot(self):
        with self._storage():
            return self._public(self._read())

    @staticmethod
    def _goal(data, goal_id):
        return next((goal for goal in data['goals'] if goal['id'] == goal_id), None)

    def _ensure(self, data, ref, row=None, segment=''):
        key = _key(ref)
        node = data['sessions'].get(key)
        if node is not None and node['segment'] == segment:
            return node, self._goal(data, node['goalId'])
        if len(data['goals']) >= MAX_GOALS or (node is None and len(data['sessions']) >= MAX_SESSIONS):
            raise GoalError('자동 작업 저장 한도에 도달했습니다.', 'automatic_work_full', 409)
        row = row or {}
        title = _title(row.get('title')) or '대화에서 시작한 작업'
        now = _now()
        identifier = _digest({'session': key, 'segment': segment})[:32]
        goal = self._goal(data, identifier)
        if goal is None:
            goal = {**{field: '' for field in TEXT_LIMITS}, 'id': identifier, 'revision': 0,
                    'title': title, 'project': _excerpt(row.get('project'), TEXT_LIMITS['project']),
                    'status': 'todo', 'pinned': False, 'archived': False, 'sessions': [dict(ref)],
                    'createdAt': now, 'updatedAt': now, 'history': [], 'historyCount': 0,
                    'automatic': True, 'activity': 'recent', 'latestResult': '', 'resultAt': None,
                    'lastActivityAt': now, 'connection': {'state': 'unknown', 'lastKnown': True},
                    'automation': {'mode': 'evidence', 'fieldEvidence': {}, 'overrides': [], 'suggestions': {},
                                   'anchor': key, 'segment': segment}}
            if segment:
                goal['sessions'][0]['point'] = 'message:' + segment
            self._set(goal, 'title', title, {'kind': 'session_title', 'excerpt': title})
            data['goals'].append(goal)
        node = {'ref': dict(ref), 'goalId': identifier, 'phase': 'unknown', 'online': None,
                'segment': segment, 'manualGroup': False, 'seen': [], 'conversationDigest': '',
                'metadataDigest': '', 'observedPhase': False,
                'firstRequestSeen': False, 'lastMessageId': '', 'lastAssistant': '',
                'lastAssistantId': '', 'lastAssistantPhase': '', 'lastFinal': '', 'lastFinalId': '',
                'lastRole': '', 'parent': None, 'question': ''}
        data['sessions'][key] = node
        return node, goal

    @staticmethod
    def _set(goal, field, value, evidence):
        goal['automation'].setdefault('suggestions', {})[field] = {'value': value, 'evidence': evidence}
        if field in goal['automation']['overrides']:
            return
        if field == 'status' and goal['status'] == 'done':
            return
        goal[field] = value
        goal['automation']['fieldEvidence'][field] = evidence

    def _save_changes(self, data, before, reason, actor=None):
        if data == before:
            return
        prior = {goal['id']: goal for goal in before['goals']}
        now = _now()
        for goal in data['goals']:
            old = prior.get(goal['id'])
            if goal == old:
                continue
            changed = sorted(field for field in FIELDS if old is None or old[field] != goal[field])
            goal['revision'] = old['revision'] + 1 if old else 1
            goal['updatedAt'] = now
            if changed or old is None:
                event = {'at': now, 'actor': actor or {'type': 'inferred', 'label': '세션 근거 자동 반영'},
                         'reason': reason, 'fromStatus': old['status'] if old else None,
                         'toStatus': goal['status'], 'fields': changed}
                goal['history'] = (goal['history'] + [event])[-HISTORY_LIMIT:]
                goal['historyCount'] += 1
        data['revision'] += 1
        self._validate(data)
        self._write(data)

    @staticmethod
    def _rows(snapshot):
        if isinstance(snapshot, list):
            for row in snapshot:
                if isinstance(row, dict):
                    yield row, row.get('online', True)
            return
        if not isinstance(snapshot, dict):
            raise GoalError('수집된 세션 목록을 확인해 주세요.')
        for host in snapshot.get('hosts', []):
            if not isinstance(host, dict):
                continue
            for agent, rows in (host.get('data') or {}).items():
                if agent not in ('codex', 'claude', 'hermes') or not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        yield {**row, 'host': host.get('name'), 'agent': agent}, host.get('ok') is True

    def _merge(self, data, donor, winner, *, explicit=False, relation=None):
        if donor['id'] == winner['id']:
            return False
        if len(winner['sessions']) + len(donor['sessions']) > 64:
            return False
        existing = {_key(ref) for ref in winner['sessions']}
        for ref in donor['sessions']:
            if _key(ref) not in existing:
                winner['sessions'].append(copy.deepcopy(ref)); existing.add(_key(ref))
        for field in FIELDS - {'sessions', 'status', 'pinned', 'archived', 'title'}:
            if not winner[field] and donor[field]:
                winner[field] = copy.deepcopy(donor[field])
                if field in donor['automation']['fieldEvidence']:
                    winner['automation']['fieldEvidence'][field] = copy.deepcopy(donor['automation']['fieldEvidence'][field])
        # Never silently overwrite a correction when a parent is discovered late.
        for field in donor['automation']['overrides']:
            if field not in winner['automation']['overrides'] and field != 'sessions':
                winner[field] = copy.deepcopy(donor[field])
                winner['automation']['overrides'].append(field)
        winner['pinned'] = winner['pinned'] or donor['pinned']
        if donor['latestResult'] and (not winner['resultAt'] or (donor['resultAt'] or '') > winner['resultAt']):
            winner['latestResult'], winner['resultAt'] = donor['latestResult'], donor['resultAt']
        if relation:
            winner['automation']['relation'] = relation
        donor['archived'] = True
        donor['automation']['mergedInto'] = winner['id']
        for node in data['sessions'].values():
            if node['goalId'] == donor['id']:
                node['goalId'] = winner['id']
                node['manualGroup'] = explicit
        return True

    def _activity(self, data, goal):
        nodes = [node for node in data['sessions'].values() if node['goalId'] == goal['id']]
        if not nodes:
            return
        known = [node['online'] for node in nodes if node['online'] is not None]
        state = ('unknown' if not known else 'connected' if all(known) else 'offline' if not any(known) else 'mixed')
        goal['connection'] = {'state': state, 'lastKnown': state != 'connected'}
        online = [node for node in nodes if node['online'] is True]
        if not online:  # An outage does not complete, fail, or reset observed work.
            return
        phases = {node['phase'] for node in online}
        goal['automation']['runtimeKnown'] = bool(phases - {'unknown', 'unavailable', 'notLoaded'})
        if not goal['automation']['runtimeKnown']:
            # A readable historical transcript does not reveal a live execution.
            goal['activity'] = 'recent'
            return
        if 'needs_input' in phases:
            goal['activity'] = 'attention'
            self._set(goal, 'status', 'waiting', {'kind': 'native_phase', 'excerpt': 'needs_input'})
            if not goal['waitingReason']:
                self._set(goal, 'waitingReason', '세션에서 답변 또는 승인을 기다립니다.',
                          {'kind': 'native_phase', 'excerpt': 'needs_input'})
        elif 'working' in phases:
            goal['activity'] = 'active'
            self._set(goal, 'status', 'in_progress', {'kind': 'native_phase', 'excerpt': 'working'})
            if goal['automation']['fieldEvidence'].get('waitingReason', {}).get('kind') in ('native_phase', 'native_prompt', 'inferred_question'):
                self._set(goal, 'waitingReason', '', {'kind': 'native_phase', 'excerpt': 'working'})
        elif any(node.get('question') and node['lastRole'] == 'assistant' for node in online):
            question = next(node['question'] for node in online if node.get('question') and node['lastRole'] == 'assistant')
            goal['activity'] = 'attention'
            evidence = {'kind': 'inferred_question', 'confidence': 'inferred', 'excerpt': question}
            self._set(goal, 'waitingReason', question, evidence)
            self._set(goal, 'status', 'waiting', evidence)
        elif goal['status'] == 'waiting' and goal['waitingReason'] and goal['automation']['fieldEvidence'].get('waitingReason', {}).get('kind') not in ('native_phase', 'native_prompt', 'inferred_question'):
            goal['activity'] = 'waiting'
        elif goal['latestResult']:
            goal['activity'] = 'result'
            self._set(goal, 'status', 'review', {'kind': 'answer_received', 'excerpt': goal['latestResult'][:400]})
        else:
            goal['activity'] = 'recent'

    def sync(self, snapshot):
        with self._storage(write=True):
            data = self._read(); before = copy.deepcopy(data)
            if isinstance(snapshot, dict):
                offline_hosts = {host.get('name') for host in snapshot.get('hosts', [])
                                 if isinstance(host, dict) and host.get('ok') is not True}
                for node in data['sessions'].values():
                    if node['ref']['host'] in offline_hosts:
                        node['online'] = False
            for row, online in self._rows(snapshot):
                try:
                    ref = _ref(row)
                except GoalError:
                    continue
                key = _key(ref)
                existing = data['sessions'].get(key)
                node, goal = self._ensure(data, ref, row, existing['segment'] if existing else '')
                node['online'] = online
                if online:
                    metadata_digest = _digest({key: row.get(key) for key in
                                              ('phase', 'state', 'status', 'updatedAt', 'waitingFor')})
                    if metadata_digest != node.get('metadataDigest'):
                        # A repeated older list must not undo a newer conversation
                        # read. Only changed native list evidence updates its phase.
                        if node.get('metadataDigest') or not node.get('observedPhase'):
                            node['phase'] = row.get('phase') if row.get('phase') in ('working', 'needs_input', 'idle', 'done', 'closed') else 'unknown'
                        node['metadataDigest'] = metadata_digest
                if not goal['project'] and row.get('project'):
                    self._set(goal, 'project', _excerpt(row['project'], TEXT_LIMITS['project']), {'kind': 'session_project', 'excerpt': _excerpt(row['project'], 240)})
                parent_id = row.get('parentSessionId')
                if isinstance(parent_id, str) and parent_id != ref['id']:
                    try:
                        node['parent'] = _key(_ref({**ref, 'id': parent_id}))
                    except GoalError:
                        pass
                if online and node['phase'] == 'needs_input' and isinstance(row.get('waitingFor'), str):
                    self._set(goal, 'waitingReason', _excerpt(row['waitingFor']), {'kind': 'native_prompt', 'excerpt': _excerpt(row['waitingFor'], 400)})
            # Resolve exact parent chains only after every identity in this snapshot exists.
            for key, node in list(data['sessions'].items()):
                if node['manualGroup'] or not node['parent']:
                    continue
                seen, cursor = {key}, node
                while cursor.get('parent') in data['sessions']:
                    parent_key = cursor['parent']
                    if parent_key in seen:
                        cursor = node; break
                    seen.add(parent_key); cursor = data['sessions'][parent_key]
                    if cursor['manualGroup']:
                        break
                if cursor is not node:
                    self._merge(data, self._goal(data, node['goalId']), self._goal(data, cursor['goalId']),
                                relation={'kind': 'parent_session', 'source': node['ref'], 'target': cursor['ref']})
            for goal in data['goals']:
                self._activity(data, goal)
            self._save_changes(data, before, '수집된 세션과 확인된 부모 관계 반영')
            return self._public(data)

    def observe(self, source, conversation_state):
        ref = _ref(source)
        if not isinstance(conversation_state, dict):
            raise GoalError('대화 상태를 확인해 주세요.')
        messages = conversation_state.get('messages', [])
        if not isinstance(messages, list):
            raise GoalError('대화 메시지를 확인해 주세요.')
        selected = [{key: message[key] for key in ('id', 'messageId', 'role', 'text', 'ts', 'phase', 'turnId', 'turnStatus', 'parentMessageId', 'work') if key in message}
                    for message in messages[-1024:] if isinstance(message, dict)]
        work = conversation_state.get('work')
        digest = _digest({'messages': selected, 'phase': conversation_state.get('phase'),
                          'prompt': conversation_state.get('prompt'), 'work': work})
        with self._storage(write=True):
            data = self._read(); before = copy.deepcopy(data)
            old_node = data['sessions'].get(_key(ref))
            node, goal = self._ensure(data, ref, source, old_node['segment'] if old_node else '')
            if node['conversationDigest'] == digest:
                return self._public(data)
            readable_unknown = conversation_state.get('readOnly') is True and conversation_state.get('phase') == 'unknown'
            if conversation_state.get('error') or (conversation_state.get('phase') in ('unavailable', 'unknown', 'notLoaded', 'offline') and not readable_unknown):
                node['online'] = False
                self._activity(data, goal)
                self._save_changes(data, before, '대화 연결 상태 반영')
                return self._public(data)
            node['online'] = True
            if readable_unknown or conversation_state.get('phase') in ('working', 'needs_input', 'idle', 'done', 'closed'):
                node['phase'] = conversation_state['phase']
                node['observedPhase'] = True
            seen = set(node['seen'])
            for message in selected:
                fingerprint = _digest(message)
                if fingerprint in seen:
                    continue
                role = message.get('role')
                text = _excerpt(message.get('text'), 12000)
                message_id = _message_id(message)
                structured = message.get('work') if isinstance(message.get('work'), dict) else {}
                # A new segment needs an explicit user command or explicit structured
                # transition. Project/title similarity is never a transition signal.
                transition = structured.get('transition') == 'new'
                command = re.match(r'^(?:새 작업|새로운 작업|new task)\s*[:：]\s*(.+)', text, re.I)
                if role == 'user' and command:
                    transition = True; structured = {**structured, 'title': command.group(1)}
                if transition and role in ('user', 'assistant') and node['lastMessageId']:
                    old_goal, prior_seen = goal, list(node['seen'])
                    node, goal = self._ensure(data, ref, {'title': structured.get('title') or _title(text)}, message_id)
                    old_goal['sessions'] = [{**link, 'point': (link.get('point', '') + ' … message:' + message_id).strip()}
                                            if _key(link) == _key(ref) else link for link in old_goal['sessions']]
                    goal['automation']['previousWorkId'] = old_goal['id']
                    node['manualGroup'] = True
                    node['seen'] = prior_seen
                    node['online'] = True
                    node['phase'] = conversation_state.get('phase', 'idle')
                    node['observedPhase'] = True
                    seen = set(prior_seen)
                node['seen'].append(fingerprint); seen.add(fingerprint)
                node['seen'] = node['seen'][-1024:]
                if role not in ('user', 'assistant'):
                    continue
                evidence = {'kind': 'user_request' if role == 'user' else 'explicit_report',
                            'messageId': message_id, 'excerpt': text[:400], 'session': dict(ref)}
                if isinstance(message.get('turnId'), str):
                    evidence['turnId'] = _excerpt(message['turnId'], 200)
                if role == 'user':
                    node['lastFinal'], node['lastFinalId'] = '', ''
                if role == 'user' and not node['firstRequestSeen']:
                    node['firstRequestSeen'] = True
                    # Worker requests are details of their parent work, not a new title.
                    if goal['automation']['anchor'] == _key(ref) and not goal['automation']['segment']:
                        self._set(goal, 'title', _title(text) or goal['title'], evidence)
                if role != 'assistant' or not node.get('lastFinal') or message.get('phase') == 'final_answer':
                    node['question'] = _question(text) if role == 'assistant' else ''
                if role == 'assistant' and text:
                    node['lastAssistantPhase'] = message.get('phase', '')
                    if message.get('phase') == 'final_answer':
                        node['lastFinal'], node['lastFinalId'] = text[:2000], message_id
                    node['lastAssistant'], node['lastAssistantId'] = text[:2000], message_id
                    self._set(goal, 'current', text[:2000], {**evidence, 'kind': 'assistant_excerpt'})
                    for field, value in _labels(text).items():
                        # A labeled title does not replace the initial work identity.
                        if field != 'title':
                            self._set(goal, field, value, evidence)
                self._structured(goal, structured, evidence)
                node['lastRole'], node['lastMessageId'] = role, message_id
                goal['lastActivityAt'] = _now()
            if isinstance(work, dict):
                self._structured(goal, work, {'kind': 'structured_work', 'excerpt': _excerpt(work.get('evidence'), 400), 'session': dict(ref)})
            prompt = conversation_state.get('prompt')
            if node['phase'] == 'needs_input' and isinstance(prompt, dict):
                reason = prompt.get('question') or prompt.get('title') or prompt.get('text')
                if isinstance(reason, str) and reason.strip():
                    self._set(goal, 'waitingReason', _excerpt(reason), {'kind': 'native_prompt', 'excerpt': _excerpt(reason, 400)})
            result_text = node.get('lastFinal') or (node['lastAssistant'] if node.get('lastAssistantPhase') != 'commentary' else '')
            result_id = node.get('lastFinalId') or node['lastAssistantId']
            if node['phase'] in ('idle', 'done', 'closed') and node['lastRole'] == 'assistant' and result_text:
                if goal['automation'].get('resultMessageId') != result_id or goal['latestResult'] != result_text:
                    goal['latestResult'], goal['resultAt'] = result_text, _now()
                    goal['automation']['resultMessageId'] = result_id
            node['conversationDigest'] = digest
            self._activity(data, goal)
            self._save_changes(data, before, '새 대화의 요청·보고·결과 근거 반영')
            return self._public(data)

    def _structured(self, goal, work, evidence):
        for field in TEXT_LIMITS:
            if field in ('title', 'project') or not isinstance(work.get(field), str):
                continue
            self._set(goal, field, _excerpt(work[field], TEXT_LIMITS[field]), {**evidence, 'kind': 'structured_work'})
        status = work.get('status')
        if status == 'done' or work.get('completed') is True:
            if goal['completionCriteria'] and goal['evidence']:
                self._set(goal, 'status', 'done', {**evidence, 'kind': 'structured_completion'})
        elif status in STATUSES and (status != 'waiting' or goal['waitingReason']):
            self._set(goal, 'status', status, {**evidence, 'kind': 'structured_work'})

    def update(self, goal_id, payload):
        self._id(goal_id)
        allowed = FIELDS | {'revision', 'actor', 'reason', 'resetOverrides', 'action', 'targetId', 'targetRevision', 'session'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise GoalError('작업 요청의 필드를 확인해 주세요.')
        if type(payload.get('revision')) is not int:
            raise GoalError('수정할 작업의 최신 버전이 필요합니다.', 'goal_revision_required')
        actor = _actor(payload.get('actor', {'type': 'user', 'label': '사용자'}))
        with self._storage(write=True):
            data = self._read(); before = copy.deepcopy(data)
            goal = self._find(data, goal_id)
            if goal['revision'] != payload['revision']:
                raise GoalError('작업이 바뀌었습니다. 최신 내용을 확인해 주세요.', 'goal_conflict', 409, currentRevision=goal['revision'])
            action = payload.get('action')
            if action == 'merge':
                self._id(payload.get('targetId'))
                target = self._find(data, payload['targetId'])
                if target['revision'] != payload.get('targetRevision'):
                    raise GoalError('합칠 작업이 바뀌었습니다.', 'goal_conflict', 409, currentRevision=target['revision'])
                if not self._merge(data, goal, target, explicit=True, relation={'kind': 'user_merge'}):
                    raise GoalError('같은 작업이거나 연결 세션 한도를 넘었습니다.')
                for node in data['sessions'].values():
                    if node['goalId'] == target['id']:
                        node['manualGroup'] = True
                goal = target
            elif action == 'split':
                ref = _ref(payload.get('session'))
                node = data['sessions'].get(_key(ref))
                if node is None or node['goalId'] != goal_id or len(goal['sessions']) < 2:
                    raise GoalError('이 작업에 연결된 세션을 선택해 주세요.')
                segment = 'split-' + secrets.token_hex(8)
                old_node = copy.deepcopy(node)
                node, split = self._ensure(data, ref, {'title': goal['title']}, segment)
                node.update({key: value for key, value in old_node.items() if key not in ('goalId', 'segment', 'manualGroup')})
                node['manualGroup'] = True
                goal['sessions'] = [link for link in goal['sessions'] if _key(link) != _key(ref)]
                split['automation']['relation'] = {'kind': 'user_split', 'sourceWorkId': goal_id}
                goal = split
            elif action is not None:
                raise GoalError('지원하지 않는 작업 변경입니다.')
            reset = payload.get('resetOverrides', [])
            if not isinstance(reset, list) or any(field not in FIELDS for field in reset):
                raise GoalError('자동 갱신으로 되돌릴 필드를 확인해 주세요.')
            goal['automation']['overrides'] = [field for field in goal['automation']['overrides'] if field not in reset]
            for field in reset:
                suggestion = goal['automation'].get('suggestions', {}).get(field)
                if suggestion:
                    goal[field] = copy.deepcopy(suggestion['value'])
                    goal['automation']['fieldEvidence'][field] = copy.deepcopy(suggestion['evidence'])
            for field in FIELDS & set(payload):
                value = payload[field]
                if field in TEXT_LIMITS:
                    value = _text(value, TEXT_LIMITS[field], required=field == 'title')
                elif field in ('pinned', 'archived'):
                    if type(value) is not bool:
                        raise GoalError('고정·보관 값을 확인해 주세요.')
                elif field == 'status' and value not in STATUSES:
                    raise GoalError('작업 상태를 확인해 주세요.')
                elif field == 'sessions':
                    # Membership changes need merge/split, not a dangling UI-only ref.
                    if _sessions(value) != goal['sessions']:
                        raise GoalError('세션 연결은 작업 합치기·분리하기로 변경해 주세요.')
                if goal[field] != value:
                    goal[field] = value
                    if field not in goal['automation']['overrides']:
                        goal['automation']['overrides'].append(field)
                    goal['automation']['fieldEvidence'][field] = {'kind': 'user_correction', 'excerpt': str(value)[:400]}
            # Explicit user completion is sufficient: no required registration form.
            if goal['status'] == 'waiting' and not goal['waitingReason']:
                goal['waitingReason'] = '사용자가 대기 상태로 변경했습니다.'
            self._activity(data, goal)
            self._save_changes(data, before, _text(payload.get('reason', '사용자 작업 교정'), 2000, required=True), actor)
            return {'goal': copy.deepcopy(goal)}

    def link_sessions(self, source, target, *, actor_label='세션홀릭'):
        source, target = _ref(source), _ref(target)
        if _key(source) == _key(target):
            return 0
        with self._storage(write=True):
            data = self._read(); before = copy.deepcopy(data)
            source_node = data['sessions'].get(_key(source))
            if source_node is None:
                return 0
            target_node = data['sessions'].get(_key(target))
            target_node, target_goal = self._ensure(data, target, segment=target_node['segment'] if target_node else '')
            source_goal = self._goal(data, source_node['goalId'])
            changed = self._merge(data, target_goal, source_goal, explicit=True,
                                  relation={'kind': 'confirmed_handoff', 'source': source, 'target': target})
            if changed:
                self._activity(data, source_goal)
                self._save_changes(data, before, '확인된 세션 인계 연결', {'type': 'agent', 'label': _text(actor_label, 120, required=True)})
            return int(changed)

    def close(self):
        """No background thread or external resource is owned by this store."""
