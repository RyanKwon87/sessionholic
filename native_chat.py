"""Exact native-session chat IO. No auth/permission overrides or automatic retries.

Only the owning host invokes handle(). Attachment paths are server-resolved,
never browser-provided. Native approvals remain in the original terminal.
"""
from __future__ import annotations

import fcntl
from collections import Counter
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
import instance
import launch
import transfer_native as native

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
KNOWN_TURN_STATUS = ("inProgress", "completed", "interrupted", "failed")
NOT_STEERABLE = "현재 실행 중인 작업(/review·/compact 등)은 새 입력을 받을 수 없습니다. 끝난 뒤 다시 보내 주세요."
FALLBACK = "승인·질문 응답과 지원되지 않는 입력은 원본 native 터미널에서 처리하세요."
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z")
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def model_settings(value):
    value = value if isinstance(value, dict) else {}
    model, effort = value.get('model'), value.get('reasoningEffort')
    return {'model': model if isinstance(model, str) and MODEL_ID.fullmatch(model) else None,
            'effort': effort if effort in EFFORTS else None}


def _preferences(identity, home):
    root = ledger_root(home)
    path = root / 'chat-receipts' / (_ledger_key(identity, 'model-settings') + '.json')
    try:
        for folder in (root, path.parent):
            info = folder.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('모델 설정 저장 경로를 확인해 주세요.')
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('모델 설정 기록을 확인해 주세요.')
        value = json.loads(stream.read(4097))
    if (not isinstance(value, dict) or set(value) != {'model', 'effort'} or
            not isinstance(value['model'], str) or not MODEL_ID.fullmatch(value['model']) or value['effort'] not in EFFORTS):
        raise ValueError('모델 설정 기록을 확인해 주세요.')
    return value


def _models(client):
    models, cursor = [], None
    for _ in range(4):
        result = client.call('model/list', {'limit': 100, **({'cursor': cursor} if cursor else {})})
        if not isinstance(result, dict) or not isinstance(result.get('data'), list) or len(result['data']) > 100:
            raise ValueError('모델 목록 형식을 확인하지 못했습니다.')
        for row in result.get('data', []):
            if not isinstance(row, dict) or row.get('hidden') or not isinstance(row.get('model'), str) or not MODEL_ID.fullmatch(row['model']):
                continue
            supported = row.get('supportedReasoningEfforts')
            if not isinstance(supported, list) or len(supported) > 16: continue
            efforts = [item.get('reasoningEffort') for item in supported
                       if isinstance(item, dict) and item.get('reasoningEffort') in EFFORTS]
            if efforts:
                models.append({'model': row['model'], 'label': str(row.get('displayName') or row['model'])[:120],
                               'efforts': list(dict.fromkeys(efforts)), 'defaultEffort': row.get('defaultReasoningEffort')})
        cursor = result.get('nextCursor')
        if not cursor: return models
    raise ValueError('모델 목록을 모두 확인하지 못했습니다. 다시 시도해 주세요.')


