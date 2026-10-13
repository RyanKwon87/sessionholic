#!/usr/bin/env python3
"""Collect this host's Claude Code and Codex sessions as one read-only JSON snapshot.

Runs on the host that owns the sessions, either locally (`python3 collector.py snapshot`)
or over SSH without leaving a file behind (`ssh host python3 -I - snapshot < collector.py`).
Standard library only and Python 3.9 compatible because POSIX host's /usr/bin/python3 is 3.9.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

try:
    import settings
except ModuleNotFoundError:
    # -I excludes the script directory; load only the sibling installed module.
    import importlib.util
    spec = importlib.util.spec_from_file_location("settings", Path(__file__).resolve().with_name("settings.py"))
    settings = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings)
import instance  # noqa: E402  (registered by settings, also under -I or the remote stdin program)
try:
    import agent_router
except ModuleNotFoundError as missing:
    # Same sibling rule as settings; the remote stdin program registers it beforehand.
    if missing.name != "agent_router":
        raise
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_router", Path(__file__).resolve().with_name("agent_router.py"))
    agent_router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_router)

NAMES = instance.current()
HOME = Path.home()
ROUTER = HOME / ".codex-router"
SOCKET_REL = Path("app-server-control/app-server-control.sock")
# Codex homes are addressed by their path relative to HOME so callers can never name another socket.
HOME_KEY = re.compile(r"^\.codex(-[A-Za-z0-9_-]{1,40})?$|^\.codex-router/[0-9a-f]{16}$")
SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
EXTRA_PATH = ["/opt/homebrew/bin", str(HOME / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
TEXT_LIMIT = 4000
MESSAGE_LIMIT = 80
# Codex history is read newest-first in pages of this many items, at most this many pages.
HISTORY_PAGE = 100
HISTORY_PAGES = 10
SNIPPET_TAILS = (32 * 1024, 128 * 1024)
CODEX_THREAD_LIMIT = 25
# Codex homes listed at once; each uses its own daemon connection.
CODEX_PARALLEL = 4
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
COMMAND_NAME = re.compile(r"<command-name>([^<]{1,60})</command-name>")
BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)


class RpcError(Exception):
    pass


class RpcReplyError(RpcError):
    """The daemon answered one request with a JSON-RPC error; the connection is still usable."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ---------- small helpers ----------

def tool_env():
    """Child tools see the host's own login, never the caller's agent session variables."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "ANTHROPIC_", "CODEX_"))
           and k not in ("CLAUDECODE", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY")}
    env["PATH"] = os.pathsep.join(EXTRA_PATH + [os.environ.get("PATH", "")])
    return env


def which(name):
    return shutil.which(name, path=os.pathsep.join(EXTRA_PATH + [os.environ.get("PATH", "")]))


def run(args, timeout=20):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=tool_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def clip(text, limit=TEXT_LIMIT):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + " …"


def text_row(role, text, ts):
    """A clipped message row; only cut rows carry `truncated` and the original `fullLength`."""
    text = str(text or "")
    row = {"role": role, "text": clip(text), "ts": ts}
    if len(text) > TEXT_LIMIT:
        row.update(truncated=True, fullLength=len(text))
    return row


def one_line(value, limit=160):
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def to_epoch(value):
    """Accept epoch seconds, epoch milliseconds or ISO 8601 and return epoch seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 1e12 else int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


# ---------- project and account labels ----------

def load_projects(path=None):
    """Labels from the explicit Sessionholic JSON settings (root projects), then the optional
    agent-router project list. Component projects only decide environments."""
    config = settings.load(HOME, path)
    router = agent_router.from_settings(config)
    return ([p for p in config["projects"] if p.get("match") != "component"]
            + (router.projects() if router is not None else []))


def project_for(cwd, projects):
    """Label a working directory with the longest matching registered project root."""
    if not cwd:
        return ""
    cwd = str(cwd)
    best = None
    for project in projects:
        for root in project.get("roots") or (project["root"],):
            if root.startswith("/") and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
                if best is None or len(root) > best[0]:
                    best = (len(root), project["label"])
    if best:
        return best[1]
    return "~" if cwd == str(HOME) else Path(cwd).name


def project_label(project_id, projects):
    return next((p["label"] for p in projects if p["id"] == project_id), project_id or "")


def home_label(key, path, labels=None, config=None, router=None):
    """(project id or None, account label): the profile row's label and project; with the
    optional agent-router adapter, its names for homes the row does not label."""
    config = config if config is not None else settings.load(HOME)
    configured = settings.profile("codex", key, HOME, config)
    project = configured.get("project")
    if configured.get("label"):
        return project, configured["label"]
    router = router if router is not None else agent_router.from_settings(config)
    if router is not None:
        found, account = router.home_label(key, path, labels)
        return project or found, account
    return project, "기본" if key == ".codex" else key


# ---------- phases shared by the board ----------

def claude_phase(state, status):
    if state in ("blocked", "failed") or status == "waiting":
        return "needs_input"
    if state == "working" or status == "busy":
        return "working"
    if state in ("done", "stopped"):
        return "done"
    return "idle"


def codex_phase(status):
    status = status or {}
    if status.get("type") == "systemError" or status.get("activeFlags"):
        return "needs_input"
    if status.get("type") == "active":
        return "working"
    if status.get("type") == "idle":
        return "idle"
    return "closed"


# ---------- Claude Code ----------

def claude_transcript(session_id):
    if not SESSION_ID.match(session_id or ""):
        return None
    matches = glob.glob(str(HOME / ".claude/projects" / "*" / (session_id + ".jsonl")))
    return Path(max(matches, key=os.path.getmtime)) if matches else None


def claude_transcript_index():
    """One directory scan for a whole snapshot: {session id: newest transcript path}."""
    index = {}
    for name in glob.glob(str(HOME / ".claude/projects" / "*" / "*.jsonl")):
        path = Path(name)
        if not SESSION_ID.match(path.stem):
            continue
        previous = index.get(path.stem)
        if previous is not None:
            try:
                if path.stat().st_mtime <= previous.stat().st_mtime:
                    continue
            except OSError:
                # A transcript removed during the scan loses to the one still present.
                if not path.exists():
                    continue
        index[path.stem] = path
    return index


def tail_entries(path, max_bytes):
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        data = stream.read()
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]  # the first line was cut in the middle
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def user_text(text):
    """Hide harness wrappers in user lines; returns (role, text) or None to skip.

    Lines Claude Code writes as the user are not the user's words: "[Request interrupted by
    user…]" becomes a system note, a `!` shell command (<bash-input>) a command record, and
    its output (<bash-stdout>/<bash-stderr>) is not shown, like other tool output."""
    head = text[:300]
    if "<task-notification>" in head:
        return "system", "백그라운드 작업 알림"
    match = COMMAND_NAME.search(text)
    if match:
        return "system", "명령 " + match.group(1).strip()
    stripped = text.lstrip()
    if stripped.startswith("<local-command") or stripped.startswith(("<bash-stdout>", "<bash-stderr>")):
        return None
    if stripped.startswith("[Request interrupted by user"):
        return "system", "사용자가 중단함"
    match = BASH_INPUT.match(stripped)
    if match:
        return "tool", "$ " + one_line(match.group(1), 160)
    text = REMINDER.sub("", text).strip()
    return ("user", text) if text else None


def tool_summary(name, args):
    args = args if isinstance(args, dict) else {}
    for key in ("description", "command", "file_path", "pattern", "url", "query", "prompt"):
        if args.get(key):
            return (name or "도구") + " · " + one_line(args[key], 140)
    return name or "도구"


# A tool row's kind, so the board can say "도구 실행 7개 · 명령 4 · 파일 수정 2".
CLAUDE_TOOL_KINDS = {
    "Bash": "command", "BashOutput": "command", "KillShell": "command", "KillBash": "command",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Read": "read", "NotebookRead": "read",
    "Grep": "search", "Glob": "search", "LS": "search", "WebSearch": "search", "WebFetch": "search",
    "ToolSearch": "search", "Task": "agent", "Agent": "agent",
}
CODEX_TOOL_KINDS = {"commandExecution": "command", "fileChange": "edit", "webSearch": "search",
                    "mcpToolCall": "mcp", "dynamicToolCall": "other"}


