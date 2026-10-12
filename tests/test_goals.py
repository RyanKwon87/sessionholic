import concurrent.futures
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goals import GoalError, Goals, HISTORY_LIMIT


REF = {"host": "fixture-laptop", "agent": "codex", "home": ".codex", "id": "fixture-session-001"}


class GoalsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "board-state"
        self.goals = Goals(self.folder)

    def create(self, **values):
        return self.goals.save({"title": "자료 정리", **values})["goal"]

    def update(self, goal, **values):
        return self.goals.save({"revision": goal["revision"], **values}, goal["id"])["goal"]

    def assert_error(self, code, action, status=400):
        with self.assertRaises(GoalError) as raised:
            action()
        self.assertEqual((raised.exception.payload["code"], raised.exception.status), (code, status))

    def test_create_defaults_private_persistence_and_restart(self):
        self.assertEqual(self.goals.snapshot(), {"goals": [], "revision": 0})
        goal = self.create()
        self.assertEqual((goal["status"], goal["revision"], goal["sessions"]), ("todo", 1, []))
        self.assertEqual(goal["history"][0]["actor"], {"type": "user", "label": "사용자"})
        self.assertTrue(goal["createdAt"].endswith("Z"))
        self.assertEqual(goal["createdAt"], goal["updatedAt"])
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        for filename in ("goals.json", "goals.lock"):
            self.assertEqual(stat.S_IMODE((self.folder / filename).stat().st_mode), 0o600)
        self.assertEqual(Goals(self.folder).get(goal["id"])["goal"], goal)

    def test_many_to_many_identity_preserves_host_and_account(self):
        refs = [REF, {**REF, "home": ".codex-alt"}, {**REF, "host": "fixture-desktop", "point": "turn 2 · 검증 결과"}]
        first = self.create(sessions=refs)
        second = self.create(title="별도 목표", sessions=[REF])
        self.assertEqual(len(first["sessions"]), 3)
        self.assertEqual(first["sessions"][2]["point"], "turn 2 · 검증 결과")
        self.assertEqual(first["sessions"][0], second["sessions"][0])
        moved = self.update(first, sessions=refs + [{**REF, "id": "fixture-session-002"}])
        self.assertEqual(moved["id"], first["id"])
        self.assertEqual(len(moved["sessions"]), 4)
        self.assertEqual(Goals(self.folder).get(first["id"])["goal"], moved)

    def test_session_defaults_and_duplicate_rejection(self):
        goal = self.create(sessions=[{"host": "fixture", "agent": "claude", "id": "fixture-session-002"}])
        self.assertEqual(goal["sessions"][0]["home"], ".claude")
        self.assert_error("invalid_goal", lambda: self.create(sessions=[REF, {key: value for key, value in REF.items() if key != "home"}]))

    def test_done_requires_criteria_and_evidence_without_changing_existing_record(self):
        goal = self.create(status="in_progress")
        before = self.goals.path.read_bytes()
        for values in ({}, {"completionCriteria": "문서 전달"}, {"evidence": "결과 문서 위치"}):
            self.assert_error("completion_evidence_required", lambda: self.update(goal, status="done", **values))
            self.assertEqual(self.goals.path.read_bytes(), before)
        review = self.update(goal, status="review", current="결과 검토 대기")
        done = self.update(review, status="done", completionCriteria="정리한 문서 전달", evidence="공유 문서에서 최종본 확인")
        self.assertEqual(done["status"], "done")
        self.assert_error("completion_evidence_required", lambda: self.update(done, evidence="  "))
        self.assertEqual(self.update(done, status="in_progress", evidence="")["status"], "in_progress")

    def test_waiting_reason_is_required_and_retained_for_history_context(self):
        goal = self.create()
        self.assert_error("waiting_reason_required", lambda: self.update(goal, status="waiting"))
        waiting = self.update(goal, status="waiting", waitingReason="자료 수신 대기")
        self.assertEqual(waiting["waitingReason"], "자료 수신 대기")

    def test_change_actor_reason_status_history_and_reversible_archive(self):
        goal = self.create()
        updated = self.update(goal, status="in_progress", pinned=True,
                              actor={"type": "agent", "label": "일반 연구 에이전트"}, reason="명시적 착수 보고")
        event = updated["history"][-1]
        self.assertEqual((event["fromStatus"], event["toStatus"]), ("todo", "in_progress"))
        self.assertEqual(event["reason"], "명시적 착수 보고")
        self.assertEqual(event["fields"], ["pinned", "status"])
        archived = self.update(updated, archived=True)
        restored = self.update(archived, archived=False, reason="보관 실행 취소")
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["historyCount"], 4)
        self.assertEqual(restored["revision"], 4)
        self.assertEqual(restored["history"][-1]["actor"]["type"], "user")

    def test_inference_is_distinct_from_explicit_agent_report(self):
        goal = self.create(actor={"type": "inferred", "label": "대화에서 추정"})
        self.assertEqual(goal["history"][0]["actor"]["type"], "inferred")
        updated = self.update(goal, title="사용자가 확정한 목표", actor={"type": "user", "label": "사용자"})
        self.assertEqual(updated["history"][-1]["actor"]["type"], "user")

    def test_noop_does_not_reorder_or_write_history(self):
        goal = self.create()
        before = self.goals.path.read_bytes()
        self.assertEqual(self.update(goal, title=goal["title"]), goal)
        self.assertEqual(self.goals.path.read_bytes(), before)

    def test_revision_conflict_never_overwrites_and_independent_goals_can_change(self):
        first, second = self.create(), self.create(title="다른 작업")
        changed = self.update(first, current="첫 화면 수정")
        self.assert_error("goal_conflict", lambda: self.update(first, current="늦은 화면 수정"), 409)
        self.assertEqual(self.goals.get(first["id"])["goal"], changed)
        self.assertEqual(self.update(second, current="독립적 수정")["current"], "독립적 수정")
        self.assert_error("goal_revision_required", lambda: self.goals.save({"title": "버전 누락"}, first["id"]))

    def test_two_store_instances_have_one_concurrent_winner(self):
        goal = self.create()
        barrier = threading.Barrier(2)
        def edit(text):
            separate = Goals(self.folder)
            barrier.wait(timeout=2)
            try:
                separate.save({"revision": 1, "current": text}, goal["id"])
                return "saved"
            except GoalError as exc:
                return exc.payload["code"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(edit, value) for value in ("first", "second")]
            self.assertEqual(sorted(f.result(timeout=3) for f in futures), ["goal_conflict", "saved"])
        self.assertEqual(self.goals.get(goal["id"])["goal"]["revision"], 2)

    def test_failed_atomic_replace_preserves_existing_file_and_cleans_temporary(self):
        goal = self.create()
        before = self.goals.path.read_bytes()
        with patch("goals.os.replace", side_effect=OSError("fixture-private-detail")):
            self.assert_error("goals_unavailable", lambda: self.update(goal, current="failed save"), 503)
        self.assertEqual(self.goals.path.read_bytes(), before)
        self.assertEqual(list(self.folder.glob(".goals-*.tmp")), [])

    def test_failed_fsync_never_replaces_existing_file(self):
        goal = self.create()
        before = self.goals.path.read_bytes()
        with patch("goals.os.fsync", side_effect=OSError("fixture-private-detail")):
            self.assert_error("goals_unavailable", lambda: self.update(goal, title="새 제목"), 503)
        self.assertEqual(self.goals.path.read_bytes(), before)

    def test_corrupt_or_future_file_blocks_only_goals_without_overwrite(self):
        self.create()
        for content in (b"broken", b'{"version":2,"revision":0,"goals":[]}', b'[]'):
            self.goals.path.write_bytes(content)
            self.assert_error("goals_unavailable", self.goals.snapshot, 503)
            self.assert_error("goals_unavailable", self.create, 503)
            self.assertEqual(self.goals.path.read_bytes(), content)

    def test_corrupt_record_is_not_returned_as_empty(self):
        self.create()
        data = json.loads(self.goals.path.read_bytes())
        data["goals"][0]["status"] = "done"
        self.goals.path.write_text(json.dumps(data))
        self.assert_error("goals_unavailable", self.goals.snapshot, 503)

    def test_permissions_symlink_and_hardlink_are_rejected(self):
        self.create()
        self.goals.path.chmod(0o644)
        self.assert_error("goals_unavailable", self.goals.snapshot, 503)
        self.goals.path.chmod(0o600)
        target = self.folder / "original.json"
        self.goals.path.rename(target)
        self.goals.path.symlink_to(target)
        self.assert_error("goals_unavailable", self.goals.snapshot, 503)
        self.goals.path.unlink()
        os.link(target, self.goals.path)
        self.assert_error("goals_unavailable", self.goals.snapshot, 503)
        self.assertTrue(target.exists())

    def test_state_directory_symlink_is_rejected(self):
        target = Path(self.temp.name) / "separate-state"
        target.mkdir(mode=0o700)
        self.folder.symlink_to(target, target_is_directory=True)
        self.assert_error("goals_unavailable", self.create, 503)
        self.assertEqual(list(target.iterdir()), [])

    def test_instance_state_is_isolated(self):
        goal = self.create()
        other = Goals(Path(self.temp.name) / "other-state")
        self.assertEqual(other.snapshot()["goals"], [])
        self.assert_error("goal_not_found", lambda: other.get(goal["id"]), 404)

    def test_exact_handoff_links_all_matching_goals_once_without_changing_status(self):
        first = self.create(sessions=[REF], status="in_progress")
        second = self.create(sessions=[REF], status="done", completionCriteria="검토 완료", evidence="최종본 확인")
        unrelated = self.create(sessions=[{**REF, "home": ".codex-other"}])
        target = {**REF, "host": "fixture-desktop", "home": ".codex-new", "id": "fixture-destination"}
        self.assertEqual(self.goals.link_sessions(REF, target), 2)
        for prior in (first, second):
            goal = self.goals.get(prior["id"])["goal"]
            self.assertEqual((goal["status"], len(goal["sessions"]), goal["revision"]), (prior["status"], 2, 2))
            self.assertEqual(goal["history"][-1]["fields"], ["sessions"])
            self.assertEqual(goal["history"][-1]["actor"]["type"], "agent")
            self.assertEqual(goal["history"][-1]["fromStatus"], goal["history"][-1]["toStatus"])
        self.assertEqual(self.goals.get(unrelated["id"])["goal"], unrelated)
        before = self.goals.path.read_bytes()
        self.assertEqual(self.goals.link_sessions(REF, target), 0)
        self.assertEqual(self.goals.link_sessions(REF, REF), 0)
        self.assertEqual(self.goals.path.read_bytes(), before)

    def test_handoff_link_is_atomic_across_all_goals_on_storage_failure(self):
        self.create(sessions=[REF])
        self.create(sessions=[REF])
        before = self.goals.path.read_bytes()
        with patch("goals.os.replace", side_effect=OSError("fixture failure")):
            self.assert_error("goals_unavailable", lambda: self.goals.link_sessions(REF, {**REF, "id": "fixture-next"}), 503)
        self.assertEqual(self.goals.path.read_bytes(), before)

    def test_bounded_history_reports_total_change_count(self):
        goal = self.create()
        for index in range(HISTORY_LIMIT + 3):
            goal = self.update(goal, current=str(index))
        self.assertEqual((len(goal["history"]), goal["historyCount"]), (HISTORY_LIMIT, HISTORY_LIMIT + 4))
        self.assertEqual(Goals(self.folder).get(goal["id"])["goal"], goal)

    def test_invalid_input_types_unknown_keys_limits_and_inert_references(self):
        for value in ({"title": " "}, {"title": "x" * 241}, {"status": "completed"}, {"pinned": 1},
                      {"archived": "true"}, {"receipt": "completed"}, {"online": False}, {"revision": True},
                      {"actor": {"type": "system", "label": "unknown"}}, {"reason": ""},
                      {"sessions": [{**REF, "home": "../private"}]}, {"sessions": [{**REF, "host": ";run"}]},
                      {"sessions": [{**REF, "id": "../../private"}]}, {"sessions": [REF] * 65}):
            with self.subTest(value=value):
                with self.assertRaises(GoalError):
                    self.create(**value)
        self.assertEqual(self.goals.snapshot()["goals"], [])


if __name__ == "__main__":
    unittest.main()
