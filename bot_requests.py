"""Explicit, read-only bot request sources; also a standalone SSH-stdin reader.

This module never imports a bot runtime, decrypts content, changes a database,
or treats a displayed authority label as permission to control a native session.
Request lists read metadata only. A bot's conversation (its user and assistant
text, never tool calls or results) is read only for the request the board was
asked to show, bounded and read-only, and is shown watch-only.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import threading
import time
import unicodedata
import uuid


LIMIT = 100
CACHE_SECONDS = 120
# A source whose read failed is read again after this long without a forced refresh (a down bot
# would otherwise keep its new sessions out of the bot-session guard until someone refreshes).
FAILED_RETRY_SECONDS = 600
KINDS = {"holicii_sqlite", "hermes_sqlite", "slack_ledger"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
FIELDS = {"id", "label", "host", "kind", "path", "authorityLabel", "scopeLabel"}
# A Hermes session: a gateway's own row, or the reference a request ledger keeps for the
# session that answered it ("hermes:<session>#<generation>").
HERMES_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,127}\Z")
HERMES_REF = re.compile(r"hermes:([0-9A-Za-z][0-9A-Za-z_.-]{0,127})(?:#[0-9A-Za-z_-]{1,64})?\Z")
# One conversation read: the newest rows of at most this many sessions, each body cut before
# its Slack envelope is unwrapped (on the board), and all bodies within one budget.
CONVERSATION_ROWS = 80
CONVERSATION_SESSIONS = 6
BODY_LIMIT = 16000
CONVERSATION_BUDGET = 400_000
SCAN_ROWS = 1000
# Secrets: masked in labels and in conversation text alike.
SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*", re.I),
    re.compile(r"(?i)\b(?:Bearer\s+\S+|sk-(?:proj-|ant-api\d{2}-)?[\w-]{10,}|xox[baprs]-[\w-]+|gh[pousr]_[\w]{16,}|github_pat_[\w]{16,})"),
    re.compile(r"(?i)\b(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)|token)\s*[=:]\s*[\"']?[^\s,;\"']+"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"),
)
# Labels also hide addresses, contacts and mentions. Conversation text keeps them, as the
# board's other conversations do: the contact pattern would also hide dates and numbers.
REDACTIONS = SECRETS + (
    re.compile(r"(?i)\bhttps?://[^\s<>]+"),
    re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)"),
    re.compile(r"<(?:@[UW][A-Z0-9]+|#[A-Z0-9]+(?:\|[^>]+)?)>"),
)


class SourceError(ValueError):
    pass


def _text(value, fallback="", limit=100):
    if not isinstance(value, str):
        return fallback
    value = value[:2048]
    for pattern in REDACTIONS:
        value = pattern.sub("[가림]", value)
    return " ".join("".join(c if not unicodedata.category(c).startswith("C") else " " for c in value).split())[:limit] or fallback


def _stamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (float, int)):
            seconds = float(value) / (1000 if value > 100_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _opaque(source, value):
    return source["id"] + ":" + hashlib.sha256(str(value).encode()).hexdigest()[:24]


def _alias(label, source, value):
    return label + " " + _opaque(source, value).split(":", 1)[1][:6] if value else label + " 미확인"


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise SourceError("봇 원장의 상대 경로가 올바르지 않습니다.")
    value = value[2:] if value.startswith("~/") else value
    if ("\\" in value or ":" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise SourceError("봇 원장은 해당 기기의 홈 아래 경로만 사용할 수 있습니다.")
    if PurePosixPath(value).suffix not in (".db", ".sqlite", ".sqlite3"):
        raise SourceError("봇 원장의 SQLite 파일 경로를 확인해 주세요.")
    return value


def _safe_file(path, home, *, private=False):
    home = Path(home).resolve(strict=True)
    path = Path(path).absolute()
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise SourceError("봇 원장은 해당 기기의 홈 아래 경로만 사용할 수 있습니다.") from None
    if not relative.parts:
        raise SourceError("봇 원장 파일 경로를 확인해 주세요.")
    current = home
    for part in relative.parts:
        current /= part
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022))):
            raise SourceError("봇 원장 경로의 소유권 또는 연결 경로를 확인해 주세요.")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not os.access(path, os.R_OK):
        raise SourceError("봇 원장 파일을 안전하게 읽을 수 없습니다.")
    if private and os.name != "nt" and info.st_mode & 0o077:
        raise SourceError("봇 출처 설정은 소유자만 읽을 수 있어야 합니다.")
    return path


def _validate_source(source, hosts=None):
    if not isinstance(source, dict) or set(source) != FIELDS:
        raise SourceError("봇 출처 설정 항목이 올바르지 않습니다.")
    if any(not isinstance(source.get(key), str) or not IDENTIFIER.fullmatch(source[key]) for key in ("id", "host")):
        raise SourceError("봇 출처와 기기 식별자가 올바르지 않습니다.")
    if source["kind"] not in KINDS or hosts is not None and source["host"] not in hosts:
        raise SourceError("등록되지 않은 봇 출처 형식 또는 기기입니다.")
    for key in ("label", "authorityLabel", "scopeLabel"):
        value = source[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or _text(value, limit=200) != value:
            raise SourceError("봇 출처의 표시 이름과 권한 범위를 확인해 주세요.")
    return {**source, "path": _relative(source["path"])}


def _columns(connection, table, required):
    columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
    if not required <= columns:
        raise SourceError("봇 원장의 스키마가 지원 형식과 다릅니다.")
    return columns


def _primary_key(connection, table, column, integer=False):
    rows = list(connection.execute('PRAGMA table_info("' + table + '")'))
    keys = [row for row in rows if row[5]]
    if (len(keys) != 1 or keys[0][1] != column
            or integer and keys[0][2].upper() != "INTEGER"):
        raise SourceError("봇 원장의 고유 인덱스를 확인하지 못했습니다.")


def _index_on(connection, table, columns):
    """An existing index led by these columns, so a bounded read never scans the table."""
    for row in connection.execute('PRAGMA index_list("' + table + '")'):
        name = row[1]
        info = [item[2] for item in connection.execute('PRAGMA index_info("' + name.replace('"', '""') + '")')]
        if info[:len(columns)] == list(columns):
            return name
    return None


def _base(source, key, *, title, status, updated, channel=None, requester=None, native=None, conversation=None,
          thread=None):
    # `_conversation` (a Hermes session id) stays with the board; the page sees only its alias.
    # `conversationKey` aliases the bot's own conversation key (a direct message, a thread): the
    # requests sharing it are one conversation on the page.
    return {"id": _opaque(source, key), "sourceId": source["id"], "botName": source["label"],
            "host": source["host"], "channelLabel": channel or "채널 미확인",
            "requesterLabel": requester or "요청자 미확인", "authorityLabel": source["authorityLabel"],
            "scopeLabel": source["scopeLabel"], "title": title, "status": status,
            "updatedAt": _stamp(updated), "nativeRef": native,
            "conversationRef": _conversation_ref(source, conversation), "_conversation": conversation,
            "conversationKey": _opaque(source, "conversation:" + thread) if isinstance(thread, str) and thread.strip() else None}


def _conversation_ref(source, session):
    return {"kind": "hermes", "id": _opaque(source, "hermes:" + session)} if session else None


def _native(value):
    try:
        return {"agent": "codex", "id": str(uuid.UUID(str(value))), "home": ".codex"}
    except (ValueError, TypeError, AttributeError):
        return None


def _hermes_session(value):
    """The Hermes session a ledger names for a request, or None (a Codex thread, nothing)."""
    match = HERMES_REF.fullmatch(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _holicii(connection, source):
    _columns(connection, "event_inbox", {"id", "source_id", "actor", "state", "received_at", "processed_at"})
    # rowid follows receipt insertion; encrypted payload/refs/scope are never selected.
    rows = connection.execute("SELECT id,source_id,actor,state,received_at,processed_at "
                              "FROM event_inbox ORDER BY rowid DESC LIMIT ?", (LIMIT,))
    return [_base(source, row["id"], title="등록된 업무 요청", status=_text(row["state"], "unknown", 40),
                  updated=row["processed_at"] or row["received_at"],
                  channel=_alias("수신 경로", source, row["source_id"]),
                  requester=_alias("요청자", source, row["actor"])) for row in rows]


def _hermes(connection, source):
    columns = _columns(connection, "sessions", {"id", "source", "session_key", "user_id", "chat_id", "started_at", "ended_at", "origin_json"})
    # Use an existing time index; the list never reads message bodies or imported native
    # history (a conversation is read on its own, for one request: read_conversation).
    index = _index_on(connection, "sessions", ("started_at",))
    if index is None:
        raise SourceError("봇 세션의 최신 기록 인덱스를 확인하지 못했습니다.")
    activity = "last_activity_at" if "last_activity_at" in columns else "started_at"
    query = ('SELECT id,user_id,chat_id,substr(session_key,1,512) AS session_key,started_at,ended_at,'
             + activity + ' AS activity,substr(origin_json,1,16384) AS origin '
             'FROM sessions INDEXED BY "' + index.replace('"', '""') + '" '
             "WHERE source='slack' AND session_key IS NOT NULL ORDER BY started_at DESC LIMIT ?")
    result = []
    for row in connection.execute(query, (LIMIT,)):
        try:
            origin = json.loads(row["origin"] or "{}")
        except ValueError:
            raise SourceError("봇 세션의 출처 정보를 확인하지 못했습니다.") from None
        if not isinstance(origin, dict):
            raise SourceError("봇 세션의 출처 정보를 확인하지 못했습니다.")
        result.append(_base(source, row["id"], title="Slack 봇 대화", status="closed" if row["ended_at"] else "unknown",
                            updated=row["ended_at"] or row["activity"] or row["started_at"],
                            channel=_text(origin.get("chat_name"), _alias("채널", source, row["chat_id"]), 60),
                            requester=_text(origin.get("user_name"), _alias("요청자", source, row["user_id"]), 40),
                            conversation=row["id"] if isinstance(row["id"], str) and HERMES_ID.fullmatch(row["id"]) else None,
                            thread=row["session_key"]))
    return result


def _ledger(connection, source):
    columns = _columns(connection, "tasks", {"id", "channel", "user", "text", "status", "codex_thread", "created_at", "started_at", "finished_at"})
    # The bot's own conversation key (a direct message's, a thread's), when it keeps one.
    thread = "substr(session_key,1,512)" if "session_key" in columns else "NULL"
    _columns(connection, "users", {"id", "name", "real_name"})
    _columns(connection, "channels", {"id", "name"})
    _primary_key(connection, "tasks", "id", integer=True)
    _primary_key(connection, "users", "id")
    _primary_key(connection, "channels", "id")
    # The integer primary key is the receipt order. Exact ID joins are bounded
    # to these requests. A later session mapping never rewrites old task ownership.
    query = ("SELECT t.id,t.channel,t.user,substr(t.text,1,2048) AS title,t.status,t.codex_thread,t.thread,"
             "t.created_at,t.started_at,t.finished_at,substr(u.real_name,1,200) AS real_name,"
             "substr(u.name,1,200) AS user_name,substr(c.name,1,200) AS channel_name "
             "FROM (SELECT id,channel,user,substr(text,1,2048) AS text,status,codex_thread," + thread + " AS thread,"
             "created_at,started_at,finished_at "
             "FROM tasks ORDER BY id DESC LIMIT ?) t LEFT JOIN users u ON u.id=t.user "
             "LEFT JOIN channels c ON c.id=t.channel ORDER BY t.id DESC")
    result = []
    for row in connection.execute(query, (LIMIT,)):
        result.append(_base(source, row["id"], title=_text(row["title"], "봇 요청", 120),
                            status=_text(row["status"], "unknown", 40),
                            updated=row["finished_at"] or row["started_at"] or row["created_at"],
                            channel=_text(row["channel_name"], _alias("채널", source, row["channel"]), 60),
                            requester=_text(row["real_name"] or row["user_name"], _alias("요청자", source, row["user"]), 40),
                            native=_native(row["codex_thread"]), conversation=_hermes_session(row["codex_thread"]),
                            thread=row["thread"]))
    if len(result) > LIMIT or len({row["id"] for row in result}) != len(result):
        raise SourceError("봇 원장의 요청과 사용자 연결이 고유하지 않습니다.")
    return result


def read_source(source, home=None):
    """One source, one read-only SQLite connection, no runtime imports or writes."""
    source = _validate_source(source)
    home = Path(home) if home is not None else Path.home()
    path = _safe_file(home / source["path"], home)
    deadline = time.monotonic() + 1
    connection = _connect(path, home)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = {"holicii_sqlite": _holicii, "hermes_sqlite": _hermes,
                "slack_ledger": _ledger}[source["kind"]](connection, source)
    finally:
        connection.close()
    if source["kind"] == "slack_ledger":
        _verify_native_home(rows, home, deadline)
    return {"requests": rows}


def _hermes_store(source):
    """Where a source's Hermes conversations are: a gateway's own database, or for a Slack
    request ledger that names Hermes sessions, the bot's `hermes/state.db` beside the ledger's
    folder (<bot>/<folder>/<ledger> -> <bot>/hermes/state.db). Derived, never searched for."""
    if source["kind"] == "hermes_sqlite":
        return source["path"]
    parts = PurePosixPath(source["path"]).parts
    if source["kind"] != "slack_ledger" or len(parts) < 3:
        return None
    return _relative(str(PurePosixPath(*parts[:-2], "hermes", "state.db")))


def _body(value):
    """A message as written (lines, indents, code) without secrets or control characters."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in SECRETS:
        value = pattern.sub("[가림]", value)
    return "".join(c if c in "\n\t" or unicodedata.category(c) != "Cc" else " " for c in value)


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(float(value) / (1000 if value > 100_000_000_000 else 1), 3)