def claude_tool_kind(name):
    name = str(name or "")
    if name.startswith("mcp__"):
        return "mcp"
    return CLAUDE_TOOL_KINDS.get(name, "other")


def native_attachments(blocks):
    """Attachment evidence only: never expose URLs, paths or encoded contents."""
    out = []
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and block.get("type") in ("image", "localImage", "document"):
            image = block["type"] != "document"
            out.append({"id": None, "name": "이미지" if image else "파일",
                        "type": "image/*" if image else "application/octet-stream",
                        "size": None, "kind": "image" if image else "file", "source": "native"})
    return out[:32]


def queued_prompt(entry):
    """The content of input the user typed while Claude was working, or None.

    Claude Code records such input (a message the board pasted mid-turn included) as a
    queued_command attachment instead of a user line; a notification queued the same way
    is not the user's words."""
    attachment = entry.get("attachment")
    if (not isinstance(attachment, dict) or attachment.get("type") != "queued_command"
            or attachment.get("commandMode") not in (None, "prompt")):
        return None
    origin = attachment.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    prompt = attachment.get("prompt")
    if isinstance(prompt, str):
        return prompt
    return prompt if isinstance(prompt, list) else None


def evidence_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) else None


def claude_messages(entries):
    out = []
    # Queued input since the last answer; an interrupt hands it back to the input line, and
    # Claude Code then records it once more as a plain prompt, which is shown instead.
    queued = []
    dropped = set()
    for entry in entries:
        kind = entry.get("type")
        content = queued_prompt(entry) if kind == "attachment" else None
        if content is not None:
            kind = "user"
        elif kind not in ("user", "assistant"):
            continue
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        ts = to_epoch(entry.get("timestamp"))
        if entry.get("isCompactSummary"):
            # The summary Claude Code writes after /compact (or an automatic compaction) is
            # recorded as a user line; the board shows one note instead of its text.
            out.append({"role": "system", "text": "이전 대화 요약으로 이어짐", "ts": ts})
            continue
        if content is None:
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        attached = native_attachments(blocks) if kind == "user" else []
        attachment_row = None
        start = len(out)
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                shown = user_text(str(block["text"])) if kind == "user" else ("assistant", block["text"])
                if shown and shown[0] == "tool":
                    out.append({"role": "tool", "text": shown[1], "ts": ts, "toolKind": "command"})
                elif shown:
                    out.append(text_row(shown[0], shown[1], ts))
                    if shown[0] == "user" and attachment_row is None:
                        attachment_row = out[-1]
            elif block.get("type") == "tool_use":
                out.append({"role": "tool", "text": tool_summary(block.get("name"), block.get("input")), "ts": ts,
                            "toolKind": claude_tool_kind(block.get("name"))})
        if attached:
            if attachment_row is None:
                attachment_row = {"role": "user", "text": "[이미지 첨부]" if all(a["kind"] == "image" for a in attached) else "[파일 첨부]", "ts": ts}
                out.append(attachment_row)
            attachment_row["attachments"] = attached
        for row in out[start:]:
            if evidence_id(entry.get("uuid")):
                row["messageId"] = entry["uuid"]
            if evidence_id(entry.get("parentUuid")):
                row["parentMessageId"] = entry["parentUuid"]
        if kind == "assistant":
            queued = []
        elif attachment_row is not None:
            words = " ".join(str(attachment_row["text"]).split())
            if entry.get("type") == "attachment":
                queued.append((words, out[start:]))
            else:
                again = next((item for item in queued if item[0] == words), None)
                if again is not None:
                    queued.remove(again)
                    dropped.update(id(row) for row in again[1])
    if dropped:
        out = [row for row in out if id(row) not in dropped]
    return out[-MESSAGE_LIMIT:]


def claude_prompt_at(entries):
    """When the newest prompt the user sent was recorded (epoch seconds), or None.

    Claude Code records no turn start, so a working session's "작업 중" time counts from
    this approximation: tool results, harness lines and summaries are not prompts."""
    for entry in reversed(list(entries)):
        if (entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain")
                or entry.get("isCompactSummary")):
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        blocks = blocks if isinstance(blocks, list) else []
        prompt = bool(native_attachments(blocks)) or any(
            isinstance(block, dict) and block.get("type") == "text"
            and (user_text(str(block.get("text") or "")) or ("",))[0] == "user" for block in blocks)
        if prompt:
            return to_epoch(entry.get("timestamp"))
    return None


def claude_agent_rows(errors):
    """Raw `claude agents` rows, or None when Claude Code is not installed here."""
    binary = which("claude")
    if not binary:
        return None
    raw = run([binary, "agents", "--json", "--all"], timeout=30)
    if raw is None:
        errors.append("Claude 세션 목록을 읽지 못했습니다.")
        return []
    try:
        return json.loads(raw)
    except ValueError:
        errors.append("Claude 세션 목록 형식이 예상과 다릅니다.")
        return []


def claude_sessions(errors, projects):
    rows = claude_agent_rows(errors)
    return claude_session_rows(rows, errors, projects) if rows else []


def claude_session_rows(rows, errors, projects, transcripts=None):
    """Board rows from raw `claude agents` rows, with each transcript's preview.

    `transcripts` is an index from `claude_transcript_index`, scanned here when not given.
    """
    if transcripts is None:
        try:
            transcripts = claude_transcript_index()
        except OSError:
            transcripts = {}
            errors.append("Claude 대화 기록 폴더를 읽지 못했습니다.")
    out, skipped = [], 0
    for row in rows if isinstance(rows, list) else []:
        # One malformed or racing row never hides the other sessions.
        try:
            out.append(claude_row(row, transcripts, projects))
        except Exception:
            skipped += 1
    if skipped:
        errors.append(f"Claude 세션 {skipped}건을 읽지 못했습니다.")
    return out


def claude_snippet(path, size):
    """Last answer preview from a small tail first; widen only when the tail has none."""
    for limit in SNIPPET_TAILS:
        messages = claude_messages(tail_entries(path, limit))
        last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        if last:
            return one_line(last["text"], 140)
        if size <= limit:
            break
    return ""


def _metadata_text(value, limit=256):
    if (isinstance(value, str) and value.strip() and len(value) <= limit
            and not any(ord(char) < 32 for char in value)):
        return value.strip()
    return None


def session_source(value):
    """Keep a bounded native source, including a DB's stringified structured enum.

    The app-server spells `subAgent`; persisted Codex records spell `subagent`.
    Unrecognized sources remain visible and do not imply who started a session.
    """
    if isinstance(value, str):
        if len(value) > 4096:
            return None
        if value.lstrip().startswith(("{", '"')):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (str, dict)):
                    value = parsed
            except (ValueError, RecursionError):
                pass
        if isinstance(value, str):
            return _metadata_text(value, 4096)
    if isinstance(value, dict):
        try:
            encoded = json.dumps(value, allow_nan=False)
            if len(encoded) <= 4096:
                return json.loads(encoded)
        except (ValueError, TypeError, RecursionError):
            pass
    return None


