"""Authenticated board coordinator; clients can choose identities, never host paths."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

MAX_FILE = 20 * 1024 * 1024
REQUEST = re.compile(r'[A-Za-z0-9_-]{8,100}\Z')
STATE_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')
CLAUDE_BLOCKED = ('Claude 터미널에 승인·선택 메뉴가 떠 있어 보내지 않았습니다. '
                  '입력이 메뉴 선택으로 처리될 수 있으니 터미널에서 먼저 선택해 주세요.')
CLAUDE_EXTERNAL = ('이 Claude 세션은 보드 밖 터미널에서 실행 중이라 웹에서 입력할 수 없습니다. '
                   '그 터미널에서 입력하거나 백그라운드 세션(claude --bg)으로 실행해 주세요.')
# A background session takes web input only through a board terminal the user opened.
CLAUDE_OPEN_TERMINAL = '보내려면 먼저 터미널을 여세요.'
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
CLAUDE_READY_SECONDS = 12
CLAUDE_PROBE = 160
# Claude Code records a pasted image path as "[Image #1]" in the prompt (before, after or
# instead of the words) and may show a long paste as "[Pasted text #1 +12 lines]".
CLAUDE_INPUT_TOKEN = re.compile(r'\[(?:Image|Pasted text|Pasted image) #\d+(?: \+\d+ lines?)?\]')
ATTACHMENT_LINE = '첨부 파일:'
ATTACHMENT_ONLY = ('[이미지 첨부]', '[파일 첨부]')


def claude_words(text):
    """A Claude prompt as the board compares it: collapsed, without Claude Code's input
    tokens and without the trailing "첨부 파일: …" lines the board appends for files."""
    lines = CLAUDE_INPUT_TOKEN.sub(' ', str(text or '')).split('\n')
    while lines and (not lines[-1].strip() or lines[-1].strip().startswith(ATTACHMENT_LINE)):
        lines.pop()
    words = ' '.join(' '.join(lines).split())
    return '' if words in ATTACHMENT_ONLY else words

# ---------- approval / question menus drawn by the agent TUIs ----------
# "❯ 1. Yes" (Claude Code), "› 1. Yes, proceed (y)" (Codex): optional cursor, number, label.
MENU_OPTION = re.compile(r'^(\s*)([❯›▸▶>])?\s*(\d{1,2})[.)]\s+(\S.*)$')
# The normal input line ("> ", "❯ ", "› Ask Codex…") is hidden while a menu is open.
INPUT_PROMPT = re.compile(r'^\s*[❯›>](?:\s|$)')
BOX_LEAD = re.compile(r'^\s*[─-▟]+')
BOX_TAIL = re.compile(r'\s*[─-▟]+$')
BOX_VERTICAL = frozenset('│┃║╎╏┆┇┊┋▌▐▏▕')
MULTI_MARKS = ('[ ]', '[x]', '[X]', '[✓]', '[✔]', '☐', '☑', '☒', '◯', '◉')
MENU_TAIL = 6
MENU_GAP = 12
QUESTION_LINES = 12
PROMPT_SETTLE = 0.6
PROMPT_KEY_LIMIT = 40
PROMPT_RESULTS = 256
PROMPT_CHANGED = '질문이 바뀌었거나 웹에서 고를 수 없는 메뉴입니다. 다시 확인해 주세요.'
PROMPT_NO_TERMINAL = ('이 세션에는 보드가 연결한 터미널이 없어 질문에 답할 수 없습니다. '
                      '실행 중인 터미널에서 직접 선택해 주세요.')
PROMPT_BUSY = '다른 입력을 전달하고 있습니다. 잠시 뒤 다시 시도해 주세요.'
TERMINAL_STATE = '보드 터미널 상태를 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.'
# Conversation state reads are shared for this many seconds.
STATE_CACHE_SECONDS = 2
# Message ids a client recently asked receipts for, per conversation: a conversation
# watcher reads them along, so the client's next read is answered from the cache.
WAITING_SECONDS = 60
WAITING_IDS = 20
WAITING_CONVERSATIONS = 64
# A reader sharing another reader's remote read waits this long (the RPC itself has 20 s).
STATE_SHARE_SECONDS = 30
STATE_UNAVAILABLE = '대화 상태를 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.'


class StateRead:
    """One conversation read in progress. Readers asking for a subset of its receipt ids
    share it instead of starting their own remote read."""
    def __init__(self, ids, epoch, cwd):
        self.ids, self.epoch, self.cwd = frozenset(ids), epoch, cwd
        self.done = threading.Event()
        self.response = self.error = None
        self.joined = 0


class SendRejected(ValueError):
    """Validation failed before the native send boundary was entered."""
    def __init__(self, reason, status=400, code=None):
        super().__init__(reason)
        self.status, self.code = status, code


class PromptRejected(ValueError):
    """A menu answer was refused before any key reached the terminal."""
    def __init__(self, reason, status=409, code='prompt_changed'):
        super().__init__(reason)
        self.status, self.code = status, code


def _screen_lines(screen):
    """Screen rows without box borders; a border-only rule becomes None (a hard boundary)."""
    lines = []
    for raw in str(screen or '').replace('\r', '').split('\n'):
        raw = raw.rstrip()
        core = raw.strip()
        if core and all('─' <= char <= '▟' or char.isspace() for char in core):
            # "╭────╮" or "────" separates regions; "│      │" is an empty row inside a box.
            lines.append('' if all(char in BOX_VERTICAL or char.isspace() for char in core) else None)
            continue
        lead = BOX_LEAD.match(raw)
        if lead:
            # Same width in spaces, so indentation inside the box keeps its columns.
            raw = ' ' * len(lead.group(0)) + raw[lead.end():]
        lines.append(BOX_TAIL.sub('', raw).rstrip())
    return lines


def _indent(line):
    return len(line) - len(line.lstrip())


def parse_menu(screen):
    """The numbered selection menu at the bottom of a TUI screen, or None.

    Only a block that ends the screen counts, so numbered lists in an answer above
    the input line are not menus. The fingerprint leaves out the cursor position.
    """
    lines = _screen_lines(screen)
    last = next((index for index in range(len(lines) - 1, -1, -1) if lines[index]), None)
    if last is None:
        return None
    end = None
    for index in range(last, max(last - MENU_TAIL, 0) - 1, -1):
        line = lines[index]
        if not line:
            continue
        if MENU_OPTION.match(line):
            end = index
            break
        if INPUT_PROMPT.match(line):
            return None
    if end is None:
        return None
    found, expected, index = [], None, end
    while index >= 0:
        line = lines[index]
        if line is None or (found and found[-1][0] - index > MENU_GAP):
            return None
        match = MENU_OPTION.match(line) if line else None
        if match:
            number = int(match.group(3))
            if expected is not None and number != expected:
                return None
            found.append((index, match))
            if number == 1:
                break
            expected = number - 1
        index -= 1
    if len(found) < 2 or int(found[-1][1].group(3)) != 1:
        return None
    found.reverse()
    options, cursors = [], []
    for position, (index, match) in enumerate(found):
        column = match.start(3)
        stop = found[position + 1][0] if position + 1 < len(found) else last + 1
        details = []
        for line in lines[index + 1:stop]:
            if not line or not line.strip():
                if position + 1 == len(found):
                    break  # the last option's description is contiguous; hints follow a gap
                continue
            if _indent(line) <= column or INPUT_PROMPT.match(line):
                if position + 1 < len(found):
                    return None  # an unindented row between options: not one menu
                break  # guidance under the last option ("Enter to select")
            details.append(line.strip())
        number = int(match.group(3))
        if match.group(2):
            cursors.append(number)
        options.append({'number': number, 'label': match.group(4).rstrip(), 'detail': '\n'.join(details)})
    groups, current_group, index = [], [], found[0][0] - 1
    while index >= 0 and len(groups) < 2:
        line = lines[index]
        if line is None:
            break
        if line.strip():
            current_group.append(line)
        elif current_group:
            groups.append(current_group)
            current_group = []
        index -= 1
    if current_group and len(groups) < 2:
        groups.append(current_group)
    kept = []  # bottom-up, nearest the options first
    for number, group in enumerate(groups):
        for line in group:
            if len(kept) < QUESTION_LINES:
                kept.append((number, line))
    kept.reverse()
    margin = min((_indent(line) for _, line in kept), default=0)
    rows, previous = [], None
    for number, line in kept:
        if previous is not None and number != previous:
            rows.append('')
        rows.append(line[margin:])
        previous = number
    question = '\n'.join(rows)
    fingerprint = hashlib.sha256(json.dumps(
        [question, [[o['number'], o['label'], o['detail']] for o in options]],
        ensure_ascii=False).encode()).hexdigest()
    return {'question': question, 'options': options,
            'current': cursors[0] if len(cursors) == 1 else None,
            'multi': any(o['label'].startswith(MULTI_MARKS) for o in options),
            'fingerprint': fingerprint}


class Chat:
    def __init__(self, board, workflow, state_dir):
        self.board, self.workflow = board, workflow
        self.root = Path(state_dir) / 'chat'
        self.lock = threading.RLock()
        self.cache = {}
        self.cache_epoch = 0
        self.read_sequence = 0
        self.upload_gate = threading.BoundedSemaphore(2)
        # Serialises everything the board types into agent terminals (pastes, menu keys).
        self.claude_lock = threading.Lock()
        self.prompt_results = {}
        # state key -> {request id: monotonic expiry}, oldest first.
        self.waiting = {}
        # state key -> the newest remote read in progress (StateRead).
        self.reads = {}

    @staticmethod
    def identity(source):
        return {k: source.get(k) for k in ('host', 'agent', 'home', 'id')}

    @classmethod
    def state_key(cls, source):
        # The conversation cache key; event watchers use the same key (events.watch_key).
        return json.dumps(cls.identity(source), sort_keys=True)

    def resolve(self, ref):
        """The listed source a client reference names, whether or not its host is reachable."""
        if not isinstance(ref, dict):
            raise ValueError('작업을 선택해 주세요.')
        # Ordinary messages stay on the selected source even after a transfer.
        # A fresh destination is accepted only from server-written nativeSource metadata.
        try:
            source = self.board.source(ref)
            # Found among the board's rows of exactly this agent.
            source.setdefault('agent', ref.get('agent'))
            return source
        except ValueError:
            for terminal in self.workflow.terminals.list():
                native = terminal.get('nativeSource') or terminal.get('metadata', {}).get('nativeSource')
                if isinstance(native, dict) and self.identity(native) == self.identity(ref or {}):
                    return {**native, 'title': terminal.get('title', '이어가는 작업')}
            raise

    def source(self, ref):
        source = self.resolve(ref)
        host = next(h for h in self.board.hosts if h['name'] == source['host'])
        if not self.board.state[host['name']]['ok']:
            raise RuntimeError('기기 연결을 다시 확인한 뒤 메시지를 보내 주세요.')
        return source, host

    def rpc(self, host, action, source, **kwargs):
        return self.workflow.transfers.rpc(host, 'chat', {'action': action, 'source': source, **kwargs}, timeout=20)

    def _direct_session(self, source):
        guard = getattr(self.workflow, 'ensure_direct_session', None)
        if guard is not None:
            guard(source)

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

    def state(self, data, watching=False):
        """The conversation view. A watcher read (watching=True) also carries the receipts
        clients recently asked for, so their next read is answered from the cache; a read
        still in progress is shared by readers asking for a subset of its receipts."""
        source, host = self.source(data.get('source'))
        identity = self.identity(source)
        key = self.state_key(source)
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
            now = time.monotonic()
            if watching:
                # The watcher's own reads never extend how long an id is waited for.
                request_ids = carried = self._waiting_ids(key, now)
            else:
                self._remember(key, request_ids, now)
                # A new read also carries every id still waited on here, so the watcher's
                # read and other clients' reads can share it.
                carried = list(dict.fromkeys(request_ids + self._waiting_ids(key, now)))
            cached = self.cache.get(key)
            if (cached and cached[3] == source.get('cwd') and now - cached[0] < STATE_CACHE_SECONDS
                    and cached[4].issuperset(request_ids)):
                return self._cached_view(cached[1], request_ids)
            shared = self.reads.get(key)
            # Shared only when it started after the latest send or terminal input (the same
            # epoch) and carries every id asked for here.
            if (shared is not None and shared.epoch == self.cache_epoch and shared.cwd == source.get('cwd')
                    and shared.ids.issuperset(request_ids)):
                shared.joined += 1
            else:
                shared = None
                self.read_sequence += 1
                sequence, epoch = self.read_sequence, self.cache_epoch
                read = self.reads[key] = StateRead(carried, epoch, source.get('cwd'))
        if shared is not None:
            return self._shared_view(shared, request_ids)
        if watching:
            kwargs = {'requestIds': carried} if carried else {}
        else:
            kwargs = {'requestId': request_id} if request_id else {}
            extra = [identifier for identifier in carried if identifier not in request_ids]
            if additional or extra:
                kwargs['requestIds'] = additional + extra
        try:
            response = self._read_state(source, host, identity, key, carried, kwargs, sequence, epoch)
        except Exception as exc:
            read.error = exc
            raise
        else:
            read.response = response
        finally:
            with self.lock:
                if self.reads.get(key) is read:
                    del self.reads[key]
            read.done.set()
        return self._cached_view(response, request_ids)

    @staticmethod
    def _shared_view(shared, request_ids):
        """The asked receipts from another reader's read; its failure is this reader's too."""
        if not shared.done.wait(STATE_SHARE_SECONDS):
            raise RuntimeError(STATE_UNAVAILABLE)
        if shared.error is not None:
            raise shared.error
        if shared.response is None:
            raise RuntimeError(STATE_UNAVAILABLE)
        # A send after this reader joined keeps the result out of the cache, not from this reader.
        return Chat._cached_view(shared.response, request_ids)

    def _read_state(self, source, host, identity, key, request_ids, kwargs, sequence, epoch):
        """One remote read with the receipts of `request_ids`, cached unless a send or a newer
        read came in meanwhile."""
        try:
            result = self.rpc(host, 'read', source, **kwargs)
        except (RuntimeError, OSError, ValueError):
            if source.get('agent') != 'codex' or source.get('phase') not in ('closed', 'notLoaded'):
                raise
            # A disconnected daemon does not erase the saved conversation.
            try:
                result = self.board.read(source['host'], 'codex', source['id'], source['home'])
            except (RuntimeError, OSError, ValueError):
                result = {'error': 'unavailable'}
            if result.get('error'):
                result = {'messages': [], 'conversationUnavailable': True,
                          'reason': '앱 서버 연결이 없어 대화 기록을 불러오지 못했습니다. 세션을 다시 열어 확인해 주세요.'}
            result = {**result, 'phase': 'closed', 'capabilities': {'canSend': False}, 'readOnly': True}
        caps = result.get('capabilities', {})
        phase = result.get('phase', 'unavailable')
        if phase == 'notLoaded': phase = 'closed'
        if phase == 'unknown' and not result.get('readOnly') and host.get('platform') != 'windows': phase = 'unavailable'
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
        try:
            terminal, terminal_failed = self._board_terminal(source), False
        except (RuntimeError, OSError, ValueError):
            terminal, terminal_failed = None, True
        action = None
        if claude:
            supported, reason, action = self._claude_capability(source, terminal, terminal_failed)
        else:
            supported, reason = bool(caps.get('canSend')), caps.get('reason') or result.get('reason')
        capability = {'supported': supported, 'attachments': bool(source.get('cwd')) and host.get('platform') != 'windows',
                      'reason': None if supported else reason}
        if action:
            # What the user can do to make sending possible ("open_terminal": the board's terminal button).
            capability['action'] = action
        recovery = None
        if not claude and phase in ('closed', 'notLoaded'):
            try:
                preview = self.workflow.resume_preview(source)
                recovery = {'supported': preview.get('allowed') is True,
                            'closedByBoard': preview.get('blockedByBoard') is True,
                            'action': 'resume_session' if preview.get('allowed') else None,
                            'reason': preview.get('reason')}
            except (RuntimeError, ValueError, OSError):
                recovery = {'supported': True, 'closedByBoard': False, 'action': 'refresh_host',
                            'reason': '재개 가능 여부를 확인하지 못했습니다. 기기 연결을 다시 확인해 주세요.'}
            if recovery.get('action') == 'resume_session':
                capability['action'] = 'resume_session'
                capability['reason'] = '연결이 끝난 세션입니다. 같은 대화를 다시 열어 이어갈 수 있습니다.'
            elif recovery.get('reason'):
                capability['reason'] = recovery['reason']
        response = {'route': identity, 'phase': phase,
                    'capability': capability,
                    'messages': messages,
                    'receipts': receipts, 'maxAttachmentBytes': MAX_FILE,
                    'requiresNativeTerminal': phase == 'needs_input' if claude else
                    result.get('requiresNativeTerminal', phase == 'needs_input'),
                    # A menu on the board terminal's screen; the web may answer it.
                    'prompt': self._screen_prompt(terminal, source) if terminal else None,
                    # A background Claude session can be attached to read its menu.
                    'promptOpenable': bool(claude and source.get('kind') == 'background'
                                           and terminal is None and not terminal_failed)}
        if recovery is not None:
            response['recovery'] = recovery
        if result.get('conversationUnavailable'):
            response['conversationUnavailable'] = True
        if claude and result.get('executionMode') == 'one-shot':
            response.update(executionMode='one-shot',
                            readOnly=True, prompt=None, promptOpenable=False, requiresNativeTerminal=False)
            response['capability'] = {'supported': False, 'attachments': False,
                                      'reason': '일회성 작업은 후속 대화를 받지 않습니다. 요청한 에이전트에서 이어가 주세요.'}
            notice = result.get('historyNotice')
            if (result.get('transcriptAvailability') == 'disabled'
                    and isinstance(notice, dict) and notice.get('code') == 'not_persisted'):
                response.update(historyNotice={'code': 'not_persisted',
                                'message': '대화 기록을 저장하지 않는 일회성 실행입니다. 실행 상태를 확인하고 결과는 요청한 에이전트에서 확인해 주세요.'},
                                transcriptAvailability='disabled')
        bot_lookup = getattr(self.workflow, 'bot_request', None)
        if bot_lookup is not None and bot_lookup(source):
            response.update(readOnly=True, recovery=None, prompt=None, promptOpenable=False)
            response['capability'] = {'supported': False, 'attachments': False,
                                      'reason': '봇 요청입니다. 원래 봇의 권한과 사용 범위로 이어가 주세요.'}
        # Since when the agent works (epoch seconds), for "작업 중 · 12분 34초": Codex sends its
        # running turn's startedAt; Claude, which records no turn start, its newest prompt's
        # time (workingSinceApprox). Nothing while the agent is not working.
        since = result.get('workingSince')
        valid = (phase == 'working' and isinstance(since, (int, float)) and not isinstance(since, bool)
                 and since > 0)
        response['workingSince'] = int(since) if valid else None
        response['workingSinceApprox'] = bool(valid and result.get('workingSinceApprox'))
        # Stable native references let work summaries cite the actual message/turn.
        for field in ('activeTurnId', 'activeFlags', 'turns', 'updatedAt'):
            if field in result:
                response[field] = scrub_payload(result[field])
        if result.get('readOnly'):
            response.update(readOnly=True, runtimeKnown=result.get('runtimeKnown') is True)
        with self.lock:
            cached = self.cache.get(key)
            # The ids whose receipts this read carries are kept with it: a later read may
            # use it only for a subset of them. A send or a newer read must also prevent
            # an earlier in-flight snapshot from repopulating cache.
            if epoch == self.cache_epoch and (cached is None or sequence > cached[2]):
                if key not in self.cache and len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = (time.monotonic(), response, sequence, source.get('cwd'),
                                   frozenset(request_ids))
        return response

    @staticmethod
    def _cached_view(response, request_ids):
        """A cached view with only the requested receipts, in the requested order."""
        receipts = {receipt['requestId']: receipt for receipt in response['receipts']}
        return {**response, 'receipts': [receipts[request_id] for request_id in request_ids]}

    def _remember(self, key, request_ids, now):
        """Note ids a client is waiting on (already validated); caller holds self.lock."""
        if not request_ids:
            return
        waiting = self.waiting.pop(key, {})
        for request_id in request_ids:
            waiting.pop(request_id, None)
            waiting[request_id] = now + WAITING_SECONDS
        while len(waiting) > WAITING_IDS:
            waiting.pop(next(iter(waiting)))
        # Most recently asked conversation last; the least recent one is dropped first.
        self.waiting[key] = waiting
        while len(self.waiting) > WAITING_CONVERSATIONS:
            self.waiting.pop(next(iter(self.waiting)))

    def _waiting_ids(self, key, now):
        """Ids still waited on in this conversation, oldest first; caller holds self.lock."""
        waiting = self.waiting.get(key)
        if not waiting:
            return []
        for request_id in [request_id for request_id, expiry in waiting.items() if expiry <= now]:
            del waiting[request_id]
        if not waiting:
            del self.waiting[key]
        return list(waiting)

    def _forget_state(self, source):
        # Terminal input changes what the next read must show; drop the cached view.
        with self.lock:
            self.cache_epoch += 1
            self.cache.pop(self.state_key(source), None)

    # ---------- the board terminal attached to a session ----------
    @staticmethod
    def _same_native(native, source):
        if (not isinstance(native, dict) or native.get('host') != source.get('host')
                or native.get('agent') != source.get('agent')):
            return False
        if source.get('agent') == 'claude':
            ids = {source.get('id'), source.get('sessionId')} - {None}
            return native.get('id') in ids or native.get('sessionId') in ids
        if source.get('agent') == 'codex':
            return (source.get('id') is not None and native.get('id') == source.get('id')
                    and (native.get('home') or '') == (source.get('home') or ''))
        return False

    def _board_terminal(self, source):
        for terminal in self.workflow.terminals.list():
            native = terminal.get('nativeSource') or terminal.get('metadata', {}).get('nativeSource')
            if terminal.get('alive') and not terminal.get('closed') and self._same_native(native, source):
                return terminal
        return None

    def _claude_capability(self, source, terminal, failed):
        """(supported, reason, action). Only a live board terminal takes input; the board never
        opens one by itself, so a background session without one asks the user to open it."""
        # A session waiting for the user can still take a reply; a menu on its
        # screen is checked at send time, right before anything is pasted.
        if failed:
            return False, TERMINAL_STATE, None
        if terminal:
            return True, None, None
        if source.get('kind') == 'background':
            return False, CLAUDE_OPEN_TERMINAL, 'open_terminal'
        # An interactive session cannot be attached (launch.build_launch), so no button is offered.
        return False, CLAUDE_EXTERNAL, None

    # ---------- approval / question menus answered from the web ----------
    @staticmethod
    def _present(menu, terminal, source):
        from launch import scrub_text
        # Display text is redacted like messages; the fingerprint stays the raw screen's.
        return {**menu, 'question': scrub_text(menu['question']),
                'options': [{**option, 'label': scrub_text(option['label']),
                             'detail': scrub_text(option['detail'])} for option in menu['options']],
                'terminalId': terminal['id'], 'agent': source.get('agent')}

    def _screen_prompt(self, terminal, source):
        try:
            menu = parse_menu(self.workflow.terminals.capture(terminal['id']))
        except (RuntimeError, ValueError, KeyError, OSError):
            return None
        return None if menu is None else self._present(menu, terminal, source)

    def _prompt_terminal(self, source):
        try:
            return self._board_terminal(source)
        except (RuntimeError, OSError, ValueError):
            raise PromptRejected(TERMINAL_STATE, code='terminal_unavailable') from None

    def prompt(self, data):
        action = data.get('action')
        if action not in ('open', 'answer'):
            raise ValueError('질문 요청 종류가 올바르지 않습니다.')
        request_id = data.get('requestId')
        if (action == 'answer' or request_id is not None) and (
                not isinstance(request_id, str) or not REQUEST.fullmatch(request_id)):
            raise ValueError('질문 응답 요청 식별자가 필요합니다.')
        source, host = self.source(data.get('source'))
        self._direct_session(source)
        if action == 'open':
            return self._open_prompt(data.get('source'), source, request_id)
        fingerprint, number = data.get('fingerprint'), data.get('number')
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise ValueError('질문 식별값이 올바르지 않습니다.')
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError('선택지 번호가 올바르지 않습니다.')
        return self._answer_prompt(source, request_id, fingerprint, number)

    def _open_prompt(self, ref, source, request_id):
        """Show a menu: attach a background Claude session first when it has no terminal."""
        if not self.claude_lock.acquire(timeout=30):
            raise PromptRejected(PROMPT_BUSY, code='busy')
        try:
            terminal = self._prompt_terminal(source)
            if terminal is None:
                if source.get('agent') != 'claude' or source.get('kind') != 'background':
                    raise PromptRejected(PROMPT_NO_TERMINAL, code='terminal_required')
                try:
                    terminal = self._open_claude_terminal(ref, source, request_id or 'prompt-' + secrets.token_hex(12))
                except SendRejected as exc:
                    raise PromptRejected(str(exc), exc.status, 'terminal_unavailable') from None
                except (ValueError, RuntimeError, OSError) as exc:
                    raise PromptRejected(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else
                                         'Claude 터미널을 열지 못했습니다.', 409, 'terminal_unavailable') from None
        finally:
            self.claude_lock.release()
        self._forget_state(source)
        return {'prompt': self._screen_prompt(terminal, source)}

    @staticmethod
    def _answer_refusal(menu, fingerprint, number):
        if menu is None or menu['fingerprint'] != fingerprint:
            return PROMPT_CHANGED
        if menu['multi']:
            return '여러 개를 고르는 메뉴는 웹에서 답할 수 없습니다. 터미널에서 직접 선택해 주세요.'
        if menu['current'] is None:
            return '메뉴의 현재 선택 위치를 확인하지 못했습니다. 터미널에서 직접 선택해 주세요.'
        if (all(option['number'] != number for option in menu['options'])
                or abs(number - menu['current']) >= PROMPT_KEY_LIMIT):
            return '메뉴에 없는 선택지입니다. 질문을 다시 확인해 주세요.'
        return None

    def _answer_prompt(self, source, request_id, fingerprint, number):
        """Move the menu cursor and press Enter, at most once per request id."""
        key = json.dumps([self.identity(source), request_id], sort_keys=True)

        def saved():
            with self.lock:
                entry = self.prompt_results.get(key)
            if entry and entry[0] != [fingerprint, number]:
                raise ValueError('이미 사용한 질문 응답 요청입니다.')
            return entry[1] if entry else None

        previous = saved()
        if previous:
            return previous
        if not self.claude_lock.acquire(timeout=30):
            raise PromptRejected(PROMPT_BUSY, code='busy')
        try:
            previous = saved()
            if previous:
                return previous
            terminal = self._prompt_terminal(source)
            if terminal is None:
                raise PromptRejected(PROMPT_NO_TERMINAL, code='terminal_required')
            try:
                menu = parse_menu(self.workflow.terminals.capture(terminal['id']))
            except (RuntimeError, ValueError, KeyError, OSError):
                raise PromptRejected('터미널 화면을 확인하지 못해 선택을 보내지 않았습니다. 다시 시도해 주세요.',
                                     code='terminal_unavailable') from None
            reason = self._answer_refusal(menu, fingerprint, number)
            if reason:
                raise PromptRejected(reason)
            current = menu['current']
            keys = ['Down'] * (number - current) if number > current else ['Up'] * (current - number)
            from terminal import InputNotSent
            try:
                self.workflow.terminals.send_keys(terminal['id'], keys + ['Enter'])
                sent = True
            except (InputNotSent, ValueError, KeyError) as exc:
                # Refused before tmux delivered a key: the same request may be retried.
                raise PromptRejected(str(exc) if isinstance(exc, InputNotSent) else
                                     '터미널에 선택을 보내지 못했습니다. 다시 시도해 주세요.',
                                     code='terminal_unavailable') from None
            except (RuntimeError, OSError):
                sent = False  # the keys may or may not have arrived; never press them again
            after = None
            if sent:
                time.sleep(PROMPT_SETTLE)
                try:
                    after = parse_menu(self.workflow.terminals.capture(terminal['id']))
                except (RuntimeError, ValueError, KeyError, OSError):
                    sent = False
            answered = sent and (after is None or after['fingerprint'] != fingerprint)
            result = {'requestId': request_id, 'number': number,
                      'status': 'answered' if answered else 'unconfirmed',
                      'reason': None if answered else
                      '선택이 반영됐는지 화면에서 확인하지 못했습니다. 같은 선택을 다시 보내지 않으니 터미널을 확인해 주세요.',
                      'prompt': self._present(after, terminal, source) if after else None}
            with self.lock:
                if len(self.prompt_results) >= PROMPT_RESULTS:
                    self.prompt_results.pop(next(iter(self.prompt_results)))
                self.prompt_results[key] = ([fingerprint, number], result)
            return result
        finally:
            self.claude_lock.release()
            self._forget_state(source)

    def _open_claude_terminal(self, ref, source, request_id):
        """Attach the exact background session in a board terminal, never a new session.
        Used only by the question card's "질문 불러오기", a button the user presses; sending never opens one."""
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

    @staticmethod
    def _recorded(record, message):
        """Whether a transcript user message (recorded after the send) is the one this
        record sent: its whole opening text as pasted, or its words as Claude Code recorded
        them (an image path becomes "[Image #1]" and its "첨부 파일:" line goes)."""
        text = message.get('text')
        probe, length = record.get('probe'), record.get('probeLength')
        if isinstance(probe, str) and isinstance(length, int):
            collapsed = ' '.join(str(text or '').split())[:length]
            if hashlib.sha256(collapsed.encode()).hexdigest() == probe:
                return True
        probe, length = record.get('wordsProbe'), record.get('wordsLength')
        if not isinstance(probe, str) or not isinstance(length, int):
            return False
        words = claude_words(text)
        # A short message is compared whole; a long one by its opening, as the transcript
        # may cut it. An attachment-only message has no words but does have its files.
        words = words if length < CLAUDE_PROBE else words[:length]
        if not words and not (message.get('attachments') or ATTACHMENT_LINE in str(text or '')):
            return False
        return hashlib.sha256(words.encode()).hexdigest() == probe

    def _claude_receipt(self, source, request_id, messages):
        record = self._claude_read_record(source, request_id)
        if not record:
            return {'delivery': 'unknown', 'reason': '전송 결과를 확인하지 못했습니다. 상태를 다시 확인해 주세요.'}
        receipt = dict(record.get('receipt') or {})
        for message in messages:
            if (isinstance(message, dict) and message.get('role') == 'user'
                    and (message.get('ts') or 0) >= record.get('sentAt', 0) - 5
                    and self._recorded(record, message)):
                receipt.update(readbackConfirmed=True, currentSnapshotConfirmed=True, reason=None)
                if not receipt.get('acknowledged') and receipt.get('delivery') == 'unknown':
                    # The paste's own result was lost, but the transcript has the message:
                    # it was delivered (the board still never sends it again).
                    receipt.update(delivery='accepted', confirmed=True)
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
                terminal = self._board_terminal(source)
            except (RuntimeError, OSError):
                raise SendRejected('보드 터미널 상태를 확인하지 못해 보내지 않았습니다. 잠시 뒤 다시 시도해 주세요.', 409) from None
            if terminal is None:
                if source.get('kind') != 'background':
                    raise SendRejected(CLAUDE_EXTERNAL, 409)
                # Terminals open only when the user asks (the composer's "터미널 열기");
                # nothing was reserved or pasted, so the draft stays and may be sent later.
                raise SendRejected(CLAUDE_OPEN_TERMINAL, 409, code='terminal_required')
            try:
                screen = self.workflow.terminals.capture(terminal['id'])
            except (RuntimeError, ValueError, KeyError, OSError):
                raise SendRejected('Claude 터미널 화면을 확인하지 못해 보내지 않았습니다. 다시 시도해 주세요.', 409) from None
            # Enter would confirm the highlighted choice of an open menu.
            if parse_menu(screen):
                raise SendRejected(CLAUDE_BLOCKED, 409)
            lines = [text.strip()] if text.strip() else []
            for aid in ids:
                record = json.loads((self.root / 'attachments' / (aid + '.json')).read_text())
                lines.append('첨부 파일: ' + record['attachment']['path'])
            body = '\n'.join(lines)
            # ESC and other controls would end the bracketed paste and act as keys.
            if CONTROL.search(body):
                raise SendRejected('메시지에 터미널 제어 문자가 있어 보내지 않았습니다. 일반 텍스트만 보낼 수 있습니다.', 400)
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
            words = claude_words(body)[:CLAUDE_PROBE]
            record = {'requestId': request_id, 'sentAt': time.time(), 'probe': digest,
                      'probeLength': len(probe), 'wordsProbe': hashlib.sha256(words.encode()).hexdigest(),
                      'wordsLength': len(words), 'receipt': unknown}
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
        self._direct_session(source)
        if host.get('platform') == 'windows':
            raise ValueError('Windows 세션에는 현재 텍스트만 보낼 수 있습니다.')
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
            self._direct_session(source)
            if host.get('platform') == 'windows' and ids:
                raise ValueError('Windows 세션에는 현재 텍스트만 보낼 수 있습니다.')
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
        if STATE_REQUEST.fullmatch(request_id):
            # The client asks for this receipt next; the watcher's read right after the
            # send carries it, so that question is answered from the cache.
            with self.lock:
                self._remember(self.state_key(source), [request_id], time.monotonic())
        self._forget_state(source)
        return {'status': receipt['status'], 'receipt': receipt}