# A Hermes gateway stores a user turn with the model's framing around the person's words: the
# sender's label, the thread's or channel's earlier messages, the message it replies to, notes
# on attachments, a skill's whole body, or a notice of its own. The markers are the gateway's
# own, byte for byte (gateway/run.py or run_inbound.py, its Slack adapter, agent/skill_commands.py).
HERMES_NOTICE = "[IMPORTANT:"
HERMES_SKILL = "[IMPORTANT: The user has invoked the "
HERMES_SKILL_NAME = re.compile(re.escape(HERMES_SKILL) + r'"([^"]*)"')
HERMES_SKILL_LOADED = "The full skill content is loaded below.]"
HERMES_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "
HERMES_SKILL_RUNTIME = "\n\n[Runtime note:"
HERMES_BUNDLE = " skill bundle,"
HERMES_BUNDLE_INSTRUCTION = "\nUser instruction: "
HERMES_BUNDLE_FIRST = "\n\n[Loaded as part of the "
HERMES_THREAD = "[Thread context — prior messages in this thread"
HERMES_THREAD_END = "\n[End of thread context]"
HERMES_NEW = "[New message]\n"
HERMES_SENDER = re.compile(r"\[[^\]\n]{0,200} \| Slack user <@[UW][A-Z0-9]+>\] ?")
HERMES_REPLY = re.compile(r'\[Replying to(?: your previous message)?: "')
HERMES_NOTE = "[The user sent "
# A note the gateway puts before the person's words (a restart, a reset, a first message).
HERMES_SYSTEM_NOTE = "[System note:"
# Turns that carry no words of the person's, as Hermes itself tells them
# (agent/context_compressor.py _synthetic_user_row; compaction summaries are unwrapped).
HERMES_SYNTHETIC = ("[System:", "[Your active task list", "[Planning state preserved", "[ASYNC DELEGATION",
                    "[OUT-OF-BAND", "Cronjob Response:")