def session_origin(row):
    """Explicit initiator evidence, independent of client mode or current executor.

    CLI/interactive and exec/background are transport modes, never human/agent evidence.
    A parent reference alone can be a fork, so only a structured subagent source or an
    explicit initiator establishes agent initiation. Native nicknames name the child.
    """
    source = session_source(row.get("source"))
    subagent = next((source[key] for key in ("subAgent", "subagent") if key in source), None) \
        if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
    spawn = spawn if isinstance(spawn, dict) else {}
    explicit = row.get("initiator")
    detail = explicit if isinstance(explicit, dict) else {}
    kind = detail.get("type", detail.get("kind")) if detail else explicit
    kind = {"user": "human", "human": "human", "agent": "agent"}.get(kind) if isinstance(kind, str) else None
    # Known native subagent variants. An unknown object is preserved but not classified.
    is_subagent = (isinstance(subagent, str) and subagent in ("review", "compact", "memory_consolidation")) or \
        (isinstance(subagent, dict) and
         (isinstance(subagent.get("thread_spawn"), dict) or isinstance(subagent.get("other"), str)))
    evidence = "explicit" if kind else "subagent" if is_subagent else "unknown"
    parent = next((text for key in ("parentSessionId", "parent_session_id", "parentThreadId", "parent_thread_id")
                   for text in [_metadata_text(row.get(key))] if text), None)
    parent = parent or _metadata_text(spawn.get("parent_thread_id"))
    name = (_metadata_text(row.get("initiatorName")) or _metadata_text(row.get("initiator_name")) or
            _metadata_text(detail.get("name")))
    agent_name = (_metadata_text(row.get("agentName")) or _metadata_text(row.get("agentNickname")) or
                  _metadata_text(row.get("agent_nickname")) or _metadata_text(spawn.get("agent_nickname")))
    role = (_metadata_text(row.get("agentRole")) or _metadata_text(row.get("agent_role")) or
            _metadata_text(spawn.get("agent_role")) or _metadata_text(spawn.get("agent_type")))
    return {"initiator": kind or ("agent" if is_subagent else "unknown"),
            "initiatorName": name, "initiatorEvidence": evidence, "parentSessionId": parent,
            "agentName": agent_name, "agentRole": role, "source": source,
            "originator": _metadata_text(row.get("originator"))}


def claude_row(row, transcripts, projects):
    if not isinstance(row, dict):
        raise TypeError("Claude 세션 행 형식이 올바르지 않습니다.")
    session_id = str(row.get("sessionId") or "")
    path = transcripts.get(session_id) if SESSION_ID.match(session_id) else None
    snippet, updated = "", to_epoch(row.get("startedAt"))
    if path:
        try:
            info = path.stat()
            updated = int(info.st_mtime)
            snippet = claude_snippet(path, info.st_size)
        except OSError:
            # Removed or unreadable transcripts keep the session row without a preview.
            pass
    out = {
        "agent": "claude", "id": str(row.get("id") or ""), "sessionId": session_id,
        "title": row.get("name") or "(이름 없음)", "cwd": row.get("cwd") or "",
        "project": project_for(row.get("cwd"), projects), "account": "",
        "kind": row.get("kind"), "state": row.get("state"), "status": row.get("status"),
        "waitingFor": row.get("waitingFor"), "phase": claude_phase(row.get("state"), row.get("status")),
        "startedAt": to_epoch(row.get("startedAt")), "updatedAt": updated,
        "snippet": snippet, "resume": "claude attach " + str(row.get("id") or ""),
        **session_origin(row),
    }
    pid = row.get("pid")
    if not out["id"] and isinstance(pid, int) and not isinstance(pid, bool):
        # A row `claude agents` lists without an id (seen for a working interactive
        # session) cannot be read or sent to; its process id still tells two apart on the board.
        out["pid"] = pid
    return out


# ---------- Codex (app-server daemon over a Unix socket) ----------

class AppServer:
    """Tiny JSON-RPC client for a Codex app-server daemon socket (WebSocket over a Unix socket)."""

    def __init__(self, path, timeout=10.0, capabilities=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buf = bytearray()
        self.next_id = 0
        try:
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buf:
                self._fill()
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
                raise RpcError("WebSocket 연결이 거부됐습니다.")
            params = {"clientInfo": {"name": NAMES["brand"]["codexClientName"], "version": "1"}}
            if capabilities:
                params["capabilities"] = capabilities
            self.call("initialize", params)
            self._send(0x1, json.dumps({"method": "initialized", "params": {}}).encode())
        except BaseException:
            self.sock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.sock.close()

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise RpcError("Codex daemon 연결이 끊겼습니다.")
        # Large histories can contain inline images. Extend in place instead
        # of copying the entire accumulated response on every socket read.
        self.buf.extend(chunk)

    def _need(self, size):
        while len(self.buf) < size:
            self._fill()

    def _send(self, opcode, data):
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes([0x80 | opcode, 0x80 | len(data)])
        elif len(data) < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _frame(self):
        self._need(2)
        fin, opcode = self.buf[0] & 0x80, self.buf[0] & 0x0F
        masked, size, offset = self.buf[1] & 0x80, self.buf[1] & 0x7F, 2
        if size == 126:
            self._need(4)
            size, offset = struct.unpack(">H", self.buf[2:4])[0], 4
        elif size == 127:
            self._need(10)
            size, offset = struct.unpack(">Q", self.buf[2:10])[0], 10
        mask = b""
        if masked:
            self._need(offset + 4)
            mask, offset = self.buf[offset:offset + 4], offset + 4
        self._need(offset + size)
        payload = bytes(self.buf[offset:offset + size])
        del self.buf[:offset + size]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _message(self):
        """Reassemble one data message; answer pings and stop on close."""
        parts = []
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x8:
                raise RpcError("Codex daemon이 연결을 닫았습니다.")
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if fin:
                    return b"".join(parts)

    def call(self, method, params):
        self.next_id += 1
        number = self.next_id
        self._send(0x1, json.dumps({"id": number, "method": method, "params": params}).encode())
        while True:
            try:
                message = json.loads(self._message())
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == number and "method" not in message:
                if "error" in message:
                    error = message["error"] if isinstance(message["error"], dict) else {}
                    code = error.get("code") if isinstance(error.get("code"), int) else None
                    raise RpcReplyError(str(error.get("message") or "요청 실패"), code)
                return message.get("result") or {}
            if isinstance(message, dict):
                self._other(message)

    def _other(self, message):
        """Messages that are not the awaited reply; this one-shot client ignores them."""


def codex_homes():
    """Homes with a daemon socket, read over that daemon."""
    return codex_home_sets()[0]


def codex_offline_homes():
    return codex_home_sets()[1]


def codex_home_sets():
    """(live, offline): homes with a daemon socket, and homes without one whose state DB
    still lists their threads read only. Kept router homes (`.codex-router/`) are archives:
    without a daemon they stay out, as before."""
    live, offline = [], []
    for key, path, socket_there in _codex_home_candidates():
        if socket_there:
            live.append((key, path))
        elif not key.startswith(".codex-router/") and state_db(path) is not None:
            offline.append((key, path))
    return live, offline


def _codex_home_candidates():
    # Symlinked homes (for example `.codex-alias` -> `.codex-work`) are skipped, so one
    # daemon is never listed twice under two names, unless `codexHomes.allowSymlinks`.
    # `codexHomes.excludeSuspended` skips `.codex-<a>` and `.codex-personal-<a>` of the
    # agent-router policy's suspended accounts.
    try:
        config = settings.load(HOME)
    except ValueError:
        config = None
    options = config["codexHomes"] if config is not None else {}
    # `codexHomes.hide`: retired homes, left out of collection as of the launch targets.
    skipped = set(options.get("hide", []))
    router = agent_router.from_settings(config) if config is not None else None
    if options.get("excludeSuspended") and router is not None:
        for account in router.suspended():
            skipped.update((".codex-" + account, ".codex-personal-" + account))
    homes = []
    router = HOME / ".codex-router"
    candidates = [HOME / ".codex"] + sorted(HOME.glob(".codex-*")) + sorted(router.glob("*"))
    for path in candidates:
        if path == router or not path.is_dir():
            continue
        if not options.get("allowSymlinks") and path.resolve() != path.absolute():
            continue
        key = str(path.relative_to(HOME))
        if key not in skipped and HOME_KEY.match(key):
            homes.append((key, path, (path / SOCKET_REL).exists()))
    return homes


STATE_DB = re.compile(r"state_(\d{1,4})\.sqlite\Z")
# Native source filters are transport kinds, not the initiator classification.
CODEX_SOURCES = ("cli", "vscode", "exec", "appServer", "subAgent", "unknown")


def state_db(path):
    """This home's newest Codex state DB (`state_<n>.sqlite`), or None."""
    best = None
    try:
        names = os.listdir(path)
    except OSError:
        return None
    for name in names:
        match = STATE_DB.match(name)
        if match and (best is None or int(match.group(1)) > best[0]):
            candidate = Path(path) / name
            if candidate.is_file() and not candidate.is_symlink():
                best = (int(match.group(1)), candidate)
    return best[1] if best else None


def _wal_without_index(database):
    """A database in WAL mode (header byte 18 is 2) with no `-shm` beside it."""
    try:
        with open(str(database), "rb") as stream:
            header = stream.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18] == 2 and not Path(str(database) + "-shm").exists()


