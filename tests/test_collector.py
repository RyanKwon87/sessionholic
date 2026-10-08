import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector  # noqa: E402


class LabelTest(unittest.TestCase):
    def test_home_labels_use_explicit_settings_and_ignore_legacy_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config/sessionholic/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"version": 1, "profiles": [
                {"agent": "codex", "home": ".codex-beta", "label": "Example"}]}))
            legacy = home / ".codex-router/0123456789abcdef/ag-sessions"
            legacy.mkdir(parents=True)
            (legacy / "old.json").write_text('{"scope":{"account":"DO-NOT-IMPORT"}}')
            with patch.object(collector, "HOME", home):
                self.assertEqual(collector.home_label(".codex-beta", home / ".codex-beta"), (None, "Example"))
                self.assertEqual(collector.home_label(".codex-router/0123456789abcdef", legacy.parent),
                                 (None, ".codex-router/0123456789abcdef"))
                self.assertEqual(collector.home_label(".codex", home / ".codex"), (None, "기본"))

    def test_project_for_prefers_longest_root(self):
        projects = [{"id": "a", "label": "예제", "root": "/Users/u/workspace"},
                    {"id": "b", "label": "하위 프로젝트", "root": "/Users/u/workspace/backoffice"}]
        self.assertEqual(collector.project_for("/Users/u/workspace/backoffice/src", projects), "하위 프로젝트")
        self.assertEqual(collector.project_for("/Users/u/workspace", projects), "예제")
        self.assertEqual(collector.project_for("/Users/u/workspaceX", projects), "workspaceX")
        self.assertEqual(collector.project_for("", projects), "")

    def test_load_projects_uses_explicit_json_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"version": 1, "projects": [{"id": "example", "label": "Example", "root": "/example"}]}))
            self.assertEqual(collector.load_projects(path), [{"id": "example", "label": "Example", "root": "/example"}])

    def test_home_key_rejects_other_paths(self):
        for good in (".codex", ".codex-beta", ".codex-router/0123456789abcdef"):
            self.assertTrue(collector.HOME_KEY.match(good), good)
        for bad in ("../.codex", ".codex/../../etc", ".codex-router/xyz", "/tmp/x", ".codex-router/0123456789abcdef/.."):
            self.assertFalse(collector.HOME_KEY.match(bad), bad)


class PhaseTest(unittest.TestCase):
    def test_claude(self):
        self.assertEqual(collector.claude_phase("blocked", "idle"), "needs_input")
        self.assertEqual(collector.claude_phase("failed", None), "needs_input")
        self.assertEqual(collector.claude_phase("done", "waiting"), "needs_input")
        self.assertEqual(collector.claude_phase("working", "busy"), "working")
        self.assertEqual(collector.claude_phase("done", "busy"), "working")
        self.assertEqual(collector.claude_phase("done", "idle"), "done")
        self.assertEqual(collector.claude_phase("something-new", None), "idle")

    def test_codex(self):
        self.assertEqual(collector.codex_phase({"type": "active", "activeFlags": ["waitingOnApproval"]}), "needs_input")
        self.assertEqual(collector.codex_phase({"type": "systemError"}), "needs_input")
        self.assertEqual(collector.codex_phase({"type": "active", "activeFlags": []}), "working")
        self.assertEqual(collector.codex_phase({"type": "idle"}), "idle")
        self.assertEqual(collector.codex_phase({"type": "notLoaded"}), "closed")
        self.assertEqual(collector.codex_phase(None), "closed")


class MessageTest(unittest.TestCase):
    def test_claude_messages_hide_harness_wrappers(self):
        entries = [
            {"type": "user", "timestamp": "2026-10-07T01:00:00Z",
             "message": {"content": "<system-reminder>숨김</system-reminder>\n정산 확인해줘"}},
            {"type": "user", "message": {"content": "<task-notification><task-id>x</task-id></task-notification>"}},
            {"type": "user", "message": {"content": "<command-name>/model</command-name>"}},
            {"type": "user", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "숨김"},
                {"type": "text", "text": "확인하겠습니다."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls\n-la", "description": "목록 보기"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "결과"}]}},
            {"type": "attachment", "message": {"content": "x"}},
        ]
        got = [(m["role"], m["text"]) for m in collector.claude_messages(entries)]
        self.assertEqual(got, [("user", "정산 확인해줘"), ("system", "백그라운드 작업 알림"), ("system", "명령 /model"),
                               ("assistant", "확인하겠습니다."), ("tool", "Bash · 목록 보기")])
        self.assertEqual(collector.claude_messages(entries[:1])[0]["ts"], 1791334800)

    def test_codex_messages(self):
        turns = [{"startedAt": 1791334800, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "배포해"}, {"type": "image"}]},
            {"type": "reasoning", "summary": ["숨김"]},
            {"type": "commandExecution", "command": ["git", "status"]},
            {"type": "fileChange", "changes": [{"path": "a.py"}, {"path": "b.py"}]},
            {"type": "mcpToolCall", "server": "slack", "tool": "send"},
            {"type": "agentMessage", "text": "완료했습니다."},
        ]}]
        got = [(m["role"], m["text"]) for m in collector.codex_messages(turns)]
        self.assertEqual(got, [("user", "배포해"), ("tool", "$ git status"), ("tool", "파일 수정 · a.py, b.py"),
                               ("tool", "slack send"), ("assistant", "완료했습니다.")])

    def test_tail_entries_drops_cut_first_line(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as f:
            for i in range(50):
                f.write((json.dumps({"type": "user", "n": i, "pad": "x" * 40}) + "\n").encode())
        entries = list(collector.tail_entries(f.name, 300))
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["n"], 49)
        self.assertTrue(all("n" in e for e in entries))

    def test_to_epoch(self):
        self.assertEqual(collector.to_epoch(1791334800), 1791334800)
        self.assertEqual(collector.to_epoch(1791334800123), 1791334800)
        self.assertEqual(collector.to_epoch("2026-10-07T01:00:00Z"), 1791334800)
        self.assertIsNone(collector.to_epoch("not a date"))
        self.assertIsNone(collector.to_epoch(True))

    def test_read_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            collector.read("claude", "../../etc/passwd")
        with self.assertRaises(ValueError):
            collector.read("codex", "019a0000-0000-7000-8000-000000000000", "../x")
        with self.assertRaises(ValueError):
            collector.read("gemini", "019a0000-0000-7000-8000-000000000000")


if __name__ == "__main__":
    unittest.main()