HERMES_ATTACHED = "(첨부 파일)"
# Its context compaction writes a summary as a turn of either side, alone (its own words may
# follow it) or merged after a turn's own words (agent/context_compressor.py).
HERMES_SUMMARY = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")
HERMES_SUMMARY_END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
HERMES_PRIOR = "[PRIOR CONTEXT — for reference only; not a new message]"
HERMES_PRIOR_END = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"


def _hermes_unsummarized(head, tail=None):
    """A turn without the summary a compaction put in it, as (head, tail) to read on, or None
    for the summary alone (Hermes's own unwrap: the words merged before it, or after its end)."""
    if head.startswith(HERMES_PRIOR):
        end = head.find(HERMES_PRIOR_END)
        if end < 0:
            return head[len(HERMES_PRIOR):].lstrip(), tail  # its words run past the read
        prior = head[len(HERMES_PRIOR):end].strip()
        return (prior, None) if prior else None
    if not head.startswith(HERMES_SUMMARY):
        return head, tail
    end = head.find(HERMES_SUMMARY_END)
    if end >= 0:
        rest = head[end + len(HERMES_SUMMARY_END):].lstrip()
        return (rest, tail) if rest else None
    end = tail.find(HERMES_SUMMARY_END) if tail is not None else -1
    rest = tail[end + len(HERMES_SUMMARY_END):].lstrip() if end >= 0 else ""
    return (rest, None) if rest else None