def _codex_command(client, identity, args, thread, home):
    command = args.get('command')
    if command == 'models':
        return {'models': _models(client), 'current': model_settings(thread), 'next': _preferences(identity, home)}
    if command not in ('model', 'effort', 'compact'):
        raise ValueError('현재 세션에서 이 명령을 사용할 수 없습니다.')
    if (args.get('source') or {}).get('cwd') not in (None, thread.get('cwd')):
        raise ValueError('실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.')
    request_id = _request(args.get('requestId'))
    value = args.get('value')
    if command == 'compact' and value is not None:
        raise ValueError('요약 명령에는 인자를 넣지 않습니다.')
    digest = hashlib.sha256(json.dumps([command, value], sort_keys=True).encode()).hexdigest()
    with _Ledger(identity, 'command-' + request_id, home) as receipt:
        prior = receipt.read()
        if prior:
            if prior.get('digest') != digest: raise ValueError('같은 명령 ID에 다른 값을 사용할 수 없습니다.')
            return prior['result']
        if not _capabilities(thread)['canSend']:
            raise ValueError('현재 세션에서 이 명령을 사용할 수 없습니다.')
        if command == 'compact' and _state(thread)['phase'] != 'idle':
            raise ValueError('진행 중인 작업이 끝난 뒤 명령을 실행해 주세요.')
        if command in ('model', 'effort'):
            with _Ledger(identity, 'model-settings', home) as preferences:
                current = _preferences(identity, home) or model_settings(thread)
                model = value if command == 'model' else current.get('model')
                chosen = next((row for row in _models(client) if row['model'] == model), None)
                if chosen is None: raise ValueError('이 계정에서 선택할 수 있는 모델을 골라 주세요.')
                effort = value if command == 'effort' else current.get('effort')
                if command == 'model' and effort not in chosen['efforts']: effort = chosen['defaultEffort']
                if effort not in chosen['efforts']: raise ValueError('이 모델이 지원하는 에포트 레벨을 골라 주세요.')
                selected = {'model': model, 'effort': effort}
                receipt.save({'digest': digest, 'result': {'status': 'unknown', 'reason': '모델 설정 결과를 다시 확인해 주세요.'}})
                preferences.save(selected)
                result = {'status': 'configured', 'current': model_settings(thread), 'next': selected,
                          'reason': '다음 웹 입력부터 적용됩니다.'}
        else:
            unknown = {'status': 'unknown', 'reason': '명령 수신 여부를 확인하지 못했습니다. 자동으로 재실행하지 않습니다.'}
            receipt.save({'digest': digest, 'result': unknown})
            try:
                client.call('thread/compact/start', {'threadId': identity['id']})
                result = {'status': 'accepted', 'reason': '대화 요약을 시작했습니다.'}
            except NativeRejected:
                result = {'status': 'rejected', 'reason': '이 CLI에서 요약 요청을 거절했습니다.'}
            except (OSError, TimeoutError, collector.RpcError):
                return unknown
        receipt.save({'digest': digest, 'result': result})
        return result


class NativeRejected(collector.RpcError):
    def __init__(self, error):
        message = str(error.get("message") or "").lower()
        data = json.dumps(error.get("data"), default=str) if error.get("data") is not None else ""
        self.code = error.get("code") if isinstance(error.get("code"), int) else None
        # An older daemon names a newer method an unknown variant (and lists every method it
        # knows); only the name is kept, never the rest of the message.
        variant = re.match(r"invalid request: unknown variant `([a-z/]{1,64})`", message) if self.code == -32600 else None
        self.unknown_variant = variant.group(1) if variant else None
        self.category = ("unknownVariant" if self.unknown_variant else
                         "notSteerable" if "activeTurnNotSteerable" in data or "not steerable" in message else
                         "attachment" if "attachment" in message else
                         "inputTooLarge" if "input" in message and "large" in message else
                         "nativeRejected")
        super().__init__("Native 요청을 완료하지 못했습니다.")


class ChatAppServer(native._DeadlineAppServer):
    """Experimental thread fields, retain only safe notification/request metadata."""
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
    # Only the latest turns: a whole-history read of a long session can outlast the deadline.
    result = collector.codex_recent_thread(client, identity["id"])
    if result is None:
        result = client.call("thread/read", {"threadId": identity["id"], "includeTurns": True})
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict) or thread.get("id") != identity["id"]:
        raise ValueError("정확한 원본 Codex 세션을 확인하지 못했습니다.")
    turns, status = thread.get("turns"), thread.get("status")
    if not isinstance(turns, list) or not isinstance(status, dict):
        raise ValueError("원본 Codex 상태 형식이 올바르지 않습니다.")
    if any(not isinstance(t, dict) or not isinstance(t.get("status"), str) for t in turns):
        raise ValueError("원본 Codex turn 상태를 확인하지 못했습니다.")
    # A status added by a newer CLI must not hide the conversation; it only
    # disables direct input until the board recognizes the state.
    thread["unknownTurnStatus"] = any(t["status"] not in KNOWN_TURN_STATUS for t in turns)
    return thread


def _state(thread):
    status = thread.get("status") or {}
    flags = status.get("activeFlags") or []
    if not isinstance(flags, list):
        flags = ["unknown"]
    active = [t for t in thread["turns"] if t.get("status") == "inProgress"]
    kind = status.get("type")
    # Codex stopped on an error (systemError): the list calls it 답 필요, and so does this view.
    phase = ("needs_input" if flags else "working") if kind == "active" else "needs_input" if kind == "systemError" else (
        "idle" if kind == "idle" and not active else "closed" if kind == "notLoaded" and not active else "unknown")
    if thread.get("unknownTurnStatus") and phase == "idle":
        phase = "unknown"
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


