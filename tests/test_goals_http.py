import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


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


class GoalsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.board = server.Board([], 60, runner=lambda *args: self.fail("native collector called"), live=False)
        terminals = SimpleNamespace(list=lambda: [], close=lambda: None)
        self.httpd = server.make_server("127.0.0.1", 0, self.board, "fixture-token", state_dir=self.temp.name, terminals=terminals)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        response, result = self.request("POST", "/login", {"token": "fixture-token"}, authenticated=False)
        self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.csrf = result["csrfToken"]

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, authenticated=True, csrf=True, extra=None):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Cookie"] = self.cookie
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        headers.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, json.loads(data)

    def create(self, **values):
        response, body = self.request("POST", "/api/goals", {"title": "합성 검토 작업", **values})
        self.assertEqual(response.status, 201, body)
        return body["goal"]

    def test_login_csrf_and_cross_origin_write_protection(self):
        for method, path, body in (("GET", "/api/goals", None), ("POST", "/api/goals", {"title": "무인증"})):
            response, _ = self.request(method, path, body, authenticated=False)
            self.assertEqual(response.status, 401)
        response, result = self.request("POST", "/api/goals", {"title": "무CSRF"}, csrf=False)
        self.assertEqual((response.status, result["code"]), (403, "csrf_expired"))
        response, _ = self.request("POST", "/api/goals", {"title": "외부 origin"}, extra={"Origin": "https://example.invalid"})
        self.assertEqual(response.status, 403)
        response, result = self.request("GET", "/api/goals", csrf=False)
        self.assertEqual((response.status, result["goals"]), (200, []))

    def test_create_read_update_conflict_and_bearer_read(self):
        goal = self.create()
        path = "/api/goals/" + goal["id"]
        response, result = self.request("GET", path)
        self.assertEqual((response.status, result["goal"]), (200, goal))
        response, result = self.request("POST", path, {"revision": 1, "status": "in_progress", "current": "자료 정리 중"})
        self.assertEqual((response.status, result["goal"]["revision"]), (200, 2))
        response, result = self.request("POST", path, {"revision": 1, "current": "오래된 화면"})
        self.assertEqual((response.status, result["code"], result["currentRevision"]), (409, "goal_conflict", 2))
        response, result = self.request("GET", "/api/goals", authenticated=False, extra={"Authorization": "Bearer fixture-token"})
        self.assertEqual((response.status, result["revision"]), (200, 2))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_done_validation_and_missing_goal(self):
        goal = self.create()
        path = "/api/goals/" + goal["id"]
        response, result = self.request("POST", path, {"revision": 1, "status": "done"})
        self.assertEqual((response.status, result["code"]), (400, "completion_evidence_required"))
        response, result = self.request("POST", path, {"revision": 1, "status": "done", "completionCriteria": "검토 완료", "evidence": "결과 파일 확인"})
        self.assertEqual((response.status, result["goal"]["status"]), (200, "done"))
        response, result = self.request("GET", "/api/goals/" + "0" * 32)
        self.assertEqual((response.status, result["code"]), (404, "goal_not_found"))

    def test_corrupt_goals_file_does_not_break_snapshot_or_overwrite_state(self):
        goal = self.create()
        self.httpd.goals.path.write_text("invalid private fixture")
        response, result = self.request("GET", "/api/goals")
        self.assertEqual((response.status, result["code"]), (503, "goals_unavailable"))
        self.assertNotIn("invalid private fixture", json.dumps(result))
        response, _ = self.request("GET", "/api/snapshot")
        self.assertEqual(response.status, 200)
        response, result = self.request("POST", "/api/goals/" + goal["id"], {"revision": 1, "title": "변경 시도"})
        self.assertEqual((response.status, result["code"]), (503, "goals_unavailable"))
        self.assertEqual(self.httpd.goals.path.read_text(), "invalid private fixture")

    def test_corrupt_file_does_not_prevent_server_start(self):
        self.create()
        self.httpd.goals.path.write_text("invalid")
        restarted = server.make_server("127.0.0.1", 0, self.board, "fixture-token", state_dir=self.temp.name,
                                       terminals=SimpleNamespace(list=lambda: [], close=lambda: None))
        restarted.server_close()

    def test_session_receipt_and_offline_snapshot_never_update_goal(self):
        goal = self.create(status="in_progress")
        before = self.httpd.goals.path.read_bytes()
        with patch.object(self.board, "snapshot", return_value={"hosts": [{"name": "fixture", "ok": False,
                           "data": {"codex": [{"id": "fixture-session", "phase": "idle"}]}}]}):
            self.assertEqual(self.request("GET", "/api/snapshot")[0].status, 200)
        with patch.object(self.httpd.chat, "state", return_value={"receipts": [{"status": "completed"}]}):
            self.assertEqual(self.request("POST", "/api/chat/state", {})[0].status, 200)
        self.assertEqual(self.httpd.goals.path.read_bytes(), before)
        self.assertEqual(self.request("GET", "/api/goals/" + goal["id"])[1]["goal"]["status"], "in_progress")

    def test_confirmed_handoff_hook_links_and_estimated_new_sessions_do_not(self):
        source = {"host": "fixture", "agent": "codex", "home": ".codex", "id": "fixture-source"}
        target = {**source, "home": ".codex-other", "id": "fixture-target"}
        goal = self.create(sessions=[source], status="review")
        flow = self.httpd.workflow
        terminal = {"mode": "handoff", "sourceKey": json.dumps(source), "nativeSource": target}
        for excluded in ({**terminal, "mode": "new-session"}, {**terminal, "linkConfidence": "estimated"}):
            flow._link_handoff_goals({"terminal": excluded})
            self.assertEqual(flow.goals.get(goal["id"])["goal"]["revision"], 1)
        result = flow._link_handoff_goals({"terminal": terminal, "mode": "handoff"})
        self.assertEqual(result["goalLinksUpdated"], 1)
        updated = flow.goals.get(goal["id"])["goal"]
        self.assertEqual((updated["status"], len(updated["sessions"])), ("review", 2))
        flow._link_handoff_goals({"terminal": terminal, "mode": "reconnect"})
        self.assertEqual(flow.goals.get(goal["id"])["goal"], updated)

    def test_handoff_goal_storage_failure_does_not_repeat_or_fail_native_launch(self):
        source = {"host": "fixture", "agent": "claude", "home": None, "id": "fixture-source"}
        target = {"host": "fixture-target", "agent": "codex", "home": ".codex", "id": "fixture-target"}
        self.create(sessions=[source])
        flow = self.httpd.workflow
        terminal = {"mode": "native", "launchMode": "transfer", "sourceKey": json.dumps(source), "nativeSource": target}
        launched = {"terminal": terminal, "mode": "transfer"}
        flow.requests["fixture-repeat"] = ("fixture-plan", launched)
        self.httpd.goals.path.write_text("invalid")
        # Idempotent launch return path does not inspect a plan, poll or run a CLI.
        result = flow.launch("fixture-plan", "fixture-repeat")
        self.assertIs(result, launched)
        self.assertIn("goalLinkWarning", result)
        self.assertEqual(result["terminal"], terminal)


if __name__ == "__main__":
    unittest.main()