def _hermes_framed(text):
    """Whether a stored user turn starts with the gateway's framing, the person's words after it."""
    head = text.lstrip()
    return (head.startswith((HERMES_NOTICE, HERMES_THREAD, HERMES_NOTE, HERMES_NEW, HERMES_SYSTEM_NOTE))
            or bool(HERMES_REPLY.match(head)) or "\n" + HERMES_NEW in head)


def _hermes_skill(head, tail):
    """A skill turn as it was typed: "/name instruction" (or "/name" alone)."""
    match = HERMES_SKILL_NAME.match(head)
    name = (match.group(1) if match else "").strip()
    label = name if name.startswith("/") else "/" + name if name else ""
    instruction = None
    if HERMES_BUNDLE in head:
        # A bundle names the instruction before the skills it loads.
        start = head.find(HERMES_BUNDLE_INSTRUCTION)
        if start >= 0:
            instruction = head[start + len(HERMES_BUNDLE_INSTRUCTION):]
            end = instruction.find(HERMES_BUNDLE_FIRST)
            instruction = instruction[:end] if end >= 0 else instruction
    elif HERMES_SKILL_LOADED in head:
        # A single skill puts it after its body: the last one is the person's (the body may quote it).
        text = tail if tail is not None else head
        start = text.rfind(HERMES_SKILL_INSTRUCTION)
        if start >= 0:
            instruction = text[start + len(HERMES_SKILL_INSTRUCTION):]
            end = instruction.find(HERMES_SKILL_RUNTIME)
            instruction = instruction[:end] if end >= 0 else instruction
    instruction = (instruction or "").strip()
    typed = (label + " " + instruction).strip() if label else instruction
    return typed or None


def _hermes_typed(head, tail=None):
    """The words the person typed in a Hermes gateway's stored user turn, and whether they are
    whole; None for a turn the gateway wrote itself (a background job's end, a reload).

    `tail` is the end of a turn longer than the read: the framing comes first, so the words
    are there. An attachment alone shows as (첨부 파일)."""
    unwrapped = _hermes_unsummarized(head, tail)
    if unwrapped is None:
        return None
    if unwrapped != (head, tail):
        head, tail = unwrapped
        if not head.strip():
            return None
    if head.startswith(HERMES_SKILL):
        typed = _hermes_skill(head, tail)
        return (typed, True) if typed else None
    if head.startswith((HERMES_NOTICE,) + HERMES_SYNTHETIC):
        return None
    # A long turn without a block before the words is the person's own long text: its start, cut.
    rest, whole = head, tail is None
    if tail is not None and _hermes_framed(head):
        # Past the last context block the tail reaches; the words follow it.
        cut = max(tail.rfind(HERMES_THREAD_END), tail.rfind("\n" + HERMES_NEW))
        if cut >= 0:
            rest, whole = tail[cut + 1:], True
            rest = rest[rest.find("\n") + 1:] if rest.startswith(HERMES_THREAD_END.lstrip("\n")) else rest
        else:
            rest = tail
    framed = attached = False
    while True:
        current = rest.lstrip()
        if current.startswith(HERMES_THREAD):
            end = current.find(HERMES_THREAD_END)
            if end < 0:
                break
            rest = current[end + len(HERMES_THREAD_END):]
        elif current.startswith(HERMES_NEW):
            rest = current[len(HERMES_NEW):]
        elif HERMES_REPLY.match(current):
            end = current.find('"]\n')
            if end < 0:
                break
            rest = current[end + 3:]
        elif current.startswith(HERMES_NOTE):
            end = current.find("]\n")
            if end < 0:
                break
            rest, attached = current[end + 2:], True
        elif current.startswith(HERMES_SYSTEM_NOTE):
            end = current.find("]\n\n")
            rest = current[end + 3:] if end >= 0 else ""  # a note alone carries no words
        elif HERMES_SENDER.match(current):
            rest = current[HERMES_SENDER.match(current).end():]
        elif current.startswith("[") and "\n\n" + HERMES_NEW in current:
            # A channel's earlier messages, then the new one.
            rest = current[current.find("\n\n" + HERMES_NEW) + 2:]
        else:
            break
        framed = True
    if not framed and tail is None:
        return head, True
    # The gateway frames its own events too (a background job's end in a thread gets the thread's
    # context): after the framing, a notice is still the gateway's and a skill is still typed so.
    current = rest.lstrip()
    if current.startswith(HERMES_SKILL):
        typed = _hermes_skill(current, None)
        return (typed, whole) if typed else None
    if current.startswith((HERMES_NOTICE,) + HERMES_SYNTHETIC):
        return None
    typed = rest.strip()
    if not typed:
        return (HERMES_ATTACHED, whole) if attached else None
    return typed, whole