def _capabilities(thread):
    state = _state(thread)
    stopped = (thread.get("status") or {}).get("type") == "systemError"
    can = (thread.get("canAcceptDirectInput") is True and not _remote(thread.get("source"))
           and not _remote(thread.get("threadSource")) and state["phase"] in ("idle", "working", "needs_input")
           and not stopped)
    if can:
        reason = None
    elif stopped:
        # Whether the app server takes a new turn in this state is not known: the terminal does.
        reason = "Codex가 오류로 멈췄습니다. 원본 터미널에서 확인한 뒤 이어서 입력해 주세요."
    elif _remote(thread.get("source")) or _remote(thread.get("threadSource")):
        reason = "이 원격 세션은 로컬 직접 입력을 지원하지 않습니다. 원본 터미널을 사용하세요."
    elif state["phase"] == "closed":
        reason = "닫힌 세션입니다. 실제 터미널을 먼저 열면 같은 세션에 메시지를 보낼 수 있습니다."
    elif thread.get("canAcceptDirectInput") is not True:
        reason = "이 세션은 직접 입력을 지원하지 않습니다. 원본 터미널을 사용하세요."
    elif thread.get("unknownTurnStatus"):
        reason = "이 Codex 버전의 새 실행 상태를 아직 확인하지 못해 입력을 잠갔습니다. 대화는 계속 볼 수 있습니다."
    else:
        reason = "현재 세션 상태를 확인하지 못했습니다. 상태를 다시 확인하세요."
    return {"canRead": True, "canSend": can, "canAttachImages": can,
            "canAnswerApprovals": False, "canAnswerQuestions": False,
            "reason": reason, "fallback": FALLBACK}


def _receipt(identity, request_id, thread):
    for turn in reversed(thread["turns"]):
        for item in turn.get("items") or []:
            if isinstance(item, dict) and item.get("type") == "userMessage" and item.get("clientId") == request_id:
                delivery = turn.get("status")
                delivery = delivery if delivery in ("completed", "interrupted", "failed") else "accepted"
                return {"source": identity, "requestId": request_id, "delivery": delivery,
                        "confirmed": True, "readbackConfirmed": True, "turnId": turn.get("id"),
                        "canRetry": False}
    return {"source": identity, "requestId": request_id, "delivery": "unknown", "confirmed": False,
            "readbackConfirmed": False, "turnId": None, "canRetry": False,
            "reason": "Native 수신 여부를 확인하지 못했습니다. 같은 입력을 자동으로 다시 보내지 않습니다."}


def _read_receipt(identity, request_id, thread, home):
    """Reuse the exact snapshot; this path never submits or retries input."""
    receipt = _receipt(identity, request_id, thread)
    current = receipt["confirmed"]
    if not receipt["confirmed"]:
        with _Ledger(identity, request_id, home) as ledger:
            saved = (ledger.read() or {}).get("receipt") or {}
            if saved.get("confirmed") or saved.get("delivery") == "rejected":
                receipt = saved
    receipt = {**receipt, "currentSnapshotConfirmed": current}
    if not current:
        # A durable acknowledgement is not evidence of membership in this read.
        receipt["readbackConfirmed"] = False
    manifest = _history_attachments(identity, request_id, home)
    if manifest:
        receipt = {**receipt, "attachments": manifest}
    return receipt


def _request_ids(args):
    ids = args.get("requestIds", [])
    if not isinstance(ids, list) or len(ids) > 20:
        raise ValueError("한 번에 확인할 메시지는 20개까지 선택할 수 있습니다.")
    ids = [_request(identifier) for identifier in ids]
    if args.get("requestId") is not None:
        ids.insert(0, _request(args["requestId"]))
    ids = list(dict.fromkeys(ids))
    if len(ids) > 20:
        raise ValueError("한 번에 확인할 메시지는 20개까지 선택할 수 있습니다.")
    return ids