def _open_read_only(sqlite3, database, immutable=False):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""),
                                 uri=True, timeout=2)
    try:
        connection.execute("PRAGMA schema_version").fetchone()
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def codex_db_threads(path):
    """(listed, loaded, failure) like `codex_home_threads`, read from the home's state DB
    without a daemon: newest unarchived threads of every source, none loaded.
    Unknown source kinds stay visible. Nothing starts or writes."""
    import sqlite3
    database = state_db(path)
    if database is None:
        return None, None, "read"
    try:
        # A WAL database whose daemon left no -shm index: a read-only connection either
        # fails (older SQLite) or creates the index (newer). Nothing may be created in the
        # account's home, so with no daemon writing it is read as immutable.
        if _wal_without_index(database):
            connection = _open_read_only(sqlite3, database, immutable=True)
        else:
            try:
                connection = _open_read_only(sqlite3, database)
            except sqlite3.OperationalError:
                connection = _open_read_only(sqlite3, database, immutable=True)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "cwd", "title", "created_at", "updated_at", "archived", "source"} <= columns:
                return None, None, "read"
            optional = [name for name in ("name", "preview", "first_user_message", "originator",
                                          "agent_nickname", "agent_role", "parent_thread_id",
                                          "initiator", "initiator_name", "parent_session_id") if name in columns]
            rows = connection.execute(
                "SELECT id, cwd, title, created_at, updated_at, source%s FROM threads WHERE archived = 0"
                " ORDER BY updated_at DESC LIMIT ?" % "".join(", " + n for n in optional),
                (CODEX_THREAD_LIMIT,)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None, None, "read"
    data = []
    for row in rows:
        extra = dict(zip(optional, row[6:]))
        data.append({**{key: extra[key] for key in optional
                         if key not in ("name", "preview", "first_user_message")},
                     "id": row[0], "cwd": row[1], "name": extra.get("name") or None,
                     "preview": extra.get("preview") or extra.get("first_user_message") or row[2],
                     "createdAt": row[3], "updatedAt": row[4], "source": session_source(row[5]),
                     "status": {"type": "notLoaded"}})
    return {"data": data}, set(), None


THREAD_LIST = {"limit": CODEX_THREAD_LIMIT, "archived": False, "sortKey": "updated_at", "sortDirection": "desc",
               "sourceKinds": list(CODEX_SOURCES)}


def list_threads(server, quick=True):
    """(thread/list result, whether the state DB answered).

    `quick` reads Codex's state DB only, without scanning and repairing JSONL rollouts.
    A daemon that rejects that argument (an older Codex, or a state DB it cannot use)
    is asked once more without it, which keeps Codex's own scan-and-repair listing.
    """
    if quick:
        try:
            return server.call("thread/list", dict(THREAD_LIST, useStateDbOnly=True)), True
        except RpcReplyError:
            pass
    try:
        return server.call("thread/list", dict(THREAD_LIST)), False
    except RpcReplyError:
        # Older daemons may reject the source filter. Keep their interactive listing and
        # report its narrower coverage rather than claiming every source is supported.
        listed = server.call("thread/list", {key: value for key, value in THREAD_LIST.items() if key != "sourceKinds"})
        if isinstance(listed, dict):
            listed = dict(listed, _sourceCoverage="interactive")
        return listed, False


def loaded_threads(server):
    loaded = server.call("thread/loaded/list", {})
    return set(loaded.get("data") or []) if isinstance(loaded, dict) else set()


def codex_home_threads(path, full=False):
    """One home's thread list over its own daemon connection: (listed, loaded, failure)."""
    try:
        with AppServer(path / SOCKET_REL) as server:
            listed, _ = list_threads(server, quick=not full)
            loaded = loaded_threads(server)
    except (OSError, RpcError):
        return None, None, "connect"
    except Exception:
        return None, None, "read"
    return listed, loaded, None


def codex_labels(homes):
    try:
        config = settings.load(HOME)
        router = agent_router.from_settings(config)
        hashes = router.labels() if router is not None else None
    except Exception:
        config = router = hashes = None
    labels = []
    for key, path in homes:
        try:
            if config is None:
                raise ValueError("settings")
            labels.append(home_label(key, path, hashes, config, router))
        except Exception:
            labels.append((None, key))
    return labels


def codex_home_rows(key, project_id, account, reply, projects, offline=False):
    """(rows, errors) of one home's (listed, loaded, failure) reply; `offline` rows come from
    its state DB without a daemon and say so (`appServer: "off"`)."""
    listed, loaded, failure = reply
    if failure and offline:
        return [], [f"Codex({account}) 기록을 읽지 못했습니다."]
    if failure == "connect":
        return [], [f"Codex daemon({account})에 연결하지 못했습니다."]
    if failure:
        return [], [f"Codex daemon({account}) 응답을 읽지 못했습니다."]
    threads = listed.get("data") if isinstance(listed, dict) else None
    rows, skipped = [], 0
    for thread in threads if isinstance(threads, list) else []:
        # Each thread row is isolated so one unexpected shape keeps the rest visible.
        try:
            row = codex_row(thread, key, project_id, account, loaded, projects)
            if offline:
                row["appServer"] = "off"
            rows.append(row)
        except Exception:
            skipped += 1
    errors = [f"Codex({account}) 작업 {skipped}건을 읽지 못했습니다."] if skipped else []
    if isinstance(listed, dict) and listed.get("_sourceCoverage") == "interactive":
        errors.append(f"Codex({account}) 버전이 시작 경로 필터를 지원하지 않아 대화형 기록만 표시합니다.")
    return rows, errors


def codex_threads(errors, projects, full=False):
    out = []
    homes, offline = codex_homes(), codex_offline_homes()
    labels = codex_labels(homes)
    # Each home has its own daemon socket, so the lists are read concurrently;
    # rows and errors are still assembled in the original home order.
    paths = [path for _, path in homes]
    if len(paths) > 1:
        with ThreadPoolExecutor(max_workers=min(CODEX_PARALLEL, len(paths))) as pool:
            replies = list(pool.map(lambda path: codex_home_threads(path, full), paths))
    else:
        replies = [codex_home_threads(path, full) for path in paths]
    for (key, _), (project_id, account), reply in zip(homes, labels, replies):
        rows, failures = codex_home_rows(key, project_id, account, reply, projects)
        out.extend(rows)
        errors.extend(failures)
    for (key, path), (project_id, account) in zip(offline, codex_labels(offline)):
        rows, failures = codex_home_rows(key, project_id, account, codex_db_threads(path), projects, offline=True)
        out.extend(rows)
        errors.extend(failures)
    return out


def codex_row(thread, key, project_id, account, loaded, projects):
    if not isinstance(thread, dict):
        raise TypeError("Codex 작업 행 형식이 올바르지 않습니다.")
    status = thread.get("status") or {}
    cwd = str(thread.get("cwd") or "")
    name = thread.get("name")
    preview = one_line(thread.get("preview"), 140)
    return {
        "agent": "codex", "id": str(thread.get("id") or ""), "home": key,
        "title": name or preview[:60] or "(제목 없음)", "named": bool(name), "cwd": cwd,
        "project": project_label(project_id, projects) if project_id else project_for(cwd, projects),
        "account": account, "state": status.get("type"),
        "status": ",".join(status.get("activeFlags") or []), "phase": codex_phase(status),
        "loaded": thread.get("id") in loaded,
        "startedAt": to_epoch(thread.get("createdAt")), "updatedAt": to_epoch(thread.get("updatedAt")),
        "snippet": preview if name else "", "resume": "codex resume " + str(thread.get("id") or ""),
        **session_origin(thread),
    }


def codex_turn_rows(turn):
    """[(item, rows)] for one Codex turn, in order.

    Codex records no time per item: a Turn has `startedAt`/`completedAt` (epoch seconds) and
    a ThreadItem none. So the turn's first user message shows when the turn started, its
    final answer (`phase: "final_answer"`, or for a finished turn whose answers carry no
    phase its last answer) shows when it completed, and every other row carries no time
    instead of the start time of a turn that may have run for many minutes."""
    started, completed = to_epoch(turn.get("startedAt")), to_epoch(turn.get("completedAt"))
    items = [item for item in turn.get("items") or [] if isinstance(item, dict)]
    answers = [index for index, item in enumerate(items)
               if item.get("type") == "agentMessage" and str(item.get("text") or "").strip()]
    final = {index for index in answers if items[index].get("phase") == "final_answer"}
    if not final and answers and (completed is not None or turn.get("status") in ("completed", "interrupted", "failed")):
        final = {answers[-1]}
    first_user = next((index for index, item in enumerate(items) if item.get("type") == "userMessage"), None)
    out = []
    for index, item in enumerate(items):
        kind = item.get("type")
        rows = []
        if kind == "userMessage":
            ts = started if index == first_user else None
            content = item.get("content") if isinstance(item.get("content"), list) else []
            text = "\n".join(str(c.get("text") or "") for c in content
                             if isinstance(c, dict) and c.get("type") == "text").strip()
            attached = native_attachments(content)
            if text or attached:
                fallback = "[이미지 첨부]" if all(a["kind"] == "image" for a in attached) else "[파일 첨부]"
                row = text_row("user", text, ts) if text else {"role": "user", "text": fallback, "ts": ts}
                if attached:
                    row["attachments"] = attached
                rows.append(row)
        elif kind == "agentMessage" and str(item.get("text") or "").strip():
            rows.append(text_row("assistant", item["text"], completed if index in final else None))
        elif kind in CODEX_TOOL_KINDS:
            if kind == "commandExecution":
                text = "$ " + one_line(item.get("command"), 160)
            elif kind == "fileChange":
                changes = item.get("changes") if isinstance(item.get("changes"), list) else []
                paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
                text = "파일 수정 · " + one_line(", ".join(paths), 160)
            elif kind == "webSearch":
                text = "웹 검색 · " + one_line(item.get("query"), 140)
            else:
                label = " ".join(str(v) for v in (item.get("server") or item.get("namespace"), item.get("tool")) if v)
                text = label or "도구 호출"
            rows.append({"role": "tool", "text": text, "ts": None, "toolKind": CODEX_TOOL_KINDS[kind]})
        out.append((item, rows))
    return out


def paging_unsupported(error, method):
    """True when this thread's history cannot be paged: a daemon that predates `method` names
    it an unknown variant, and a thread stored in the legacy format answers method-not-found
    (-32601, "... is not supported yet"). A rejected parameter value is an unknown variant
    too, and must stay an error."""
    if getattr(error, "code", None) == -32601:
        return True
    variant = getattr(error, "unknown_variant", None)
    if variant is not None:
        return variant == method
    return isinstance(error, RpcReplyError) and error.code == -32600 and ("unknown variant `%s`" % method) in str(error)


def codex_recent_thread(server, thread_id, rows=MESSAGE_LIMIT):
    """`thread/read` result whose thread carries only its latest turns, read page by page.

    `includeTurns` loads the whole rollout, which grows to hundreds of MB after hours of
    image work, so one conversation read could outlast its deadline. The metadata read is
    cheap, and items come newest-first until `rows` rows are covered and the turn of the oldest
    of them reaches its user message. Turns come with their status only; their items are those
    read, so the oldest turn may be cut short.
    None when the daemon predates the paged methods or the thread is stored in the legacy
    format, so the caller reads the thread whole."""
    result = server.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return result
    entries, cursor, method = [], None, "thread/items/list"
    try:
        for _ in range(HISTORY_PAGES):
            params = {"threadId": thread_id, "limit": HISTORY_PAGE, "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = server.call(method, params)
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list) or any(not isinstance(entry, dict) or not isinstance(entry.get("item"), dict)
                                                 or not isinstance(entry.get("turnId"), str) for entry in data):
                raise ValueError("Codex 대화 기록 형식이 올바르지 않습니다.")
            entries.extend(data)
            cursor = page.get("nextCursor")
            if not cursor:
                break
            shown = [entry for entry in entries
                     if entry["item"].get("type") in ("userMessage", "agentMessage") or entry["item"].get("type") in CODEX_TOOL_KINDS]
            # Enough once the turn of the oldest row shown reaches back to its user message.
            edge = shown[rows - 1]["turnId"] if len(shown) >= rows else None
            if edge and any(entry["turnId"] == edge and entry["item"].get("type") == "userMessage" for entry in entries):
                break
        wanted = set(entry["turnId"] for entry in entries)
        turns, cursor, method = [], None, "thread/turns/list"
        for _ in range(HISTORY_PAGES):
            params = {"threadId": thread_id, "limit": len(wanted) + 2, "sortDirection": "desc", "itemsView": "notLoaded"}
            if cursor:
                params["cursor"] = cursor
            page = server.call(method, params)
            data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(data, list) or any(not isinstance(turn, dict) or not isinstance(turn.get("id"), str) for turn in data):
                raise ValueError("Codex turn 기록 형식이 올바르지 않습니다.")
            turns.extend(data)
            cursor = page.get("nextCursor")
            if not cursor or wanted <= set(turn["id"] for turn in turns):
                break
    except RpcError as error:
        if paging_unsupported(error, method):
            return None
        raise
    items = {}
    for entry in reversed(entries):
        items.setdefault(entry["turnId"], []).append(entry["item"])
    # Newest first: every turn down to the oldest one with items read (a just-started turn
    # may have none yet). A turn past the pages read is left out with its items.
    found = [turn["id"] for turn in turns]
    last = max((found.index(turn_id) for turn_id in wanted if turn_id in found), default=-1)
    thread["turns"] = [{**turn, "items": items.get(turn["id"], []), "itemsView": "full"}
                       for turn in reversed(turns[:last + 1] if wanted else turns[:1])]
    return {**result, "thread": thread}


def codex_messages(turns):
    out = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        for _item, rows in codex_turn_rows(turn):
            for row in rows:
                if evidence_id(turn.get("id")):
                    row["turnId"] = turn["id"]
                if isinstance(turn.get("status"), str):
                    row["turnStatus"] = turn["status"]
                if evidence_id(_item.get("id")):
                    row["messageId"] = _item["id"]
                if _item.get("phase") in ("commentary", "final_answer"):
                    row["phase"] = _item["phase"]
            out.extend(rows)
    return out[-MESSAGE_LIMIT:]


# ---------- tmux ----------

def tmux_sessions():
    """Sessions of the default tmux server; `tmux.titleOption` and `tmux.taskOption` name
    user options (for example `@title`) that a launcher sets as the title and the task."""
    binary = which("tmux")
    if not binary:
        return []
    try:
        options = settings.load(HOME)["tmux"]
    except ValueError:
        options = {}
    title, task = options.get("titleOption"), options.get("taskOption")
    fields = (["#{session_name}", "#{session_attached}", "#{session_activity}", "#{pane_current_command}"]
              + ["#{%s}" % name for name in (title, task) if name] + ["#{pane_title}"])
    raw = run([binary, "list-panes", "-a", "-F", "\t".join(fields)], timeout=8)
    seen, out = set(), []
    for line in (raw or "").splitlines():
        row = line.split("\t")
        if len(row) < len(fields) or row[0] in seen:
            continue
        seen.add(row[0])
        title_value = row[4] if title else ""
        task_value = row[4 + bool(title)] if task else ""
        out.append({"name": row[0], "attached": int(row[1] or 0) if row[1].isdigit() else 0,
                    "activity": int(row[2]) if row[2].isdigit() else None,
                    "command": row[3], "title": title_value or row[len(fields) - 1], "task": task_value})
    return out


# ---------- entry points ----------

def section(errors, label, collect, *args):
    """Isolate one source; a failure is reported in `errors` and the others still return."""
    try:
        rows = collect(*args)
    except Exception:
        errors.append(f"{label}을 읽는 중 오류가 발생했습니다.")
        return []
    return rows if isinstance(rows, list) else []


def snapshot(full=False):
    """`full` asks Codex to scan and repair its rollout metadata instead of its state DB only."""
    errors = []
    projects = load_projects()
    return {"host": socket.gethostname(), "collectedAt": int(time.time()),
            "claude": section(errors, "Claude 세션 목록", claude_sessions, errors, projects),
            "codex": section(errors, "Codex 작업 목록", codex_threads, errors, projects, full),
            "tmux": section(errors, "tmux 세션 목록", tmux_sessions), "errors": errors}


def read(agent, session_id, home_key=None):
    if not SESSION_ID.match(session_id or ""):
        raise ValueError("세션 ID 형식이 올바르지 않습니다.")
    if agent == "claude":
        path = claude_transcript(session_id)
        if not path:
            return {"messages": [], "error": "대화 기록 파일을 찾지 못했습니다."}
        try:
            return {"messages": claude_messages(tail_entries(path, 2 * 1024 * 1024))}
        except OSError:
            # The transcript can move or lose permissions between lookup and read.
            return {"messages": [], "error": "대화 기록 파일을 읽지 못했습니다."}
    if agent == "codex":
        if not HOME_KEY.match(home_key or ""):
            raise ValueError("Codex home 형식이 올바르지 않습니다.")
        if not (HOME / home_key / SOCKET_REL).exists():
            # Listed from its state DB; the conversation itself needs the account's daemon.
            return {"messages": [], "code": "app_server_off",
                    "error": "이 계정의 Codex 앱 서버가 꺼져 있어 대화를 읽지 못했습니다."}
        try:
            with AppServer(HOME / home_key / SOCKET_REL, timeout=30) as server:
                result = codex_recent_thread(server, session_id)
                if result is None:
                    result = server.call("thread/read", {"threadId": session_id, "includeTurns": True})
        except (OSError, RpcError, ValueError) as exc:
            return {"messages": [], "error": "Codex 대화를 읽지 못했습니다: " + str(exc)}
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != session_id:
            return {"messages": [], "error": "요청한 Codex 대화의 식별자를 확인하지 못했습니다."}
        turns = thread.get("turns") if isinstance(thread, dict) else None
        status = thread.get("status") or {}
        status = status if isinstance(status, dict) else {"type": status}
        flags = status.get("activeFlags") or []
        phase = {"active": "needs_input" if flags else "working", "idle": "idle", "notLoaded": "unavailable"}.get(status.get("type"), "unavailable")
        return {"messages": codex_messages(turns if isinstance(turns, list) else []),
                "phase": phase, "activeFlags": flags, "updatedAt": thread.get("updatedAt"),
                "turns": [{"id": turn.get("id"), "status": turn.get("status")} for turn in (turns or []) if isinstance(turn, dict)]}
    raise ValueError("지원하지 않는 에이전트입니다.")


# ---------- resident watch: one process per board and host ----------

WATCH_PROTO = 1
# Thread-list changes a daemon broadcasts to every initialized connection; no subscription
# (which would load the thread and change its lifetime) is ever made.
WATCH_EVENTS = frozenset(("thread/started", "thread/status/changed", "thread/name/updated",
                          "thread/archived", "thread/unarchived", "thread/closed", "thread/deleted"))
# Broadcasts the watcher never uses.
WATCH_OPT_OUT = ["account/updated", "account/rateLimits/updated", "remoteControl/status/changed",
                 "skills/changed", "app/list/updated", "mcpServer/startupStatus/updated", "fs/changed",
                 "thread/goal/updated"]
WATCH_DEBOUNCE = 0.25        # notifications are gathered this long before one quick list
WATCH_CODEX_POLL = 5.0       # quick (state DB) list without a notification
WATCH_FULL = 120.0           # Codex scan-and-repair list
WATCH_QUICK_RETRY = 600.0    # a daemon that refused a state DB list is asked again after this long
WATCH_CLAUDE_STAT = 1.0      # Claude registry and transcript stat fingerprint
WATCH_CLAUDE_GAP = 2.0       # at most one `claude agents` per this many seconds
WATCH_CLAUDE_REFRESH = 60.0  # `claude agents` even without a visible change
WATCH_TMUX = 10.0
WATCH_HOMES = 60.0           # Codex homes are listed again this often
WATCH_RECONNECT = (1.0, 2.0, 5.0, 10.0)
WATCH_READY = 20.0           # the first snapshot waits at most this long for every source
WATCH_GAP = 1.0              # between two snapshots
WATCH_MINOR_GAP = 5.0        # when only times or previews changed
WATCH_HEARTBEAT = 10.0
WATCH_PING_TIMEOUT = 30.0    # no stdin line for this long: the board is gone
WATCH_LOCK_WAIT = 35.0       # longer than an orphan's ping timeout
WATCH_LOCK_ROOT = Path("/tmp")
# /tmp/<name>-watch-<uid>: the same short per-user name as the board's SSH socket folder.
WATCH_LOCK_NAME = NAMES["names"]["sshFallback"] + "-watch-%d"
BOARD_ID = re.compile(r"^[0-9a-f]{8,32}$")
# Row fields that change while an agent works; such changes alone wait for WATCH_MINOR_GAP.
MINOR_KEYS = ("updatedAt", "snippet", "activity")


class WatchAppServer(AppServer):
    """A long-lived daemon connection that hands every notification name to `on_notice`.

    Notifications that arrive while a request waits for its reply are kept, not dropped.
    """
    def __init__(self, path, on_notice, timeout=10.0):
        self.on_notice = on_notice
        super().__init__(path, timeout, capabilities={"experimentalApi": True,
                                                      "optOutNotificationMethods": WATCH_OPT_OUT})

    def _other(self, message):
        method = message.get("method")
        # Server requests (with an id) only reach subscribed connections and are never answered.
        if isinstance(method, str) and "id" not in message:
            self.on_notice(method)

    def pump(self, timeout):
        """Handle at most one incoming message; False when nothing arrived within `timeout`."""
        if not self.buf:
            readable, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
            if not readable:
                return False
        try:
            message = json.loads(self._message())
        except ValueError:
            return True
        if isinstance(message, dict):
            self._other(message)
        return True


def stat_key(path):
    try:
        info = os.stat(path)
    except OSError:
        return (str(path), None, None)
    return (str(path), info.st_mtime_ns, info.st_size)


def claude_registry_fingerprint():
    """Stat-only signal of Claude's session registry, background jobs and supervisor.

    These are private formats, so they only trigger `claude agents`; nothing is parsed.
    """
    root = HOME / ".claude"
    names = (sorted(glob.glob(str(root / "sessions" / "*.json")))
             + sorted(glob.glob(str(root / "jobs" / "*" / "state.json"))) + [str(root / "daemon" / "roster.json")])
    return tuple(stat_key(name) for name in names)


def claude_transcript_fingerprint(rows, index):
    """Stat-only signal of the listed sessions' transcripts and of new transcript files.

    A project folder's mtime changes when a transcript is created in it, not on append,
    so the listed transcripts are stat'ed one by one.
    """
    folders = tuple(stat_key(name) for name in sorted(glob.glob(str(HOME / ".claude/projects" / "*"))))
    ids = sorted({str(row.get("sessionId") or "") for row in rows if isinstance(row, dict)}) \
        if isinstance(rows, list) else []
    return folders, tuple(stat_key(index[i]) if i in index else (i, None, None) for i in ids)


def watch_signatures(data):
    """(everything but collectedAt, the same without fields that move while agents work)."""
    body = {key: value for key, value in data.items() if key != "collectedAt"}

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).digest()

    def major(rows):
        return [{k: v for k, v in row.items() if k not in MINOR_KEYS} if isinstance(row, dict) else row
                for row in rows]
    return digest(body), digest({**body, **{key: major(body.get(key) or []) for key in ("claude", "codex", "tmux")}})


