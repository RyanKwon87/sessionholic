import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import push  # noqa: E402
import server  # noqa: E402

APPLE = "https://web.push.apple.com/QGuQyavXutnMH-fixture-token"
FCM = "https://fcm.googleapis.com/fcm/send/fixture-token:APA91-fixture"
OPENSSL = shutil.which("openssl", path="/usr/bin:/opt/homebrew/bin")


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import pathlib
    import shutil
    import tempfile
    from unittest import mock
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", pathlib.Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class FakeBoard:
    interval = 120

    def __init__(self):
        self.hosts, self.listeners, self.refreshes = [], [], 0

    def snapshot(self):
        return {"hosts": self.hosts}

    def add_listener(self, callback):
        self.listeners.append(callback)

    def remove_listener(self, callback):
        self.listeners.remove(callback)

    def request_refresh(self, *_):
        self.refreshes += 1
        return True

    def set(self, *rows, ok=True, name="laptop", label="노트북"):
        self.hosts = [{"name": name, "label": label, "ok": ok, "data": {
            "codex": [row for row in rows if row.get("agent", "codex") == "codex"],
            "claude": [row for row in rows if row.get("agent") == "claude"]}}]


class FakeVapid:
    def available(self):
        return True

    def public_key(self):
        return "BFixturePublicKey"

    def authorization(self, endpoint):
        return "vapid t=fixture, k=BFixturePublicKey"


class Clock:
    def __init__(self, now=1_800_000_000):
        self.now = now

    def __call__(self):
        return self.now


def row(identifier="019a0000-0000-7000-8000-000000000001", phase="working", **fields):
    return {"id": identifier, "home": ".codex", "title": "정산 자동화", "phase": phase,
            "updatedAt": fields.pop("updatedAt", 1_800_000_000), "snippet": "  마지막\n 응답  ", **fields}


class VapidTest(unittest.TestCase):
    @unittest.skipUnless(OPENSSL, "openssl is required")
    def test_es256_token_verifies_with_the_board_key_and_names_only_the_push_service(self):
        with tempfile.TemporaryDirectory() as directory:
            vapid = push.Vapid(directory, openssl=OPENSSL, clock=lambda: 1_800_000_000)
            header = vapid.authorization(APPLE + "/more")
            key = Path(directory) / "vapid.pem"
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            token, public = header[len("vapid t="):].split(", k=")
            self.assertEqual(public, vapid.public_key())
            point = unb64(public)
            self.assertEqual((len(point), point[0]), (65, 4))
            head, claims, signature = token.split(".")
            self.assertEqual(json.loads(unb64(head)), {"typ": "JWT", "alg": "ES256"})
            self.assertEqual(json.loads(unb64(claims)),
                             {"aud": "https://web.push.apple.com", "exp": 1_800_000_000 + 12 * 3600, "sub": push.SUBJECT})
            raw = unb64(signature)
            self.assertEqual(len(raw), 64)

            def integer(value):
                value = value.lstrip(b"\0") or b"\0"
                if value[0] & 0x80:
                    value = b"\0" + value
                return b"\x02" + bytes([len(value)]) + value
            body = integer(raw[:32]) + integer(raw[32:])
            der = b"\x30" + bytes([len(body)]) + body
            sig, pem = Path(directory) / "sig", Path(directory) / "pub.pem"
            sig.write_bytes(der)
            subprocess.run([OPENSSL, "ec", "-in", str(key), "-pubout", "-out", str(pem)], check=True, capture_output=True)
            done = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", str(pem), "-signature", str(sig)],
                                  input=(head + "." + claims).encode(), capture_output=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            # A cached token is reused for the same push service.
            self.assertEqual(vapid.authorization(APPLE + "/other"), header)

    def test_der_signature_pads_and_strips_integers(self):
        der = b"\x30\x08\x02\x02\x00\x81\x02\x02\x01\x02"
        self.assertEqual(push.der_signature(der), b"\0" * 31 + b"\x81" + b"\0" * 30 + b"\x01\x02")
        with self.assertRaises(ValueError):
            push.der_signature(b"\x31\x00")