def _messages(thread, identity=None, home=None):
    out = []
    # Only the retained history window may read attachment metadata from disk.
    for turn in reversed(thread["turns"]):
        # Rows of a whole turn at once: which answer is final (and so carries the turn's
        # completion time) depends on the turn's other items.
        for item, rows in reversed(collector.codex_turn_rows(turn)):
            manifest = _history_attachments(identity, item.get("clientId"), home, item.get("content")) if rows and identity and item.get("type") == "userMessage" else []
            group = []
            # As collector.codex_messages marks them: which answer is the turn's final one.
            marks = {}
            if collector.evidence_id(item.get("id")):
                marks["messageId"] = item["id"]
            if item.get("phase") in ("commentary", "final_answer"):
                marks["phase"] = item["phase"]
            for row in rows:
                if manifest:
                    generic = row.get("attachments", [])
                    images = [a for a in generic if a["kind"] == "image"]
                    extra_images = max(0, len(images) - sum(a["kind"] == "image" for a in manifest))
                    row["attachments"] = manifest + images[:extra_images] + [a for a in generic if a["kind"] != "image"]
                group.append({**row, **marks, "turnId": turn.get("id"), "turnStatus": turn.get("status"),
                            "itemId": item.get("id"), "clientId": item.get("clientId")})
            out[0:0] = group
            if len(out) >= collector.MESSAGE_LIMIT:
                return out[-collector.MESSAGE_LIMIT:]
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


def _ledger_key(identity, request_id):
    return hashlib.sha256(json.dumps([identity, request_id], sort_keys=True).encode()).hexdigest()


def _manifest(value):
    """Accept only bounded, path-free metadata written by attachment resolution."""
    if not isinstance(value, list) or len(value) > 8:
        return []
    out = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"id", "name", "type", "size", "kind", "source"}
                or not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", item["id"])
                or not isinstance(item.get("name"), str) or not item["name"] or len(item["name"].encode()) > 240
                or any(c in item["name"] for c in ("/", "\\")) or any(ord(c) < 32 or ord(c) == 127 for c in item["name"])
                or not isinstance(item.get("type"), str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+", item["type"])
                or len(item["type"]) > 128 or type(item.get("size")) is not int or not 0 <= item["size"] <= attachments.MAX_FILE_BYTES
                or item.get("kind") not in ("image", "file") or item.get("source") != "sessionholic"):
            return []
        out.append(dict(item))
    return out


def _input_marker(block):
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    value = block.get("text") if kind == "text" else block.get("path") if kind == "localImage" else None
    if not isinstance(value, str):
        return None
    return hashlib.sha256(json.dumps([kind, value], ensure_ascii=False).encode()).hexdigest()


