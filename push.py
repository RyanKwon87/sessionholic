"""Phone notifications for sessions that need you or finished, without message payloads.

The board sends an empty, VAPID-signed Web Push message (RFC 8030, RFC 8292) to each
subscribed browser. Its service worker then reads what to show from this board's
`/api/push/pending`, so no conversation text passes through a push service and no
payload encryption is needed. The ES256 signature comes from the system `openssl`.

Only general sessions notify: a session that starts waiting for your answer, and one
that stops after working at least `MIN_WORK` seconds. Agent-started, bot and one-shot
sessions, unknown runtimes and stale rows never do. The first view of a session is a
baseline, and the last seen phases are saved so a restart does not repeat an alert.
While at least one browser is subscribed, hosts are refreshed every board interval
even with no screen open; with none, nothing runs in the background.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request

# Browser push services; the board never posts to any other host a browser hands it.
PUSH_HOSTS = (re.compile(r"fcm\.googleapis\.com"), re.compile(r"android\.googleapis\.com"),
              re.compile(r"updates\.push\.services\.mozilla\.com"),
              re.compile(r"(?:[a-z0-9-]+\.)*push\.apple\.com"),
              re.compile(r"(?:[a-z0-9-]+\.)*notify\.windows\.com"))
# The VAPID `sub` claim: a contact URL for push services, never this board's address.
SUBJECT = "https://github.com/RyanKwon87/sessionholic"
OPENSSL = "/usr/bin/openssl"
MAX_SUBSCRIPTIONS = 16
MAX_ENDPOINT = 2048
MAX_LABEL = 80
MAX_PENDING = 20
MIN_WORK = 120            # seconds of work (until the row's last update) before a stop counts as finished
RETRY = 60                # a device whose last wake-up failed for a passing reason is tried again,
RETRY_LIMIT = 6           # ... with doubling waits (1, 2, 4 ... minutes), this many times at most
PENDING_TTL = 3600        # a notice nobody read for an hour is dropped, not shown late
FRESH = 30 * 60           # a row last updated longer ago never notifies
SEEN_LIMIT = 4000
SEEN_TTL = 7 * 24 * 3600
TICK = 15.0
SEND_TIMEOUT = 10
AGENTS = {"claude": "Claude Code", "codex": "Codex"}
FINISHED = ("idle", "done", "closed")
DISABLED = "이 보드는 알림을 끈 채 실행 중입니다(--no-push)."


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def subscription_id(endpoint):
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:32]


def valid_endpoint(endpoint):
    """An https URL on a known push service: a plain host name, no credentials, brackets,
    port other than 443 or fragment. (Python 3.9's urlsplit reads `[a].b` as host `a`, so
    the whole netloc must be that host name.)"""
    if not isinstance(endpoint, str) or not 0 < len(endpoint) <= MAX_ENDPOINT or any(c.isspace() for c in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host, netloc = (parts.hostname or "").lower(), parts.netloc.lower()
    return (parts.scheme == "https" and port in (None, 443) and netloc in (host, host + ":443")
            and re.fullmatch(r"[a-z0-9.-]+", host) is not None
            and not parts.fragment and parts.path.startswith("/") and len(parts.path) > 1
            and any(pattern.fullmatch(host) for pattern in PUSH_HOSTS))


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(str(temporary), str(path))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_private(path, default):
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def der_signature(der):
    """An ECDSA DER signature as the raw 64-byte r||s that JOSE ES256 uses."""
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("서명 형식이 올바르지 않습니다.")
    index = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    values = []
    for _ in range(2):
        if der[index] != 0x02:
            raise ValueError("서명 형식이 올바르지 않습니다.")
        length = der[index + 1]
        number = der[index + 2:index + 2 + length].lstrip(b"\0")
        if len(number) > 32:
            raise ValueError("서명 형식이 올바르지 않습니다.")
        values.append(number.rjust(32, b"\0"))
        index += 2 + length
    return values[0] + values[1]


class Vapid:
    """One P-256 key for this board, kept private under its state folder."""

    def __init__(self, directory, openssl=OPENSSL, clock=time.time):
        self.path = Path(directory) / "vapid.pem"
        self.openssl, self.clock = openssl, clock
        self.lock = threading.Lock()
        self.public, self.tokens = None, {}

    def available(self):
        return bool(self.openssl) and os.access(self.openssl, os.X_OK)

    def _run(self, args, data=None):
        done = subprocess.run([self.openssl] + args, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10)
        if done.returncode != 0:
            raise RuntimeError("알림 서명 키를 준비하지 못했습니다.")
        return done.stdout

    def _key(self):
        if not self.path.exists():
            # The PEM comes back on stdout and is written private here; no umask change.
            pem = self._run(["ecparam", "-name", "prime256v1", "-genkey", "-noout"])
            if b"PRIVATE KEY" not in pem:
                raise RuntimeError("알림 서명 키를 만들지 못했습니다.")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_name("vapid." + secrets.token_hex(6) + ".tmp")
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(pem)
                os.replace(str(temporary), str(self.path))
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        if self.path.is_symlink() or self.path.stat().st_uid != os.getuid():
            raise RuntimeError("알림 서명 키 파일이 안전하지 않습니다.")
        os.chmod(str(self.path), 0o600)
        return str(self.path)

    def public_key(self):
        with self.lock:
            if self.public is None:
                der = self._run(["ec", "-in", self._key(), "-pubout", "-outform", "DER"])
                point = der[-65:]
                if len(der) < 65 or point[0] != 0x04:
                    raise RuntimeError("알림 서명 키를 읽지 못했습니다.")
                self.public = b64url(point)
            return self.public

    def authorization(self, endpoint):
        parts = urlsplit(endpoint)
        audience = "%s://%s" % (parts.scheme, parts.hostname)
        now = int(self.clock())
        with self.lock:
            cached = self.tokens.get(audience)
            if cached and cached[1] - now > 3600:
                token = cached[0]
            else:
                expires = now + 12 * 3600
                header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
                claims = b64url(json.dumps({"aud": audience, "exp": expires, "sub": SUBJECT},
                                           separators=(",", ":")).encode())
                signing = (header + "." + claims).encode("ascii")
                signature = der_signature(self._run(["dgst", "-sha256", "-sign", self._key()], signing))
                token = header + "." + claims + "." + b64url(signature)
                self.tokens[audience] = (token, expires)
        return "vapid t=%s, k=%s" % (token, self.public_key())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A push service answers itself; a redirect would send the board somewhere else."""
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_post(url, headers, timeout=SEND_TIMEOUT):
    # An empty body: urllib would otherwise label it a form submission.
    request = urllib.request.Request(url, data=b"", headers={"Content-Type": "application/octet-stream", **headers},
                                     method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, {}, b""
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(512)
        except OSError:
            pass
        return exc.code, dict(exc.headers or {}), body


class PushService:
    def __init__(self, board, state_dir, classify=None, labels=None, sender=None, vapid=None,
                 clock=time.time, monotonic=time.monotonic, log_prefix="세션 알림", enabled=True):
        self.board, self.log_prefix, self.enabled = board, log_prefix, enabled
        self.directory = Path(state_dir) / "push"
        self.classify = classify or (lambda host, row: "general")
        self.labels = labels or (lambda snapshot: snapshot)
        self.sender = sender or http_post
        self.vapid = vapid or Vapid(self.directory, openssl=shutil.which("openssl", path="/usr/bin:/opt/homebrew/bin") or OPENSSL)
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.RLock()
        self.wake, self.closed, self.thread = threading.Event(), False, None
        self.subscriptions = [row for row in read_private(self.directory / "subscriptions.json", [])
                              if isinstance(row, dict) and valid_endpoint(row.get("endpoint"))
                              and row.get("id") == subscription_id(row["endpoint"])][:MAX_SUBSCRIPTIONS]
        seen = read_private(self.directory / "seen.json", {})
        self.seen = seen if isinstance(seen, dict) else {}
        self.pending, self.retry_after, self.reported, self.failed = {}, {}, set(), {}
        # Hosts looked at by this process: a session first seen on one of them is new.
        self.observed_hosts = set()
        self.last_poll = -1e9

    # ---------- subscriptions ----------
    def status(self, identifier=None):
        """`registered` tells a browser whether the board still has its subscription."""
        available = self.enabled and self.vapid.available()
        result = {"available": available, "subscriptions": len(self.subscriptions)}
        if isinstance(identifier, str) and re.fullmatch(r"[a-f0-9]{32}", identifier):
            with self.lock:
                result["registered"] = any(row["id"] == identifier for row in self.subscriptions)
        if available:
            try:
                result["publicKey"] = self.vapid.public_key()
            except (RuntimeError, OSError, subprocess.SubprocessError):
                result.update(available=False, reason="알림 서명 키를 준비하지 못했습니다.")
        else:
            result["reason"] = (DISABLED if not self.enabled else "이 보드에서는 알림을 보낼 수 없습니다.")
        return result

    def subscribe(self, data):
        if not self.enabled:
            raise RuntimeError(DISABLED)
        subscription = data.get("subscription") if isinstance(data, dict) else None
        endpoint = subscription.get("endpoint") if isinstance(subscription, dict) else None
        if not valid_endpoint(endpoint):
            raise ValueError("이 브라우저의 알림 주소를 사용할 수 없습니다.")
        label = data.get("label") if isinstance(data.get("label"), str) else ""
        label = re.sub(r"\s+", " ", label).strip()[:MAX_LABEL]
        identifier = subscription_id(endpoint)
        with self.lock:
            rows = [row for row in self.subscriptions if row["id"] != identifier]
            if len(rows) >= MAX_SUBSCRIPTIONS:
                raise ValueError("알림을 받는 기기가 너무 많습니다. 쓰지 않는 기기에서 알림을 꺼 주세요.")
            rows.append({"id": identifier, "endpoint": endpoint, "label": label, "createdAt": int(self.clock())})
            self._save_subscriptions(rows)
            self.retry_after.pop(identifier, None)
        self.wake.set()
        return {"ok": True, "id": identifier, "subscriptions": len(rows)}

    def unsubscribe(self, data):
        endpoint = data.get("endpoint") if isinstance(data, dict) else None
        if not isinstance(endpoint, str) or len(endpoint) > MAX_ENDPOINT:
            raise ValueError("알림 주소가 올바르지 않습니다.")
        identifier = subscription_id(endpoint)
        with self.lock:
            rows = [row for row in self.subscriptions if row["id"] != identifier]
            if len(rows) != len(self.subscriptions):
                self._save_subscriptions(rows)
            self.pending.pop(identifier, None)
        return {"ok": True, "subscriptions": len(rows)}

    def _save_subscriptions(self, rows):
        write_private(self.directory / "subscriptions.json", rows)
        self.subscriptions = rows

    def pending_for(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("알림 기기 정보가 올바르지 않습니다.")
        with self.lock:
            return {"notifications": self._fresh(self.pending.pop(identifier, []))}

    def test(self, data):
        if not self.enabled:
            raise RuntimeError(DISABLED)
        endpoint = data.get("endpoint") if isinstance(data, dict) else None
        identifier = subscription_id(endpoint) if isinstance(endpoint, str) else None
        with self.lock:
            target = next((row for row in self.subscriptions if row["id"] == identifier), None)
        if target is None:
            raise ValueError("이 기기는 알림을 받도록 등록되어 있지 않습니다. 알림을 다시 켜 주세요.")
        notice = {"title": "알림 테스트", "body": "이 기기에서 알림을 받을 수 있어요.",
                  "tag": "notice-test", "url": "/", "createdAt": int(self.clock())}
        self._queue([target], notice)
        status = self._send(target, ttl=300, urgency="high")
        if status not in (200, 201, 202):
            raise RuntimeError("알림 서비스가 테스트 알림을 받지 않았습니다(%s)." % status)
        return {"ok": True}

    # ---------- watching ----------
    def start(self):
        if self.thread is not None:
            return
        self.board.add_listener(self.changed)
        self.thread = threading.Thread(target=self.run, daemon=True, name="board-push")
        self.thread.start()

    def changed(self, *_):
        self.wake.set()

    def close(self):
        self.closed = True
        self.wake.set()
        try:
            self.board.remove_listener(self.changed)
        except ValueError:
            pass

    def run(self):
        while not self.closed:
            self.wake.wait(TICK)
            self.wake.clear()
            if self.closed:
                return
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the watcher must survive one bad round
                sys.stderr.write("%s: 알림 상태 확인 실패 (%s)\n" % (self.log_prefix, type(exc).__name__))

    def step(self):
        with self.lock:
            active = bool(self.subscriptions)
        if not active:
            return []
        now = self.monotonic()
        if now - self.last_poll >= getattr(self.board, "interval", 120):
            self.last_poll = now
            try:
                self.board.request_refresh()
            except (ValueError, RuntimeError):
                pass
        events = self.observe(self.labels(self.board.snapshot()))
        if events:
            self.notify(events)
        self.retry()
        return events

    def retry(self):
        """Wake again a device whose last wake-up failed for a passing reason (network, 429,
        5xx) while its notices still wait; a refusal like 400 or 403 waits for the next alert."""
        now = self.monotonic()
        with self.lock:
            for identifier in list(self.pending):
                self.pending[identifier] = self._fresh(self.pending[identifier])
            due = []
            for target in self.subscriptions:
                failed = self.failed.get(target["id"])
                if not self.pending.get(target["id"]) or not failed or not failed[1] or failed[2] > RETRY_LIMIT:
                    continue
                if now >= max(self.retry_after.get(target["id"], 0), failed[0] + RETRY * 2 ** (failed[2] - 1)):
                    due.append(target)
            urgent = {target["id"]: any(item.get("kind") == "needs_input" for item in self.pending[target["id"]])
                      for target in due}
        for target in due:
            self._send(target, ttl=86400 if urgent[target["id"]] else 3600,
                       urgency="high" if urgent[target["id"]] else "normal")

    def observe(self, snapshot):
        """Phase transitions worth an alert since the last look; records every row seen."""
        now = int(self.clock())
        events, changed = [], False
        for host in snapshot.get("hosts") or []:
            if not host.get("ok") or not isinstance(host.get("data"), dict):
                continue
            known_host = host["name"] in self.observed_hosts
            for agent in ("codex", "claude"):
                for row in host["data"].get(agent) or []:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    key = "|".join([host["name"], agent, row.get("home") or "", str(row["id"])])
                    phase = "unknown" if row.get("runtimeKnown") is False else str(row.get("phase") or "unknown")
                    previous = self.seen.get(key)
                    previous = previous if isinstance(previous, dict) else None
                    if phase == "unknown":
                        # An unreadable runtime says nothing new: the last known phase stays.
                        if previous is not None:
                            self.seen[key] = {**previous, "seenAt": now}
                        continue
                    working_since = now
                    if previous is not None and previous.get("phase") == "working":
                        working_since = previous.get("workingSince") or now
                    record = {"phase": phase, "seenAt": now}
                    if phase == "working":
                        record["workingSince"] = working_since
                    changed = changed or previous is None or previous.get("phase") != phase
                    self.seen[key] = record
                    updated = row.get("updatedAt")
                    updated = updated if isinstance(updated, (int, float)) and not isinstance(updated, bool) else None
                    kind = None
                    if previous is None:
                        # New since this process last looked at the host: a session that
                        # started and asked between two looks (the first look is a baseline).
                        started = row.get("startedAt") if isinstance(row.get("startedAt"), (int, float)) else updated
                        if known_host and phase == "needs_input" and started is not None and now - started <= FRESH:
                            kind = "needs_input"
                    elif previous.get("phase") != phase:
                        if phase == "needs_input":
                            kind = "needs_input"
                        elif previous.get("phase") == "working" and phase in FINISHED:
                            # The work ended at the row's last update, not when it was looked at.
                            end = min(now, int(updated)) if updated is not None else now
                            if end - working_since >= MIN_WORK:
                                kind = "finished"
                    if (kind is None or row.get("executionMode") == "one-shot"
                            or (agent == "codex" and row.get("source") == "exec")
                            or (updated is not None and now - updated > FRESH)):
                        continue
                    if self.classify(host, {**row, "agent": agent}) != "general":
                        continue
                    events.append({"kind": kind, "key": key, "host": host, "row": {**row, "agent": agent}})
            self.observed_hosts.add(host["name"])
        if changed or len(self.seen) > SEEN_LIMIT:
            self._save_seen(now)
        return events

    def _save_seen(self, now):
        rows = {key: value for key, value in self.seen.items()
                if isinstance(value, dict) and now - (value.get("seenAt") or 0) <= SEEN_TTL}
        if len(rows) > SEEN_LIMIT:
            rows = dict(sorted(rows.items(), key=lambda item: item[1].get("seenAt") or 0)[-SEEN_LIMIT:])
        self.seen = rows
        try:
            write_private(self.directory / "seen.json", rows)
        except OSError:
            sys.stderr.write("%s: 알림 상태 기록 저장 실패\n" % self.log_prefix)

    # ---------- sending ----------
    def notice(self, event):
        row, host = event["row"], event["host"]
        title = str(row.get("displayTitle") or row.get("title") or "제목 없는 세션")[:120]
        state = "답이 필요해요" if event["kind"] == "needs_input" else "작업이 끝났어요"
        where = " · ".join(part for part in (host.get("label") or host.get("name"), AGENTS.get(row["agent"])) if part)
        snippet = re.sub(r"\s+", " ", str(row.get("snippet") or "")).strip()
        body = state + (" · " + where if where else "") + ("\n" + snippet[:140] if snippet else "")
        return {"title": title, "body": body, "tag": "session:" + event["key"],
                "url": "/?session=" + quote(event["key"], safe=""), "kind": event["kind"],
                "createdAt": int(self.clock())}

    def notify(self, events):
        """Queue every alert of one round, then wake each browser once: its service worker
        reads them all, so a second push never finds an empty queue."""
        with self.lock:
            targets = list(self.subscriptions)
        for event in events:
            self._queue(targets, self.notice(event))
        urgent = any(event["kind"] == "needs_input" for event in events)
        for target in targets:
            self._send(target, ttl=86400 if urgent else 3600, urgency="high" if urgent else "normal")

    def _queue(self, targets, notice):
        with self.lock:
            for target in targets:
                queue = self.pending.setdefault(target["id"], [])
                queue[:] = [item for item in self._fresh(queue) if item.get("tag") != notice["tag"]][-(MAX_PENDING - 1):]
                queue.append(dict(notice))

    def _fresh(self, items):
        now = int(self.clock())
        return [item for item in items if now - (item.get("createdAt") or now) <= PENDING_TTL]

    def _send(self, target, ttl, urgency):
        """One device's wake-up; whatever goes wrong stays with this device."""
        identifier = target["id"]
        if self.retry_after.get(identifier, 0) > self.monotonic():
            return 429
        try:
            headers = {"TTL": str(ttl), "Urgency": urgency, "Content-Length": "0",
                       "Authorization": self.vapid.authorization(target["endpoint"])}
            status, response_headers, body = self.sender(target["endpoint"], headers)
        except Exception as exc:  # noqa: BLE001 - one device must never stop the others
            # A network or protocol error passes; a local one (signing) would only repeat.
            self._fail(identifier, isinstance(exc, (OSError, http.client.HTTPException)))
            self._report(identifier, "network", type(exc).__name__)
            return 0
        try:
            if status in (200, 201, 202):
                self.failed.pop(identifier, None)
            elif status in (404, 410):
                with self.lock:
                    self._save_subscriptions([row for row in self.subscriptions if row["id"] != identifier])
                    self.pending.pop(identifier, None)
                    self.failed.pop(identifier, None)
            elif status == 429:
                try:
                    delay = min(max(int(response_headers.get("Retry-After", "60")), 30), 3600)
                except (ValueError, TypeError):
                    delay = 60
                self.retry_after[identifier] = self.monotonic() + delay
                self._fail(identifier, True)
            else:
                self._fail(identifier, status >= 500)
                reason = body.decode("utf-8", "replace")[:160] if isinstance(body, bytes) else ""
                self._report(identifier, status, re.sub(r"\s+", " ", reason))
        except Exception as exc:  # noqa: BLE001
            self._report(identifier, "local", type(exc).__name__)
        return status

    def _fail(self, identifier, passing):
        """(when, whether it may pass, how many times in a row)."""
        count = (self.failed.get(identifier) or (0, False, 0))[2] + 1
        self.failed[identifier] = (self.monotonic(), passing, count)

    def _report(self, identifier, status, reason):
        mark = (identifier, status)
        if mark in self.reported:
            return
        self.reported.add(mark)
        sys.stderr.write("%s: 알림 서비스 응답 %s %s\n" % (self.log_prefix, status, reason))
