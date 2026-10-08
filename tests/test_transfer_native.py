import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector
import launch
import transfer_native as native


class FakeAppServer:
    def __init__(self, reads, queues=None):
        self.reads = iter(reads)
        self.queues = iter(queues) if queues is not None else None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/queue/list":
            response = next(self.queues) if self.queues is not None else {"data": [], "nextCursor": None}
            if isinstance(response, Exception):
                raise response
            return response
        if method == "turn/interrupt":
            return {}
        if method != "thread/read":
            raise AssertionError("unexpected native command: " + method)
        response = next(self.reads)
        if isinstance(response, Exception):
            raise response
        return response


class TransferNativeTest(unittest.TestCase):
    def setUp(self):
        self.source = {"agent": "codex", "home": ".codex", "id": str(uuid.uuid4())}
        self.claude = {"agent": "claude", "home": ".claude", "id": "a1b2c3d4"}

    def thread(self, kind="idle", turns=None, updated=1, flags=None):
        status = {"type": kind}
        if flags is not None:
            status["activeFlags"] = flags
        return {"thread": {"id": self.source["id"], "status": status,
                           "turns": [] if turns is None else turns, "updatedAt": updated}}

    def state(self, response):
        with patch.object(native, "_client", return_value=FakeAppServer([response])):
            return native.source_state(self.source)

    def test_profiles_delegates_without_running_native(self):
        with patch.object(launch, "discover_profiles", return_value=[{"id": "fixture"}]) as discover:
            self.assertEqual(native.profiles("/fixture"), [{"id": "fixture"}])
        discover.assert_called_once_with("/fixture")

    def test_idle_and_closed_are_confirmed_with_revision(self):
        for kind, phase in (("idle", "idle"), ("notLoaded", "closed")):
            with self.subTest(kind=kind):
                state = self.state(self.thread(kind))
                self.assertTrue(state["safeToTransfer"])
                self.assertTrue(state["confirmed"])
                self.assertEqual(state["phase"], phase)
                self.assertEqual(len(state["revision"]), 64)
        self.assertNotEqual(self.state(self.thread(updated=1))["revision"],
                            self.state(self.thread(updated=2))["revision"])

    def test_unknown_mismatch_and_conflicting_runtime_fail_closed(self):
        mismatch = self.thread()
        mismatch["thread"]["id"] = str(uuid.uuid4())
        for response in (mismatch, {}, self.thread("systemError"),
                         self.thread("idle", [{"id": "t", "status": "inProgress"}]),
                         self.thread("idle", [{"id": "t", "status": "unknown"}]),
                         self.thread("idle", flags="malformed")):
            with self.subTest(response=response):
                state = self.state(response)
                self.assertFalse(state["confirmed"])
                self.assertFalse(state["safeToTransfer"])

    def test_pending_message_prevents_idle_export_and_active_interrupt(self):
        for kind, turns in (("idle", []), ("active", [{"id": "current", "status": "inProgress"}])):
            client = FakeAppServer([self.thread(kind, turns)], [{"data": [{"id": "pending"}], "nextCursor": None}])
            with self.subTest(kind=kind), patch.object(native, "_client", return_value=client):
                result = native.interrupt_source(self.source)
            self.assertFalse(result["safeToTransfer"])
            self.assertFalse(result["canInterrupt"])
            self.assertFalse(result["interrupted"])
            self.assertFalse(result["confirmed"])
            self.assertIn("대기 중인 메시지", result["reason"])
            self.assertFalse(any(method == "turn/interrupt" for method, unused in client.calls))
            self.assertEqual([params for method, params in client.calls if method == "thread/queue/list"],
                             [{"threadId": self.source["id"], "limit": 1}])

    def test_unavailable_or_malformed_queue_blocks_transfer_without_interrupt(self):
        for response in (collector.RpcError("unsupported"), TimeoutError(), {},
                         {"data": None}, {"data": ["invalid"]}, {"data": [], "nextCursor": "more"}):
            client = FakeAppServer([self.thread("active", [{"id": "current", "status": "inProgress"}])], [response])
            with self.subTest(response=type(response).__name__), patch.object(native, "_client", return_value=client):
                result = native.interrupt_source(self.source)
            self.assertFalse(result["safeToTransfer"])
            self.assertFalse(result["canInterrupt"])
            self.assertFalse(result["interrupted"])
            self.assertIn("대기열", result["reason"])
            self.assertFalse(any(method == "turn/interrupt" for method, unused in client.calls))

    def test_pending_message_appearing_after_interrupt_stops_export_confirmation(self):
        client = FakeAppServer([self.thread("active", [{"id": "current", "status": "inProgress"}]),
                                self.thread("idle", [{"id": "current", "status": "interrupted"}])],
                               [{"data": []}, {"data": [{"id": "pending"}]}])
        with patch.object(native, "_client", return_value=client), patch.object(native.time, "sleep") as sleep:
            result = native.interrupt_source(self.source)
        self.assertTrue(result["interrupted"])
        self.assertFalse(result["safeToTransfer"])
        self.assertFalse(result["confirmed"])
        self.assertIn("대기 중인 메시지", result["reason"])
        sleep.assert_not_called()
        self.assertEqual(sum(method == "turn/interrupt" for method, unused in client.calls), 1)

    def test_native_initialization_enables_only_experimental_queue_api(self):
        client = object.__new__(native._DeadlineAppServer)
        with patch.object(collector.AppServer, "call", return_value={}) as call:
            client.call("initialize", {"clientInfo": {"name": "fixture"}})
            client.call("thread/read", {"threadId": self.source["id"]})
        self.assertEqual(call.call_args_list[0].args,
                         ("initialize", {"clientInfo": {"name": "fixture"}, "capabilities": {"experimentalApi": True}}))
        self.assertEqual(call.call_args_list[1].args, ("thread/read", {"threadId": self.source["id"]}))

    def test_interrupt_exact_active_turn_then_confirm_native_completion(self):
        active = [{"id": "exact-turn", "status": "inProgress"}]
        ended = [{"id": "exact-turn", "status": "interrupted"}]
        client = FakeAppServer([self.thread("active", active, flags=["waitingOnUserInput"]),
                                self.thread("active", active), self.thread("idle", ended, updated=2)])
        with patch.object(native, "_client", return_value=client), patch.object(native.time, "sleep"):
            result = native.interrupt_source(self.source)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["safeToTransfer"])
        self.assertEqual(result["interruptedTurnId"], "exact-turn")
        self.assertEqual([call for call in client.calls if call[0] == "turn/interrupt"],
                         [("turn/interrupt", {"threadId": self.source["id"], "turnId": "exact-turn"})])
        self.assertTrue(all(params == {"threadId": self.source["id"], "includeTurns": True}
                            for method, params in client.calls if method == "thread/read"))

    def test_new_turn_after_interrupt_is_not_interrupted_or_exported(self):
        client = FakeAppServer([self.thread("active", [{"id": "old", "status": "inProgress"}]),
                                self.thread("active", [{"id": "old", "status": "interrupted"},
                                                       {"id": "new", "status": "inProgress"}])])
        with patch.object(native, "_client", return_value=client):
            result = native.interrupt_source(self.source)
        self.assertFalse(result["safeToTransfer"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(sum(method == "turn/interrupt" for method, _ in client.calls), 1)

    def test_idle_and_ambiguous_active_do_not_issue_interrupt(self):
        for response in (self.thread(), self.thread("active", [
                {"id": "one", "status": "inProgress"}, {"id": "two", "status": "inProgress"}])):
            client = FakeAppServer([response])
            with patch.object(native, "_client", return_value=client):
                result = native.interrupt_source(self.source)
            self.assertFalse(result["interrupted"])
            self.assertFalse(any(method == "turn/interrupt" for method, _ in client.calls))

    def test_timeout_after_interrupt_does_not_confirm(self):
        client = FakeAppServer([self.thread("active", [{"id": "t", "status": "inProgress"}]), TimeoutError()])
        with patch.object(native, "_client", return_value=client):
            result = native.interrupt_source(self.source)
        self.assertTrue(result["interrupted"])
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["safeToTransfer"])

    def claude_row(self, state="working", kind="background", **extra):
        return json.dumps([{"id": self.claude["id"], "kind": kind, "state": state,
                            "status": "busy" if state == "working" else "idle", **extra}]).encode()

    def test_claude_background_stop_exact_id_then_confirm(self):
        with patch.object(native, "_claude_binary", return_value="/fixture/claude"), \
                patch.object(native, "_claude_run", side_effect=[self.claude_row(), b"", self.claude_row("stopped")]) as run:
            result = native.interrupt_source(self.claude)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["interrupted"])
        self.assertEqual([call.args[1] for call in run.call_args_list],
                         [["agents", "--json", "--all"], ["stop", "a1b2c3d4"], ["agents", "--json", "--all"]])

    def test_interactive_claude_returns_original_terminal_requirement(self):
        with patch.object(native, "_claude_binary", return_value="/fixture/claude"), \
                patch.object(native, "_claude_run", return_value=self.claude_row(kind="interactive")) as run:
            result = native.interrupt_source(self.claude)
        self.assertTrue(result["requiresBoardInterrupt"])
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["canInterrupt"])
        run.assert_called_once()

    def test_claude_timeout_and_unknown_fail_closed(self):
        for failure in (subprocess.TimeoutExpired("fixture", 1), ValueError("fixture")):
            with patch.object(native, "_claude_binary", return_value="/fixture/claude"), \
                    patch.object(native, "_claude_run", side_effect=failure):
                self.assertFalse(native.source_state(self.claude)["safeToTransfer"])
                self.assertFalse(native.interrupt_source(self.claude)["confirmed"])
        with patch.object(native, "_claude_binary", return_value="/fixture/claude"), \
                patch.object(native, "_claude_run", return_value=self.claude_row("unexpected")):
            self.assertFalse(native.source_state(self.claude)["confirmed"])

    def test_claude_transcript_stat_changes_revision_without_reading_content(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            transcript = home / ".claude/projects/project/session-12345.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("private fixture")
            with patch.object(native, "_claude_binary", return_value="/fixture/claude"), \
                    patch.object(native, "_claude_run", return_value=self.claude_row("idle", sessionId="session-12345")), \
                    patch.object(Path, "read_text", side_effect=AssertionError("transcript must not be opened")):
                before = native.source_state(self.claude, home=home)
                transcript.write_bytes(b"different-size private fixture")
                after = native.source_state(self.claude, home=home)
            self.assertTrue(before["confirmed"])
            self.assertNotEqual(before["revision"], after["revision"])
            self.assertNotIn("private fixture", json.dumps(after))

    def test_native_private_socket_symlink_is_supported_but_writable_parent_denied(self):
        with tempfile.TemporaryDirectory(prefix="sb-", dir="/tmp") as temp:
            home = Path(temp).resolve()
            private = home / "runtime"
            private.mkdir(mode=0o700)
            endpoint = private / "daemon.sock"
            path = home / ".codex" / launch.SOCKET_REL
            path.parent.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.bind(str(endpoint))
                path.symlink_to(endpoint)
                self.assertEqual(native._socket_path(self.source, home), path)
                private.chmod(0o777)
                with self.assertRaises(ValueError):
                    native._socket_path(self.source, home)


class TransferredLaunchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.destination = self.home / ".local/share/sessionholic/workspaces/copied project"
        self.destination.mkdir(parents=True)
        self.source = {"host": "local", "agent": "codex", "home": ".codex", "id": str(uuid.uuid4()),
                       "cwd": "/Users/local/work", "phase": "idle"}
        self.metadata = {"transferId": "fixture-transfer", "sourceHost": "local", "targetHost": "remote",
                         "sourceCwd": self.source["cwd"], "destinationCwd": str(self.destination),
                         "projectRoot": self.source["cwd"], "originalHead": "abc123", "sourceStopped": True, "sourceEnvironment": "default"}
        config = self.home / ".config/sessionholic/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"version": 1, "environments": {"isolated": {
            "variables": {"GH_CONFIG_DIR": "~/.config/fixture-git/gh", "GIT_CONFIG_NOSYSTEM": "1"}}},
            "profiles": [{"agent": "codex", "home": ".codex-isolated", "environment": "isolated"}],
            "projects": [{"id": "isolated", "label": "Isolated", "root": str(self.home / "Isolated"), "environment": "isolated"}]}))
        self.messages = [{"role": "user", "text": "변경 파일 확인 후 이어서 작업"}]
        self.binary = patch.object(launch, "_binary", side_effect=lambda agent, home: "/native/" + agent)
        self.binary.start()
        self.git = patch.object(launch, "_git_metadata", return_value={"available": False})
        self.git.start()

    def tearDown(self):
        self.git.stop()
        self.binary.stop()
        self.temp.cleanup()

    def target(self, key=".codex", agent="codex"):
        profile = self.home / key
        profile.mkdir(parents=True, exist_ok=True)
        if agent == "codex":
            (profile / "auth.json").write_text("fixture: never read")
        return {"host": "remote", "agent": agent, "home": key,
                "id": "codex:" + key if agent == "codex" else "claude:default"}

    def build(self, target=None):
        return launch.build_transferred_launch(self.source, self.target() if target is None else target,
                                              self.messages, self.home / "state", self.destination,
                                              self.metadata, home=self.home)

    def test_same_route_always_builds_new_native_session_and_private_handoff(self):
        with patch.object(subprocess, "run") as run:
            result = self.build()
        run.assert_not_called()
        self.assertEqual(result["mode"], "transfer")
        self.assertEqual(result["argv"][:3], ["/native/codex", "--cd", str(self.destination)])
        self.assertNotIn("resume", result["argv"])
        self.assertNotIn("--remote", result["argv"])
        path = Path(result["handoffPath"])
        text = path.read_text()
        record = json.loads(text[text.index("{\n"):])
        self.assertEqual(record["transfer"], self.metadata)
        self.assertEqual(record["source"]["id"], self.source["id"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_environment_boundary_uses_original_path_not_import_location(self):
        self.source["cwd"] = "/Users/local/Isolated/project"
        self.metadata["sourceCwd"] = self.metadata["projectRoot"] = self.source["cwd"]
        self.metadata["sourceEnvironment"] = "isolated"
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build()
        result = self.build(self.target(".codex-isolated"))
        self.assertEqual(result["env"]["GH_CONFIG_DIR"], str(self.home / ".config/fixture-git/gh"))
        self.assertEqual(result["env"]["GIT_CONFIG_NOSYSTEM"], "1")

    def imported_claude(self, environment):
        tid = "a" * 32
        cwd = self.home / ".local/share/sessionholic/workspaces" / tid / "project/subdir"
        cwd.mkdir(parents=True)
        record = self.home / ".local/state/sessionholic-transfers" / tid / "transfer.json"
        record.parent.mkdir(parents=True, mode=0o700)
        record.write_text(json.dumps({"sourceEnvironment": environment}))
        record.chmod(0o600)
        source = {"host": "remote", "agent": "claude", "home": ".claude", "id": "a1b2c3d4",
                  "cwd": str(cwd), "phase": "idle"}
        return source, record

    def test_imported_claude_local_switch_keeps_original_environment_boundary(self):
        source, record = self.imported_claude("isolated")
        self.assertEqual(launch.source_environment(source, self.home), "isolated")
        with self.assertRaisesRegex(ValueError, "환경"):
            launch.build_launch(source, self.target(), self.messages, self.home / "state", home=self.home)
        result = launch.build_launch(source, self.target(".codex-isolated"), self.messages,
                                     self.home / "state", home=self.home)
        self.assertEqual(result["env"]["GH_CONFIG_DIR"], str(self.home / ".config/fixture-git/gh"))
        record.write_text(json.dumps({"sourceEnvironment": "default"}))
        self.assertEqual(launch.source_environment(source, self.home), "default")

    def test_imported_claude_roundtrip_uses_trusted_orchestration_classification(self):
        self.source.update(agent="claude", home=".claude", id="a1b2c3d4",
                           cwd="/Users/source/.local/share/sessionholic/workspaces/" + "a" * 32 + "/project")
        self.metadata["sourceCwd"] = self.metadata["projectRoot"] = self.source["cwd"]
        self.metadata["sourceEnvironment"] = "isolated"
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build()
        result = self.build(self.target(".codex-isolated"))
        self.assertEqual(result["env"]["CODEX_HOME"], str(self.home / ".codex-isolated"))
        record = Path(result["handoffPath"]).read_text()
        self.assertIn('"sourceEnvironment": "isolated"', record)
        self.metadata["sourceEnvironment"] = True
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build(self.target(".codex-isolated"))

    def test_imported_classification_missing_unsafe_or_invalid_group_fails_closed(self):
        source, record = self.imported_claude("isolated")
        for contents in ({}, {"sourceEnvironment": True}, {"sourceEnvironment": 1}, {"sourceEnvironment": "../unsafe"}):
            record.write_text(json.dumps(contents))
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                launch.source_environment(source, self.home)
        record.write_text(json.dumps({"sourceEnvironment": "isolated"}))
        record.chmod(0o644)
        with self.assertRaises(ValueError):
            launch.source_environment(source, self.home)
        record.chmod(0o600)
        other = self.home / "private-record.json"
        record.rename(other)
        with self.assertRaises(ValueError):
            launch.source_environment(source, self.home)
        record.symlink_to(other)
        with self.assertRaises(ValueError):
            launch.source_environment(source, self.home)

    def test_original_environment_profile_and_resolved_path_are_preserved(self):
        self.assertEqual(launch.source_environment(dict(self.source, home=".codex-isolated"), self.home), "isolated")
        environment = self.home / "Isolated/project"
        environment.mkdir(parents=True)
        link = self.home / "project-link"
        link.symlink_to(environment, target_is_directory=True)
        self.assertEqual(launch.source_environment(dict(self.source, cwd=str(link)), self.home), "isolated")

    def test_claude_new_session_and_inherited_credentials_removed(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "CODEX_THREAD_ID": "other",
                                     "CLAUDE_CONFIG_DIR": "/other", "CLAUDECODE": "1"}):
            result = self.build(self.target(".claude", "claude"))
        self.assertEqual(len(result["argv"]), 2)
        self.assertEqual(result["argv"][0], "/native/claude")
        self.assertTrue({"OPENAI_API_KEY", "CODEX_THREAD_ID", "CLAUDE_CONFIG_DIR", "CLAUDECODE"}.isdisjoint(result["env"]))

    def test_unconfirmed_source_and_mismatched_paths_do_not_write_handoff(self):
        original = self.metadata.copy()
        for field, value in (("sourceStopped", False), ("sourceCwd", "/different"),
                             ("destinationCwd", str(self.home)), ("targetHost", "local"),
                             ("projectRoot", "relative")):
            self.metadata = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build()
            self.assertFalse((self.home / "state").exists())

    def test_transfer_metadata_and_conversation_are_redacted(self):
        self.messages = [{"role": "user", "text": "contact fixture@example.test and sk-proj-" + "x" * 30}]
        self.metadata["note"] = "Bearer fixture-token-123456"
        text = Path(self.build()["handoffPath"]).read_text()
        self.assertNotIn("fixture@example.test", text)
        self.assertNotIn("fixture-token-123456", text)
        self.assertNotIn("sk-proj-", text)


if __name__ == "__main__":
    unittest.main()