def _proven_manifest(manifest, evidence, content):
    """A matching request proves reception, not preservation of each input."""
    if not isinstance(evidence, list) or len(evidence) > 8 or not isinstance(content, list):
        return []
    proofs = {}
    for item in evidence:
        if (not isinstance(item, dict) or set(item) != {"id", "kind", "sha256", "occurrence"}
                or not isinstance(item.get("id"), str) or item["id"] in proofs
                or not re.fullmatch(r"[a-f0-9]{32}", item["id"])
                or item.get("kind") not in ("image", "file")
                or type(item.get("occurrence")) is not int or not 1 <= item["occurrence"] <= 17
                or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            return []
        proofs[item["id"]] = item
    markers = Counter(marker for marker in (_input_marker(block) for block in content) if marker)
    out = []
    for item in manifest:
        proof = proofs.get(item["id"])
        if proof and proof["kind"] == item["kind"] and markers[proof["sha256"]] >= proof["occurrence"]:
            out.append(item)
    return out


def ledger_root(home=None):
    """This host's chat ledger folder (instance `remote.chatLedgerDir` under its home)."""
    return instance.remote_path("chatLedgerDir", launch._home(home))


def _history_attachments(identity, request_id, home, content=None):
    """Atomic ledger read only; absent or unsafe history never writes or locks."""
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        return []
    root = ledger_root(home)
    folder = root / "chat-receipts"
    try:
        for path in (root, folder):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return []
        fd = os.open(str(folder / (_ledger_key(identity, request_id) + ".json")), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                return []
            record = json.loads(file.read(16385))
        if not isinstance(record, dict):
            return []
        manifest = _manifest(record.get("attachments"))
        return manifest if content is None else _proven_manifest(manifest, record.get("attachmentEvidence"), content)
    except (OSError, ValueError, UnicodeError):
        return []


class _Ledger:
    """Deduplication and bounded attachment metadata, never message/file contents."""
    def __init__(self, identity, request_id, home):
        root = ledger_root(home)
        _private_dir(root)
        folder = root / "chat-receipts"
        _private_dir(folder)
        key = _ledger_key(identity, request_id)
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


def _send(client, identity, args, thread, home):
    request_id = _request(args.get("requestId"))
    snapshot_cwd = (args.get("source") or {}).get("cwd")
    if snapshot_cwd is not None and snapshot_cwd != thread.get("cwd"):
        raise ValueError("실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.")
    raw_inputs = args.get("input")
    if not isinstance(raw_inputs, list):
        raise ValueError("메시지 입력이 올바르지 않습니다.")
    raw_inputs = list(raw_inputs)
    ids = args.get("attachmentIds", [])
    manifest, evidence = [], []
    if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("첨부 식별자가 올바르지 않습니다.")
    if ids:
        cwd = thread.get("cwd")
        if not isinstance(cwd, str) or cwd != (args.get("source") or {}).get("cwd"):
            raise ValueError("실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.")
        for identifier in ids:
            item = attachments.resolve(cwd, identifier)
            manifest.append({"id": item["id"], "name": item["name"], "type": item["mime"],
                             "size": item["size"], "kind": "image" if item["mime"].startswith("image/") else "file", "source": "sessionholic"})
            if item["mime"] in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                raw_inputs.append({"type": "localImage", "path": item["path"]})
            else:
                # A path reference, never fake inline extraction of arbitrary files.
                raw_inputs.append({"type": "text", "text": "첨부 파일: " + json.dumps(item["path"], ensure_ascii=False)})
            marker = _input_marker(raw_inputs[-1])
            evidence.append({"id": item["id"], "kind": manifest[-1]["kind"], "sha256": marker,
                             "occurrence": sum(_input_marker(block) == marker for block in raw_inputs)})
    inputs = _inputs(raw_inputs)
    digest = hashlib.sha256(json.dumps([inputs], sort_keys=True).encode()).hexdigest()
    # Records written before direct sending hashed the old queue mode as well.
    legacy = hashlib.sha256(json.dumps([inputs, "queue", None], sort_keys=True).encode()).hexdigest()
    with _Ledger(identity, request_id, home) as ledger:
        prior = ledger.read()
        if prior and prior.get("digest") not in (digest, legacy):
            raise ValueError("이미 사용한 요청 ID에 다른 메시지를 보낼 수 없습니다.")
        before = _receipt(identity, request_id, thread)
        recorded = _manifest(prior.get("attachments")) if prior else ([] if before["confirmed"] else manifest)
        if recorded:
            before["attachments"] = recorded
        if before["confirmed"]:
            return before
        if prior:
            # An unknown result remains read-only even when native history lags.
            saved = prior.get("receipt") or {}
            return saved if saved.get("confirmed") or saved.get("delivery") == "rejected" else before
        capabilities = _capabilities(thread)
        if not capabilities["canSend"]:
            return {**before, "delivery": "rejected", "reason": capabilities["reason"], "fallback": FALLBACK}
        options = _preferences(identity, home) if _state(thread)['phase'] == 'idle' else {}
        if options:
            chosen = next((row for row in _models(client) if row['model'] == options['model']), None)
            if chosen is None or options['effort'] not in chosen['efforts']:
                return {**before, 'delivery': 'rejected', 'reason': '선택한 모델·에포트를 다시 확인해 주세요.'}
        ledger.save({"digest": digest, "receipt": before, "attachments": manifest, "attachmentEvidence": evidence})
        acknowledged, turn_id = False, None
        try:
            # turn/start begins a turn when idle and joins the active turn otherwise;
            # the native CLI decides when the model reads it. No board-side queue.
            result = client.call("turn/start", {"threadId": identity["id"], "input": inputs,
                                                "clientUserMessageId": request_id, **options})
            acknowledged, turn_id = True, (result.get("turn") or {}).get("id")
            after = _thread(client, identity)
            receipt = _receipt(identity, request_id, after)
            receipt.update(_state(after))
        except (OSError, ValueError, TimeoutError, collector.RpcError) as error:
            receipt = dict(before)
            if isinstance(error, NativeRejected):
                receipt.update({"nativeErrorCode": error.code, "nativeErrorCategory": error.category})
                if not acknowledged and error.category == "notSteerable":
                    receipt.update({"delivery": "rejected", "reason": NOT_STEERABLE})
                elif not acknowledged and error.code in (-32600, -32602) and error.category in ("attachment", "inputTooLarge"):
                    receipt.update({"delivery": "rejected", "reason": "Native에서 첨부 또는 메시지 입력을 거절했습니다."})
        receipt["acknowledged"] = acknowledged
        if acknowledged and not receipt["confirmed"]:
            receipt.update({"delivery": "accepted", "confirmed": True, "turnId": turn_id,
                            "reason": "Native 수신 응답을 받았습니다. 대화 기록 반영은 아직 확인되지 않았습니다."})
        receipt["fallback"] = FALLBACK
        if manifest:
            receipt["attachments"] = manifest
        try:
            ledger.save({"digest": digest, "receipt": receipt, "attachments": manifest, "attachmentEvidence": evidence})
        except (OSError, ValueError):
            # Input already crossed the native boundary; never report it as a pre-send failure.
            receipt["ledgerPending"] = True
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
                               "canAnswerApprovals": False, "canAnswerQuestions": False, "fallback": FALLBACK,
                               # The board delivers Claude input through its own attached terminal.
                               "reason": "Claude 입력은 보드가 연결한 터미널로 전달합니다."}}
    result['modelSettings'] = model_settings({**row, 'reasoningEffort': row.get('effort') or row.get('effortLevel')})
    if action == "read":
        sid = row.get("sessionId")
        if not isinstance(sid, str) or not collector.SESSION_ID.fullmatch(sid):
            raise ValueError("원본 Claude 대화 ID를 확인하지 못했습니다.")
        root = launch._home(home) / ".claude/projects"
        paths = [p for p in root.glob("*/" + sid + ".jsonl") if p.is_file() and not p.is_symlink()]
        if len(paths) != 1:
            raise ValueError("정확한 원본 Claude 대화 기록을 확인하지 못했습니다.")
        entries = list(collector.tail_entries(paths[0], 2 * 1024 * 1024))
        result["messages"] = collector.claude_messages(entries)
        if not result['modelSettings']['model']:
            for entry in reversed(entries):
                model = (entry.get('message') or {}).get('model') if entry.get('type') == 'assistant' and isinstance(entry.get('message'), dict) else None
                if isinstance(model, str) and MODEL_ID.fullmatch(model):
                    result['modelSettings'].update(model=model, observed='last_response')
                    break
        if result["phase"] == "working":
            # Claude Code records no turn start: "작업 중" counts from the newest user prompt
            # in the transcript, an approximation the board labels as such (workingSinceApprox).
            since = collector.claude_prompt_at(entries)
            if since:
                result.update(workingSince=since, workingSinceApprox=True)
    if action in ("send", "receipt"):
        result.update({"requestId": _request(args.get("requestId")), "delivery": "rejected",
                       "confirmed": False, "readbackConfirmed": False, "canRetry": False,
                       "reason": result["capabilities"]["reason"], "fallback": FALLBACK})
    return result


