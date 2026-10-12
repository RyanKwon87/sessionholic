"""Private, revisioned goals, independent of native session/connection/receipt state.

Session links are inert references. This module never reads a conversation or runs a
native CLI. Actor metadata describes an explicit user/agent report or an inference;
it is not an authenticated identity. The last 100 changes are retained per goal.
"""
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

import settings

STATUSES = ("todo", "in_progress", "waiting", "review", "done")
GOAL_ID = re.compile(r"[a-f0-9]{32}\Z")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MAX_BYTES = 8 * 1024 * 1024
MAX_GOALS = 2000
HISTORY_LIMIT = 100
TEXT_LIMITS = {"title": 240, "completionCriteria": 8000, "current": 2000, "next": 2000,
               "waitingReason": 2000, "evidence": 8000, "project": 240}
FIELDS = set(TEXT_LIMITS) | {"status", "pinned", "archived", "sessions"}
META = {"id", "revision", "createdAt", "updatedAt", "history", "historyCount"}


class GoalError(Exception):
    def __init__(self, message, code="invalid_goal", status=400, **details):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, "code": code, **details}


def _text(value, limit, *, required=False):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
            or (required and not value.strip())):
        raise GoalError("작업 내용의 형식 또는 길이를 확인해 주세요.")
    return value.strip()


def _actor(value):
    if (not isinstance(value, dict) or set(value) != {"type", "label"}
            or value.get("type") not in ("user", "agent", "inferred")):
        raise GoalError("변경 주체는 사용자·에이전트 보고·추정 중 하나여야 합니다.")
    return {"type": value["type"], "label": _text(value.get("label"), 120, required=True)}


def _sessions(value):
    if not isinstance(value, list) or len(value) > 64:
        raise GoalError("작업에는 세션을 64개까지 연결할 수 있습니다.")
    result, seen = [], set()
    for ref in value:
        if not isinstance(ref, dict) or set(ref) - {"host", "agent", "id", "home", "point"}:
            raise GoalError("연결할 세션 정보를 확인해 주세요.")
        for field, pattern in (("host", NAME), ("agent", NAME), ("id", SESSION_ID)):
            if not isinstance(ref.get(field), str) or not pattern.fullmatch(ref[field]):
                raise GoalError("연결할 세션의 기기·도구·식별자가 필요합니다.")
        home = ref.get("home", "")
        if home is None:
            home = ""
        if (not isinstance(home, str) or len(home) > 128 or any(c.isspace() for c in home)
                or (home and (home.startswith("/") or ".." in home.split("/")
                              or not re.fullmatch(r"[A-Za-z0-9_./-]+", home)))):
            raise GoalError("세션의 계정 홈 식별자를 확인해 주세요.")
        # Existing Claude rows do not carry a home; Codex's default home is explicit.
        home = home or (".codex" if ref["agent"] == "codex" else ".claude" if ref["agent"] == "claude" else "")
        key = (ref["host"], ref["agent"], home, ref["id"])
        if key in seen:
            raise GoalError("같은 세션이 중복 연결되어 있습니다.")
        seen.add(key)
        result.append({"host": ref["host"], "agent": ref["agent"], "id": ref["id"], "home": home,
                       "point": _text(ref.get("point", ""), 1024)})
    return result


def _fields(value):
    if set(value) != FIELDS:
        raise GoalError("작업 필드를 확인해 주세요.")
    result = {field: _text(value[field], limit, required=field == "title") for field, limit in TEXT_LIMITS.items()}
    if value.get("status") not in STATUSES:
        raise GoalError("작업 상태를 확인해 주세요.")
    result["status"] = value["status"]
    for key in ("pinned", "archived"):
        if type(value.get(key)) is not bool:
            raise GoalError("고정·보관 값은 참 또는 거짓이어야 합니다.")
        result[key] = value[key]
    result["sessions"] = _sessions(value.get("sessions"))
    if result["status"] == "done" and not (result["completionCriteria"] and result["evidence"]):
        raise GoalError("완료 조건과 충족 근거를 입력해 주세요. 검토가 남았다면 확인 필요로 옮겨 주세요.",
                        "completion_evidence_required")
    if result["status"] == "waiting" and not result["waitingReason"]:
        raise GoalError("기다리는 이유를 입력해 주세요.", "waiting_reason_required")
    return result


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("timestamp")
    datetime.fromisoformat(value[:-1] + "+00:00")