def read_conversation(source, sessions, home=None):
    """The newest user and assistant text of these Hermes sessions (newest session first),
    read-only and bounded. Tool calls, tool results and reasoning are never selected."""
    source = _validate_source(source)
    if (not isinstance(sessions, list) or not 0 < len(sessions) <= CONVERSATION_SESSIONS
            or len(set(sessions)) != len(sessions)
            or not all(isinstance(value, str) and HERMES_ID.fullmatch(value) for value in sessions)):
        raise SourceError("봇 대화 식별자가 올바르지 않습니다.")
    relative = _hermes_store(source)
    if relative is None:
        raise SourceError("이 봇 출처의 대화 기록은 읽을 수 없습니다.")
    home = Path(home) if home is not None else Path.home()
    path = _safe_file(home / relative, home)
    deadline = time.monotonic() + 2
    connection = _connect(path, home)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
    budget, result = CONVERSATION_BUDGET, []
    try:
        connection.execute("PRAGMA query_only=ON")
        _columns(connection, "messages", {"session_id", "role", "content", "timestamp"})
        index = _index_on(connection, "messages", ("session_id", "timestamp"))
        if index is None:
            raise SourceError("봇 대화의 세션 인덱스를 확인하지 못했습니다.")
        # A user turn (or a compaction's) longer than the read also gives its end: the gateway's
        # framing and a summary come first.
        query = ("SELECT role,substr(content,1,?) AS body,"
                 "CASE WHEN (role='user' OR substr(content,1,1)='[') AND length(content)>? "
                 "THEN substr(content,-?) END AS tail,"
                 "length(content) AS size,timestamp "
                 'FROM messages INDEXED BY "' + index.replace('"', '""') + '" '
                 "WHERE session_id=? AND role IN ('user','assistant') ORDER BY timestamp DESC LIMIT ?")
        for session in sessions:
            rows, more = [], False
            for row in connection.execute(query, (BODY_LIMIT, BODY_LIMIT, BODY_LIMIT, session, SCAN_ROWS)):
                body = row["body"] if isinstance(row["body"], str) else ""
                if not body.strip():
                    continue  # a turn that only called tools (its calls are never read)
                size = row["size"] if isinstance(row["size"], int) and not isinstance(row["size"], bool) else len(body)
                whole = size <= len(body)
                if row["role"] == "user":
                    # Only what the person typed; a notice the gateway wrote itself is not shown.
                    typed = _hermes_typed(body, row["tail"] if isinstance(row["tail"], str) else None)
                    if typed is None:
                        continue
                    body, whole = typed
                else:
                    # An answer a compaction merged its summary into keeps only the answer.
                    tail = row["tail"] if isinstance(row["tail"], str) else None
                    kept = _hermes_unsummarized(body, tail)
                    if kept is None:
                        continue
                    if kept != (body, tail):
                        body, whole = kept[0], kept[1] is None
                        if not body.strip():
                            continue
                if len(rows) >= CONVERSATION_ROWS or len(body) > budget:
                    more = True
                    break
                budget -= len(body)
                text = _body(body)
                # Its length as shown when whole (masking changes it), else longer: the board says cut.
                rows.append({"role": row["role"], "text": text, "ts": _seconds(row["timestamp"]),
                             "size": len(text) if whole else max(size, len(text) + 1)})
            rows.reverse()
            result.append({"id": session, "messages": rows, "more": more})
    finally:
        connection.close()
    return {"conversations": result}


def _connect(path, home):
    """Do not create absent WAL sidecars while inspecting another application's DB."""
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    for sidecar in (wal, shm):
        if sidecar.exists() or sidecar.is_symlink():
            _safe_file(sidecar, home)
    if wal.exists() and not shm.exists() and wal.stat().st_size:
        raise SourceError("봇 원장의 읽기 전용 WAL 상태를 확인하지 못했습니다.")
    immutable = False
    if not shm.exists():
        with path.open("rb") as stream:
            header = stream.read(20)
        immutable = len(header) > 18 and header[18] == 2
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=0.1)


def _verify_native_home(rows, home, deadline):
    candidates = {row["nativeRef"]["id"] for row in rows if row["nativeRef"]}
    for row in rows:
        row["_candidate"] = row["nativeRef"]
        row["nativeRef"] = None
    if candidates and time.monotonic() < deadline:
        try:
            account = Path(home) / ".codex"
            databases = [(int(match.group(1)), path) for path in account.iterdir()
                         if (match := re.fullmatch(r"state_(\d+)\.sqlite", path.name))]
            database = _safe_file(max(databases)[1], home)
            connection = _connect(database, home)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            try:
                _columns(connection, "threads", {"id"})
                _primary_key(connection, "threads", "id")
                query = "SELECT id FROM threads WHERE id IN (" + ",".join("?" for unused in candidates) + ") LIMIT 100"
                matched = {row[0] for row in connection.execute(query, sorted(candidates))}
            finally:
                connection.close()
            for row in rows:
                candidate = row["_candidate"]
                if candidate and candidate["id"] in matched:
                    row["nativeRef"] = candidate
        except (OSError, ValueError, sqlite3.Error):
            pass
    for row in rows:
        row.pop("_candidate", None)


