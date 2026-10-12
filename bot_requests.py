"""Explicit, read-only bot request sources; also a standalone SSH-stdin reader.

This module never imports a bot runtime, decrypts content, changes a database,
or treats a displayed authority label as permission to control a native session.
"""
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
KINDS = {"holicii_sqlite", "hermes_sqlite", "slack_ledger"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
FIELDS = {"id", "label", "host", "kind", "path", "authorityLabel", "scopeLabel"}
REDACTIONS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*", re.I),
    re.compile(r"(?i)\b(?:Bearer\s+\S+|sk-(?:proj-|ant-api\d{2}-)?[\w-]{10,}|xox[baprs]-[\w-]+|gh[pousr]_[\w]{16,}|github_pat_[\w]{16,})"),
    re.compile(r"(?i)\b(?:[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)|token)\s*[=:]\s*[\"']?[^\s,;\"']+"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"),
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


def _base(source, key, *, title, status, updated, channel=None, requester=None, native=None):
    return {"id": _opaque(source, key), "sourceId": source["id"], "botName": source["label"],
            "host": source["host"], "channelLabel": channel or "채널 미확인",
            "requesterLabel": requester or "요청자 미확인", "authorityLabel": source["authorityLabel"],
            "scopeLabel": source["scopeLabel"], "title": title, "status": status,
            "updatedAt": _stamp(updated), "nativeRef": native}


def _native(value):
    try:
        return {"agent": "codex", "id": str(uuid.UUID(str(value))), "home": ".codex"}
    except (ValueError, TypeError, AttributeError):
        return None


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
    # Use an existing time index; never scan message bodies or imported native history.
    index = None
    for row in connection.execute('PRAGMA index_list("sessions")'):
        name = row[1]
        info = list(connection.execute('PRAGMA index_info("' + name.replace('"', '""') + '")'))
        if info and info[0][2] == "started_at":
            index = name
            break
    if index is None:
        raise SourceError("봇 세션의 최신 기록 인덱스를 확인하지 못했습니다.")
    activity = "last_activity_at" if "last_activity_at" in columns else "started_at"
    query = ('SELECT id,user_id,chat_id,started_at,ended_at,' + activity + ' AS activity,substr(origin_json,1,16384) AS origin '
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
                            requester=_text(origin.get("user_name"), _alias("요청자", source, row["user_id"]), 40)))
    return result


def _ledger(connection, source):
    _columns(connection, "tasks", {"id", "channel", "user", "text", "status", "codex_thread", "created_at", "started_at", "finished_at"})
    _columns(connection, "users", {"id", "name", "real_name"})
    _columns(connection, "channels", {"id", "name"})
    _primary_key(connection, "tasks", "id", integer=True)
    _primary_key(connection, "users", "id")
    _primary_key(connection, "channels", "id")
    # The integer primary key is the receipt order. Exact ID joins are bounded
    # to these requests. A later session mapping never rewrites old task ownership.
    query = ("SELECT t.id,t.channel,t.user,substr(t.text,1,2048) AS title,t.status,t.codex_thread,"
             "t.created_at,t.started_at,t.finished_at,substr(u.real_name,1,200) AS real_name,"
             "substr(u.name,1,200) AS user_name,substr(c.name,1,200) AS channel_name "
             "FROM (SELECT id,channel,user,substr(text,1,2048) AS text,status,codex_thread,created_at,started_at,finished_at "
             "FROM tasks ORDER BY id DESC LIMIT ?) t LEFT JOIN users u ON u.id=t.user "
             "LEFT JOIN channels c ON c.id=t.channel ORDER BY t.id DESC")
    result = []
    for row in connection.execute(query, (LIMIT,)):
        result.append(_base(source, row["id"], title=_text(row["title"], "봇 요청", 120),
                            status=_text(row["status"], "unknown", 40),
                            updated=row["finished_at"] or row["started_at"] or row["created_at"],
                            channel=_text(row["channel_name"], _alias("채널", source, row["channel"]), 60),
                            requester=_text(row["real_name"] or row["user_name"], _alias("요청자", source, row["user"]), 40),
                            native=_native(row["codex_thread"])))
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


OWNER_LIMIT = 4096  # bot sessions remembered after they leave the ledgers


class BotRequests:
    def __init__(self, hosts, config_path, state_dir):
        self.hosts = {row["name"]: dict(row) for row in hosts}
        self.config_path, self.state_dir = Path(config_path), Path(state_dir)
        self.home = Path.home()
        self._lock = threading.Lock()
        self._cache, self._source_cache = None, {}
        self._owner_keys = None

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
            self._owner_keys = [key for key in data if isinstance(key, str)][-OWNER_LIMIT:] if isinstance(data, list) else []
        return self._owner_keys

    def _remember(self, rows):
        keys = self._owners()
        known = set(keys)
        new = [key for key in dict.fromkeys(self.owner_key(row["host"], row["nativeRef"]) for row in rows
                                            if isinstance(row.get("nativeRef"), dict)) if key not in known]
        if not new:
            return
        keys.extend(new)
        del keys[:-OWNER_LIMIT]
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
        except OSError:
            pass  # kept for this process; the next new session tries the file again

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

    def _collect(self, source):
        host = self.hosts[source["host"]]
        if host.get("local"):
            return read_source(source, self.home)
        from server import collector_command
        program = ("BOT_REQUEST = " + ascii(source) + "\n").encode() + Path(__file__).read_bytes()
        done = subprocess.run(collector_command(host, [], self.state_dir), input=program,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
        if done.returncode or len(done.stdout) > 1024 * 1024:
            raise SourceError("봇 원장의 원격 읽기를 완료하지 못했습니다.")
        result = json.loads(done.stdout)
        if not isinstance(result, dict) or result.get("error"):
            raise SourceError("봇 원장의 원격 읽기를 완료하지 못했습니다.")
        return result

    @staticmethod
    def _checked(source, result):
        rows = result.get("requests") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > LIMIT:
            raise SourceError("봇 요청 목록 형식이 올바르지 않습니다.")
        checked = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("sourceId") != source["id"]
                    or row.get("host") != source["host"] or not isinstance(row.get("id"), str)
                    or not re.fullmatch(re.escape(source["id"]) + r":[0-9a-f]{24}", row["id"])):
                raise SourceError("봇 요청의 원본 출처가 다릅니다.")
            native = row.get("nativeRef")
            if native is not None and (not isinstance(native, dict) or _native(native.get("id")) != native
                                       or source["kind"] != "slack_ledger"):
                raise SourceError("봇 요청의 native 세션 연결이 올바르지 않습니다.")
            checked.append({"id": row["id"], "sourceId": source["id"], "host": source["host"],
                            "botName": source["label"], "authorityLabel": source["authorityLabel"], "scopeLabel": source["scopeLabel"],
                            **{key: _text(row.get(key), "미확인", limit) for key, limit in
                               (("channelLabel", 60), ("requesterLabel", 40), ("title", 120), ("status", 40))},
                            "updatedAt": _stamp(row.get("updatedAt")), "nativeRef": native})
        if len({row["id"] for row in checked}) != len(checked):
            raise SourceError("봇 요청 식별자가 중복되었습니다.")
        return checked

    def snapshot(self, force=False):
        if type(force) is not bool:
            raise ValueError("봇 요청 새로고침 값이 올바르지 않습니다.")
        with self._lock:
            result = {"requests": [], "sources": [], "fetchedAt": _now()}
            changed = self._cache is None
            try:
                sources = self._sources()
            except (OSError, ValueError, RuntimeError):
                sources, changed = [], True
                # A broken config must not erase prior bot/native ownership evidence.
                if self._cache:
                    result["requests"] = [{**row, "stale": True} for row in self._cache["requests"]]
                    result["sources"] = [{**row, "ok": False, "stale": True,
                                          "error": "봇 출처 설정을 읽지 못했습니다."}
                                         for row in self._cache["sources"] if row["id"] != "configuration"]
                result["sources"].append({"id": "configuration", "label": "봇 출처 설정", "host": None,
                                          "ok": False, "error": "봇 출처 설정을 읽지 못했습니다.", "updatedAt": None})
            for source in sources:
                signature = json.dumps(source, sort_keys=True)
                cached = self._source_cache.get(source["id"])
                if cached and cached["signature"] != signature:
                    cached = None
                if cached and not force and (cached["failed"] or time.monotonic() - cached["fetched"] < CACHE_SECONDS):
                    result["requests"].extend(cached["rows"])
                    result["sources"].append(cached["status"])
                    continue
                changed = True
                status = {key: source[key] for key in ("id", "label", "host")}
                try:
                    rows = self._checked(source, self._collect(source))
                    status.update(ok=True, error=None, updatedAt=_now())
                    failed = False
                except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
                    failed = True
                    rows = [{**row, "stale": True} for row in cached["rows"]] if cached else []
                    status.update(ok=False, error="봇 원장을 읽지 못했습니다. 출처와 연결을 확인해 주세요.",
                                  updatedAt=cached["status"]["updatedAt"] if cached else None, stale=bool(rows))
                self._source_cache[source["id"]] = {"signature": signature, "rows": rows, "status": status,
                                                    "failed": failed, "fetched": time.monotonic()}
                result["requests"].extend(rows)
                result["sources"].append(status)
            result["requests"].sort(key=lambda row: row["updatedAt"] or "", reverse=True)
            self._remember(result["requests"])
            if not changed and self._cache and result["sources"] == self._cache["sources"]:
                result["fetchedAt"] = self._cache["fetchedAt"]
            self._cache = result
            return copy.deepcopy(result)


def main():
    try:
        result = read_source(globals().get("BOT_REQUEST"))
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        result = {"requests": [], "error": "봇 원장을 읽지 못했습니다."}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
