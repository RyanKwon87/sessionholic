"""Exact native-session chat IO. No auth/model/permission overrides or retries.

Only the owning host invokes handle(). Attachment paths are server-resolved,
never browser-provided. Native approvals remain in the original terminal.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import attachments
import collector
import launch
import transfer_native as native

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
FALLBACK = "승인·질문 응답과 지원되지 않는 입력은 원본 native 터미널에서 처리하세요."


class NativeRejected(collector.RpcError):
    def __init__(self, error):
        message = str(error.get("message") or "").lower()
        self.code = error.get("code") if isinstance(error.get("code"), int) else None
        self.category = ("attachment" if "attachment" in message else
                         "inputTooLarge" if "input" in message and "large" in message else
                         "nativeRejected")
        super().__init__("Native 요청을 완료하지 못했습니다.")


class ChatAppServer(native._DeadlineAppServer):
    """Enable queue API, retain only safe notification/request metadata."""
    def __init__(self, path, deadline):
        self.native_requests = []
        super().__init__(path, deadline)

    def call(self, method, params):
        if method == "initialize":
            params = {**params, "capabilities": {"experimentalApi": True}}
        self.next_id += 1
        number = self.next_id
        self._send(0x1, json.dumps({"id": number, "method": method, "params": params}).encode())
        while True:
            try:
                message = json.loads(self._message())
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == number and "method" not in message:
                if "error" in message:
                    # Native error messages may echo submitted text or paths.
                    raise NativeRejected(message["error"] if isinstance(message["error"], dict) else {})
                return message.get("result") or {}
            if "method" in message and "id" in message:
                self.native_requests.append(str(message["method"])[:100])
                self.native_requests = self.native_requests[-16:]
                # Never answer permission/elicitation prompts from this observer.


def _client(identity, home, deadline):
    return ChatAppServer(native._socket_path(identity, home), deadline)


def _thread(client, identity):
    result = client.call("thread/read", {"threadId": identity["id"], "includeTurns": True})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != identity["id"]:
        raise ValueError("정확한 원본 Codex 세션을 확인하지 못했습니다.")
    turns, status = thread.get("turns"), thread.get("status")
    if not isinstance(turns, list) or not isinstance(status, dict):
        raise ValueError("원본 Codex 상태 형식이 올바르지 않습니다.")
    if any(not isinstance(t, dict) or t.get("status") not in
           ("inProgress", "completed", "interrupted", "failed") for t in turns):
        raise ValueError("원본 Codex turn 상태를 확인하지 못했습니다.")
    return thread


def _state(thread):
    status = thread.get("status") or {}
    flags = status.get("activeFlags") or []
    if not isinstance(flags, list):
        flags = ["unknown"]
    active = [t for t in thread["turns"] if t.get("status") == "inProgress"]
    kind = status.get("type")
    phase = ("needs_input" if flags else "working") if kind == "active" else (
        "idle" if kind == "idle" and not active else "closed" if kind == "notLoaded" and not active else "unknown")
    return {"phase": phase, "activeTurnId": active[0].get("id") if len(active) == 1 else None,
            "activeFlags": flags, "turns": [{"id": t.get("id"), "status": t.get("status")}
                                              for t in thread["turns"][-80:]],
            "updatedAt": thread.get("updatedAt"), "requiresNativeTerminal": bool(flags)}


def _remote(value):
    if isinstance(value, dict):
        return any(k == "vendorRemote" or _remote(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_remote(v) for v in value)
    return value == "vendorRemote"


def _capabilities(thread, queue_available):
    state = _state(thread)
    can = (thread.get("canAcceptDirectInput") is True and not _remote(thread.get("source"))
           and not _remote(thread.get("threadSource")) and state["phase"] in ("idle", "working", "needs_input"))
    can = can and queue_available
    if can:
        reason = None
    elif _remote(thread.get("source")) or _remote(thread.get("threadSource")):
        reason = "이 원격 세션은 로컬 직접 입력을 지원하지 않습니다. 원본 터미널을 사용하세요."
    elif state["phase"] == "closed":
        reason = "닫힌 세션입니다. 실제 터미널을 먼저 열면 같은 세션에 메시지를 보낼 수 있습니다."
    elif thread.get("canAcceptDirectInput") is not True:
        reason = "이 세션은 직접 입력을 지원하지 않습니다. 원본 터미널을 사용하세요."
    elif not queue_available:
        reason = "이 기기의 Native 대기열 기능을 확인하지 못했습니다. 원본 터미널을 사용하세요."
    else:
        reason = "현재 세션 상태를 확인하지 못했습니다. 상태를 다시 확인하세요."
    return {"canRead": True, "canSend": can, "canAttachImages": can, "canQueue": can,
            "canSteer": can and state["phase"] == "working" and bool(state["activeTurnId"]),
            "canAnswerApprovals": False, "canAnswerQuestions": False,
            "reason": reason, "fallback": FALLBACK}


def _queue(client, identity):
    out, cursor = [], None
    for _ in range(4):
        params = {"threadId": identity["id"], "limit": 200}
        if cursor:
            params["cursor"] = cursor
        result = client.call("thread/queue/list", params)
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or any(not isinstance(q, dict) for q in data):
            raise ValueError("원본 대기열을 확인하지 못했습니다.")
        out.extend(data)
        cursor = result.get("nextCursor")
        if not cursor:
            return out
    raise ValueError("원본 대기열이 너무 커 전송 여부를 확인하지 못했습니다.")


def _receipt(identity, request_id, thread, queue):
    for turn in reversed(thread["turns"]):
        for item in turn.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "userMessage" and item.get("clientId") == request_id:
                delivery = turn.get("status")
                delivery = "accepted" if delivery == "inProgress" else delivery
                return {"source": identity, "requestId": request_id, "delivery": delivery,
                        "confirmed": True, "readbackConfirmed": True, "turnId": turn.get("id"),
                        "queueId": None, "canRetry": False}
    for item in queue:
        if item.get("clientUserMessageId") == request_id:
            return {"source": identity, "requestId": request_id, "delivery": "queued",
                    "confirmed": True, "readbackConfirmed": True, "turnId": None,
                    "queueId": item.get("id"), "canRetry": False}
    return {"source": identity, "requestId": request_id, "delivery": "unknown", "confirmed": False,
            "readbackConfirmed": False, "turnId": None, "queueId": None, "canRetry": False,
            "reason": "Native 수신 여부를 확인하지 못했습니다. 같은 입력을 자동으로 다시 보내지 않습니다."}


def _messages(thread):
    out = []
    for turn in thread["turns"]:
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            rows = collector.codex_messages([{**turn, "items": [item]}])
            if not rows and item.get("type") == "userMessage":
                images = [c for c in item.get("content") or [] if isinstance(c, dict)
                          and c.get("type") in ("image", "localImage")]
                if images:
                    rows = [{"role": "user", "text": "[이미지 첨부]", "ts": collector.to_epoch(turn.get("startedAt"))}]
            for row in rows:
                out.append({**row, "turnId": turn.get("id"), "turnStatus": turn.get("status"),
                            "itemId": item.get("id"), "clientId": item.get("clientId")})
    return out[-collector.MESSAGE_LIMIT:]


def _inputs(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 17:
        raise ValueError("메시지 입력이 올바르지 않습니다.")
    out, total = [], 0
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("메시지 입력이 올바르지 않습니다.")
        if item.get("type") == "text" and set(item) <= {"type", "text"}:
            text = item.get("text")
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise ValueError("메시지가 비어 있거나 올바르지 않습니다.")
            total += len(text.encode())
            out.append({"type": "text", "text": text})
        elif item.get("type") == "localImage" and set(item) <= {"type", "path"}:
            path = item.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError("서버 첨부 경로가 올바르지 않습니다.")
            file = Path(path)
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 32 * 1024 * 1024:
                raise ValueError("안전한 서버 이미지 첨부를 확인하지 못했습니다.")
            out.append({"type": "localImage", "path": str(file)})
        else:
            raise ValueError("지원되지 않는 메시지 입력입니다.")
    if total > 256 * 1024:
        raise ValueError("메시지가 너무 큽니다.")
    return out


def _request(value):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise ValueError("메시지 요청 ID가 올바르지 않습니다.")
    return value


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("안전한 메시지 수신 기록 경로를 확인하지 못했습니다.")


class _Ledger:
    """Cross-worker deduplication. Persists only IDs, hashes and receipt metadata."""
    def __init__(self, identity, request_id, home):
        root = launch._home(home) / ".sessionholic"
        _private_dir(root)
        folder = root / "chat-receipts"
        _private_dir(folder)
        key = hashlib.sha256(json.dumps([identity, request_id], sort_keys=True).encode()).hexdigest()
        self.path = folder / (key + ".json")
        fd = os.open(str(folder / (key + ".lock")), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise ValueError("이 메시지의 전송 확인이 이미 진행 중입니다.") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.lock.close()

    def read(self):
        if not self.path.exists():
            return None
        fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as file:
            return json.load(file)

    def save(self, value):
        temporary = self.path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temporary), str(self.path))


def _send(client, identity, args, thread, queue, home):
    request_id = _request(args.get("requestId"))
    snapshot_cwd = (args.get("source") or {}).get("cwd")
    if snapshot_cwd is not None and snapshot_cwd != thread.get("cwd"):
        raise ValueError("실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.")
    raw_inputs = args.get("input")
    if not isinstance(raw_inputs, list):
        raise ValueError("메시지 입력이 올바르지 않습니다.")
    raw_inputs = list(raw_inputs)
    ids = args.get("attachmentIds", [])
    if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("첨부 식별자가 올바르지 않습니다.")
    if ids:
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or cwd != (args.get("source") or {}).get("cwd"):
            raise ValueError("실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.")
        for identifier in ids:
            item = attachments.resolve(cwd, identifier)
            if item["mime"] in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                raw_inputs.append({"type": "localImage", "path": item["path"]})
            else:
                # A path reference, never fake inline extraction of arbitrary files.
                raw_inputs.append({"type": "text", "text": "첨부 파일: " + json.dumps(item["path"], ensure_ascii=False)})
    inputs = _inputs(raw_inputs)
    mode = args.get("mode", "queue")
    if mode not in ("queue", "steer"):
        raise ValueError("메시지 전송 모드가 올바르지 않습니다.")
    digest = hashlib.sha256(json.dumps([inputs, mode, args.get("expectedTurnId")], sort_keys=True).encode()).hexdigest()
    with _Ledger(identity, request_id, home) as ledger:
        prior = ledger.read()
        if prior and prior.get("digest") != digest:
            raise ValueError("이미 사용한 요청 ID에 다른 메시지를 보낼 수 없습니다.")
        before = _receipt(identity, request_id, thread, queue)
        if before["confirmed"]:
            return before
        if prior:
            # An unknown result remains read-only even when native history lags.
            saved = prior.get("receipt") or {}
            return saved if saved.get("confirmed") or saved.get("delivery") == "rejected" else before
        capabilities, state = _capabilities(thread, True), _state(thread)
        if not capabilities["canSend"]:
            return {**before, "delivery": "rejected", "reason": capabilities["reason"], "fallback": FALLBACK}
        if mode == "steer" and (not capabilities["canSteer"] or
                                args.get("expectedTurnId") != state["activeTurnId"]):
            return {**before, "delivery": "rejected", "reason": "정확한 실행 중 turn을 확인해야 방향을 수정할 수 있습니다."}
        ledger.save({"digest": digest, "receipt": before})
        acknowledged, queue_id, turn_id = False, None, None
        try:
            if mode == "steer":
                result = client.call("turn/steer", {"threadId": identity["id"], "input": inputs,
                                     "clientUserMessageId": request_id, "expectedTurnId": state["activeTurnId"]})
                acknowledged, turn_id = True, result.get("turnId") or state["activeTurnId"]
            else:
                result = client.call("thread/queue/add", {"threadId": identity["id"], "input": inputs,
                                                         "clientUserMessageId": request_id})
                item = result.get("queuedSubmission") or {}
                if item.get("clientUserMessageId") != request_id or not isinstance(item.get("id"), str):
                    raise ValueError("메시지 대기열 수신을 확인하지 못했습니다.")
                acknowledged, queue_id = True, item["id"]
                if state["phase"] == "idle":
                    try:
                        started = client.call("thread/queue/start", {"threadId": identity["id"],
                                                                     "queuedSubmissionId": queue_id})
                        turn_id = (started.get("turn") or {}).get("id")
                    except collector.RpcError:
                        # Queue auto-dispatch or concurrent active turn. No steer or retry.
                        pass
            after = _thread(client, identity)
            receipt = _receipt(identity, request_id, after, _queue(client, identity))
            receipt.update(_state(after))
        except (OSError, ValueError, TimeoutError, collector.RpcError) as error:
            receipt = before
            if isinstance(error, NativeRejected):
                receipt.update({"nativeErrorCode": error.code, "nativeErrorCategory": error.category})
                if not acknowledged and error.code in (-32600, -32602) and error.category in ("attachment", "inputTooLarge"):
                    receipt.update({"delivery": "rejected", "reason": "Native에서 첨부 또는 메시지 입력을 거절했습니다."})
        receipt["acknowledged"] = acknowledged
        if acknowledged and not receipt["confirmed"]:
            receipt.update({"delivery": "accepted" if turn_id else "queued", "confirmed": True,
                            "turnId": turn_id, "queueId": queue_id,
                            "reason": "Native 수신 응답을 받았습니다. 대화 기록 반영은 아직 확인되지 않았습니다."})
        receipt["fallback"] = FALLBACK
        ledger.save({"digest": digest, "receipt": receipt})
        return receipt


def _claude(identity, action, home, deadline, args):
    binary = native._claude_binary(home)
    rows = json.loads(native._claude_run(binary, ["agents", "--json", "--all"], deadline))
    matches = [r for r in rows if isinstance(r, dict) and r.get("id") == identity["id"]] if isinstance(rows, list) else []
    source = args.get("source") or {}
    if not matches and source.get("kind") == "interactive" and source.get("sessionId") == identity["id"]:
        # Board-created --session-id UUID differs from agents' terminal row ID.
        # The server supplies this binding; cwd and native sessionId must agree.
        try:
            bound_id = str(uuid.UUID(identity["id"]))
        except (ValueError, AttributeError):
            bound_id = None
        if bound_id == identity["id"] and isinstance(rows, list) and source.get("cwd"):
            matches = [r for r in rows if isinstance(r, dict) and r.get("kind") == "interactive"
                       and r.get("sessionId") == bound_id and r.get("cwd") == source["cwd"]]
    if len(matches) != 1:
        raise ValueError("정확한 원본 Claude 세션을 확인하지 못했습니다.")
    row = matches[0]
    if source.get("cwd") is not None and source["cwd"] != row.get("cwd"):
        raise ValueError("실제 Claude 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.")
    result = {"source": identity, "phase": collector.claude_phase(row.get("state"), row.get("status")),
              "kind": row.get("kind"), "cwd": row.get("cwd"), "requiresNativeTerminal": True,
              "capabilities": {"canRead": True, "canSend": False, "canAttachImages": False,
                               "canQueue": False, "canSteer": False, "canAnswerApprovals": False,
                               "canAnswerQuestions": False, "fallback": FALLBACK,
                               "reason": "현재 Claude CLI에는 검증된 기존 세션 직접 입력 계약이 없습니다."}}
    if action == "read":
        sid = row.get("sessionId")
        if not isinstance(sid, str) or not collector.SESSION_ID.fullmatch(sid):
            raise ValueError("원본 Claude 대화 ID를 확인하지 못했습니다.")
        root = launch._home(home) / ".claude/projects"
        paths = [p for p in root.glob("*/" + sid + ".jsonl") if p.is_file() and not p.is_symlink()]
        if len(paths) != 1:
            raise ValueError("정확한 원본 Claude 대화 기록을 확인하지 못했습니다.")
        result["messages"] = collector.claude_messages(collector.tail_entries(paths[0], 2 * 1024 * 1024))
    if action in ("send", "receipt"):
        result.update({"requestId": _request(args.get("requestId")), "delivery": "rejected",
                       "confirmed": False, "readbackConfirmed": False, "canRetry": False,
                       "reason": result["capabilities"]["reason"], "fallback": FALLBACK})
    return result


def handle(args, *, home=None, timeout=10):
    """One bounded operation on the owning host; never opens/resumes a thread."""
    if not isinstance(args, dict) or args.get("action") not in ("capabilities", "read", "send", "receipt"):
        raise ValueError("Native 메시지 작업이 올바르지 않습니다.")
    identity, action = native._identity(args.get("source")), args["action"]
    deadline = time.monotonic() + min(max(float(timeout), 0.05), 30)
    if identity["agent"] == "claude":
        return _claude(identity, action, home, deadline, args)
    with _client(identity, home, deadline) as client:
        thread = _thread(client, identity)
        queue_available, queue = True, []
        try:
            queue = _queue(client, identity)
        except (ValueError, collector.RpcError):
            queue_available = False
        result = {"source": identity, "cwd": thread.get("cwd"), **_state(thread),
                  "capabilities": _capabilities(thread, queue_available)}
        if action == "read":
            result["messages"] = _messages(thread)
            result["queue"] = [{"id": q.get("id"), "clientId": q.get("clientUserMessageId")} for q in queue]
        elif action == "receipt":
            request_id = _request(args.get("requestId"))
            receipt = _receipt(identity, request_id, thread, queue)
            if not receipt["confirmed"]:
                with _Ledger(identity, request_id, home) as ledger:
                    saved = (ledger.read() or {}).get("receipt") or {}
                    if saved.get("confirmed") or saved.get("delivery") == "rejected":
                        receipt = saved
            result.update(receipt)
        elif action == "send":
            if not queue_available:
                return {**result, "requestId": _request(args.get("requestId")), "delivery": "rejected",
                        "confirmed": False, "readbackConfirmed": False, "canRetry": False,
                        "reason": result["capabilities"]["reason"], "fallback": FALLBACK}
            result.update(_send(client, identity, args, thread, queue, home))
        return result