def watch_log(where, exc):
    """One stderr line (the error's type and its first line), never its traceback or data."""
    try:
        text = str(exc).splitlines()[0][:200] if str(exc) else ""
        sys.stderr.write("watch %s: %s%s\n" % (where, type(exc).__name__, ": " + text if text else ""))
        sys.stderr.flush()
    except Exception:
        pass


class CodexHome:
    """One Codex home: a kept daemon connection, notifications mark it dirty, quick lists follow."""

    def __init__(self, watch, key, path, label):
        self.watch, self.key, self.path, self.label = watch, key, path, label
        self.stopping = threading.Event()
        self.full_requested = False
        self.dirty_at = None
        self.notices = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name=NAMES["brand"]["threadPrefix"] + "-watch-codex")

    def notice(self, method):
        # Called only from this home's own thread, the only reader of its socket.
        if method in WATCH_EVENTS:
            self.notices += 1
            if self.dirty_at is None:
                self.dirty_at = self.watch.clock()

    def publish(self, reply):
        project_id, account = self.label
        rows, errors = codex_home_rows(self.key, project_id, account, reply, self.watch.projects)
        self.watch.publish(("codex", self.key), rows, errors)

    def run(self):
        failures = 0
        while not self.stopping.is_set():
            listed = [False]
            client = None
            try:
                # Always through the home's symlink: a restarted daemon has a new socket target.
                client = WatchAppServer(self.path / SOCKET_REL, self.notice)
                self.serve(client, listed)
                return
            except (OSError, RpcError):
                failure = "connect"
            except Exception:
                failure = "read"
            finally:
                if client is not None:
                    client.sock.close()
            if listed[0] and failure == "connect":
                # A working connection dropped (daemon restart or update): reconnect once
                # quietly, keeping the last rows until a reconnect fails.
                failures = 0
                self.stopping.wait(WATCH_RECONNECT[0])
                continue
            self.publish((None, None, failure))
            failures += 1
            self.stopping.wait(WATCH_RECONNECT[min(failures, len(WATCH_RECONNECT)) - 1])

    def serve(self, client, listed):
        clock = self.watch.clock
        # When a refused state DB list may be asked again (None: it was not refused).
        quick_retry = None
        self.dirty_at = None
        now = clock()
        next_poll, next_full = now, now + WATCH_FULL
        while not self.stopping.is_set():
            now = clock()
            full = self.full_requested or now >= next_full
            dirty = self.dirty_at is not None and now >= self.dirty_at + WATCH_DEBOUNCE
            if full or dirty or now >= next_poll:
                if full:
                    self.full_requested = False
                # Notifications that arrive during these calls mark the home dirty again.
                self.dirty_at = None
                quick = not full and (quick_retry is None or now >= quick_retry)
                result, used = list_threads(client, quick=quick)
                if quick:
                    # A refusal (an older daemon, or a state DB it cannot use now) is asked
                    # again only after a while; meanwhile each list scans as before.
                    quick_retry = None if used else now + WATCH_QUICK_RETRY
                self.publish((result, loaded_threads(client), None))
                listed[0] = True
                now = clock()
                next_poll = now + WATCH_CODEX_POLL
                if full:
                    next_full = now + WATCH_FULL
                continue
            due = min([next_poll, next_full] + ([self.dirty_at + WATCH_DEBOUNCE] if self.dirty_at is not None else []))
            client.pump(min(1.0, max(0.0, due - now)))


