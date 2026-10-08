import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

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

    def test_host_allowed(self):
        self.assertTrue(server.host_allowed("127.0.0.1:8790"))
        self.assertTrue(server.host_allowed("localhost"))
        self.assertTrue(server.host_allowed("[::1]:8790"))
        self.assertTrue(server.host_allowed("remote.tailc1299f.ts.net"))
        self.assertFalse(server.host_allowed("ts.net.evil.com"))
        self.assertFalse(server.host_allowed(""))


if __name__ == "__main__":
    unittest.main()
