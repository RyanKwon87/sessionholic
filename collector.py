#!/usr/bin/env python3
"""Collect this host's Claude Code and Codex sessions as one read-only JSON snapshot.

Runs on the host that owns the sessions, either locally (`python3 collector.py snapshot`)
or over SSH without leaving a file behind (`ssh host python3 -I - snapshot < collector.py`).
Standard library only and Python 3.9 compatible because POSIX host's /usr/bin/python3 is 3.9.
"""
from __future__ import annotations

import base64
from datetime import datetime
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

try:
    import settings
except ModuleNotFoundError:
    # -I excludes the script directory; load only the sibling installed module.
    import importlib.util
    spec = importlib.util.spec_from_file_location("settings", Path(__file__).resolve().with_name("settings.py"))
    settings = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings)

HOME = Path.home()
ROUTER = HOME / ".codex-router"
SOCKET_REL = Path("app-server-control/app-server-control.sock")
# Codex homes are addressed by their path relative to HOME so callers can never name another socket.
HOME_KEY = re.compile(r"^\.codex(-[A-Za-z0-9_-]{1,40})?$|^\.codex-router/[0-9a-f]{16}$")
SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
EXTRA_PATH = ["/opt/homebrew/bin", str(HOME / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
TEXT_LIMIT = 4000
MESSAGE_LIMIT = 80
CODEX_THREAD_LIMIT = 25
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
COMMAND_NAME = re.compile(r"<command-name>([^<]{1,60})</command-name>")


class RpcError(Exception):
    pass


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
    """Labels come only from the explicit Sessionholic JSON settings."""
    return settings.load(HOME, path)["projects"]


def project_for(cwd, projects):
    """Label a working directory with the longest matching registered project root."""
    if not cwd:
        return ""
    cwd = str(cwd)
    best = None
    for project in projects:
        for root in (project["root"],):
            if root.startswith("/") and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
                if best is None or len(root) > best[0]:
                    best = (len(root), project["label"])
    if best:
        return best[1]
    return "~" if cwd == str(HOME) else Path(cwd).name


def project_label(project_id, projects):
    return next((p["label"] for p in projects if p["id"] == project_id), project_id or "")


def home_label(key, path, labels=None):
    """Configured display label only; never read external router scope records."""
    configured = settings.profile("codex", key, HOME)
    return None, configured.get("label") or ("기본" if key == ".codex" else key)


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
    """Hide harness wrappers in user lines; returns (role, text) or None to skip."""
    if "<task-notification>" in text[:300]:
        return "system", "백그라운드 작업 알림"
    match = COMMAND_NAME.search(text)
    if match:
        return "system", "명령 " + match.group(1).strip()
    if text.lstrip().startswith("<local-command"):
        return None
    text = REMINDER.sub("", text).strip()
    return ("user", text) if text else None


def tool_summary(name, args):
    args = args if isinstance(args, dict) else {}
    for key in ("description", "command", "file_path", "pattern", "url", "query", "prompt"):
        if args.get(key):
            return (name or "도구") + " · " + one_line(args[key], 140)
    return name or "도구"


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


def claude_messages(entries):
    out = []
    for entry in entries:
        kind = entry.get("type")
        if kind not in ("user", "assistant") or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        ts = to_epoch(entry.get("timestamp"))
        attached = native_attachments(blocks) if kind == "user" else []
        attachment_row = None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                shown = user_text(block["text"]) if kind == "user" else ("assistant", block["text"])
                if shown:
                    out.append({"role": shown[0], "text": clip(shown[1]), "ts": ts})
                    if shown[0] == "user" and attachment_row is None:
                        attachment_row = out[-1]
            elif block.get("type") == "tool_use":
                out.append({"role": "tool", "text": tool_summary(block.get("name"), block.get("input")), "ts": ts})
        if attached:
            if attachment_row is None:
                attachment_row = {"role": "user", "text": "[이미지 첨부]" if all(a["kind"] == "image" for a in attached) else "[파일 첨부]", "ts": ts}
                out.append(attachment_row)
            attachment_row["attachments"] = attached
    return out[-MESSAGE_LIMIT:]


def claude_sessions(errors, projects):
    binary = which("claude")
    if not binary:
        return []
    raw = run([binary, "agents", "--json", "--all"], timeout=30)
    if raw is None:
        errors.append("Claude 세션 목록을 읽지 못했습니다.")
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        errors.append("Claude 세션 목록 형식이 예상과 다릅니다.")
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        session_id = str(row.get("sessionId") or "")
        path = claude_transcript(session_id)
        snippet = ""
        if path:
            messages = claude_messages(tail_entries(path, 128 * 1024))
            last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
            snippet = one_line(last["text"], 140) if last else ""
        out.append({
            "agent": "claude", "id": str(row.get("id") or ""), "sessionId": session_id,
            "title": row.get("name") or "(이름 없음)", "cwd": row.get("cwd") or "",
            "project": project_for(row.get("cwd"), projects), "account": "",
            "kind": row.get("kind"), "state": row.get("state"), "status": row.get("status"),
            "waitingFor": row.get("waitingFor"), "phase": claude_phase(row.get("state"), row.get("status")),
            "startedAt": to_epoch(row.get("startedAt")),
            "updatedAt": int(path.stat().st_mtime) if path else to_epoch(row.get("startedAt")),
            "snippet": snippet, "resume": "claude attach " + str(row.get("id") or ""),
        })
    return out


# ---------- Codex (app-server daemon over a Unix socket) ----------

class AppServer:
    """Tiny JSON-RPC client for a Codex app-server daemon socket (WebSocket over a Unix socket)."""

    def __init__(self, path, timeout=10.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.buf = b""
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
            self.call("initialize", {"clientInfo": {"name": "sessionholic", "version": "1"}})
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
        self.buf += chunk

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
        payload, self.buf = self.buf[offset:offset + size], self.buf[offset + size:]
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
                    raise RpcError(str((message["error"] or {}).get("message") or "요청 실패"))
                return message.get("result") or {}


def codex_homes():
    homes = []
    candidates = [HOME / ".codex"] + sorted(HOME.glob(".codex-*")) + sorted(ROUTER.glob("*"))
    for path in candidates:
        if path == ROUTER or not path.is_dir() or path.resolve() != path.absolute():
            continue
        key = str(path.relative_to(HOME))
        if HOME_KEY.match(key) and (path / SOCKET_REL).exists():
            homes.append((key, path))
    return homes


def codex_threads(errors, projects):
    out = []
    for key, path in codex_homes():
        project_id, account = home_label(key, path)
        try:
            with AppServer(path / SOCKET_REL) as server:
                listed = server.call("thread/list", {"limit": CODEX_THREAD_LIMIT, "archived": False,
                                                     "sortKey": "updated_at", "sortDirection": "desc"})
                loaded = set(server.call("thread/loaded/list", {}).get("data") or [])
        except (OSError, RpcError):
            errors.append(f"Codex daemon({account})에 연결하지 못했습니다.")
            continue
        for thread in listed.get("data") or []:
            status = thread.get("status") or {}
            cwd = str(thread.get("cwd") or "")
            name = thread.get("name")
            preview = one_line(thread.get("preview"), 140)
            out.append({
                "agent": "codex", "id": str(thread.get("id") or ""), "home": key,
                "title": name or preview[:60] or "(제목 없음)", "named": bool(name), "cwd": cwd,
                "project": project_label(project_id, projects) if project_id else project_for(cwd, projects),
                "account": account, "state": status.get("type"),
                "status": ",".join(status.get("activeFlags") or []), "phase": codex_phase(status),
                "loaded": thread.get("id") in loaded,
                "startedAt": to_epoch(thread.get("createdAt")), "updatedAt": to_epoch(thread.get("updatedAt")),
                "snippet": preview if name else "", "resume": "codex resume " + str(thread.get("id") or ""),
            })
    return out


def codex_messages(turns):
    out = []
    for turn in turns:
        ts = to_epoch(turn.get("startedAt"))
        for item in turn.get("items") or []:
            kind = item.get("type")
            if kind == "userMessage":
                text = "\n".join(str(c.get("text") or "") for c in item.get("content") or []
                                 if isinstance(c, dict) and c.get("type") == "text").strip()
                attached = native_attachments(item.get("content"))
                if text or attached:
                    fallback = "[이미지 첨부]" if all(a["kind"] == "image" for a in attached) else "[파일 첨부]"
                    row = {"role": "user", "text": clip(text) if text else fallback, "ts": ts}
                    if attached:
                        row["attachments"] = attached
                    out.append(row)
            elif kind == "agentMessage" and str(item.get("text") or "").strip():
                out.append({"role": "assistant", "text": clip(item["text"]), "ts": ts})
            elif kind == "commandExecution":
                out.append({"role": "tool", "text": "$ " + one_line(item.get("command"), 160), "ts": ts})
            elif kind == "fileChange":
                paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict) and c.get("path")]
                out.append({"role": "tool", "text": "파일 수정 · " + one_line(", ".join(paths), 160), "ts": ts})
            elif kind in ("mcpToolCall", "dynamicToolCall"):
                label = " ".join(str(v) for v in (item.get("server") or item.get("namespace"), item.get("tool")) if v)
                out.append({"role": "tool", "text": label or "도구 호출", "ts": ts})
            elif kind == "webSearch":
                out.append({"role": "tool", "text": "웹 검색 · " + one_line(item.get("query"), 140), "ts": ts})
    return out[-MESSAGE_LIMIT:]


# ---------- tmux ----------

def tmux_sessions():
    binary = which("tmux")
    if not binary:
        return []
    fields = ["#{session_name}", "#{session_attached}", "#{session_activity}", "#{pane_current_command}",
              "#{pane_title}"]
    raw = run([binary, "list-panes", "-a", "-F", "\t".join(fields)], timeout=8)
    seen, out = set(), []
    for line in (raw or "").splitlines():
        row = line.split("\t")
        if len(row) < len(fields) or row[0] in seen:
            continue
        seen.add(row[0])
        out.append({"name": row[0], "attached": int(row[1] or 0) if row[1].isdigit() else 0,
                    "activity": int(row[2]) if row[2].isdigit() else None,
                    "command": row[3], "title": row[4], "task": ""})
    return out


# ---------- entry points ----------

def snapshot():
    errors = []
    projects = load_projects()
    return {"host": socket.gethostname(), "collectedAt": int(time.time()),
            "claude": claude_sessions(errors, projects), "codex": codex_threads(errors, projects),
            "tmux": tmux_sessions(), "errors": errors}


def read(agent, session_id, home_key=None):
    if not SESSION_ID.match(session_id or ""):
        raise ValueError("세션 ID 형식이 올바르지 않습니다.")
    if agent == "claude":
        path = claude_transcript(session_id)
        if not path:
            return {"messages": [], "error": "대화 기록 파일을 찾지 못했습니다."}
        return {"messages": claude_messages(tail_entries(path, 2 * 1024 * 1024))}
    if agent == "codex":
        if not HOME_KEY.match(home_key or ""):
            raise ValueError("Codex home 형식이 올바르지 않습니다.")
        try:
            with AppServer(HOME / home_key / SOCKET_REL, timeout=30) as server:
                thread = server.call("thread/read", {"threadId": session_id, "includeTurns": True}).get("thread") or {}
        except (OSError, RpcError) as exc:
            return {"messages": [], "error": "Codex 대화를 읽지 못했습니다: " + str(exc)}
        return {"messages": codex_messages(thread.get("turns") or [])}
    raise ValueError("지원하지 않는 에이전트입니다.")


def main(argv):
    command = argv[0] if argv else "snapshot"
    try:
        if command == "snapshot":
            result = snapshot()
        elif command == "read" and len(argv) >= 3:
            result = read(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        else:
            raise ValueError("사용법: collector.py snapshot | read <claude|codex> <id> [codex-home]")
    except ValueError as exc:
        result = {"error": str(exc)}
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
