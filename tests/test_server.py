import http.client
import io
import json
import os
import shutil
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
        res, _ = self.request("GET", "/", headers={"Host": "remote.tailc1299f.ts.net"})
        self.assertEqual(res.status, 200)


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
        self.assertIsNone(server.ssh_control_path(fits + "s"))
        for unsafe in ("/tmp/with space", "/tmp/100%", "/tmp/quote'", "/tmp/hash#x"):
            self.assertIsNone(server.ssh_control_path(unsafe), unsafe)

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

    def test_ssh_control_options_are_omitted_for_long_state_paths(self):
        directory = tempfile.mkdtemp(prefix="sh", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory)
        state = Path(directory) / ("long-state-" + "x" * 60)
        server.Board(HOSTS, 60, state_dir=state)
        self.assertTrue((state / "ssh").is_dir())
        self.assertEqual(server.ssh_control_options(state), [])
        self.assertNotIn("ControlMaster=auto", server.collector_command(HOSTS[0], ["snapshot"], state))

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
        self.assertTrue(server.host_allowed("remote.tailc1299f.ts.net"))
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


if __name__ == "__main__":
    unittest.main()