def _record(value):
    """Validate stored data before returning it; never silently replace corrupt state."""
    if not isinstance(value, dict) or set(value) != FIELDS | META:
        raise ValueError("record")
    if (not isinstance(value["id"], str) or not GOAL_ID.fullmatch(value["id"])
            or type(value["revision"]) is not int or value["revision"] < 1):
        raise ValueError("identity")
    _fields({key: value[key] for key in FIELDS})
    _timestamp(value["createdAt"])
    _timestamp(value["updatedAt"])
    history = value["history"]
    if (not isinstance(history, list) or not 1 <= len(history) <= HISTORY_LIMIT
            or type(value["historyCount"]) is not int or value["historyCount"] < len(history)):
        raise ValueError("history")
    for event in history:
        if (not isinstance(event, dict) or set(event) != {"at", "actor", "reason", "fromStatus", "toStatus", "fields"}
                or event["fromStatus"] not in (None,) + STATUSES or event["toStatus"] not in STATUSES
                or not isinstance(event["fields"], list) or any(field not in FIELDS for field in event["fields"])):
            raise ValueError("history event")
        _timestamp(event["at"])
        _actor(event["actor"])
        _text(event["reason"], 2000, required=True)
    return value


class Goals:
    def __init__(self, state_dir):
        # Lazy I/O: a bad goals file must not prevent conversations from starting.
        self.directory = Path(state_dir)
        self.path = self.directory / "goals.json"
        self.lock_path = self.directory / "goals.lock"
        self.lock = threading.Lock()

    @staticmethod
    def _unavailable():
        return GoalError("작업 기록을 안전하게 읽거나 저장하지 못했습니다. 기존 기록은 유지됩니다.",
                         "goals_unavailable", 503)

    @staticmethod
    def _private_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("private file")
        return info

    @contextmanager
    def _storage(self, *, write=False):
        with self.lock:
            fd = None
            try:
                directory = settings.safe_path(self.directory, directory=True, private=True, allow_missing=True)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                settings.safe_path(self.lock_path, private=True, allow_missing=True)
                fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                self._private_file(fd)
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise self._unavailable()
                        time.sleep(0.01)
                yield
            except (OSError, ValueError, UnicodeError):
                raise self._unavailable() from None
            finally:
                if fd is not None:
                    os.close(fd)

    def _read(self):
        settings.safe_path(self.path, private=True, allow_missing=True)
        try:
            fd = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "revision": 0, "goals": []}
        try:
            with os.fdopen(fd, "rb") as stream:
                if self._private_file(stream.fileno()).st_size > MAX_BYTES:
                    raise ValueError("file size")
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("file size")
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {"version", "revision", "goals"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or type(data["revision"]) is not int or data["revision"] < 0
                    or not isinstance(data["goals"], list) or len(data["goals"]) > MAX_GOALS):
                raise ValueError("store")
            ids = [_record(goal)["id"] for goal in data["goals"]]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate id")
            return data
        except (GoalError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
            raise self._unavailable() from None

    def _write(self, data):
        raw = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise GoalError("작업 기록 저장 한도에 도달했습니다. 기록 파일을 확인해 주세요.", "goals_full", 409)
        settings.safe_path(self.path, private=True, allow_missing=True)
        temporary = self.path.with_name(".goals-" + secrets.token_hex(12) + ".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _id(goal_id):
        if not isinstance(goal_id, str) or not GOAL_ID.fullmatch(goal_id):
            raise GoalError("작업 식별자가 올바르지 않습니다.")

    @staticmethod
    def _find(data, goal_id):
        for goal in data["goals"]:
            if goal["id"] == goal_id:
                return goal
        raise GoalError("작업을 찾지 못했습니다.", "goal_not_found", 404)

    def snapshot(self):
        with self._storage():
            data = self._read()
            return {"goals": data["goals"], "revision": data["revision"]}

    def get(self, goal_id):
        self._id(goal_id)
        with self._storage():
            return {"goal": self._find(self._read(), goal_id)}

    @staticmethod
    def _changed(values, prior, actor, reason):
        changed = sorted(key for key in FIELDS if prior is None or values[key] != prior[key])
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = {"at": now, "actor": actor, "reason": reason, "fromStatus": prior["status"] if prior else None,
                 "toStatus": values["status"], "fields": changed}
        return {**values, "id": prior["id"] if prior else secrets.token_hex(16),
                "revision": prior["revision"] + 1 if prior else 1,
                "createdAt": prior["createdAt"] if prior else now, "updatedAt": now,
                "history": ((prior["history"] if prior else []) + [event])[-HISTORY_LIMIT:],
                "historyCount": prior["historyCount"] + 1 if prior else 1}

    def link_sessions(self, source, target, *, actor_label="세션홀릭"):
        """Link one confirmed handoff destination to all goals of its exact source.

        Caller supplies reviewed native launch identities, never a time/title/project
        match. This is atomic across all affected goals and never changes their status.
        """
        def ref(value):
            if not isinstance(value, dict):
                raise GoalError("인계 세션 정보가 올바르지 않습니다.")
            return _sessions([{key: value.get(key) for key in ("host", "agent", "id", "home")}])[0]
        def identity(value):
            return tuple(value[key] for key in ("host", "agent", "id", "home"))
        source, target = ref(source), ref(target)
        source_key, target_key = identity(source), identity(target)
        if source_key == target_key:
            return 0
        actor = _actor({"type": "agent", "label": actor_label})
        count = 0
        with self._storage(write=True):
            data = self._read()
            for index, goal in enumerate(data["goals"]):
                keys = {identity(link) for link in goal["sessions"]}
                if source_key not in keys or target_key in keys:
                    continue
                values = {key: goal[key] for key in FIELDS}
                values["sessions"] = _sessions(goal["sessions"] + [target])
                data["goals"][index] = self._changed(values, goal, actor, "확인된 세션 인계 기록으로 대상 세션 연결")
                count += 1
            if count:
                data["revision"] += 1
                self._write(data)
        return count

    def save(self, payload, goal_id=None):
        if not isinstance(payload, dict) or set(payload) - FIELDS - {"revision", "actor", "reason"}:
            raise GoalError("작업 요청의 필드를 확인해 주세요.")
        if goal_id is not None:
            self._id(goal_id)
            if type(payload.get("revision")) is not int or payload["revision"] < 1:
                raise GoalError("수정할 작업의 최신 버전이 필요합니다.", "goal_revision_required")
        elif "revision" in payload:
            raise GoalError("새 작업에는 기존 버전을 지정하지 않습니다.")
        actor = _actor(payload.get("actor", {"type": "user", "label": "사용자"}))
        reason = _text(payload.get("reason", "작업 수정" if goal_id else "새 작업 생성"), 2000, required=True)
        with self._storage(write=True):
            data = self._read()
            prior = self._find(data, goal_id) if goal_id is not None else None
            if prior is not None and prior["revision"] != payload["revision"]:
                raise GoalError("다른 화면에서 작업이 바뀌었습니다. 최신 내용을 확인한 뒤 다시 저장해 주세요.",
                                "goal_conflict", 409, currentRevision=prior["revision"])
            if prior is None and len(data["goals"]) >= MAX_GOALS:
                raise GoalError("작업 저장 한도에 도달했습니다.", "goals_full", 409)
            values = {key: prior[key] for key in FIELDS} if prior else {
                **{key: "" for key in TEXT_LIMITS}, "status": "todo", "pinned": False, "archived": False, "sessions": []}
            values.update({key: value for key, value in payload.items() if key in FIELDS})
            values = _fields(values)
            if prior is not None and all(values[key] == prior[key] for key in FIELDS):
                return {"goal": copy.deepcopy(prior)}
            goal = self._changed(values, prior, actor, reason)
            if prior is not None:
                data["goals"][data["goals"].index(prior)] = goal
            else:
                data["goals"].append(goal)
            data["revision"] += 1
            self._write(data)
            return {"goal": copy.deepcopy(goal)}