class Watch:
    """`collector.py watch`: keep this host's snapshot current; print it only when it changes.

    Output is one JSON object per line: hello, snapshot (the same data as `snapshot`),
    heartbeat, busy and bye. Input lines: `ping` (any line counts) and `refresh`.
    """

    def __init__(self, stdin, stdout, board_id=None, clock=time.monotonic):
        self.stdin, self.stdout, self.board_id, self.clock = stdin, stdout, board_id, clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.local_wake = threading.Event()
        self.stopping = threading.Event()
        self.last_input = clock()
        self.input_closed = False
        self.local_refresh = False
        self.parts = {}
        self.homes = {}
        self.order = []
        self.projects = []
        self.seq = 0
        self.last_output = clock()
        self.hostname = socket.gethostname()
        self.local = threading.Thread(target=self.watch_local, daemon=True, name=NAMES["brand"]["threadPrefix"] + "-watch-local")

    # ---------- input and output ----------
    def emit(self, message):
        self.stdout.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.stdout.flush()
        self.last_output = self.clock()

    def read_input(self):
        try:
            while True:
                line = self.stdin.readline(4096)
                if not line:
                    break
                with self.lock:
                    self.last_input = self.clock()
                if line.strip() == b"refresh":
                    self.request_refresh()
        except (OSError, ValueError):
            pass
        with self.lock:
            self.input_closed = True
        self.wake.set()

    def stop_reason(self):
        with self.lock:
            if self.input_closed:
                return "stdin-closed"
            if self.clock() - self.last_input >= WATCH_PING_TIMEOUT:
                return "ping-timeout"
        return None

    def request_refresh(self):
        """A manual refresh: every source reads now, Codex with its scan-and-repair listing."""
        with self.lock:
            homes = list(self.homes.values())
            self.local_refresh = True
        for home in homes:
            home.full_requested = True
        self.local_wake.set()

    # ---------- sources ----------
    def publish(self, key, rows, errors):
        with self.lock:
            if self.parts.get(key) == (rows, errors):
                return
            self.parts[key] = (rows, errors)
        self.wake.set()

    def load_projects(self):
        try:
            self.projects = load_projects()
        except Exception:
            # Labels fall back to folder names until the settings can be read again.
            pass

    def sync_homes(self):
        self.load_projects()
        try:
            homes, offline = codex_homes(), codex_offline_homes()
        except OSError:
            return
        labels = codex_labels(homes)
        keys = [key for key, _ in homes]
        offline_keys = [key for key, _ in offline]
        started, stopped = [], []
        with self.lock:
            for (key, path), label in zip(homes, labels):
                if key in self.homes:
                    self.homes[key].label = label
                else:
                    self.homes[key] = CodexHome(self, key, path, label)
                    started.append(self.homes[key])
            for key in [key for key in self.homes if key not in keys]:
                stopped.append(self.homes.pop(key))
                if key not in offline_keys:
                    self.parts.pop(("codex", key), None)
            listed = set(keys) | set(offline_keys)
            for part in [part for part in self.parts if isinstance(part, tuple) and part[1] not in listed]:
                self.parts.pop(part)
            self.order = keys + offline_keys
        for home in stopped:
            home.stopping.set()
        for home in started:
            home.thread.start()
        # A home without a daemon is listed from its state DB on each sync; a daemon that
        # appears later takes the home over on the next sync.
        for (key, path), (project_id, account) in zip(offline, codex_labels(offline)):
            rows, errors = codex_home_rows(key, project_id, account, codex_db_threads(path), self.projects, offline=True)
            self.publish(("codex", key), rows, errors)

    def watch_local(self):
        """Claude Code from stat fingerprints plus `claude agents`, and tmux every few seconds."""
        agents, agent_errors, registry, transcripts, index = None, [], None, None, {}
        last_agents, next_tmux = None, 0.0
        while not self.stopping.is_set():
            with self.lock:
                refresh, self.local_refresh = self.local_refresh, False
            now = self.clock()
            try:
                try:
                    seen = claude_registry_fingerprint()
                except OSError:
                    seen = None
                call = (refresh or last_agents is None or now - last_agents >= WATCH_CLAUDE_REFRESH
                        or (seen != registry and now - last_agents >= WATCH_CLAUDE_GAP))
                if call:
                    agent_errors = []
                    try:
                        agents = claude_agent_rows(agent_errors)
                    except Exception:
                        agents, agent_errors = [], ["Claude 세션 목록을 읽는 중 오류가 발생했습니다."]
                    registry, last_agents = seen, self.clock()
                try:
                    watched = claude_transcript_fingerprint(agents, index) if agents else None
                except OSError:
                    watched = None
                if call or watched != transcripts:
                    errors = list(agent_errors)
                    rows = []
                    if agents:
                        try:
                            index = claude_transcript_index()
                        except OSError:
                            index = {}
                            errors.append("Claude 대화 기록 폴더를 읽지 못했습니다.")
                        try:
                            rows = claude_session_rows(agents, errors, self.projects, index)
                        except Exception:
                            rows, errors = [], errors + ["Claude 세션 목록을 읽는 중 오류가 발생했습니다."]
                        try:
                            watched = claude_transcript_fingerprint(agents, index)
                        except OSError:
                            watched = None
                    transcripts = watched
                    self.publish("claude", rows, errors)
                if refresh or now >= next_tmux:
                    errors = []
                    self.publish("tmux", section(errors, "tmux 세션 목록", tmux_sessions), errors)
                    next_tmux = now + WATCH_TMUX
            except Exception as exc:
                # One unexpected failure ends neither this thread nor the watcher: the
                # sources are read again on the next round.
                watch_log("local", exc)
            self.local_wake.wait(WATCH_CLAUDE_STAT)
            self.local_wake.clear()

    def ready(self):
        with self.lock:
            return all(key in self.parts for key in ["claude", "tmux"] + [("codex", k) for k in self.order])

    def compose(self):
        """The snapshot from every source read so far. A source still unread (the first
        snapshot waits at most WATCH_READY) is named in errors, never shown as empty."""
        with self.lock:
            claude = self.parts.get("claude", ([], ["Claude 세션 목록을 아직 읽지 못했습니다."]))
            tmux = self.parts.get("tmux", ([], ["tmux 세션 목록을 아직 읽지 못했습니다."]))
            codex = [self.parts.get(("codex", key)) or
                     ([], ["Codex daemon(%s) 목록을 아직 읽지 못했습니다." % self.homes[key].label[1]
                           if key in self.homes else "Codex 작업 목록을 아직 읽지 못했습니다."])
                     for key in self.order]
        codex_rows, codex_errors = [], []
        for part in codex:
            codex_rows.extend(part[0])
            codex_errors.extend(part[1])
        return {"host": self.hostname, "collectedAt": int(time.time()), "claude": claude[0],
                "codex": codex_rows, "tmux": tmux[0], "errors": claude[1] + codex_errors + tmux[1]}

    # ---------- lifetime ----------
    def acquire_lock(self):
        """(descriptor, None) with this board's watch lock held, or (None, reason)."""
        import fcntl
        folder = WATCH_LOCK_ROOT / (WATCH_LOCK_NAME % os.getuid())
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            info = os.lstat(folder)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None, "lock-unavailable"
            descriptor = os.open(str(folder / (self.board_id + ".lock")),
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            return None, "lock-unavailable"
        deadline = self.clock() + WATCH_LOCK_WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor, None
            except OSError:
                pass
            # The same board's previous watcher leaves within its ping timeout.
            reason = self.stop_reason()
            now = self.clock()
            if reason or now >= deadline:
                os.close(descriptor)
                return None, reason or "busy"
            if now - self.last_output >= WATCH_HEARTBEAT:
                self.emit({"type": "heartbeat", "seq": self.seq, "t": int(time.time())})
            self.wake.wait(min(0.5, deadline - now))
            self.wake.clear()

    def run(self):
        threading.Thread(target=self.read_input, daemon=True, name=NAMES["brand"]["threadPrefix"] + "-watch-input").start()
        descriptor = None
        try:
            self.emit({"type": "hello", "proto": WATCH_PROTO, "pid": os.getpid(),
                       "python": ".".join(str(part) for part in sys.version_info[:3])})
            if self.board_id:
                descriptor, reason = self.acquire_lock()
                if descriptor is None:
                    self.emit({"type": "busy"} if reason == "busy" else {"type": "bye", "reason": reason})
                    return 0
            self.sync_homes()
            self.local.start()
            self.emit({"type": "bye", "reason": self.loop()})
        except OSError:
            # The board closed its end (BrokenPipe): nobody reads this watcher any more.
            pass
        except Exception as exc:
            # Anything unexpected ends this watcher cleanly; the board starts another or
            # collects one-shot meanwhile.
            watch_log("watch", exc)
            try:
                self.emit({"type": "bye", "reason": "error"})
            except (OSError, ValueError):
                pass
            return 1
        finally:
            self.stop()
            if descriptor is not None:
                os.close(descriptor)
        return 0

    def stop(self, timeout=2.0):
        self.stopping.set()
        self.local_wake.set()
        with self.lock:
            homes = list(self.homes.values())
        for home in homes:
            home.stopping.set()
        for thread in [self.local] + [home.thread for home in homes]:
            if thread.is_alive():
                thread.join(timeout)

    def loop(self):
        started = self.clock()
        next_homes = started + WATCH_HOMES
        sent = (None, None)
        last_emit = None
        while True:
            reason = self.stop_reason()
            if reason:
                return reason
            now = self.clock()
            if now >= next_homes:
                self.sync_homes()
                next_homes = now + WATCH_HOMES
            dues = [next_homes, self.last_output + WATCH_HEARTBEAT, self.last_input + WATCH_PING_TIMEOUT]
            if self.seq or self.ready() or now - started >= WATCH_READY:
                data = self.compose()
                signatures = watch_signatures(data)
                if signatures[0] != sent[0]:
                    gap = WATCH_GAP if (signatures[1] != sent[1] or not self.seq) else WATCH_MINOR_GAP
                    if last_emit is None or now - last_emit >= gap:
                        self.seq += 1
                        self.emit({"type": "snapshot", "seq": self.seq,
                                   "reason": "initial" if self.seq == 1 else "change", "data": data})
                        sent, last_emit = signatures, now
                    else:
                        dues.append(last_emit + gap)
            else:
                dues.append(started + WATCH_READY)
            if self.clock() - self.last_output >= WATCH_HEARTBEAT:
                self.emit({"type": "heartbeat", "seq": self.seq, "t": int(time.time())})
            self.wake.wait(min(1.0, max(0.01, min(dues) - self.clock())))
            self.wake.clear()


def watch_main(argv):
    board_id = argv[1] if len(argv) == 2 and argv[0] == "--board" else None
    if (argv and board_id is None) or (board_id is not None and not BOARD_ID.fullmatch(board_id)):
        sys.stdout.write(json.dumps({"type": "bye", "reason": "usage"}) + "\n")
        return 2
    try:
        code = Watch(sys.stdin.buffer, sys.stdout.buffer, board_id).run()
    except BaseException as exc:
        # Never unwind into an interpreter shutdown (see below).
        watch_log("watch", exc)
        code = 1
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    # The stdin reader thread is still blocked in a read that holds the stdin buffer's
    # lock; a normal interpreter shutdown would abort on it. Everything is written.
    os._exit(code)


def main(argv):
    command = argv[0] if argv else "snapshot"
    if command == "watch":
        return watch_main(argv[1:])
    try:
        if command == "snapshot" and argv[1:] in ([], ["--full"]):
            result = snapshot(full=argv[1:] == ["--full"])
        elif command == "read" and len(argv) >= 3:
            result = read(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        else:
            raise ValueError("사용법: collector.py snapshot [--full] | watch [--board <id>] | "
                             "read <claude|codex> <id> [codex-home]")
    except ValueError as exc:
        result = {"error": str(exc)}
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