class EndpointTest(unittest.TestCase):
    def test_only_https_push_services_are_accepted(self):
        for endpoint in (APPLE, FCM, "https://FCM.googleapis.com:443/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/abc",
                         "https://wns2-par02p.notify.windows.com/w/?token=abc", "https://api.push.apple.com/3/device/x"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(push.valid_endpoint(endpoint))
        for endpoint in ("http://fcm.googleapis.com/fcm/send/x", "https://fcm.googleapis.com.evil.test/x",
                         "https://evil.test/push.apple.com", "https://user:pw@fcm.googleapis.com/x",
                         "https://fcm.googleapis.com:8443/x", "https://fcm.googleapis.com/", "https://fcm.googleapis.com/x#a",
                         "https://127.0.0.1/x", "https://mini.example.ts.net/x", "https://fcm.googleapis.com/a b",
                         "https://fcm.googleapis.com/" + "a" * 3000, None, 5,
                         "https://[fcm.googleapis.com].127.0.0.1.nip.io/x", "https://[fcm.googleapis.com].localhost/x",
                         "https://@fcm.googleapis.com/x", "https://fcm.googleapis.com./x"):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(push.valid_endpoint(endpoint))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.board, self.clock, self.sent = FakeBoard(), Clock(), []
        self.categories = {}
        self.status = 201
        self.service = self.make()

    def tearDown(self):
        self.temp.cleanup()

    def make(self):
        def sender(endpoint, headers):
            self.sent.append((endpoint, headers))
            if isinstance(self.status, Exception):
                raise self.status
            return self.status, {"Retry-After": "120"}, b'{"reason":"BadDeviceToken"}'
        return push.PushService(self.board, self.temp.name, sender=sender, vapid=FakeVapid(), clock=self.clock,
                                monotonic=self.clock,
                                classify=lambda host, item: self.categories.get(item["id"], "general"))

    def subscribe(self, endpoint=APPLE, service=None):
        return (service or self.service).subscribe({"subscription": {"endpoint": endpoint, "keys": {"p256dh": "x", "auth": "y"}},
                                                    "label": "  iPhone \n 홈 화면  "})

    def test_subscriptions_are_private_validated_and_deduplicated(self):
        with self.assertRaisesRegex(ValueError, "알림 주소"):
            self.service.subscribe({"subscription": {"endpoint": "https://evil.test/x"}})
        first = self.subscribe()
        self.subscribe()
        saved = Path(self.temp.name) / "push/subscriptions.json"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        rows = json.loads(saved.read_text())
        self.assertEqual([(r["id"], r["label"]) for r in rows], [(first["id"], "iPhone 홈 화면")])
        self.assertNotIn("keys", rows[0])
        for number in range(push.MAX_SUBSCRIPTIONS - 1):
            self.subscribe(FCM + str(number))
        with self.assertRaisesRegex(ValueError, "너무 많습니다"):
            self.subscribe(FCM + "-overflow")
        self.service.unsubscribe({"endpoint": APPLE})
        self.assertEqual(len(self.service.subscriptions), push.MAX_SUBSCRIPTIONS - 1)
        # A restart reads the same set, and ignores a tampered row.
        rows = json.loads(saved.read_text()) + [{"id": "0" * 32, "endpoint": "https://evil.test/x"}]
        saved.write_text(json.dumps(rows))
        self.assertEqual(len(self.make().subscriptions), push.MAX_SUBSCRIPTIONS - 1)

    def test_first_view_is_a_baseline_then_needs_input_and_long_work_notify(self):
        self.subscribe()
        self.board.set(row(phase="needs_input"), row("019a0000-0000-7000-8000-000000000002", phase="working"))
        self.assertEqual(self.service.step(), [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.board.refreshes, 1)
        # A short run that stops is not worth an alert; the waiting session stays quiet.
        self.clock.now += 60
        self.board.set(row(phase="needs_input"), row("019a0000-0000-7000-8000-000000000002", phase="idle"))
        self.assertEqual(self.service.step(), [])
        # Working again, then waiting for an answer: one push for the round.
        self.board.set(row(phase="working"), row("019a0000-0000-7000-8000-000000000002", phase="working"))
        self.service.step()
        self.clock.now += push.MIN_WORK
        self.board.set(row(phase="needs_input", updatedAt=self.clock.now),
                       row("019a0000-0000-7000-8000-000000000002", phase="done", updatedAt=self.clock.now))
        events = self.service.step()
        self.assertEqual(sorted(event["kind"] for event in events), ["finished", "needs_input"])
        self.assertEqual(len(self.sent), 1)
        endpoint, headers = self.sent[0]
        self.assertEqual((endpoint, headers["TTL"], headers["Urgency"], headers["Content-Length"]), (APPLE, "86400", "high", "0"))
        pending = self.service.pending_for(self.service.subscriptions[0]["id"])["notifications"]
        self.assertEqual(len(pending), 2)
        waiting = next(item for item in pending if item["kind"] == "needs_input")
        self.assertEqual(waiting["title"], "정산 자동화")
        self.assertEqual(waiting["body"], "답이 필요해요 · 노트북 · Codex\n마지막 응답")
        self.assertEqual(waiting["url"], "/?session=laptop%7Ccodex%7C.codex%7C019a0000-0000-7000-8000-000000000001")
        self.assertEqual(waiting["tag"], "session:laptop|codex|.codex|019a0000-0000-7000-8000-000000000001")
        # The queue is read once; a repeat of the same phase sends nothing.
        self.assertEqual(self.service.pending_for(self.service.subscriptions[0]["id"]), {"notifications": []})
        self.service.step()
        self.assertEqual(len(self.sent), 1)

    def test_a_claude_row_without_an_id_notifies_under_the_lists_key(self):
        self.subscribe()
        claude = {"agent": "claude", "id": "", "kind": "interactive", "pid": 4242, "title": "정산 자동화",
                  "sessionId": "0f9c3a52-1b7e-4c1a-9d6e-5a4b3c2d1e0f", "cwd": "/work/project", "startedAt": 1_799_999_000}
        self.board.set({**claude, "phase": "working", "updatedAt": self.clock.now})
        self.service.step()
        self.board.set({**claude, "phase": "needs_input", "updatedAt": self.clock.now})
        self.assertEqual([event["kind"] for event in self.service.step()], ["needs_input"])
        notice = self.service.pending_for(self.service.subscriptions[0]["id"])["notifications"][0]
        # The same key web/app.js gives the card, so the notice opens it.
        self.assertEqual(notice["tag"], "session:laptop|claude||~4242:0f9c3a52-1b7e-4c1a-9d6e-5a4b3c2d1e0f:/work/project:1799999000")

    def test_agent_bot_one_shot_unknown_stale_and_offline_rows_never_notify(self):
        self.subscribe()
        ids = ["019a0000-0000-7000-8000-00000000000%d" % n for n in range(1, 7)]
        self.categories = {ids[0]: "agent", ids[1]: "bot", ids[2]: "unknown"}
        base = [row(ids[0]), row(ids[1]), row(ids[2]), row(ids[3], executionMode="one-shot"), row(ids[4]),
                row(ids[5], updatedAt=self.clock.now - push.FRESH - 1)]
        self.board.set(*base)
        self.service.step()
        self.clock.now += push.MIN_WORK + 1
        later = [dict(item, phase="needs_input", updatedAt=item["updatedAt"] if item["id"] == ids[5] else self.clock.now)
                 for item in base]
        later[4]["runtimeKnown"] = False
        self.board.set(*later)
        self.assertEqual(self.service.step(), [])
        self.board.set(row(ids[4], phase="working"), ok=False)
        self.service.step()
        self.board.set(row(ids[4], phase="needs_input", updatedAt=self.clock.now), ok=False)
        self.assertEqual(self.service.step(), [])
        self.assertEqual(self.sent, [])

    def test_a_restart_remembers_phases_and_only_new_changes_notify(self):
        self.subscribe()
        self.board.set(row(phase="working"))
        self.service.step()
        seen = Path(self.temp.name) / "push/seen.json"
        self.assertEqual(stat.S_IMODE(seen.stat().st_mode), 0o600)
        restarted = self.make()
        self.assertEqual(restarted.step(), [])
        self.clock.now += push.MIN_WORK
        self.board.set(row(phase="idle", updatedAt=self.clock.now))
        self.assertEqual([event["kind"] for event in restarted.step()], ["finished"])
        self.assertEqual(self.make().step(), [])

    def test_a_session_first_seen_waiting_notifies_once_the_host_was_looked_at(self):
        self.subscribe()
        self.board.set(row(phase="working"))
        self.service.step()
        newcomer = row("019a0000-0000-7000-8000-000000000009", phase="needs_input", startedAt=self.clock.now - 40)
        self.board.set(row(phase="working"), newcomer)
        self.assertEqual([event["kind"] for event in self.service.step()], ["needs_input"])
        # After a restart the first look at a host stays a baseline for new sessions.
        other = row("019a0000-0000-7000-8000-000000000010", phase="needs_input", startedAt=self.clock.now - 40)
        restarted = self.make()
        self.board.set(row(phase="working"), newcomer, other)
        self.assertEqual(restarted.step(), [])

    def test_work_length_ends_at_the_rows_last_update_not_at_the_look(self):
        self.subscribe()
        self.board.set(row(phase="working"))
        self.service.step()
        started = self.clock.now
        self.clock.now += 130
        # It stopped 30 seconds in; the look came much later.
        self.board.set(row(phase="idle", updatedAt=started + 30))
        self.assertEqual(self.service.step(), [])

    def test_unknown_runtime_keeps_the_last_phase_and_codex_exec_never_notifies(self):
        self.subscribe()
        self.board.set(row(phase="needs_input"), row("019a0000-0000-7000-8000-000000000011", phase="working", source="exec"))
        self.service.step()
        self.board.set(row(phase="needs_input", runtimeKnown=False))
        self.service.step()
        self.board.set(row(phase="needs_input"))
        self.assertEqual(self.service.step(), [], "the same question is not announced again")
        self.clock.now += push.MIN_WORK
        self.board.set(row(phase="needs_input"), row("019a0000-0000-7000-8000-000000000011", phase="idle", source="exec",
                                                     updatedAt=self.clock.now))
        self.assertEqual(self.service.step(), [])

    def test_a_passing_failure_is_retried_and_one_device_never_stops_the_others(self):
        import http.client
        self.subscribe(FCM)
        self.subscribe()
        calls = []

        def sender(endpoint, headers):
            calls.append(endpoint)
            if endpoint == FCM and len(calls) == 1:
                raise http.client.BadStatusLine("x")
            return 201, {}, b""
        self.service.sender = sender
        self.board.set(row(phase="working"))
        self.service.step()
        self.board.set(row(phase="needs_input", updatedAt=self.clock.now))
        self.service.step()
        self.assertEqual(calls, [FCM, APPLE], "the second device is woken despite the first one's error")
        self.service.step()
        self.assertEqual(len(calls), 2, "a retry waits RETRY seconds")
        self.clock.now += push.RETRY
        self.service.step()
        self.assertEqual(calls, [FCM, APPLE, FCM], "the failed device is woken again while its notice waits")
        self.clock.now += push.RETRY
        self.service.step()
        self.assertEqual(len(calls), 3, "a delivered wake-up is not repeated")

    def test_retries_back_off_stop_after_a_limit_and_old_notices_expire(self):
        self.subscribe()
        target = self.service.subscriptions[0]
        self.service._queue([target], {"tag": "t", "title": "x", "kind": "needs_input", "createdAt": self.clock.now})
        self.status = 503
        self.service._send(target, 60, "high")
        waits = []
        for _ in range(push.RETRY_LIMIT + 2):
            before = len(self.sent)
            for seconds in range(0, 4000, 15):
                self.clock.now += 15
                # Kept fresh here so the limit, not the hour-long expiry, ends the retries.
                for item in self.service.pending.get(target["id"], []):
                    item["createdAt"] = self.clock.now
                self.service.retry()
                if len(self.sent) > before:
                    waits.append(seconds + 15)
                    break
        self.assertEqual(waits[:3], [60, 120, 240], "waits double after each failure")
        self.assertEqual(len(self.sent), 1 + push.RETRY_LIMIT, "no wake-up after the limit")
        # A notice older than an hour is dropped instead of shown late.
        self.service.failed.clear()
        self.clock.now += push.PENDING_TTL + 1
        self.assertEqual(self.service.pending_for(target["id"]), {"notifications": []})

    def test_a_local_signing_error_is_not_retried(self):
        self.subscribe()
        target = self.service.subscriptions[0]

        class Broken(FakeVapid):
            def authorization(self, endpoint):
                raise RuntimeError("알림 서명 키를 준비하지 못했습니다.")
        self.service.vapid = Broken()
        self.service._queue([target], {"tag": "t", "title": "x", "createdAt": self.clock.now})
        self.assertEqual(self.service._send(target, 60, "high"), 0)
        self.clock.now += 10 * push.RETRY
        self.service.retry()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.service.failed[target["id"]][1])

    def test_a_redirect_is_never_followed(self):
        import http.server
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            do_GET = do_POST

            def log_message(self, *args):
                pass
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            status, _, _ = push.http_post("http://127.0.0.1:%d/start" % httpd.server_address[1], {"TTL": "1"}, timeout=5)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((status, hits), (302, ["/start"]))

    def test_no_subscription_means_no_background_collection(self):
        self.board.set(row(phase="working"))
        self.assertEqual(self.service.step(), [])
        self.assertEqual(self.board.refreshes, 0)
        self.subscribe()
        self.service.step()
        self.service.step()
        self.assertEqual(self.board.refreshes, 1)
        self.clock.now += self.board.interval
        self.service.step()
        self.assertEqual(self.board.refreshes, 2)

    def test_gone_rate_limited_and_failing_push_services(self):
        self.subscribe()
        identifier = self.service.subscriptions[0]["id"]
        self.status = 429
        self.assertEqual(self.service._send(self.service.subscriptions[0], 60, "high"), 429)
        self.assertEqual(self.service._send(self.service.subscriptions[0], 60, "high"), 429)
        self.assertEqual(len(self.sent), 1)
        self.clock.now += 121
        self.status = 400
        self.assertEqual(self.service._send(self.service.subscriptions[0], 60, "high"), 400)
        self.status = OSError("offline")
        self.assertEqual(self.service._send(self.service.subscriptions[0], 60, "high"), 0)
        self.status = 410
        self.service._queue(self.service.subscriptions, {"tag": "t", "title": "x"})
        self.assertEqual(self.service._send(self.service.subscriptions[0], 60, "high"), 410)
        self.assertEqual(self.service.subscriptions, [])
        self.assertEqual(self.service.pending_for(identifier), {"notifications": []})

    def test_test_push_needs_a_registered_device_and_a_delivered_message(self):
        with self.assertRaisesRegex(ValueError, "등록되어 있지 않습니다"):
            self.service.test({"endpoint": APPLE})
        self.subscribe()
        self.assertEqual(self.service.test({"endpoint": APPLE}), {"ok": True})
        self.assertEqual(self.service.pending_for(self.service.subscriptions[0]["id"])["notifications"][0]["tag"],
                         "notice-test")
        self.status = 403
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.service.test({"endpoint": APPLE})

    def test_pending_queue_is_bounded_per_device_and_replaces_a_tag(self):
        self.subscribe()
        target = self.service.subscriptions
        for number in range(push.MAX_PENDING + 5):
            self.service._queue(target, {"tag": "t%d" % number, "title": str(number)})
        self.service._queue(target, {"tag": "t%d" % (push.MAX_PENDING + 4), "title": "latest"})
        items = self.service.pending_for(target[0]["id"])["notifications"]
        self.assertEqual(len(items), push.MAX_PENDING)
        self.assertEqual(items[-1]["title"], "latest")
        self.assertEqual(len({item["tag"] for item in items}), push.MAX_PENDING)
        with self.assertRaises(ValueError):
            self.service.pending_for("../x")


class RouteTest(unittest.TestCase):
    TOKEN = "push-route-token"

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.board = server.Board([{"name": "local", "label": "로컬", "local": True}], 60,
                                 runner=lambda host, args, timeout: {"codex": [], "claude": [], "tmux": [], "errors": []})
        cls.httpd = server.make_server("127.0.0.1", 0, cls.board, cls.TOKEN, state_dir=cls.temp.name, push=False)
        # Enabled without its background watcher: the routes alone are exercised.
        cls.httpd.push.enabled = True
        cls.httpd.push.vapid = FakeVapid()
        cls.httpd.push.sender = lambda endpoint, headers: (201, {}, b"")
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data or b"{}")

    def session(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/login", json.dumps({"token": self.TOKEN}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        csrf = json.loads(response.read())["csrfToken"]
        connection.close()
        return response.getheader("Set-Cookie").split(";", 1)[0], csrf

    def test_a_board_run_with_no_push_says_so_and_takes_no_device(self):
        cookie, csrf = self.session()
        self.httpd.push.enabled = False
        try:
            status, body = self.request("GET", "/api/push", headers={"Cookie": cookie})
            self.assertEqual((status, body["available"]), (200, False))
            self.assertIn("--no-push", body["reason"])
            for path, payload in (("/api/push/subscribe", {"subscription": {"endpoint": APPLE}}), ("/api/push/test", {"endpoint": APPLE})):
                status, body = self.request("POST", path, json.dumps(payload),
                                            {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf})
                self.assertEqual(status, 409)
        finally:
            self.httpd.push.enabled = True

    def test_routes_need_login_csrf_and_the_service_worker_header(self):
        self.assertEqual(self.request("GET", "/api/push")[0], 401)
        cookie, csrf = self.session()
        status, body = self.request("GET", "/api/push", headers={"Cookie": cookie})
        self.assertEqual((status, body["available"], body["publicKey"]), (200, True, "BFixturePublicKey"))
        subscription = json.dumps({"subscription": {"endpoint": APPLE}, "label": "폰"})
        self.assertEqual(self.request("POST", "/api/push/subscribe", subscription,
                                      {"Cookie": cookie, "Content-Type": "application/json"})[0], 403)
        status, body = self.request("POST", "/api/push/subscribe", subscription,
                                    {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf})
        self.assertEqual((status, body["subscriptions"]), (200, 1))
        status, body = self.request("POST", "/api/push/subscribe", json.dumps({"subscription": {"endpoint": "https://evil.test/x"}}),
                                    {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/push/test", json.dumps({"endpoint": APPLE}),
                                 {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        pending = "/api/push/pending?id=" + push.subscription_id(APPLE)
        self.assertEqual(self.request("GET", pending, headers={"Cookie": cookie})[0], 403)
        # A HEAD never clears the queue it cannot show.
        self.assertEqual(self.request("HEAD", pending, headers={"Cookie": cookie, "X-Board-Push": "1"})[0], 405)
        status, body = self.request("GET", pending, headers={"Cookie": cookie, "X-Board-Push": "1"})
        self.assertEqual((status, [item["tag"] for item in body["notifications"]]), (200, ["notice-test"]))
        self.assertEqual(self.request("GET", "/api/push/pending?id=bad", headers={"Cookie": cookie, "X-Board-Push": "1"})[0], 400)
        status, body = self.request("POST", "/api/push/unsubscribe", json.dumps({"endpoint": APPLE}),
                                    {"Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": csrf})
        self.assertEqual((status, body["subscriptions"]), (200, 0))


if __name__ == "__main__":
    unittest.main()
