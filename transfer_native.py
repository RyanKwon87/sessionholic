"""Local native-session state and exact interruption for workspace transfers.

No credentials, transcripts or environment values are returned. Actual commands
are issued only by interrupt_source(), after reading the exact native identity.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import time
import uuid

import collector
import launch


def profiles(home=None):
    return launch.discover_profiles(home)


def _identity(source):
    if not isinstance(source, dict) or source.get("agent") not in ("codex", "claude"):
        raise ValueError("원본 에이전트 정보가 올바르지 않습니다.")
    agent, sid = source["agent"], source.get("id")
    if agent == "codex":
        try:
            sid = str(uuid.UUID(str(sid)))
        except (ValueError, AttributeError):
            raise ValueError("원본 Codex 세션 UUID가 올바르지 않습니다.") from None
        key = source.get("home")
        if not isinstance(key, str) or not launch.HOME_KEY.fullmatch(key):
            raise ValueError("원본 Codex 계정 경로가 올바르지 않습니다.")
    else:
        if not isinstance(sid, str) or not launch.CLAUDE_ID.fullmatch(sid):
            raise ValueError("원본 Claude 세션 ID가 올바르지 않습니다.")
        key = ".claude"
    return {"agent": agent, "id": sid, "home": key}


def _unknown(identity, reason):
    return {**identity, "phase": "unknown", "activeTurnId": None,
            "canInterrupt": False, "safeToTransfer": False, "confirmed": False,
            "requiresBoardInterrupt": False, "revision": None, "reason": reason}


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("원본 상태 확인 시간이 초과되었습니다.")
    return remaining


class _DeadlineAppServer(collector.AppServer):
    """Bound all socket IO and frame processing by one wall-clock deadline."""
    def __init__(self, path, deadline):
        self.deadline = deadline
        super().__init__(path, timeout=min(3.0, _remaining(deadline)))

    def call(self, method, params):
        if method == "initialize":
            params = {**params, "capabilities": {"experimentalApi": True}}
        return super().call(method, params)

    def _check(self):
        self.sock.settimeout(min(3.0, _remaining(self.deadline)))

    def _fill(self):
        self._check()
        return super()._fill()

    def _frame(self):
        self._check()
        return super()._frame()

    def _send(self, opcode, data):
        self._check()
        return super()._send(opcode, data)


def _socket_path(identity, home):
    root = launch._home(home)
    path = root / identity["home"] / launch.SOCKET_REL
    try:
        resolved = path.resolve(strict=True)
        info, parent = resolved.stat(), resolved.parent.stat()
        safe = (path.parent.resolve() == path.parent.absolute()
                and path.lstat().st_uid == os.getuid()
                and info.st_uid == os.getuid() and parent.st_uid == os.getuid()
                and not (parent.st_mode & 0o022) and stat.S_ISSOCK(info.st_mode))
    except (OSError, RuntimeError):
        safe = False
    if not safe:
        raise ValueError("원본 계정의 안전한 Codex daemon socket을 확인하지 못했습니다.")
    return path


def _client(identity, home, deadline):
    return _DeadlineAppServer(_socket_path(identity, home), deadline)


def _codex_state(client, identity):
    result = client.call("thread/read", {"threadId": identity["id"], "includeTurns": True})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != identity["id"]:
        return _unknown(identity, "원본 Codex 세션을 정확히 확인하지 못했습니다.")
    # A pending native message is not part of the already-rendered transcript.
    # Never interrupt/copy while it could later dispatch on the original host.
    try:
        queued = client.call("thread/queue/list", {"threadId": identity["id"], "limit": 1})
        data = queued.get("data") if isinstance(queued, dict) else None
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("invalid native queue response")
        if data:
            return {**_unknown(identity, "대기 중인 메시지를 원래 터미널에서 처리한 뒤 이전해 주세요."),
                    "queueBlocked": True}
        if queued.get("nextCursor"):
            raise ValueError("unconfirmed empty native queue")
    except (OSError, ValueError, collector.RpcError, TimeoutError):
        return {**_unknown(identity, "원본 메시지 대기열을 확인하지 못했습니다. 파일 이전은 시작하지 않습니다."),
                "queueBlocked": True}
    status = thread.get("status") or {}
    if not isinstance(status, dict):
        return _unknown(identity, "원본 runtime 상태 형식이 올바르지 않습니다.")
    kind, flags = status.get("type"), status.get("activeFlags") or []
    if not isinstance(flags, list):
        return _unknown(identity, "원본 runtime 상태 형식이 올바르지 않습니다.")
    turns = thread.get("turns")
    if not isinstance(turns, list) or any(not isinstance(turn, dict) for turn in turns):
        return _unknown(identity, "원본 turn 기록을 확인하지 못했습니다.")
    active = [turn for turn in turns if turn.get("status") == "inProgress"]
    if any(turn.get("status") not in ("inProgress", "completed", "interrupted", "failed") for turn in turns):
        return _unknown(identity, "알 수 없는 원본 turn 상태입니다.")
    turn_id = active[0].get("id") if len(active) == 1 else None
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128):
        turn_id = None
    stable = kind in ("idle", "notLoaded") and not active and not flags
    phase = ("idle" if kind == "idle" else "closed") if stable else (
        "needs_input" if kind == "active" and flags else "working" if kind == "active" else "unknown")
    return {**identity, "phase": phase, "activeTurnId": turn_id,
            "canInterrupt": kind == "active" and turn_id is not None,
            "safeToTransfer": stable, "confirmed": stable, "requiresBoardInterrupt": False,
            "reason": None if stable or (kind == "active" and turn_id) else "원본이 안전한 대기 상태인지 확인하지 못했습니다.",
            "lastTurnStatus": turns[-1].get("status") if turns else None,
            "lastTurnId": turns[-1].get("id") if turns else None,
            "revision": _revision([thread.get("updatedAt"), status,
                                    [[turn.get("id"), turn.get("status")] for turn in turns]]),
            "turnStatuses": {turn["id"]: turn.get("status") for turn in turns if isinstance(turn.get("id"), str)}}


def _claude_binary(home):
    binary = launch._binary("claude", launch._home(home))
    if not binary:
        raise ValueError("이 호스트에 Claude native CLI가 없습니다.")
    return binary


def _claude_run(binary, args, deadline):
    result = subprocess.run([binary] + list(args), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=collector.tool_env(), timeout=min(3.0, _remaining(deadline)), check=False)
    if result.returncode:
        raise ValueError("원본 Claude 명령을 완료하지 못했습니다.")
    return result.stdout


def _claude_state(binary, identity, deadline, home=None):
    rows = json.loads(_claude_run(binary, ["agents", "--json", "--all"], deadline))
    if not isinstance(rows, list):
        return _unknown(identity, "원본 Claude 목록 형식이 올바르지 않습니다.")
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == identity["id"]]
    if len(matches) != 1:
        return _unknown(identity, "원본 Claude 세션을 정확히 확인하지 못했습니다.")
    row = matches[0]
    state, status = row.get("state"), row.get("status")
    if state not in ("idle", "working", "blocked", "failed", "done", "stopped"):
        return _unknown(identity, "알 수 없는 원본 Claude 상태입니다.")
    phase = collector.claude_phase(state, status)
    stable = phase in ("idle", "done") and state != "working" and status not in ("busy", "waiting")
    background = row.get("kind") == "background"
    stamps = []
    sid = row.get("sessionId")
    if isinstance(sid, str) and collector.SESSION_ID.fullmatch(sid):
        root = launch._home(home) / ".claude/projects"
        for path in root.glob("*/" + sid + ".jsonl"):
            info = path.stat()
            stamps.append([str(path.relative_to(root)), info.st_mtime_ns, info.st_size])
    return {**identity, "phase": phase, "activeTurnId": None, "kind": row.get("kind"),
            "canInterrupt": not stable and background, "safeToTransfer": stable, "confirmed": stable,
            "requiresBoardInterrupt": not stable and not background,
            "revision": _revision([state, status, row.get("kind"), row.get("updatedAt"),
                                    row.get("startedAt"), sorted(stamps)]),
            "reason": None if stable or background else "실행 중인 interactive Claude는 정확한 원본 터미널에서 중단해야 합니다."}


def source_state(source, *, home=None, timeout=3):
    identity = _identity(source)
    deadline = time.monotonic() + min(max(float(timeout), 0.05), 10)
    try:
        if identity["agent"] == "codex":
            with _client(identity, home, deadline) as client:
                return _codex_state(client, identity)
        return _claude_state(_claude_binary(home), identity, deadline, home)
    except (OSError, ValueError, collector.RpcError, TimeoutError, subprocess.TimeoutExpired):
        return _unknown(identity, "원본 native 상태를 확인하지 못했습니다. 파일 이전은 시작하지 않습니다.")


def interrupt_source(source, *, home=None, timeout=10):
    """Stop exactly one source, and confirm quiescence before allowing export."""
    identity = _identity(source)
    deadline = time.monotonic() + min(max(float(timeout), 0.05), 10)
    interrupted = False
    try:
        if identity["agent"] == "codex":
            with _client(identity, home, deadline) as client:
                before = _codex_state(client, identity)
                if before["safeToTransfer"]:
                    return {**before, "interrupted": False}
                if not before["canInterrupt"]:
                    return {**before, "interrupted": False, "confirmed": False}
                turn_id = before["activeTurnId"]
                client.call("turn/interrupt", {"threadId": identity["id"], "turnId": turn_id})
                interrupted = True
                for _ in range(50):
                    _remaining(deadline)
                    after = _codex_state(client, identity)
                    if after.get("queueBlocked"):
                        return {**after, "interrupted": True, "confirmed": False, "safeToTransfer": False}
                    finished = after.get("turnStatuses", {}).get(turn_id) in ("interrupted", "completed", "failed")
                    if after["safeToTransfer"] and finished:
                        return {**after, "interrupted": True, "confirmed": True, "interruptedTurnId": turn_id}
                    if after["activeTurnId"] not in (None, turn_id):
                        return {**after, "interrupted": True, "confirmed": False, "safeToTransfer": False,
                                "reason": "원본에 새 응답이 시작되어 이전하지 않았습니다."}
                    time.sleep(min(0.2, _remaining(deadline)))
        else:
            binary = _claude_binary(home)
            before = _claude_state(binary, identity, deadline, home)
            if before["safeToTransfer"]:
                return {**before, "interrupted": False}
            if not before["canInterrupt"]:
                return {**before, "interrupted": False, "confirmed": False}
            _claude_run(binary, ["stop", identity["id"]], deadline)
            interrupted = True
            for _ in range(50):
                _remaining(deadline)
                after = _claude_state(binary, identity, deadline, home)
                if after["safeToTransfer"]:
                    return {**after, "interrupted": True, "confirmed": True}
                time.sleep(min(0.2, _remaining(deadline)))
    except (OSError, ValueError, collector.RpcError, TimeoutError, subprocess.TimeoutExpired):
        pass
    return {**_unknown(identity, "원본 중단 완료를 확인하지 못했습니다. 파일 이전은 시작하지 않습니다."),
            "interrupted": interrupted}