def handle(args, *, home=None, timeout=10):
    """One bounded operation on the owning host; never opens/resumes a thread."""
    if not isinstance(args, dict) or args.get("action") not in ("capabilities", "read", "send", "receipt", "command"):
        raise ValueError("Native 메시지 작업이 올바르지 않습니다.")
    identity, action = native._identity(args.get("source")), args["action"]
    request_ids = _request_ids(args) if action == "read" else []
    deadline = time.monotonic() + min(max(float(timeout), 0.05), 30)
    if identity["agent"] == "claude":
        return _claude(identity, action, home, deadline, args)
    with _client(identity, home, deadline) as client:
        thread = _thread(client, identity)
        if action == 'command':
            return _codex_command(client, identity, args, thread, home)
        result = {"source": identity, "cwd": thread.get("cwd"), **_state(thread),
                  "capabilities": _capabilities(thread), 'modelSettings': {**model_settings(thread), 'next': _preferences(identity, home)}}
        if action == "read":
            result["messages"] = _messages(thread, identity, home)
            # "작업 중" counts from the running turn's own start (Turn.startedAt, epoch seconds).
            active = [t for t in thread["turns"] if t.get("status") == "inProgress"]
            since = collector.to_epoch(active[-1].get("startedAt")) if active else None
            if since:
                result["workingSince"] = since
            if request_ids:
                result["receipts"] = [_read_receipt(identity, identifier, thread, home) for identifier in request_ids]
        elif action == "receipt":
            request_id = _request(args.get("requestId"))
            result.update(_read_receipt(identity, request_id, thread, home))
        elif action == "send":
            result.update(_send(client, identity, args, thread, home))
        return result
