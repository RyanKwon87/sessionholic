import http.client
import io
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402

TOKEN = "test-token-123"
HOSTS = [{"name": "remote", "label": "원격 기기", "local": False, "ssh": "remote", "python": "/usr/bin/python3"}]


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


def fake_runner(calls):
    def run(host, args, timeout):
        calls.append((host["name"], list(args)))
        if args[0] == "snapshot":
            return {"host": "remote", "claude": [{"id": "a1b2c3d4", "title": "정산"}], "codex": [], "tmux": [], "errors": []}
        return {"messages": [{"role": "user", "text": "hi"}]}
    return run


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.board = server.Board(HOSTS, 60, runner=fake_runner(cls.calls))
        cls.board.poll(HOSTS[0])
        cls.temp = tempfile.TemporaryDirectory()
        cls.httpd = server.make_server("127.0.0.1", 0, cls.board, TOKEN, state_dir=cls.temp.name)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def login_cookie(self):
        res, _ = self.request("POST", "/login", json.dumps({"token": TOKEN}), {"Content-Type": "application/json"})
        self.assertEqual(res.status, 200)
        return res.getheader("Set-Cookie").split(";", 1)[0]

    def test_api_requires_login(self):
        res, _ = self.request("GET", "/api/snapshot")
        self.assertEqual(res.status, 401)

    def test_wrong_token_is_rejected(self):
        res, _ = self.request("POST", "/login", json.dumps({"token": "nope"}), {"Content-Type": "application/json"})
        self.assertEqual(res.status, 403)
        self.assertIsNone(res.getheader("Set-Cookie"))

    def test_login_cookie_is_http_only_and_strict(self):
        res, _ = self.request("POST", "/login", json.dumps({"token": TOKEN}), {"Content-Type": "application/json"})
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Secure", cookie)
        res, _ = self.request("POST", "/login", json.dumps({"token": TOKEN}),
                              {"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
        self.assertIn("Secure", res.getheader("Set-Cookie"))

    def test_snapshot_and_read_with_cookie(self):
        cookie = self.login_cookie()
        res, data = self.request("GET", "/api/snapshot", headers={"Cookie": cookie})
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        snap = json.loads(data)
        self.assertEqual(snap["hosts"][0]["data"]["claude"][0]["title"], "정산")
        res, data = self.request("GET", "/api/read?host=remote&agent=claude&id=a1b2c3d4-0000", headers={"Cookie": cookie})
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(data)["messages"][0]["text"], "hi")
        self.assertIn(("remote", ["read", "claude", "a1b2c3d4-0000"]), self.calls)

    def test_read_validates_input(self):
        cookie = self.login_cookie()
        for query in ("host=other&agent=claude&id=a1b2c3d4", "host=remote&agent=claude&id=../../x",
                      "host=remote&agent=codex&id=a1b2c3d4&home=../x", "host=remote&agent=sh&id=a1b2c3d4"):
            res, _ = self.request("GET", "/api/read?" + query, headers={"Cookie": cookie})
            self.assertEqual(res.status, 400, query)

    def test_bearer_header_also_works(self):
        res, _ = self.request("GET", "/api/snapshot", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(res.status, 200)

    def test_one_time_login_link(self):
        self.httpd.once = ("code123", time.time() + 60)
        res, _ = self.request("GET", "/login?once=code123")
        self.assertEqual(res.status, 303)
        self.assertIn("HttpOnly", res.getheader("Set-Cookie"))
        res, _ = self.request("GET", "/login?once=code123")
        self.assertEqual(res.status, 403)

    def csrf_session(self):
        res, data = self.request("POST", "/login", json.dumps({"token": TOKEN}), {"Content-Type": "application/json"})
        return res.getheader("Set-Cookie").split(";", 1)[0], json.loads(data)["csrfToken"]

    def post_json(self, path, body, cookie, csrf):
        return self.request("POST", path, json.dumps(body), {"Content-Type": "application/json",
                                                            "Cookie": cookie, "X-CSRF-Token": csrf})

    def test_unexpected_errors_return_generic_json_500(self):
        secret = "fixture-private-" + "detail"
        cookie, csrf = self.csrf_session()
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            with patch.object(self.board, "snapshot", side_effect=TypeError(secret)):
                res, data = self.request("GET", "/api/snapshot", headers={"Cookie": cookie})
            self.assertEqual(res.status, 500)
            self.assertEqual(json.loads(data), {"error": "서버 내부 오류가 발생했습니다. 다시 시도해 주세요."})
            # A send that failed unexpectedly may already have started: no dispatchState.
            with patch.object(self.httpd.chat, "send", side_effect=TypeError(secret)):
                res, data = self.post_json("/api/chat/send", {"source": {}}, cookie, csrf)
            self.assertEqual(res.status, 500)
            self.assertEqual(json.loads(data), {"error": server.INTERNAL_ERROR})
        self.assertIn("TypeError", log.getvalue())
        self.assertNotIn(secret, log.getvalue())
        self.assertNotIn(secret.encode(), data)
        res, _ = self.request("GET", "/api/snapshot", headers={"Cookie": cookie})
        self.assertEqual(res.status, 200)

    def test_snapshots_name_the_server_process_and_a_restart_names_another(self):
        # H3: a client that sees another bootId reads /api/capabilities (its CSRF token) again.
        cookie, csrf = self.csrf_session()
        res, data = self.request("GET", "/api/snapshot", headers={"Cookie": cookie})
        boot = json.loads(data)["bootId"]
        self.assertEqual(boot, self.httpd.boot_id)
        self.assertRegex(boot, r"^[A-Za-z0-9_-]{16}$")
        res, data = self.post_json("/api/refresh", {}, cookie, csrf)
        self.assertEqual((res.status, json.loads(data)["bootId"]), (200, boot))
        with tempfile.TemporaryDirectory() as directory:
            restarted = server.make_server("127.0.0.1", 0, self.board, TOKEN, state_dir=directory)
            try:
                self.assertNotEqual(restarted.boot_id, boot)
                self.assertNotEqual(restarted.csrf, self.httpd.csrf)
            finally:
                restarted.server_close()

    def test_refresh_host_must_be_a_string(self):
        cookie, csrf = self.csrf_session()
        for host in (["remote"], 5, {"name": "remote"}):
            res, data = self.post_json("/api/refresh", {"host": host}, cookie, csrf)
            self.assertEqual(res.status, 400, host)
            self.assertIn("error", json.loads(data))

    def test_non_ascii_secrets_are_rejected_not_errors(self):
        cookie, _ = self.csrf_session()
        res, _ = self.request("POST", "/api/refresh", json.dumps({}), {
            "Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": "é"})
        self.assertEqual(res.status, 403)
        self.httpd.once = ("code123", time.time() + 60)
        res, _ = self.request("GET", "/login?once=%C3%A9")
        self.assertEqual(res.status, 403)
        self.httpd.once = ("", 0)

    def test_issued_login_link_is_single_use_and_short_lived(self):
        link = urlparse(server.issue_login_link(self.httpd))
        self.assertEqual((link.scheme, link.hostname, link.port, link.path), ("http", "127.0.0.1", self.port, "/login"))
        code, expires = self.httpd.once
        self.assertEqual(link.query, "once=" + code)
        self.assertGreaterEqual(len(code), 32)
        self.assertTrue(time.time() < expires <= time.time() + server.ONCE_TTL <= time.time() + 300)
        res, _ = self.request("GET", "/login?" + link.query)
        self.assertEqual(res.status, 303)
        self.assertIn("HttpOnly", res.getheader("Set-Cookie"))
        res, _ = self.request("GET", "/login?" + link.query)
        self.assertEqual(res.status, 403)
        expired = urlparse(server.issue_login_link(self.httpd))
        self.httpd.once = (self.httpd.once[0], time.time() - 1)
        res, _ = self.request("GET", "/login?" + expired.query)
        self.assertEqual(res.status, 403)
        self.assertNotEqual(expired.query, link.query)

    def test_static_files_and_traversal(self):
        res, data = self.request("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn("default-src 'self'", res.getheader("Content-Security-Policy"))
        self.assertIn(b"<title>", data)
        for path in ("/../server.py", "/%2e%2e/server.py", "/server.py", "/web/app.js"):
            res, _ = self.request("GET", path)
            self.assertEqual(res.status, 404, path)

    def test_foreign_host_header_is_refused(self):
        res, _ = self.request("GET", "/", headers={"Host": "evil.example:8790"})
        self.assertEqual(res.status, 421)
        res, _ = self.request("GET", "/", headers={"Host": "remote.tail-example.ts.net"})
        self.assertEqual(res.status, 200)

    def test_csp_allows_blob_images_only_in_img_src(self):
        res, _ = self.request("GET", "/")
        policy = dict(part.strip().split(" ", 1) for part in res.getheader("Content-Security-Policy").split(";"))
        self.assertEqual(policy["img-src"], "'self' data: blob:")
        self.assertEqual(policy["default-src"], "'self'")
        self.assertNotIn("blob:", policy["script-src"])

    def test_json_is_gzip_compressed_when_accepted_and_large(self):
        import gzip
        large = {"hosts": [{"name": "remote", "rows": ["정산 작업 %d" % index for index in range(200)]}]}
        with patch.object(self.board, "snapshot", return_value=large):
            res, data = self.request("GET", "/api/snapshot", headers={
                "Authorization": "Bearer " + TOKEN, "Accept-Encoding": "br, gzip;q=0.8"})
            self.assertEqual((res.status, res.getheader("Content-Encoding")), (200, "gzip"))
            self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertEqual(int(res.getheader("Content-Length")), len(data))
            served = {**large, "sessionLabels": {"ok": True, "revision": 0}, "bootId": self.httpd.boot_id}
            self.assertEqual(json.loads(gzip.decompress(data)), served)
            for header in ({}, {"Accept-Encoding": "gzip;q=0"}, {"Accept-Encoding": "identity"}):
                res, data = self.request("GET", "/api/snapshot", headers={"Authorization": "Bearer " + TOKEN, **header})
                self.assertIsNone(res.getheader("Content-Encoding"), header)
                self.assertEqual(json.loads(data), served)
        # Small replies stay plain even for gzip clients.
        res, data = self.request("GET", "/api/snapshot", headers={
            "Authorization": "Bearer " + TOKEN, "Accept-Encoding": "gzip"})
        self.assertIsNone(res.getheader("Content-Encoding"))
        self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
        self.assertIn("version", json.loads(data))


WEB_INDEX = ('<!doctype html><html><head><meta charset="utf-8">\n'
             '<meta\n  name="sessionholic-assets"\n  content="{}"\n/>\n'
             '<link rel="manifest" href="/manifest.webmanifest" />\n'
             '<link rel="stylesheet" href="/vendor/x.css" />\n'
             '<script src="/app.js" defer></script>\n<script src="/missing.js" defer></script>\n'
             '<a href="/sw.js">worker</a> <a href="/app.js?keep=1">query</a> <a href="/">home</a>\n'
             '</head><body>' + '<p>세션홀릭 고정 문단</p>\n' * 60 + '</body></html>\n')


class StaticAssetTest(unittest.TestCase):
    """Versioned, compressed and revalidated public assets from a fixture web folder."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.web = Path(cls.temp.name) / "web"
        (cls.web / "vendor").mkdir(parents=True)
        (cls.web / "index.html").write_text(WEB_INDEX)
        (cls.web / "app.js").write_text("console.log('세션홀릭');\n" * 200)
        (cls.web / "sw.js").write_text("self.addEventListener('fetch', () => {});\n" * 40)
        (cls.web / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (cls.web / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(4096))
        (cls.web / "manifest.webmanifest").write_text('{"name":"세션홀릭"}')
        (cls.web / "vendor/x.css").write_text("body{margin:0}\n")
        (cls.web / "notes.txt").write_text("not served")
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        cls.httpd = server.make_server("127.0.0.1", 0, board, TOKEN, state_dir=cls.temp.name)
        cls.httpd.assets = server.StaticAssets(cls.web)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.temp.cleanup()

    def get(self, path, method="GET", **headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    @staticmethod
    def sha(data):
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def test_a_page_load_burst_of_connections_is_never_reset(self):
        # Every connection waits for the board before any is answered, as a page load through
        # Serve does: a backlog of 5 would reset the rest.
        import socket
        with tempfile.TemporaryDirectory() as directory:
            httpd = server.make_server("127.0.0.1", 0, server.Board(HOSTS, 60, runner=fake_runner([])), TOKEN,
                                       state_dir=directory)
            httpd.assets = server.StaticAssets(self.web)
            port, clients, answers, serving = httpd.server_address[1], [], [], False
            try:
                for _ in range(48):
                    try:
                        clients.append(socket.create_connection(("127.0.0.1", port), timeout=5))
                    except OSError as exc:
                        answers.append(type(exc).__name__.encode())
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                serving = True
                for client in clients:
                    answer = b""
                    try:
                        client.sendall(b"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
                        while True:
                            chunk = client.recv(65536)
                            if not chunk:
                                break
                            answer += chunk
                    except OSError as exc:
                        answer = type(exc).__name__.encode()
                    answers.append(answer.split(b"\r\n", 1)[0])
            finally:
                for client in clients:
                    client.close()
                if serving:
                    httpd.shutdown()
                httpd.server_close()
        self.assertEqual(answers, [b"HTTP/1.0 200 OK"] * 48)

    def test_gzip_etag_and_vary(self):
        import gzip
        raw = (self.web / "app.js").read_bytes()
        res, data = self.get("/app.js", **{"Accept-Encoding": "gzip, deflate"})
        self.assertEqual((res.status, res.getheader("Content-Encoding")), (200, "gzip"))
        self.assertEqual(gzip.decompress(data), raw)
        self.assertEqual(res.getheader("ETag"), '"%s-gz"' % self.sha(raw)[:16])
        self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
        self.assertEqual(res.getheader("Content-Type"), "text/javascript; charset=utf-8")
        self.assertEqual(int(res.getheader("Content-Length")), len(data))
        res, data = self.get("/app.js")
        self.assertEqual((res.getheader("Content-Encoding"), data), (None, raw))
        self.assertEqual(res.getheader("ETag"), '"%s"' % self.sha(raw)[:16])
        self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
        # Small files and images are never compressed, but still vary by encoding.
        for path in ("/icon.svg", "/icon.png", "/vendor/x.css"):
            res, data = self.get(path, **{"Accept-Encoding": "gzip"})
            self.assertIsNone(res.getheader("Content-Encoding"), path)
            self.assertEqual(data, (self.web / path[1:]).read_bytes())
            self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
        res, _ = self.get("/app.js", method="HEAD", **{"Accept-Encoding": "gzip"})
        self.assertEqual((res.status, res.getheader("Content-Encoding")), (200, "gzip"))

    def test_if_none_match_returns_304_with_cache_headers(self):
        raw = (self.web / "app.js").read_bytes()
        tag = self.sha(raw)[:16]
        res, data = self.get("/app.js?v=" + tag[:8], **{"Accept-Encoding": "gzip", "If-None-Match": '"%s-gz"' % tag})
        self.assertEqual((res.status, data), (304, b""))
        self.assertEqual(res.getheader("ETag"), '"%s-gz"' % tag)
        self.assertEqual(res.getheader("Cache-Control"), server.IMMUTABLE)
        self.assertEqual(res.getheader("Vary"), "Accept-Encoding")
        res, data = self.get("/app.js", **{"If-None-Match": 'W/"other", "%s"' % tag})
        self.assertEqual((res.status, data, res.getheader("Cache-Control")), (304, b"", "no-cache"))
        # The plain tag does not validate the compressed body, nor the reverse.
        res, data = self.get("/app.js", **{"Accept-Encoding": "gzip", "If-None-Match": '"%s"' % tag})
        self.assertEqual(res.status, 200)
        res, _ = self.get("/app.js", **{"If-None-Match": '"%s-gz"' % tag})
        self.assertEqual(res.status, 200)

    def test_only_the_current_hash_is_immutable(self):
        raw = (self.web / "app.js").read_bytes()
        res, _ = self.get("/app.js?v=" + self.sha(raw)[:8])
        self.assertEqual(res.getheader("Cache-Control"), "public, max-age=31536000, immutable")
        for path in ("/app.js", "/app.js?v=00000000", "/app.js?v="):
            res, _ = self.get(path)
            self.assertEqual(res.getheader("Cache-Control"), "no-cache", path)
        sw = (self.web / "sw.js").read_bytes()
        res, _ = self.get("/sw.js?v=" + self.sha(sw)[:8])
        self.assertEqual(res.getheader("Cache-Control"), "no-cache")
        self.assertEqual(res.getheader("Service-Worker-Allowed"), "/")
        page = self.get("/")[1]
        res, _ = self.get("/index.html?v=" + self.sha(page)[:8])
        self.assertEqual(res.getheader("Cache-Control"), "no-cache")
        for path in ("/notes.txt", "/missing.js", "/vendor/../app.js", "/%2e%2e/server.py"):
            self.assertEqual(self.get(path)[0].status, 404, path)

    def test_index_is_rendered_with_asset_versions(self):
        import html
        import re
        res, data = self.get("/")
        page = data.decode()
        self.assertEqual(res.getheader("Cache-Control"), "no-cache")
        expected = {"/" + name: self.sha((self.web / name).read_bytes())[:8]
                    for name in ("app.js", "icon.png", "icon.svg", "index.html", "manifest.webmanifest",
                                 "sw.js", "vendor/x.css")}
        content = re.search(r'name="sessionholic-assets"\s+content="([^"]*)"', page).group(1)
        self.assertEqual(json.loads(html.unescape(content)), expected)
        self.assertIn('<script src="/app.js?v=%s" defer>' % expected["/app.js"], page)
        self.assertIn('href="/vendor/x.css?v=%s"' % expected["/vendor/x.css"], page)
        self.assertIn('href="/manifest.webmanifest?v=%s"' % expected["/manifest.webmanifest"], page)
        for kept in ('src="/missing.js"', 'href="/sw.js"', 'href="/app.js?keep=1"', 'href="/"'):
            self.assertIn(kept, page)
        # The page's validator covers the rendered bytes, which change with any asset.
        self.assertEqual(res.getheader("ETag"), '"%s"' % self.sha(data)[:16])
        self.assertEqual(self.get("/index.html")[1], data)
        res, packed = self.get("/", **{"Accept-Encoding": "gzip"})
        self.assertEqual(res.getheader("Content-Encoding"), "gzip")
        self.assertEqual(self.get("/", **{"If-None-Match": '"%s"' % self.sha(data)[:16]})[0].status, 304)

    def test_changed_asset_updates_cache_and_page_validator(self):
        import hashlib
        path = self.web / "vendor/x.css"
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        first = self.get("/")[1]
        entry = self.httpd.assets.files["vendor/x.css"][1]
        self.get("/vendor/x.css")
        self.assertIs(self.httpd.assets.files["vendor/x.css"][1], entry, "unchanged files are served from memory")
        path.write_bytes(b"body{margin:1px}\n")
        stamp = path.stat().st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
        res, data = self.get("/vendor/x.css")
        self.assertEqual(data, b"body{margin:1px}\n")
        second = self.get("/", **{"If-None-Match": '"%s"' % hashlib.sha256(first).hexdigest()[:16]})
        self.assertEqual(second[0].status, 200)
        self.assertIn('href="/vendor/x.css?v=%s"' % hashlib.sha256(data).hexdigest()[:8], second[1].decode())

    def test_index_without_asset_meta_still_gets_versioned_references(self):
        index = self.web / "index.html"
        self.addCleanup(index.write_text, WEB_INDEX)
        index.write_text('<!doctype html><script src="/app.js"></script>')
        page = self.get("/")[1].decode()
        self.assertRegex(page, r'^<!doctype html><script src="/app\.js\?v=[0-9a-f]{8}"></script>$')


class PublishedWebTreeTest(unittest.TestCase):
    """The shipped web/ folder, not a fixture: every page reference is servable."""

    def test_page_references_are_static_assets_and_theme_init_runs_first(self):
        import hashlib
        import re
        root = Path(__file__).resolve().parents[1] / "web"
        page = (root / "index.html").read_text(encoding="utf-8")
        refs = re.findall(r'(?:src|href)="/([^"?#]+)"', page)
        self.assertIn("theme-init.js", refs)
        for name in refs:
            self.assertTrue(server.STATIC.fullmatch(name), name)
            self.assertTrue((root / name).is_file(), name)
        rendered = server.StaticAssets(root).index()["body"].decode("utf-8")
        sha = hashlib.sha256((root / "theme-init.js").read_bytes()).hexdigest()[:8]
        # Loaded without defer, before the stylesheet, so the stored theme applies before paint.
        self.assertIn('<script src="/theme-init.js?v=%s"></script>' % sha, rendered)
        self.assertLess(rendered.index("/theme-init.js?v="), rendered.index("/style.css?v="))


class BoardVersionTest(unittest.TestCase):
    def test_version_rises_only_when_published_state_changes(self):
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        board.clock = lambda: 1_000_000.0
        seen = []
        board.add_listener(seen.append)
        self.assertEqual(board.snapshot()["version"], 0)
        self.assertTrue(board.poll(HOSTS[0]))
        # refreshing on, then the collected result.
        self.assertEqual(board.snapshot()["version"], 2)
        self.assertEqual(seen, [1, 2])
        board.snapshot()
        self.assertFalse(board.request_refresh())
        self.assertEqual((board.version, seen), (2, [1, 2]))
        board.runner = lambda host, args, timeout: (_ for _ in ()).throw(RuntimeError("SSH 연결 실패"))
        self.assertFalse(board.poll(HOSTS[0]))
        self.assertEqual(board.version, 4)
        self.assertEqual(seen, [1, 2, 3, 4])
        board.remove_listener(seen.append)

    def test_failing_listener_does_not_break_collection(self):
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        def broken(version):
            raise ValueError("fixture")
        board.add_listener(broken)
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertTrue(board.poll(HOSTS[0]))
        self.assertTrue(board.state["remote"]["ok"])
        self.assertIn("ValueError", log.getvalue())


class EventStreamTest(unittest.TestCase):
    """/api/events over real loopback sockets."""
    SOURCE = "host=remote&agent=claude&id=a1b2c3d4"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = {"claude": [{"id": "a1b2c3d4", "title": "정산", "phase": "idle"}], "codex": []}
        self.board = server.Board(HOSTS, 60, runner=lambda host, args, timeout: json.loads(json.dumps(self.data)))
        self.board.poll(HOSTS[0])
        self.httpd = server.make_server("127.0.0.1", 0, self.board, TOKEN, state_dir=self.temp.name)
        hub = self.httpd.events
        hub.heartbeat, hub.peer_check, hub.grace = 0.2, 0.05, 0.2
        hub.fast, hub.idle, hub.error = 0.05, (0.05,), 0.05
        self.chat_state = {"phase": "working", "messages": [{"role": "user", "text": "hi"}]}
        self.reads, self.watching = [], []
        def state(data, watching=False):
            self.reads.append(data["source"])
            self.watching.append(watching)
            return json.loads(json.dumps(self.chat_state))
        patcher = patch.object(self.httpd.chat, "state", side_effect=state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sockets = []
        self.closed = False
        self.addCleanup(self.stop)

    def stop(self):
        for sock in self.sockets:
            sock.close()
        if not self.closed:
            self.httpd.shutdown()
            self.httpd.server_close()

    def open(self, query="", method="GET", auth=True, extra=""):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.sockets.append(sock)
        headers = "Host: 127.0.0.1\r\nAccept-Encoding: gzip\r\n" + extra
        if auth:
            headers += "Authorization: Bearer %s\r\n" % TOKEN
        sock.sendall(("%s /api/events%s HTTP/1.1\r\n%s\r\n" % (method, "?" + query if query else "", headers)).encode())
        return sock

    @staticmethod
    def read_until(sock, marker, received=b"", timeout=5):
        deadline = time.monotonic() + timeout
        while marker not in received:
            if time.monotonic() > deadline:
                raise AssertionError("missing %r in %r" % (marker, received[-300:]))
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError("stream closed before %r: %r" % (marker, received[-300:]))
            received += chunk
        return received

    @staticmethod
    def head(received):
        head = received.split(b"\r\n\r\n", 1)[0].decode()
        lines = head.split("\r\n")
        return lines[0], {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in lines[1:]}

    def refusal(self, sock):
        """Status code, Retry-After and JSON error code of a plain (non-stream) answer."""
        received = self.read_until(sock, b"\r\n\r\n")
        status, headers = self.head(received)
        body = received.split(b"\r\n\r\n", 1)[1]
        while len(body) < int(headers["content-length"]):
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertNotIn("content-encoding", headers)
        return int(status.split()[1]), headers.get("retry-after"), json.loads(body).get("code")

    def test_stream_headers_hello_heartbeat_and_snapshot_event(self):
        sock = self.open()
        received = self.read_until(sock, b": ping\n\n")
        status, headers = self.head(received)
        self.assertIn(" 200 ", status)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-accel-buffering"], "no")
        self.assertNotIn("content-length", headers)
        self.assertNotIn("content-encoding", headers)
        body = received.split(b"\r\n\r\n", 1)[1]
        version = self.board.version
        self.assertTrue(body.startswith(b'retry: 3000\n\nevent: hello\ndata: {"snapshotVersion":%d,"bootId":"%s",'
                                        b'"terminalsVersion":%d}\n\n'
                                        % (version, self.httpd.boot_id.encode(), self.httpd.events.terminals_version)))
        self.data = {"claude": [{"id": "a1b2c3d4", "title": "정산 완료", "phase": "done"}], "codex": []}
        self.board.poll(HOSTS[0])
        received = self.read_until(sock, b'event: snapshot\ndata: {"version":%d}\n\n' % self.board.version, received)
        res = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        res.request("GET", "/api/snapshot", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(json.loads(res.getresponse().read())["version"], self.board.version)
        res.close()

    def test_chat_subscription_announces_digest_changes(self):
        sock = self.open(self.SOURCE)
        received = self.read_until(sock, b"event: chat\n")
        # The watcher reads the server's identity of the listed row, as a watcher read.
        self.assertEqual(self.reads[0], {"host": "remote", "agent": "claude", "home": None, "id": "a1b2c3d4"})
        self.assertTrue(all(self.watching))
        first = received.split(b"event: chat\ndata: ", 1)[1].split(b"\n", 1)[0]
        self.chat_state = {"phase": "idle", "messages": [{"role": "assistant", "text": "끝"}]}
        received = self.read_until(sock, b"event: chat\n", received.split(b"event: chat\n", 1)[1])
        second = received.split(b"event: chat\ndata: ", 1)[1].split(b"\n", 1)[0]
        self.assertNotEqual(json.loads(first)["digest"], json.loads(second)["digest"])
        self.assertEqual(len(self.httpd.events.watchers), 1)

    def test_login_source_validation_and_head(self):
        sock = self.open(auth=False)
        self.assertIn(b" 401 ", self.read_until(sock, b"\r\n\r\n"))
        for query in ("host=remote&agent=sh&id=a1b2c3d4", "host=other&agent=claude&id=a1b2c3d4",
                      "host=remote&agent=claude&id=ffffffff"):
            sock = self.open(query)
            received = self.read_until(sock, b"\r\n\r\n")
            self.assertIn(b" 400 ", received, query)
        sock = self.open(method="HEAD")
        received = self.read_until(sock, b"\r\n\r\n")
        self.assertIn(b"text/event-stream", received)
        wait_for(lambda: not self.httpd.events.streams)
        self.assertEqual(self.reads, [])

    def test_spellings_of_one_conversation_share_one_watcher(self):
        identity = {"host": "remote", "agent": "claude", "home": None, "id": "a1b2c3d4"}
        socks = [self.open(self.SOURCE + extra) for extra in ("", "&home=.claude", "&home=other")]
        for sock in socks:
            self.read_until(sock, b"event: hello")
        from events import watch_key
        self.assertEqual(list(self.httpd.events.watchers), [watch_key(identity)])
        wait_for(lambda: self.reads)
        self.assertTrue(all(read == identity for read in self.reads))
        # A send naming the row with a stray home speeds up that same watcher.
        with patch.object(self.httpd.events, "boost", wraps=self.httpd.events.boost) as boost, \
             patch.object(self.httpd.chat, "send", return_value={"status": "accepted"}):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/api/chat/send", json.dumps({"source": {**identity, "home": ".claude"},
                         "requestId": "request-0001", "text": "hi"}),
                         {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN,
                          "X-CSRF-Token": self.httpd.csrf})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            boost.assert_called_once_with(identity)

    def test_watcher_cap_counts_grace_watchers(self):
        self.data = {"claude": [{"id": "a1b2c3d4", "title": "정산", "phase": "idle"},
                                {"id": "e5f6a7b8", "title": "정리", "phase": "idle"}], "codex": []}
        self.board.poll(HOSTS[0])
        hub = self.httpd.events
        hub.max_watchers, hub.grace = 1, 30.0
        first = self.open(self.SOURCE)
        self.read_until(first, b"event: hello")
        other = "host=remote&agent=claude&id=e5f6a7b8"
        # Only a full hub is "busy": the deploy check accepts nothing else as saturation.
        self.assertEqual(self.refusal(self.open(other)), (503, "10", "events_busy"))
        self.assertEqual(len(hub.watchers), 1)
        # Left but still in its grace period: the next conversation takes its place.
        first.close()
        wait_for(lambda: not hub.streams)
        self.assertEqual(len(hub.watchers), 1)
        self.read_until(self.open(other), b"event: hello")
        from events import watch_key
        self.assertEqual(list(hub.watchers), [watch_key({"host": "remote", "agent": "claude", "id": "e5f6a7b8"})])
        # A source the board cannot resolve right now is refused without a watcher.
        with patch.object(self.httpd.chat, "resolve", side_effect=RuntimeError("fixture")):
            self.assertEqual(self.refusal(self.open(self.SOURCE)), (503, "10", "events_unavailable"))
        self.assertEqual(len(hub.watchers), 1)

    def test_connection_limit_and_departed_clients(self):
        self.httpd.events.max_streams = 2
        first, second = self.open(), self.open()
        for sock in (first, second):
            self.read_until(sock, b"event: hello")
        third = self.open()
        self.assertEqual(self.refusal(third), (503, "10", "events_busy"))
        # A client that leaves frees its slot without waiting for a heartbeat write.
        first.close()
        wait_for(lambda: len(self.httpd.events.streams) == 1)
        self.read_until(self.open(), b"event: hello")

    def test_failures_and_shutdown_are_not_reported_as_busy(self):
        hub = self.httpd.events
        # A watcher thread that cannot start is a failure, not a full hub.
        with patch.object(hub, "open", side_effect=RuntimeError("can't start new thread")):
            self.assertEqual(self.refusal(self.open(self.SOURCE)), (503, "10", "events_unavailable"))
        self.read_until(self.open(), b"event: hello")
        # A hub closing with the server is unavailable too.
        hub.close()
        self.assertEqual(self.refusal(self.open()), (503, "10", "events_unavailable"))

    def test_successful_send_and_menu_answer_speed_up_the_watcher(self):
        from chat import SendRejected
        source = {"host": "remote", "agent": "claude", "id": "a1b2c3d4"}
        def post(path, body):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json",
                         "Authorization": "Bearer " + TOKEN, "X-CSRF-Token": self.httpd.csrf})
            res = conn.getresponse()
            res.read()
            conn.close()
            return res.status
        identity = {**source, "home": None}
        with patch.object(self.httpd.events, "boost") as boost, \
             patch.object(self.httpd.chat, "send", return_value={"requestId": "r", "status": "accepted"}), \
             patch.object(self.httpd.chat, "prompt", return_value={"ok": True}):
            self.assertEqual(post("/api/chat/send", {"source": source, "requestId": "r", "text": "hi"}), 200)
            # The watcher is found by the server's identity of the row, not the posted spelling.
            boost.assert_called_once_with(identity)
            self.assertEqual(post("/api/chat/prompt", {"source": source, "action": "open"}), 200)
            self.assertEqual(boost.call_count, 1, "opening a menu is not an answer")
            self.assertEqual(post("/api/chat/prompt", {"source": source, "action": "answer"}), 200)
            self.assertEqual(boost.call_count, 2)
            # A row no longer listed has no watcher to speed up; the send still succeeded.
            self.assertEqual(post("/api/chat/send", {"source": {**source, "id": "ffffffff"},
                                                     "requestId": "r", "text": "hi"}), 200)
            self.assertEqual(boost.call_count, 2)
        with patch.object(self.httpd.events, "boost") as boost, \
             patch.object(self.httpd.chat, "send", side_effect=SendRejected("fixture")):
            self.assertEqual(post("/api/chat/send", {"source": source, "requestId": "r", "text": "hi"}), 400)
            boost.assert_not_called()
        # The refusal's code reaches the composer, so "터미널 열기" can appear at once.
        def refusal(body):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/api/chat/send", json.dumps(body), {"Content-Type": "application/json",
                         "Authorization": "Bearer " + TOKEN, "X-CSRF-Token": self.httpd.csrf})
            res = conn.getresponse()
            data = json.loads(res.read())
            conn.close()
            return res.status, data
        with patch.object(self.httpd.chat, "send",
                          side_effect=SendRejected("보내려면 먼저 터미널을 여세요.", 409, code="terminal_required")):
            self.assertEqual(refusal({"source": source, "requestId": "r", "text": "hi"}),
                             (409, {"error": "보내려면 먼저 터미널을 여세요.", "dispatchState": "not_started",
                                    "code": "terminal_required"}))
        with patch.object(self.httpd.chat, "send", side_effect=SendRejected("fixture")):
            self.assertEqual(refusal({"source": source, "requestId": "r", "text": "hi"}),
                             (400, {"error": "fixture", "dispatchState": "not_started"}))
        # A failing hint never turns a delivered message into an error.
        with patch.object(self.httpd.events, "boost", side_effect=TypeError("fixture")), \
             patch.object(self.httpd.chat, "send", return_value={"status": "accepted"}), \
             patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(post("/api/chat/send", {"source": source, "requestId": "r", "text": "hi"}), 200)

    def test_server_close_ends_open_streams(self):
        sock = self.open(self.SOURCE)
        self.read_until(sock, b"event: hello")
        started = time.monotonic()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.closed = True
        self.assertLess(time.monotonic() - started, 3)
        sock.settimeout(3)
        tail = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            tail += chunk
        wait_for(lambda: not self.httpd.events.watchers)


class HelperTest(unittest.TestCase):
    def test_tailscale_user_file_is_private_and_single_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tailscale-user'
            path.write_text('owner@example.invalid\n')
            path.chmod(0o600)
            self.assertEqual(server.load_tailscale_user(path), 'owner@example.invalid')
            with patch('server.os.getuid', return_value=os.getuid() + 1):
                with self.assertRaises(ValueError):
                    server.load_tailscale_user(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                server.load_tailscale_user(path)
            path.chmod(0o600)
            alias = Path(directory) / 'alias'
            alias.symlink_to(path)
            with self.assertRaises(ValueError):
                server.load_tailscale_user(alias)
            for contents in ('', '\n ', 'first@example.invalid\nsecond@example.invalid', 'x' * 4097):
                path.write_text(contents)
                with self.subTest(contents=contents[:40]), self.assertRaises(ValueError):
                    server.load_tailscale_user(path)
            with self.assertRaises(ValueError):
                server.load_tailscale_user(Path(directory) / 'missing')

    def test_collector_command_quotes_args(self):
        command = server.collector_command(HOSTS[0], ["read", "codex", "id-1", ".codex-router/0123456789abcdef"])
        self.assertEqual(command[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o"])
        self.assertEqual(command[-2], "remote")
        self.assertTrue(command[-1].endswith("/usr/bin/python3 -I - read codex id-1 .codex-router/0123456789abcdef"))
        local = server.collector_command({"local": True}, ["snapshot"])
        self.assertEqual(local[1:3], ["-I", str(server.COLLECTOR)])

    def test_ssh_control_path_fits_socket_limit(self):
        self.assertIsNone(server.ssh_control_path(None))
        self.assertEqual(server.ssh_control_options(None), [])
        # "/" + 37 + "/ssh/cm-" is 46 bytes; with %C (40) and the bind suffix (17) it is 103.
        fits = "/" + "s" * 37
        self.assertEqual(server.ssh_control_path(fits), fits + "/ssh/cm-%C")
        fallback = "/tmp/sessionholic-%d/cm-%%C" % os.getuid()
        # Too long or re-tokenized state paths use the short private fallback instead.
        self.assertEqual(server.ssh_control_path(fits + "s"), fallback)
        for unsafe in ("/tmp/with space", "/tmp/100%", "/tmp/quote'", "/tmp/hash#x"):
            self.assertEqual(server.ssh_control_path(unsafe), fallback, unsafe)
        with patch.object(server, "SSH_FALLBACK_ROOT", Path("/" + "f" * 60)):
            self.assertIsNone(server.ssh_control_path(fits + "s"))

    def test_ssh_control_options_need_private_prepared_folder(self):
        directory = tempfile.mkdtemp(prefix="sh", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory)
        command = server.collector_command(HOSTS[0], ["snapshot"], directory)
        self.assertNotIn("ControlMaster=auto", command)
        board = server.Board(HOSTS, 60, state_dir=directory)
        socket_dir = Path(directory) / "ssh"
        self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
        options = server.ssh_control_options(directory)
        self.assertEqual(options, ["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                                   "-o", "ControlPath=" + str(socket_dir) + "/cm-%C"])
        command = server.collector_command(HOSTS[0], ["snapshot"], directory)
        self.assertEqual(command[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o"])
        self.assertEqual(command[7:13], options)
        self.assertEqual(command[-3:-1], ["--", "remote"])
        self.assertNotIn("ControlMaster=auto", server.collector_command({"local": True}, ["snapshot"]))
        with patch("server.run_collector", return_value={"claude": [], "codex": []}) as run:
            self.assertTrue(board.poll(HOSTS[0]))
        run.assert_called_once_with(HOSTS[0], ["snapshot"], 45, Path(directory))
        socket_dir.chmod(0o755)
        self.assertEqual(server.ssh_control_options(directory), [])
        socket_dir.rmdir()
        socket_dir.symlink_to(directory)
        self.assertEqual(server.ssh_control_options(directory), [])

    def test_long_state_paths_share_connections_through_private_fallback(self):
        directory = tempfile.mkdtemp(prefix="sh", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory)
        state = Path(directory) / ("long-state-" + "x" * 60)
        with patch.object(server, "SSH_FALLBACK_ROOT", Path(directory)):
            server.Board(HOSTS, 60, state_dir=state)
            socket_dir = Path(directory) / ("sessionholic-%d" % os.getuid())
            self.assertFalse((state / "ssh").exists())
            self.assertEqual(socket_dir.stat().st_mode & 0o777, 0o700)
            self.assertIn("ControlPath=" + str(socket_dir) + "/cm-%C", server.ssh_control_options(state))
            self.assertIn("ControlMaster=auto", server.collector_command(HOSTS[0], ["snapshot"], state))
            # Another owner's or a widened folder disables sharing rather than trusting it.
            socket_dir.chmod(0o755)
            self.assertEqual(server.ssh_control_options(state), [])

    def test_load_hosts_validates(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"name": "remote", "ssh": "remote; rm -rf ~"}], f)
        with self.assertRaises(ValueError):
            server.load_hosts(f.name)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"name": "a", "local": True}, {"name": "a", "local": True}], f)
        with self.assertRaises(ValueError):
            server.load_hosts(f.name)
        self.assertEqual([h["name"] for h in server.load_hosts(server.DEFAULT_HOSTS)], ["local"])

    def test_failed_poll_keeps_last_data(self):
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        board.poll(HOSTS[0])

        def broken(host, args, timeout):
            raise RuntimeError("SSH 연결 실패")
        board.runner = broken
        self.assertFalse(board.poll(HOSTS[0]))
        entry = board.snapshot()["hosts"][0]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["error"], "SSH 연결 실패")
        self.assertEqual(entry["data"]["claude"][0]["title"], "정산")

    def test_open_flag_prints_one_time_login_link(self):
        class FakeServer:
            server_address = ("127.0.0.1", 8799)
            def __init__(self):
                self.once, self.once_lock = ("", 0), threading.Lock()
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        for flags in ([], ["--open"]):
            fake = FakeServer()
            with tempfile.TemporaryDirectory() as directory, patch("server.settings.load"), \
                 patch("server.settings.safe_path", side_effect=lambda path, **kw: Path(path)), \
                 patch("server.load_hosts", return_value=HOSTS), patch("server.load_token", return_value=TOKEN), \
                 patch("server.make_server", return_value=fake), \
                 patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(server.main(["--state-dir", directory] + flags), 0)
            if not flags:
                self.assertEqual((out.getvalue(), fake.once), ("", ("", 0)))
                continue
            code, expires = fake.once
            self.assertTrue(code)
            self.assertEqual(out.getvalue(), f"http://127.0.0.1:8799/login?once={code}\n")
            self.assertTrue(time.time() < expires <= time.time() + server.ONCE_TTL)
            self.assertNotIn(TOKEN, out.getvalue())

    def test_refresh_rejects_non_string_host(self):
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        for value in (["remote"], {"name": "remote"}, 5, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                board.request_refresh(value, force=True)
        self.assertFalse(board.state["remote"]["refreshing"])

    def test_host_allowed(self):
        self.assertTrue(server.host_allowed("127.0.0.1:8790"))
        self.assertTrue(server.host_allowed("localhost"))
        self.assertTrue(server.host_allowed("[::1]:8790"))
        self.assertTrue(server.host_allowed("remote.tail-example.ts.net"))
        self.assertFalse(server.host_allowed("ts.net.evil.com"))
        self.assertFalse(server.host_allowed(""))


TWO_HOSTS = [{"name": "alpha", "label": "알파", "local": False, "ssh": "alpha", "python": "/usr/bin/python3"},
             {"name": "beta", "label": "베타", "local": False, "ssh": "beta", "python": "/usr/bin/python3"}]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("background collection did not finish")


class BoardCollectionTest(unittest.TestCase):
    def board(self, runner, hosts=TWO_HOSTS, state_dir=None):
        board = server.Board(hosts, 60, runner=runner, state_dir=state_dir)
        self.now = [1_000_000.0]
        board.clock = lambda: self.now[0]
        return board

    @staticmethod
    def idle(board):
        return not any(row["refreshing"] for row in board.snapshot()["hosts"])

    def test_unexpected_error_clears_refreshing_and_other_hosts_continue(self):
        secret = "fixture-private-" + "payload"
        def runner(host, args, timeout):
            return {"claude": [], "codex": [], "owner": host["name"]}
        original = server.scrub_payload
        def scrub(value):
            if isinstance(value, dict) and value.get("owner") == "alpha":
                raise ValueError(secret)
            return original(value)
        board = self.board(runner)
        with patch("server.scrub_payload", side_effect=scrub), \
             patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertTrue(board.request_refresh())
            wait_for(lambda: self.idle(board))
        alpha, beta = board.state["alpha"], board.state["beta"]
        self.assertEqual((alpha["ok"], alpha["paused"], alpha["refreshing"]), (False, True, False))
        self.assertEqual(alpha["error"], "수집 결과를 처리하지 못했습니다.")
        self.assertEqual((beta["ok"], beta["refreshing"]), (True, False))
        self.assertIn("ValueError", log.getvalue())
        self.assertNotIn(secret, log.getvalue())
        # Manual refresh is not blocked by a stuck `refreshing` flag.
        self.now[0] += 10
        self.assertTrue(board.request_refresh("alpha", force=True))
        wait_for(lambda: self.idle(board))
        self.assertTrue(board.state["alpha"]["ok"])

    def test_state_file_failure_is_only_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            board = self.board(fake_runner([]), hosts=HOSTS, state_dir=directory)
            with patch("server.private_json", side_effect=OSError("fixture")), \
                 patch("sys.stderr", new_callable=io.StringIO) as log:
                self.assertTrue(board.poll(HOSTS[0]))
                result = board.read("remote", "claude", "a1b2c3d4-0000", None)
            self.assertEqual(result["messages"][0]["text"], "hi")
            entry = board.state["remote"]
            self.assertEqual((entry["ok"], entry["refreshing"], entry["error"]), (True, False, None))
            self.assertIn("snapshot.json", log.getvalue())
            self.assertIn("conversations.json", log.getvalue())

    def test_hosts_collect_in_parallel_but_never_twice_at_once(self):
        release = threading.Event()
        lock, running, peak, calls = threading.Lock(), {}, {}, []
        def runner(host, args, timeout):
            name = host["name"]
            with lock:
                calls.append(name)
                running[name] = running.get(name, 0) + 1
                peak[name] = max(peak.get(name, 0), running[name])
            try:
                if name == "alpha":
                    release.wait(5)
                return {"claude": [], "codex": []}
            finally:
                with lock:
                    running[name] -= 1
        board = self.board(runner)
        self.assertTrue(board.request_refresh())
        # beta finishes and is published while alpha is still collecting.
        wait_for(lambda: board.state["beta"]["ok"])
        self.assertTrue(board.state["alpha"]["refreshing"])
        self.now[0] += 10
        self.assertFalse(board.request_refresh("alpha", force=True))
        direct = threading.Thread(target=board.poll, args=(TWO_HOSTS[0],))
        direct.start()
        time.sleep(0.1)
        self.assertEqual(calls.count("alpha"), 1)
        release.set()
        direct.join(5)
        wait_for(lambda: self.idle(board))
        self.assertEqual(calls.count("alpha"), 2)
        self.assertEqual(peak, {"alpha": 1, "beta": 1})
        self.assertTrue(board.state["alpha"]["ok"])

    def test_paused_host_retries_automatically_with_backoff(self):
        outcome = {"fail": True}
        calls = []
        def runner(host, args, timeout):
            calls.append(self.now[0])
            if outcome["fail"]:
                raise RuntimeError("SSH 연결 실패")
            return {"claude": [], "codex": []}
        board = self.board(runner, hosts=HOSTS)
        self.assertFalse(board.poll(HOSTS[0]))
        for delay in (120, 300, 900, 900):
            failed_at = self.now[0]
            self.now[0] = failed_at + 61
            self.assertFalse(board.request_refresh(), "no repeated SSH right after a failure")
            self.now[0] = failed_at + delay - 1
            self.assertFalse(board.request_refresh())
            self.now[0] = failed_at + delay
            self.assertTrue(board.request_refresh())
            wait_for(lambda: self.idle(board))
            self.assertTrue(board.state["remote"]["paused"])
        self.assertEqual(len(calls), 5)
        outcome["fail"] = False
        self.now[0] += 900
        self.assertTrue(board.request_refresh())
        wait_for(lambda: self.idle(board))
        self.assertEqual((board.state["remote"]["ok"], board.state["remote"]["paused"]), (True, False))
        self.assertEqual(board.backoff, {})
        # A later failure starts again from the shortest delay.
        outcome["fail"] = True
        self.assertFalse(board.poll(HOSTS[0]))
        self.assertEqual(board.backoff["remote"], (1, self.now[0] + 120))


class FakeWatcher:
    """A Popen-like stand-in for one `collector.py watch` process: the test speaks its
    output lines; control lines written to its stdin are recorded."""

    class Input:
        def __init__(self, owner):
            self.owner, self.closed = owner, False

        def write(self, data):
            if self.closed:
                raise ValueError("closed")
            self.owner.lines.append(data)

        def flush(self):
            pass

        def close(self):
            if not self.closed:
                self.closed = True
                if self.owner.leave_on_eof:
                    self.owner.say({"type": "bye", "reason": "stdin-closed"})
                    self.owner.exit(0)

    def __init__(self, leave_on_eof=True):
        read, write = os.pipe()
        self.stdout, self.out = os.fdopen(read, "rb"), os.fdopen(write, "wb", buffering=0)
        self.stdin = self.Input(self)
        self.leave_on_eof = leave_on_eof
        self.lines, self.signals = [], []
        self.exited, self.returncode = threading.Event(), None

    def say(self, message):
        try:
            self.out.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        except (OSError, ValueError):
            pass

    def exit(self, code):
        if not self.exited.is_set():
            self.returncode = code
            self.out.close()
            self.exited.set()

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("watch", timeout)
        return self.returncode

    def terminate(self):
        self.signals.append("terminate")
        self.exit(-15)

    def kill(self):
        self.signals.append("kill")
        self.exit(-9)


class Watchers:
    """Board streamer: one FakeWatcher per started host watcher."""
    def __init__(self, **options):
        self.options, self.started, self.lock = options, [], threading.Lock()

    def __call__(self, host):
        watcher = FakeWatcher(**self.options)
        with self.lock:
            self.started.append((host["name"], watcher))
        return watcher, None

    def __len__(self):
        with self.lock:
            return len(self.started)

    def __getitem__(self, index):
        with self.lock:
            return self.started[index][1]


def live_data(title="정산", collected=1):
    return {"host": "remote", "collectedAt": collected, "claude": [{"id": "a1b2c3d4", "title": title}],
            "codex": [], "tmux": [], "errors": []}


class BoardLiveTest(unittest.TestCase):
    """Resident watchers: started by viewers, one per host, through the board's state path."""

    def setUp(self):
        self.calls = []

    def live_board(self, runner=None, **options):
        watchers = Watchers(**options)
        board = server.Board(HOSTS, 60, runner=runner or fake_runner(self.calls), streamer=watchers)
        board.live_grace, board.live_ping, board.live_silence = 0.3, 0.1, 5.0
        board.live_tick, board.live_exit_wait = 0.02, 0.5
        self.addCleanup(self.finish, board, watchers)
        return board, watchers

    def finish(self, board, watchers):
        board.close_live()
        for _, watcher in watchers.started:
            self.assertTrue(watcher.exited.wait(3), "a watcher process was left running")
        # Neither the manager, a stream reader nor a fallback collection outlives the board.
        wait_for(lambda: not [t for t in threading.enumerate()
                              if t.name.startswith(("sessionholic-live", "sessionholic-collect"))])

    @staticmethod
    def live(board):
        return board.state["remote"]["live"]

    def test_one_watcher_per_host_only_while_someone_watches(self):
        board, watchers = self.live_board()
        seen = []
        board.add_listener(seen.append)
        joins = [threading.Thread(target=board.viewer_joined) for _ in range(5)]
        for thread in joins:
            thread.start()
        for thread in joins:
            thread.join()
        wait_for(lambda: len(watchers) == 1)
        watcher = watchers[0]
        watcher.say({"type": "hello", "proto": 1})
        watcher.say({"type": "snapshot", "seq": 1, "reason": "initial", "data": live_data()})
        wait_for(lambda: self.live(board))
        row = board.state["remote"]
        self.assertEqual((row["ok"], row["paused"], row["error"]), (True, False, None))
        self.assertEqual(row["data"]["claude"][0]["title"], "정산")
        version = board.version
        self.assertEqual(seen[-1], version)
        # The same content (only collectedAt moved) and heartbeats leave the version alone.
        watcher.say({"type": "snapshot", "seq": 2, "reason": "initial", "data": live_data(collected=2)})
        watcher.say({"type": "heartbeat", "seq": 2, "t": 1})
        wait_for(lambda: b"ping\n" in watcher.lines)
        time.sleep(0.1)
        self.assertEqual(board.version, version)
        watcher.say({"type": "snapshot", "seq": 3, "reason": "change", "data": live_data("정산 완료", 3)})
        wait_for(lambda: board.state["remote"]["data"]["claude"][0]["title"] == "정산 완료")
        self.assertEqual(board.version, version + 1)
        # Leaving and coming back within the grace period keeps the same watcher.
        for _ in range(5):
            board.viewer_left()
        time.sleep(0.1)
        board.viewer_joined()
        board.viewer_left()
        self.assertFalse(watcher.exited.is_set())
        wait_for(lambda: watcher.exited.is_set())
        wait_for(lambda: board.live_manager is None and not board.streams)
        # An intended stop: stdin closed, no signal, no fallback collection, one watcher in all.
        self.assertTrue(watcher.stdin.closed)
        self.assertEqual(watcher.signals, [])
        self.assertEqual((len(watchers), self.calls, self.live(board)), (1, [], False))

    def test_a_failed_watcher_falls_back_once_and_restarts_after_its_own_backoff(self):
        board, watchers = self.live_board()
        board.STREAM_BACKOFF, board.STREAM_TRIP = (0.3, 0.3, 0.3, 0.3), 60
        board.viewer_joined()
        wait_for(lambda: len(watchers) == 1)
        watchers[0].say({"type": "snapshot", "seq": 1, "data": live_data()})
        wait_for(lambda: self.live(board))
        watchers[0].exit(1)
        wait_for(lambda: not self.live(board) and self.calls == [("remote", ["snapshot"])])
        wait_for(lambda: not board.state["remote"]["refreshing"])
        self.assertEqual(board.stream_backoff["remote"][0], 1)
        # The one-shot backoff is separate and untouched.
        self.assertEqual(board.backoff, {})
        time.sleep(0.1)
        self.assertEqual(len(watchers), 1)
        wait_for(lambda: len(watchers) == 2)
        # A watcher whose board lock is still held by an orphan ends as "busy".
        watchers[1].say({"type": "busy"})
        wait_for(lambda: len(watchers) == 3)
        self.assertEqual(watchers[1].signals, [])
        watchers[2].exit(1)
        # Three quick failures in a row pause this host's watcher for the long trip delay.
        wait_for(lambda: board.stream_backoff["remote"][0] == 3)
        self.assertGreater(board.stream_backoff["remote"][1] - time.monotonic(), 30)
        time.sleep(0.4)
        self.assertEqual(len(watchers), 3)
        board.viewer_left()

    def test_refresh_goes_to_the_live_watcher_instead_of_a_one_shot_collection(self):
        board, watchers = self.live_board()
        board.viewer_joined()
        wait_for(lambda: len(watchers) == 1)
        watchers[0].say({"type": "snapshot", "seq": 1, "data": live_data()})
        wait_for(lambda: self.live(board))
        self.assertFalse(board.request_refresh())
        self.assertTrue(board.request_refresh(force=True))
        self.assertIn(b"refresh\n", watchers[0].lines)
        time.sleep(0.1)
        self.assertEqual(self.calls, [])
        board.viewer_left()

    def test_paused_host_starts_its_watcher_after_a_successful_collection(self):
        outcome, calls = {"fail": True}, []
        def runner(host, args, timeout):
            calls.append(list(args))
            if outcome["fail"]:
                raise RuntimeError("SSH 연결 실패")
            return live_data()
        board, watchers = self.live_board(runner)
        now = [1_000_000.0]
        board.clock = lambda: now[0]
        self.assertFalse(board.poll(HOSTS[0]))
        board.viewer_joined()
        time.sleep(0.2)
        self.assertEqual(len(watchers), 0)
        outcome["fail"] = False
        now[0] += 10
        self.assertTrue(board.request_refresh(force=True))
        wait_for(lambda: board.state["remote"]["ok"] and not board.state["remote"]["refreshing"])
        # A manual one-shot refresh also lets Codex scan and repair its thread metadata.
        self.assertEqual(calls, [["snapshot"], ["snapshot", "--full"]])
        wait_for(lambda: len(watchers) == 1)
        board.viewer_left()

    def test_one_shot_result_older_than_a_live_snapshot_is_not_applied(self):
        entered, release, result = threading.Event(), threading.Event(), []
        def runner(host, args, timeout):
            entered.set()
            release.wait(5)
            return live_data("오래된 목록")
        board, watchers = self.live_board(runner)
        poller = threading.Thread(target=lambda: result.append(board.poll(HOSTS[0])))
        poller.start()
        self.assertTrue(entered.wait(5))
        board.viewer_joined()
        wait_for(lambda: len(watchers) == 1)
        watchers[0].say({"type": "snapshot", "seq": 1, "data": live_data("실시간 목록")})
        wait_for(lambda: self.live(board))
        release.set()
        poller.join(5)
        row = board.state["remote"]
        self.assertEqual(result, [True])
        self.assertEqual(row["data"]["claude"][0]["title"], "실시간 목록")
        self.assertEqual((row["refreshing"], row["live"]), (False, True))
        self.assertIsNotNone(row["durationMs"])
        board.viewer_left()

    def test_silent_watcher_is_ended_and_replaced(self):
        board, watchers = self.live_board(leave_on_eof=False)
        board.live_silence, board.STREAM_BACKOFF = 0.3, (0.1, 0.1, 0.1, 0.1)
        board.viewer_joined()
        wait_for(lambda: len(watchers) == 1)
        wait_for(lambda: "terminate" in watchers[0].signals)
        wait_for(lambda: self.calls == [("remote", ["snapshot"])])
        wait_for(lambda: len(watchers) == 2)
        # A stubborn watcher that ignores stdin EOF is terminated on an intended stop.
        board.viewer_left()
        wait_for(lambda: watchers[1].exited.is_set())
        self.assertEqual(watchers[1].signals, ["terminate"])

    def test_event_streams_start_watchers_and_server_close_stops_them(self):
        import socket
        board, watchers = self.live_board()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        httpd = server.make_server("127.0.0.1", 0, board, TOKEN, state_dir=temp.name)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("HEAD", "/api/events", headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        time.sleep(0.1)
        self.assertEqual((len(watchers), board.viewers), (0, 0))
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n\r\n" % TOKEN).encode())
        received = b""
        while b"event: hello" not in received:
            received += sock.recv(65536)
        wait_for(lambda: len(watchers) == 1 and board.viewers == 1)
        httpd.shutdown()
        httpd.server_close()
        wait_for(lambda: watchers[0].exited.is_set())
        self.assertTrue(watchers[0].stdin.closed)
        wait_for(lambda: board.live_manager is None and board.viewers == 0)
        # A closed board never starts another watcher.
        board.viewer_joined()
        time.sleep(0.1)
        self.assertEqual(len(watchers), 1)
        board.viewer_left()

    def test_boards_with_their_own_runner_never_start_processes(self):
        board = server.Board(HOSTS, 60, runner=fake_runner([]))
        self.assertIsNone(board.streamer)
        board.viewer_joined()
        self.assertIsNone(board.live_manager)
        board.viewer_left()
        self.assertIsNotNone(server.Board(HOSTS, 60).streamer)
        self.assertIsNone(server.Board(HOSTS, 60, live=False).streamer)

    def test_board_id_is_private_and_kept(self):
        directory = tempfile.mkdtemp(prefix="sh", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory)
        first = server.Board(HOSTS, 60, runner=fake_runner([]), state_dir=directory).live_board_id()
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        path = Path(directory) / "board-id"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.Board(HOSTS, 60, runner=fake_runner([]), state_dir=directory).live_board_id(), first)
        path.write_text("../not-an-id\n")
        replaced = server.Board(HOSTS, 60, runner=fake_runner([]), state_dir=directory).live_board_id()
        self.assertRegex(replaced, r"^[0-9a-f]{16}$")
        self.assertRegex(server.Board(HOSTS, 60, runner=fake_runner([])).live_board_id(), r"^[0-9a-f]{16}$")

    def test_a_remote_watch_reads_its_program_before_starting_ssh(self):
        order = []
        def popen(command, **options):
            order.append("ssh")
            return "process"
        with patch.object(server.subprocess, "Popen", side_effect=popen), \
                patch.object(server, "remote_program", side_effect=OSError("fixture unreadable")):
            with self.assertRaises(OSError):
                server.start_watch(HOSTS[0], "0123456789abcdef")
        self.assertEqual(order, [], "no ssh process is left without a program to send")
        def program():
            order.append("program")
            return b"print(1)\n"
        with patch.object(server.subprocess, "Popen", side_effect=popen), \
                patch.object(server, "remote_program", side_effect=program):
            self.assertEqual(server.start_watch(HOSTS[0], "0123456789abcdef"), ("process", b"9\nprint(1)\n"))
            self.assertEqual(server.start_watch({"local": True}, "0123456789abcdef"), ("process", None))
        self.assertEqual(order, ["program", "ssh", "ssh"])

    def test_watch_commands(self):
        remote = server.watch_command(HOSTS[0], "0123456789abcdef")
        self.assertEqual(remote[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o"])
        self.assertEqual(remote[-2], "remote")
        self.assertIn("/usr/bin/python3 -I -c " + shlex.quote(server.WATCH_LOADER), remote[-1])
        self.assertTrue(remote[-1].endswith(" watch --board 0123456789abcdef"))
        local = server.watch_command({"local": True}, "0123456789abcdef")
        self.assertEqual(local[1:], ["-I", str(server.COLLECTOR), "watch", "--board", "0123456789abcdef"])
        for bad in ("../x", "0123", "0123456789ABCDEF", "a; rm -rf ~", None):
            with self.assertRaises(ValueError):
                server.watch_command(HOSTS[0], bad)

    def test_length_prefixed_program_runs_a_real_watcher_through_the_board(self):
        """The remote bootstrap without SSH: the same loader reads `<length>\\n<program>`
        from stdin, then keeps stdin for pings; the board reads its snapshot and stops it."""
        home = Path(tempfile.mkdtemp(prefix="sh", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, home)
        loader = [sys.executable, "-I", "-c", server.WATCH_LOADER, "watch"]
        processes = []
        def streamer(host):
            process, preface = server.start_watch(host, "0123456789abcdef")
            processes.append(process)
            return process, preface
        environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TMUX_TMPDIR": str(home)}
        board = server.Board(HOSTS, 60, runner=fake_runner(self.calls), streamer=streamer)
        board.live_grace, board.live_tick = 0.2, 0.05
        self.addCleanup(board.close_live)
        with patch.object(server, "watch_command", return_value=loader), patch.dict(os.environ, environment):
            board.viewer_joined()
            wait_for(lambda: self.live(board), timeout=20)
        preface = server.remote_program()
        self.assertTrue(preface.startswith(b"from __future__ import annotations\n"))
        data = board.state["remote"]["data"]
        self.assertEqual(set(data), {"host", "collectedAt", "claude", "codex", "tmux", "errors"})
        self.assertEqual(data["codex"], [])
        board.viewer_left()
        wait_for(lambda: board.live_manager is None and not board.streams, timeout=10)
        self.assertEqual(processes[0].returncode, 0)
        self.assertEqual((len(processes), self.calls, self.live(board)), (1, [], False))


if __name__ == "__main__":
    unittest.main()
