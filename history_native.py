"""Whole, text-only history on the owning host. Never copy or delete workspace files."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

import collector
import launch
import native_chat
import transfer_native as native

MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_FAMILY = 100
DELETE_VERSIONS = {'0.163.0'}
SOURCE_KINDS = ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
                'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown']
DATA_URL = re.compile(r'data:[^\s,;]+(?:;[^\s,;]+)*;base64,[A-Za-z0-9+/=]+', re.I)


def text(value):
    if not isinstance(value, str):
        return ''
    return DATA_URL.sub('[첨부 데이터 제외]', value)


def digest(value):
    checksum = hashlib.sha256()
    for part in json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(',', ':')).iterencode(value):
        checksum.update(part.encode())
    return checksum.hexdigest()


def blocks(content):
    """Keep complete visible text and attachment references; never serialize unknown blocks."""
    if isinstance(content, str):
        return text(content)
    result = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            raise ValueError('대화 본문 형식을 확인하지 못했습니다.')
        kind = block.get('type')
        if kind in ('text', 'input_text', 'output_text'):
            result.append(text(block.get('text')))
        elif kind in ('image', 'input_image', 'image_url', 'localImage', 'file', 'document', 'audio',
                      'input_audio', 'video', 'local_image', 'localAudio', 'skill', 'mention'):
            name = next((text(block.get(k)) for k in ('filename', 'fileName', 'name', 'path')
                         if isinstance(block.get(k), str)), '')
            result.append('[첨부: %s%s]' % (kind, ' · ' + name if name else ''))
        elif kind not in ('tool_result', 'tool_use', 'thinking', 'redacted_thinking'):
            # A new text-bearing shape must not silently become an incomplete archive.
            raise ValueError('지원하지 않는 대화 본문 형식입니다. 원본 기록을 유지합니다.')
    return '\n'.join(result)


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_TEXT_BYTES:
        raise ValueError('전체 텍스트 대화가 보존 한도를 넘습니다. 원본 기록을 유지합니다.')
    return value


def _claude(source, home):
    sid = source.get('sessionId') or source['id']
    path = native_chat._transcript(home, sid)
    if path is None:
        raise ValueError('원본 대화 파일을 하나로 확인하지 못했습니다.')
    paths = [path] + sorted((path.parent / sid / 'subagents').glob('*.jsonl'))
    if len(paths) > MAX_FAMILY:
        raise ValueError('하위 대화 수가 보존 한도를 넘습니다.')
    messages, signatures, family, latest, total, text_bytes = [], [], [], 0, 0, 0
    for file in paths:
        if file.is_symlink() or file.parent.resolve() != file.parent.absolute():
            raise ValueError('대화 기록 경로가 안전하지 않습니다.')
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
        checksum = hashlib.sha256()
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
                raise ValueError('원본 대화 파일을 확인하지 못했습니다.')
            family.append(sid if file == path else file.stem)
            latest = max(latest, before.st_mtime)
            total += before.st_size
            for line in stream:
                checksum.update(line)
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise ValueError('대화 기록 형식이 올바르지 않습니다.')
                kind = entry.get('type')
                queued = collector.queued_prompt(entry) if kind == 'attachment' else None
                if queued is not None:
                    kind = 'user'
                if kind not in ('user', 'assistant') or entry.get('isMeta'):
                    continue
                message = {'content': queued} if queued is not None else entry.get('message')
                if not isinstance(message, dict):
                    raise ValueError('대화 본문을 확인하지 못했습니다.')
                body = blocks(message.get('content'))
                if body:
                    text_bytes += len(body.encode())
                    if text_bytes > MAX_TEXT_BYTES:
                        raise ValueError('전체 텍스트 대화가 보존 한도를 넘습니다.')
                    messages.append({'role': kind, 'text': body, 'ts': collector.to_epoch(entry.get('timestamp')),
                                     'messageId': text(entry.get('uuid')), 'threadId': family[-1]})
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError('대화 기록이 변경되어 보존을 미룹니다.')
        signatures.append([str(file.relative_to(launch._home(home))), checksum.hexdigest()])
    return _bounded({'messages': messages, 'family': family, 'fingerprint': digest(signatures),
                     'lastActivityAt': latest, 'sourceBytes': total, 'canDelete': False,
                     'reason': 'Claude는 전체 대화를 보존합니다. 단일 세션 기록 삭제 범위가 검증되기 전까지 원본을 유지합니다.'})


def _all_threads(client, root):
    rows = []
    for archived in (False, True):
        cursor = None
        for _ in range(100):
            params = {'ancestorThreadId': root, 'archived': archived, 'limit': 100,
                      'sourceKinds': SOURCE_KINDS, 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            page = client.call('thread/list', params)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise ValueError('하위 세션 범위를 확인하지 못했습니다.')
            rows.extend(page['data'])
            if len(rows) >= MAX_FAMILY:
                raise ValueError('하위 세션 수가 보존 한도를 넘습니다.')
            cursor = page.get('nextCursor')
            if not cursor:
                break
        else:
            raise ValueError('하위 세션을 끝까지 확인하지 못했습니다.')
    return rows


def _turns(client, sid):
    turns, cursor = [], None
    try:
        for _ in range(200):
            params = {'threadId': sid, 'limit': 100, 'sortDirection': 'asc', 'itemsView': 'full'}
            if cursor:
                params['cursor'] = cursor
            page = client.call('thread/turns/list', params)
            if not isinstance(page, dict) or not isinstance(page.get('data'), list):
                raise ValueError('전체 대화를 확인하지 못했습니다.')
            if any(not isinstance(t, dict) or not isinstance(t.get('items'), list)
                   or t.get('itemsView', 'full') != 'full' for t in page['data']):
                raise ValueError('대화 본문이 생략되어 원본 기록을 유지합니다.')
            turns.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return turns
        raise ValueError('전체 대화가 보존 한도를 넘습니다.')
    except collector.RpcError as error:
        if not collector.paging_unsupported(error, 'thread/turns/list'):
            raise
        thread = client.call('thread/read', {'threadId': sid, 'includeTurns': True}).get('thread')
        if not isinstance(thread, dict) or thread.get('id') != sid or not isinstance(thread.get('turns'), list):
            raise ValueError('전체 대화를 확인하지 못했습니다.')
        return thread['turns']


def _archived_clear(identity, home, sid):
    """0.163.0 refuses queue/goal RPC for archived threads. Its persistent stores must
    independently show an archived thread, no queued input, and no unfinished goal.
    Unknown/missing schemas are never interpreted as empty stores."""
    account = launch._home(home) / identity['home']
    def query(path, sql):
        if path is None or path.is_symlink() or path.parent.resolve() != path.parent.absolute():
            raise ValueError('원본 상태 저장 경로를 확인하지 못했습니다.')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('원본 상태 저장 파일을 확인하지 못했습니다.')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as connection:
            return connection.execute(sql, (sid,)).fetchall()
    try:
        archived = query(collector.state_db(account), 'SELECT archived FROM threads WHERE id=?')
        if archived != [(1,)]:
            return False
        queue = query(account / 'queue_1.sqlite', 'SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1')
        goals = query(account / 'goals_1.sqlite', 'SELECT status FROM thread_goals WHERE thread_id=?')
        return not queue and goals in ([], [('complete',)])
    except (OSError, ValueError, sqlite3.Error):
        return False


def _codex(source, home, deadline, delete_fingerprint=None):
    identity = native._identity(source)
    with native._client(identity, home, deadline) as client:
        root = client.call('thread/read', {'threadId': identity['id'], 'includeTurns': False}).get('thread')
        if not isinstance(root, dict) or root.get('id') != identity['id']:
            raise ValueError('원본 세션을 확인하지 못했습니다.')
        descendants = _all_threads(client, identity['id'])
        family = {identity['id']: root}
        for row in descendants:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                raise ValueError('하위 세션 식별자를 확인하지 못했습니다.')
            # Refuse a server that ignored the ancestor filter.
            sid = row['id']
            if sid == identity['id'] or sid in family:
                raise ValueError('하위 세션 관계를 확인하지 못했습니다.')
            family[sid] = row
        for sid, row in family.items():
            visited, parent = {sid}, row.get('parentThreadId')
            while sid != identity['id'] and parent != identity['id']:
                if parent not in family or parent in visited:
                    raise ValueError('하위 세션 관계를 확인하지 못했습니다.')
                visited.add(parent)
                parent = family[parent].get('parentThreadId')
        messages, evidence, latest, text_bytes = [], [], 0, 0
        initialized = getattr(client, 'initialized', None)
        agent = initialized.get('userAgent', '') if isinstance(initialized, dict) else ''
        match = re.search(r'\b(\d+\.\d+\.\d+)\b', agent) if isinstance(agent, str) else None
        version = match.group(1) if match else None
        can_delete = version in DELETE_VERSIONS
        for sid in [identity['id']] + sorted(s for s in family if s != identity['id']):
            thread = client.call('thread/read', {'threadId': sid, 'includeTurns': False}).get('thread')
            if not isinstance(thread, dict) or thread.get('id') != sid:
                raise ValueError('세션 범위를 다시 확인하지 못했습니다.')
            status = thread.get('status') or {}
            if (status.get('type') != 'notLoaded' or status.get('activeFlags') or thread.get('isPinned')
                    or (thread.get('goal') or {}).get('status') == 'active'):
                can_delete = False
            turns = _turns(client, sid)
            try:
                queued = client.call('thread/queue/list', {'threadId': sid, 'limit': 1})
                goal = client.call('thread/goal/get', {'threadId': sid})
                if (not isinstance(queued.get('data'), list) or queued['data'] or queued.get('nextCursor')
                        or not isinstance(goal, dict) or 'goal' not in goal
                        or (goal.get('goal') is not None and (not isinstance(goal['goal'], dict)
                                                             or goal['goal'].get('status') != 'complete'))):
                    can_delete = False
            except collector.RpcError:
                if version not in DELETE_VERSIONS or not _archived_clear(identity, home, sid):
                    can_delete = False
            evidence.append([sid, digest([thread, turns])])
            latest = max(latest, collector.to_epoch(thread.get('updatedAt')) or 0)
            for turn in turns:
                latest = max(latest, collector.to_epoch(turn.get('startedAt')) or 0,
                             collector.to_epoch(turn.get('completedAt')) or 0)
                if turn.get('status') == 'inProgress':
                    can_delete = False
                for item in turn.get('items') or []:
                    kind = item.get('type')
                    if kind == 'userMessage':
                        body, role = blocks(item.get('content')), 'user'
                    elif kind in ('agentMessage', 'plan'):
                        body, role = text(item.get('text')), 'assistant'
                    elif kind == 'imageGeneration':
                        body, role = '[이미지 생성%s]' % (' · ' + text(item['savedPath']) if item.get('savedPath') else ''), 'assistant'
                    else:
                        continue
                    if body:
                        text_bytes += len(body.encode())
                        if text_bytes > MAX_TEXT_BYTES:
                            raise ValueError('전체 텍스트 대화가 보존 한도를 넘습니다.')
                        messages.append({'role': role, 'text': body, 'messageId': text(item.get('id')),
                                         'ts': collector.to_epoch(turn.get('startedAt') if role == 'user'
                                                                  else turn.get('completedAt')), 'threadId': sid})
        result = _bounded({'messages': messages, 'family': sorted(family), 'lastActivityAt': latest,
                           'fingerprint': digest(evidence), 'sourceBytes': None, 'canDelete': can_delete,
                           'nativeVersion': version,
                           'reason': None if can_delete else (
                               '이 Codex 앱 서버 버전은 대화만 보존하고 원본 기록을 유지합니다.' if version not in DELETE_VERSIONS
                               else '실행·연결·고정·목표 상태가 남아 있어 원본 기록을 유지합니다.')})
        if delete_fingerprint is not None:
            if not can_delete or result['fingerprint'] != delete_fingerprint:
                raise ValueError('원본 세션이 변경되어 삭제하지 않았습니다.')
            # A parent can resume while a large child's history is being read. Check loaded
            # threads again after the entire export, immediately before the destructive call.
            cursor = None
            for _ in range(200):
                params = {'limit': 100}
                if cursor:
                    params['cursor'] = cursor
                loaded = client.call('thread/loaded/list', params)
                if not isinstance(loaded, dict) or not isinstance(loaded.get('data'), list):
                    raise ValueError('원본 연결 상태를 다시 확인하지 못했습니다.')
                if any(not isinstance(sid, str) or sid in family for sid in loaded['data']):
                    raise ValueError('원본 세션이 다시 열려 삭제하지 않았습니다.')
                cursor = loaded.get('nextCursor')
                if not cursor:
                    break
            else:
                raise ValueError('원본 연결 상태를 끝까지 확인하지 못했습니다.')
            # The official API owns metadata cleanup. Never stop a shared daemon or remove a worktree.
            client.call('thread/delete', {'threadId': identity['id']})
            return {'confirmed': True, 'family': sorted(family), 'reclaimedBytes': None}
        return result


def handle(args, home=None, timeout=40):
    source = args.get('source')
    identity = native._identity(source)
    if args.get('action') not in ('export', 'delete'):
        raise ValueError('대화 보존 요청이 올바르지 않습니다.')
    deleting = args['action'] == 'delete'
    fingerprint = args.get('fingerprint')
    if deleting and (not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint)):
        raise ValueError('원본 대화 확인 정보가 올바르지 않습니다.')
    if identity['agent'] == 'claude':
        if deleting:
            raise ValueError('Claude 원본 기록의 자동 삭제는 아직 지원하지 않습니다.')
        return {**_claude(source, home), 'ownerTime': time.time()}
    result = _codex(source, home, time.monotonic() + timeout, fingerprint if deleting else None)
    return result if deleting else {**result, 'ownerTime': time.time()}