class BotRequests:
    def __init__(self, hosts, config_path, state_dir):
        self.hosts = {row["name"]: dict(row) for row in hosts}
        self.config_path, self.state_dir = Path(config_path), Path(state_dir)
        self.home = Path.home()
        self._lock = threading.Lock()
        # Held through a read of the sources (snapshot): one read at a time, outside self._lock.
        self._reading = threading.Lock()
        self._cache, self._source_cache, self._read_at, self._behind = None, {}, 0.0, False
        self._owner_keys, self._owners_unsaved = None, False

    # A ledger lists only its recent requests: a bot's session stays the bot's after it
    # leaves that window, or when a later read cannot confirm it (bot-owners.json).
    @staticmethod
    def owner_key(host, native):
        return json.dumps([host, native.get("agent"), native.get("id"), native.get("home")])

    def _owners(self):
        if self._owner_keys is None:
            try:
                data = json.loads((self.state_dir / "bot-owners.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                data = []
            self._owner_keys = [key for key in data if isinstance(key, str)] if isinstance(data, list) else []
        return self._owner_keys

    def _remember(self, rows):
        keys = self._owners()
        known = set(keys)
        new = [key for key in dict.fromkeys(self.owner_key(row["host"], row["nativeRef"]) for row in rows
                                            if isinstance(row.get("nativeRef"), dict)) if key not in known]
        # A write that failed is tried again by the next read, with or without new sessions.
        if not new and not self._owners_unsaved:
            return
        # Never dropped for room: a session that left the ledgers stays the bot's (about 100 bytes each).
        keys.extend(new)
        self._owners_unsaved = True
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.state_dir / "bot-owners.json"
            temp = path.with_name(path.name + "." + os.urandom(6).hex())
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(keys, out)
                os.replace(str(temp), str(path))
            finally:
                if temp.exists():
                    temp.unlink()
            self._owners_unsaved = False
        except OSError:
            pass  # kept for this process; the next read tries the file again

    def owns(self, source):
        """Whether a ledger ever named this native session as a bot's."""
        with self._lock:
            return self.owner_key(source.get("host"), source) in set(self._owners())

    def _sources(self):
        if not self.config_path.exists():
            if self.config_path.is_symlink():
                raise SourceError("봇 출처 설정 경로가 안전하지 않습니다.")
            return []
        path = _safe_file(self.config_path, self.home, private=True)
        if path.stat().st_size > 64 * 1024:
            raise SourceError("봇 출처 설정이 너무 큽니다.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or set(data) != {"version", "sources"}
                or type(data["version"]) is not int or data["version"] != 1
                or not isinstance(data["sources"], list) or len(data["sources"]) > 12):
            raise SourceError("봇 출처 설정 형식이 올바르지 않습니다.")
        rows = [_validate_source(row, self.hosts) for row in data["sources"]]
        if len({row["id"] for row in rows}) != len(rows):
            raise SourceError("봇 출처 식별자가 중복되었습니다.")
        return rows

    def _collect(self, source, sessions=None):
        """A source's requests, or with `sessions` (Hermes session ids) their conversations."""
        host = self.hosts[source["host"]]
        if host.get("local"):
            return read_source(source, self.home) if sessions is None else read_conversation(source, sessions, self.home)
        from server import collector_command
        prelude = "BOT_REQUEST = " + ascii(source) + "\n"
        if sessions is not None:
            prelude += "BOT_CONVERSATION = " + ascii(list(sessions)) + "\n"
        program = prelude.encode() + Path(__file__).read_bytes()
        done = subprocess.run(collector_command(host, [], self.state_dir), input=program,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if done.returncode or len(done.stdout) > (1 if sessions is None else 4) * 1024 * 1024:
            raise SourceError("봇 원장의 원격 읽기를 완료하지 못했습니다.")
        result = json.loads(done.stdout)
        if not isinstance(result, dict) or result.get("error"):
            raise SourceError("봇 원장의 원격 읽기를 완료하지 못했습니다.")
        return result

    @staticmethod
    def _checked(source, result):
        """The rows the page may see, and (kept by the board) each one's Hermes session."""
        rows = result.get("requests") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > LIMIT:
            raise SourceError("봇 요청 목록 형식이 올바르지 않습니다.")
        checked, sessions = [], {}
        for row in rows:
            if (not isinstance(row, dict) or row.get("sourceId") != source["id"]
                    or row.get("host") != source["host"] or not isinstance(row.get("id"), str)
                    or not re.fullmatch(re.escape(source["id"]) + r":[0-9a-f]{24}", row["id"])):
                raise SourceError("봇 요청의 원본 출처가 다릅니다.")
            native = row.get("nativeRef")
            if native is not None and (not isinstance(native, dict) or _native(native.get("id")) != native
                                       or source["kind"] != "slack_ledger"):
                raise SourceError("봇 요청의 native 세션 연결이 올바르지 않습니다.")
            session, ref = row.get("_conversation"), row.get("conversationRef")
            if session is not None or ref is not None:
                if (source["kind"] not in ("hermes_sqlite", "slack_ledger") or not isinstance(session, str)
                        or not HERMES_ID.fullmatch(session) or ref != _conversation_ref(source, session)):
                    raise SourceError("봇 요청의 대화 연결이 올바르지 않습니다.")
                sessions[row["id"]] = session
            thread = row.get("conversationKey")
            if thread is not None and (not isinstance(thread, str)
                                       or not re.fullmatch(re.escape(source["id"]) + r":[0-9a-f]{24}", thread)):
                raise SourceError("봇 요청의 대화 묶음이 올바르지 않습니다.")
            checked.append({"id": row["id"], "sourceId": source["id"], "host": source["host"],
                            "botName": source["label"], "authorityLabel": source["authorityLabel"], "scopeLabel": source["scopeLabel"],
                            **{key: _text(row.get(key), "미확인", limit) for key, limit in
                               (("channelLabel", 60), ("requesterLabel", 40), ("title", 120), ("status", 40))},
                            "updatedAt": _stamp(row.get("updatedAt")), "nativeRef": native,
                            "conversationRef": ref, "conversationKey": thread})
        if len({row["id"] for row in checked}) != len(checked):
            raise SourceError("봇 요청 식별자가 중복되었습니다.")
        return checked, sessions

    def _cached(self, source):
        """A source's last read, while its settings are the same."""
        cached = self._source_cache.get(source["id"])
        return cached if cached and cached["signature"] == json.dumps(source, sort_keys=True) else None

    @staticmethod
    def _current(cached, force):
        """A read still good: within its time, a failed one for longer (a bot may be down a while)."""
        limit = FAILED_RETRY_SECONDS if cached and cached["failed"] else CACHE_SECONDS
        return bool(cached) and not force and time.monotonic() - cached["fetched"] < limit

    def snapshot(self, force=False):
        if type(force) is not bool:
            raise ValueError("봇 요청 새로고침 값이 올바르지 않습니다.")
        # One read of the sources at a time. The reads themselves (a remote one over SSH, up to
        # 20 s) hold no lock that a session guard (requests_now, owns) waits on.
        with self._reading:
            with self._lock:
                try:
                    sources, broken = self._sources(), False
                except (OSError, ValueError, RuntimeError):
                    sources, broken = [], True
                due = [source for source in sources if not self._current(self._cached(source), force)]
            outcomes = {}
            for source in due:
                try:
                    outcomes[source["id"]] = self._checked(source, self._collect(source))
                except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
                    outcomes[source["id"]] = None
            with self._lock:
                return self._assemble(sources, broken, outcomes)

    def _assemble(self, sources, broken, outcomes):
        """The list from each source's new read (outcomes: rows and sessions, or None for a failed
        read) or its last one."""
        result = {"requests": [], "sources": [], "fetchedAt": _now()}
        changed = self._cache is None
        if broken:
            changed = True
            # A broken config must not erase prior bot/native ownership evidence.
            if self._cache:
                result["requests"] = [{**row, "stale": True} for row in self._cache["requests"]]
                result["sources"] = [{**row, "ok": False, "stale": True,
                                      "error": "봇 출처 설정을 읽지 못했습니다."}
                                     for row in self._cache["sources"] if row["id"] != "configuration"]
            result["sources"].append({"id": "configuration", "label": "봇 출처 설정", "host": None,
                                      "ok": False, "error": "봇 출처 설정을 읽지 못했습니다.", "updatedAt": None})
        for source in sources:
            cached = self._cached(source)
            if source["id"] not in outcomes:
                # Current when planned; still there, as _reading is held from the plan to here and
                # only a read of the sources writes _source_cache.
                result["requests"].extend(cached["rows"])
                result["sources"].append(cached["status"])
                continue
            changed = True
            status = {key: source[key] for key in ("id", "label", "host")}
            if outcomes[source["id"]] is not None:
                rows, sessions = outcomes[source["id"]]
                status.update(ok=True, error=None, updatedAt=_now())
                failed = False
            else:
                failed = True
                rows = [{**row, "stale": True} for row in cached["rows"]] if cached else []
                sessions = dict(cached["sessions"]) if cached else {}
                status.update(ok=False, error="봇 원장을 읽지 못했습니다. 출처와 연결을 확인해 주세요.",
                              updatedAt=cached["status"]["updatedAt"] if cached else None, stale=bool(rows))
            self._source_cache[source["id"]] = {"signature": json.dumps(source, sort_keys=True), "source": source,
                                                "rows": rows, "sessions": sessions, "status": status,
                                                "failed": failed, "fetched": time.monotonic()}
            result["requests"].extend(rows)
            result["sources"].append(status)
        result["requests"].sort(key=lambda row: row["updatedAt"] or "", reverse=True)
        self._remember(result["requests"])
        if not changed and self._cache and result["sources"] == self._cache["sources"]:
            result["fetchedAt"] = self._cache["fetchedAt"]
        self._cache, self._read_at = result, time.monotonic()
        return copy.deepcopy(result)

    def requests_now(self):
        """The listed requests as last read, at once, for a guard on the way of a read, a send or a
        notification (callers only read them): a list older than its time is read again behind
        (one read at a time). Only a board that has never read them waits for the first read."""
        with self._lock:
            cache = self._cache
            behind = (cache is not None and not self._behind
                      and time.monotonic() - self._read_at >= CACHE_SECONDS)
            if behind:
                self._behind = True
        if cache is None:
            return self.snapshot()["requests"]
        if behind:
            threading.Thread(target=self._read_behind, name="bot-requests", daemon=True).start()
        return cache["requests"]

    def _read_behind(self):
        try:
            self.snapshot()
        finally:
            with self._lock:
                self._behind = False

    def conversation(self, source_id, request_id, read_native=None):
        """A listed request's conversation (with the requests sharing its conversation key, one
        part per session, oldest first), watch-only: a Hermes session read from the bot's
        database (bounded, read-only), a Codex session through `read_native(host, nativeRef)`,
        the board's own read. Each part says when it could not be read; nothing is written,
        sent or resumed."""
        if (not isinstance(source_id, str) or not IDENTIFIER.fullmatch(source_id) or not isinstance(request_id, str)
                or not re.fullmatch(re.escape(source_id) + r":[0-9a-f]{24}", request_id)):
            raise ValueError("봇 요청을 확인하지 못했습니다.")
        self.requests_now()
        with self._lock:
            # Only a source still configured, as it was when its requests were read.
            configured = {row["id"]: json.dumps(row, sort_keys=True) for row in self._sources()}
            cached = self._source_cache.get(source_id)
            if not cached or configured.get(source_id) != cached["signature"]:
                cached = None
            target = next((row for row in cached["rows"] if row["id"] == request_id), None) if cached else None
            if target is None:
                raise ValueError("봇 요청을 찾지 못했습니다. 목록을 새로고침한 뒤 다시 열어 주세요.")
            source, sessions = cached["source"], dict(cached["sessions"])
            parts = self._parts(self._members(cached["rows"], target), sessions)
        hermes = [part["ref"] for part in reversed(parts) if part["kind"] == "hermes"]
        failure = "이 대화 기록을 읽지 못했습니다. 잠시 뒤 다시 확인해 주세요."
        failed = (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error, subprocess.TimeoutExpired)
        # The bot's Hermes sessions in one read, its Codex ones through the board's reads, together.
        with ThreadPoolExecutor(max_workers=4) as pool:
            hermes_read = pool.submit(self._collect, source, hermes) if hermes else None
            codex_reads = {index: pool.submit(read_native, source["host"], part["ref"])
                           for index, part in enumerate(parts) if part["kind"] == "codex" and read_native is not None}
            try:
                read = self._checked_conversations(hermes, hermes_read.result()) if hermes_read else {}
            except failed:
                read = {}
            segments = []
            for index, part in enumerate(parts):
                segment = {"kind": part["kind"], "requests": part["requests"], "firstAt": part["firstAt"],
                           "lastAt": part["lastAt"], "messages": [], "more": False, "error": None}
                if part["kind"] == "hermes":
                    if part["ref"] in read:
                        segment.update(read[part["ref"]])
                    else:
                        segment["error"] = failure
                else:
                    try:
                        if index not in codex_reads:
                            raise RuntimeError("no native reader")
                        rows = codex_reads[index].result() or []
                        segment["messages"] = [_shown(row) for row in rows
                                               if isinstance(row, dict) and row.get("role") in ("user", "assistant")]
                        # The board's read is a window of the newest rows: older ones may be cut.
                        segment["more"] = len(rows) >= CONVERSATION_ROWS
                    except failed:
                        segment["error"] = failure
                segments.append(segment)
        return {"segments": segments, "fetchedAt": _now()}

    @staticmethod
    def _members(rows, target):
        """The requests one conversation view shows: those sharing the bot's conversation key
        (a direct message, a thread) among the listed ones, or the request alone."""
        key = target.get("conversationKey")
        return [row for row in rows if row.get("conversationKey") == key] if key else [target]

    @staticmethod
    def _parts(members, sessions):
        """The sessions these requests ran in, by their first request (oldest first): a Hermes
        session, or a Codex one a ledger names. Only the newest few are read."""
        parts = {}
        for row in sorted(members, key=lambda item: item["updatedAt"] or ""):
            session, native = sessions.get(row["id"]), row.get("nativeRef")
            key = ("hermes", session) if session else ("codex", native["id"]) if native else None
            if key is None:
                continue
            part = parts.setdefault(key, {"kind": key[0], "ref": session or native, "requests": 0,
                                          "firstAt": row["updatedAt"], "lastAt": row["updatedAt"]})
            part["requests"] += 1
            part["lastAt"] = row["updatedAt"]
        return list(parts.values())[-CONVERSATION_SESSIONS:]

    @staticmethod
    def _checked_conversations(sessions, result):
        items = result.get("conversations") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) > len(sessions):
            raise SourceError("봇 대화 형식이 올바르지 않습니다.")
        found = {}
        for item in items:
            messages = item.get("messages") if isinstance(item, dict) else None
            if (not isinstance(messages, list) or len(messages) > CONVERSATION_ROWS
                    or item.get("id") not in sessions or item["id"] in found):
                raise SourceError("봇 대화 형식이 올바르지 않습니다.")
            rows = []
            for message in messages:
                text = message.get("text") if isinstance(message, dict) else None
                if (not isinstance(text, str) or len(text) > BODY_LIMIT
                        or message.get("role") not in ("user", "assistant")):
                    raise SourceError("봇 대화 형식이 올바르지 않습니다.")
                rows.append(_message(message["role"], text, message.get("ts"), message.get("size")))
            found[item["id"]] = {"messages": rows, "more": item.get("more") is True}
        return found


def _message(role, text, ts, size):
    """A conversation row as the board shows its other conversations (collector.text_row):
    a Slack request's envelope unwrapped, then cut to the board's length. The board only."""
    import collector
    ts = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 else None
    row = collector.text_row(role, text, ts)
    row.pop("bodyHash", None)
    if isinstance(size, int) and not isinstance(size, bool) and size > len(text):
        # The reader cut it before the envelope came off: its full length is not this text's.
        row["truncated"] = True
        row.pop("fullLength", None)
    return row


def _shown(row):
    """A Codex row from the board's own read, with only what a watch-only view shows."""
    shown = {key: row[key] for key in ("role", "text", "ts", "truncated", "fullLength") if key in row}
    shown["text"] = shown.get("text") if isinstance(shown.get("text"), str) else ""
    return shown


def main():
    sessions = globals().get("BOT_CONVERSATION")
    try:
        if sessions is None:
            result = read_source(globals().get("BOT_REQUEST"))
        else:
            result = read_conversation(globals().get("BOT_REQUEST"), sessions)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        result = ({"requests": [], "error": "봇 원장을 읽지 못했습니다."} if sessions is None
                  else {"conversations": [], "error": "봇 대화를 읽지 못했습니다."})
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
