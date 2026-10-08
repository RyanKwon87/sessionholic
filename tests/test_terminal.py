import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terminal  # noqa: E402


class BufferTest(unittest.TestCase):
    def test_bounded_output_and_reconnect_cursor(self):
        stream = terminal._Terminal({"id": "a" * 32}, 8)
        stream.append(b"abcdef")
        first = stream.read(0, 0)
        self.assertEqual(base64.b64decode(first["data"]), b"abcdef")
        self.assertEqual(first["after"], 6)
        self.assertFalse(first["reset"])
        stream.append(b"ghijkl")
        stale = stream.read(0, 0)
        self.assertTrue(stale["reset"])
        self.assertEqual(base64.b64decode(stale["data"]), b"efghijkl")
        current = stream.read(6, 0)
        self.assertEqual(base64.b64decode(current["data"]), b"ghijkl")
        self.assertFalse(current["reset"])
        self.assertEqual(stream.read(12, 0)["data"], "")
        self.assertTrue(stream.read(999, 0)["reset"])

    def test_public_metadata_cannot_override_identity(self):
        stream = terminal._Terminal({"id": "a" * 32, "key": "fixture",
            "metadata": {"id": "evil", "alive": False, "closed": True, "title": "fixture title"}}, 8)
        public = terminal.TerminalManager._public(stream)
        self.assertEqual(public["id"], "a" * 32)
        self.assertEqual(public["title"], "fixture title")
        self.assertTrue(public["alive"])
        self.assertFalse(public["closed"])

    def test_epoch_reset_replays_full_buffer_even_when_offsets_overlap(self):
        stream = terminal._Terminal({"id": "a" * 32}, 64)
        stream.append(b"full-new-screen")
        reset = stream.read(4, 0, "0" * 32)
        self.assertTrue(reset["reset"])
        self.assertEqual(base64.b64decode(reset["data"]), b"full-new-screen")
        current = stream.read(reset["after"], 0, reset["epoch"])
        self.assertFalse(current["reset"])
        self.assertEqual(current["data"], "")

    def test_closed_output_does_not_wait(self):
        stream = terminal._Terminal({"id": "a" * 32}, 8)
        stream.alive = False
        started = time.monotonic()
        self.assertFalse(stream.read(0, 20)["alive"])
        self.assertLess(time.monotonic() - started, 0.5)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = terminal.TerminalManager(state_dir=self.directory.name,
                                                socket_name="sb-fixture-unused")

    def tearDown(self):
        self.manager.close()
        self.directory.cleanup()

    def test_reservations_protect_capacity_and_release_on_failure(self):
        self.manager.max_terminals = 1
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with self.manager.reserve('handoff'):
                with self.assertRaisesRegex(RuntimeError, '가득'):
                    with self.manager.reserve('other'):
                        self.fail('A second launch claimed the reserved slot')
                with patch.object(self.manager, '_run') as run:
                    with self.assertRaisesRegex(RuntimeError, '초과'):
                        self.manager.create('unreserved', ['/bin/cat'], self.directory.name, {})
                    run.assert_not_called()
                raise RuntimeError('fixture failure before launch')
        with self.manager.reserve('next'):
            self.assertEqual(self.manager.reservations, {'next'})
        self.assertEqual(self.manager.reservations, set())

    def test_reserved_launch_uses_its_own_slot(self):
        self.manager.max_terminals = 1
        with self.manager.reserve('handoff'), patch.object(self.manager, '_run') as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, '시작하지'):
                self.manager.create('handoff', ['/bin/cat'], self.directory.name, {})
            self.assertEqual(run.call_count, 1)

    def test_natural_exit_tombstone_is_not_exposed_before_it_is_saved(self):
        row = {'id': 'a'*32, 'name': 'sb-'+'a'*32, 'key': 'fixture', 'metadata': {}}
        instance = terminal._Terminal(row, 1024)
        self.manager.terminals[row['id']] = instance
        with patch.object(self.manager, '_exists', return_value=False), \
                patch.object(self.manager, '_save', side_effect=OSError('fixture full disk')):
            with self.assertRaises(OSError):
                self.manager.list()
            self.assertFalse(instance.record.get('closed', False))
            with self.assertRaises(OSError):
                self.manager.close_terminal(row['id'])
        self.assertFalse(instance.record.get('closed', False))
        self.manager.terminals.clear()

    def test_rejects_untrusted_ids_and_payloads_before_execution(self):
        for value in ("../../x", "-Ldefault", "a" * 31, 5):
            with self.assertRaises(ValueError):
                self.manager.read(value, 0, 0)
        for after in (-1, True, "0"):
            with self.assertRaises(ValueError):
                self.manager.read("a" * 32, after, 0)
        for wait in (-1, 21, float("nan"), True):
            with self.assertRaises(ValueError):
                self.manager.read("a" * 32, 0, wait)
        for cols, rows in ((1, 24), (80, 301), (True, 24), (80, "24")):
            with self.assertRaises(ValueError):
                self.manager.resize("a" * 32, cols, rows)
        with self.assertRaises(ValueError):
            self.manager.write("a" * 32, "가" * 12000)
        with self.assertRaises(KeyError):
            self.manager.read("a" * 32, 0, 0)
        with self.assertRaises(ValueError):
            self.manager.read("a" * 32, 0, 0, epoch="untrusted")

    def test_launch_requires_absolute_server_chosen_command(self):
        for argv in (["sh", "-c", "true"], [], ["/bin/echo", "\0"]):
            with self.assertRaises(ValueError):
                self.manager.create("key", argv, self.directory.name, {})
        with self.assertRaises(ValueError):
            self.manager.create("key", ["/bin/echo"], self.directory.name, {"BAD=KEY": "x"})
        with self.assertRaises(ValueError):
            self.manager.create("key", ["/bin/echo"], self.directory.name, {}, {"x": {"nested": True}})

    def test_native_source_is_the_only_bounded_nested_metadata(self):
        native = {"host": "local", "agent": "codex", "id": "019aaaaa-0000-7000-8000-000000000001",
                  "home": ".codex", "cwd": self.directory.name, "kind": None}
        self.assertEqual(terminal._metadata({"nativeSource": native})["nativeSource"], native)
        claude = {**native, "agent": "claude", "home": ".claude", "kind": "interactive",
                  "sessionId": native["id"]}
        self.assertEqual(terminal._metadata({"nativeSource": claude})["nativeSource"], claude)
        for change in ({"extra": "forbidden"}, {"id": "../session"}, {"home": "../account"},
                       {"host": ["local"]}, {"cwd": "relative"}, {"cwd": "/" + "a" * 4096},
                       {"kind": {"nested": True}}, {"sessionId": native["id"]}, {"agent": "unknown"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manager.create("key", ["/bin/echo"], self.directory.name, {},
                                    {"nativeSource": {**native, **change}})
        with self.assertRaises(ValueError):
            terminal._metadata({"nativeSource": native, "title": "가" * 3000})
        with self.assertRaises(ValueError):
            terminal._metadata({"nativeSource": {"host": "local"}})

    def test_restore_rejects_invalid_nested_metadata(self):
        self.manager.close()
        row = {"id": "a" * 32, "name": "sb-" + "a" * 32, "key": "fixture",
               "metadata": {"nativeSource": {"host": "local"}}}
        (Path(self.directory.name) / "sessions.json").write_text(json.dumps([row]))
        with self.assertRaisesRegex(RuntimeError, "상태 파일 형식"):
            terminal.TerminalManager(state_dir=self.directory.name)

    def test_close_rejects_invalid_and_unknown_ids_without_tmux(self):
        with patch.object(self.manager, "_run") as run:
            for value in ("../../x", "-Ldefault", "a" * 31, 5):
                with self.assertRaises(ValueError):
                    self.manager.close_terminal(value)
            with self.assertRaises(KeyError):
                self.manager.close_terminal("a" * 32)
            run.assert_not_called()

    def test_private_state_directory_and_symlink_rejection(self):
        self.assertEqual(Path(self.directory.name).stat().st_mode & 0o777, 0o700)
        link = Path(self.directory.name) / "linked"
        link.symlink_to(self.directory.name, target_is_directory=True)
        with self.assertRaises(ValueError):
            terminal.TerminalManager(state_dir=link)

    def test_second_gateway_cannot_start_duplicate_commands(self):
        with self.assertRaises(RuntimeError):
            terminal.TerminalManager(state_dir=self.directory.name)
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name)
        replacement.close()

    def test_tmux_configuration_failure_is_not_silently_ignored(self):
        stream = terminal._Terminal({"name": "sb-" + "a" * 32}, 8)
        failed = subprocess.CompletedProcess([], 1)
        with patch.object(self.manager, "_run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "화면 설정"):
                self.manager._configure(stream)


@unittest.skipUnless(shutil.which("tmux"), "tmux fixture requires installed tmux")
class TmuxFixtureTest(unittest.TestCase):
    """Only a Python echo fixture runs in a unique tmux server, never an agent."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.socket = "sb-test-" + secrets.token_hex(8)
        self.manager = terminal.TerminalManager(state_dir=self.directory.name,
                                                socket_name=self.socket, max_terminals=1)
        self.managers = [self.manager]
        self.launch_count = Path(self.directory.name) / "fixture-starts"
        self.code = (
            "import os,sys\n"
            "with open(sys.argv[1], 'a') as f: f.write('start\\n')\n"
            "print('FIXTURE_READY:' + os.environ.get('SB_FIXTURE_ACCOUNT',''), flush=True)\n"
            "for line in sys.stdin:\n"
            " print('FIXTURE_REPLY:' + line.strip(), flush=True)\n"
        )

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        # This unique test socket is the only server this fixture may stop.
        subprocess.run([shutil.which("tmux"), "-L", self.socket, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.directory.cleanup()

    def create(self, key="fixture", account="account-one"):
        env = {"PATH": os.environ.get("PATH", ""), "SB_FIXTURE_ACCOUNT": account,
               "LANG": "en_US.UTF-8"}
        return self.manager.create(key, [sys.executable, "-u", "-c", self.code, str(self.launch_count)],
                                   self.directory.name, env, {"agent": "fixture", "host": "local"})

    def wait_for(self, manager, terminal_id, expected, after=0):
        deadline = time.monotonic() + 8
        collected = b""
        while time.monotonic() < deadline:
            result = manager.read(terminal_id, after, 0.5)
            collected += base64.b64decode(result["data"])
            after = result["after"]
            if expected in collected:
                return after
        self.fail("synthetic fixture terminal output did not arrive")

    def test_real_pty_echo_resize_reuse_detach_and_server_restart(self):
        created = self.create()
        terminal_id = created["id"]
        self.assertTrue(created["alive"])
        status = subprocess.run([shutil.which("tmux"), "-L", self.socket, "show-options", "-v",
                                 "-t", "sb-" + terminal_id, "status"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(status.stdout.strip(), "off")
        cursor = self.wait_for(self.manager, terminal_id, b"FIXTURE_READY:account-one")
        self.manager.resize(terminal_id, 90, 28)
        self.manager.write(terminal_id, "hello-fixture\n")
        cursor = self.wait_for(self.manager, terminal_id, b"FIXTURE_REPLY:hello-fixture", cursor)
        same = self.create(account="account-two")
        self.assertEqual(same["id"], terminal_id)
        self.assertEqual(self.launch_count.read_text(), "start\n")
        self.manager.detach(terminal_id)
        self.assertTrue(self.manager.list()[0]["alive"])
        self.manager.write(terminal_id, "after-detach\n")
        self.wait_for(self.manager, terminal_id, b"FIXTURE_REPLY:after-detach", cursor)
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        self.assertEqual(replacement.list()[0]["id"], terminal_id)
        replacement.write(terminal_id, "after-restart\n")
        self.wait_for(replacement, terminal_id, b"FIXTURE_REPLY:after-restart")
        self.assertEqual(self.launch_count.read_text(), "start\n")
        saved = json.loads((Path(self.directory.name) / "sessions.json").read_text())
        self.assertEqual(saved[0]["metadata"], {"agent": "fixture", "host": "local"})
        self.assertNotIn("argv", saved[0])
        self.assertNotIn("env", saved[0])
        self.assertEqual((Path(self.directory.name) / "sessions.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(Path(self.directory.name).glob("*.launch.json")), [])

    def test_workflow_native_binding_launches_real_echo_and_survives_restart(self):
        import server
        self.manager.max_terminals = 4
        hosts = [{"name": "local", "label": "Fixture", "local": True},
                 {"name": "remote", "label": "Remote fixture", "local": False, "ssh": "never-used"}]
        source = {"id": "019aaaaa-0000-7000-8000-000000000001", "agent": "codex", "home": ".codex",
                  "cwd": self.directory.name, "phase": "idle", "title": "Echo fixture"}
        def runner(host, args, timeout):
            return ({"codex": [source], "claude": []} if args[0] == "snapshot" else
                    {"messages": [{"role": "user", "text": "fixture-only"}]})
        board = server.Board(hosts, 60, runner)
        for host in hosts: board.poll(host)
        flow = server.Workflow(board, self.manager, self.directory.name)
        profiles = [{"id": "codex:.codex", "agent": "codex", "home": ".codex", "available": True, "label": "Fixture"},
                    {"id": "codex:.codex-alt", "agent": "codex", "home": ".codex-alt", "available": True, "label": "Other"}]
        flow.profiles = lambda: profiles
        spec = {"argv": [sys.executable, "-u", "-c", self.code, str(self.launch_count)],
                "cwd": self.directory.name, "env": {"PATH": os.environ.get("PATH", "")}}
        native = {"host": "local", "agent": "codex", "id": source["id"],
                  "home": ".codex-alt", "cwd": self.directory.name}
        ref = {"host": "local", "agent": "codex", "home": ".codex", "id": source["id"]}
        expected = {}
        # Replace every native/SSH launch boundary with this local Python echo.
        with patch('launch.build_launch', return_value=spec), \
             patch('managed_launch.bind', side_effect=lambda value, *a, **kw: {**value, "nativeSource": native}), \
             patch.object(flow.transfers, 'profiles', return_value=profiles), \
             patch.object(flow.transfers, 'preview', return_value={"workspace": {"sourceEnvironment": "default"}}), \
             patch.object(flow.transfers, 'execute', return_value={**spec, "transferId": "a" * 32,
                          "destinationCwd": self.directory.name, "nativeSource": {**native, "host": "remote"}}), \
             patch.object(flow.transfers, 'mark_started'):
            for index, (mode, target_host, account) in enumerate((
                    ("attach", "local", "codex:.codex"), ("handoff", "local", "codex:.codex-alt"),
                    ("transfer", "remote", "codex:.codex-alt"))):
                plan = flow.plan({"source": ref, "target": {"host": target_host, "agent": "codex", "account": account}})
                self.assertTrue(plan["allowed"])
                self.assertEqual(plan["mode"], mode)
                created = flow.launch(plan["id"], "echo-request-000" + str(index))["terminal"]
                expected[created["id"]] = json.loads(json.dumps(created["nativeSource"]))
                self.wait_for(self.manager, created["id"], b"FIXTURE_READY")
                self.manager.write(created["id"], mode + "-fixture\n")
                self.wait_for(self.manager, created["id"], ("FIXTURE_REPLY:" + mode + "-fixture").encode())
                created["nativeSource"]["id"] = "changed-outside-manager"
                created["metadata"]["nativeSource"]["cwd"] = "/changed-outside-manager"
        native["cwd"] = "/changed-original-input"
        self.assertEqual({row["id"]: row["nativeSource"] for row in self.manager.list()}, expected)
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        self.assertEqual({row["id"]: row["nativeSource"] for row in replacement.list()}, expected)
        self.assertEqual(self.launch_count.read_text(), "start\n" * 3)

    def test_terminal_limit_and_dead_sessions_are_not_implicitly_restarted(self):
        created = self.create()
        self.wait_for(self.manager, created["id"], b"FIXTURE_READY")
        with self.assertRaises(RuntimeError):
            self.create(key="another-fixture")
        subprocess.run([shutil.which("tmux"), "-L", self.socket, "kill-session", "-t", "=sb-" + created["id"]],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertFalse(self.manager.list()[0]["alive"])
        self.assertTrue(self.manager.list()[0]["closed"])
        saved = json.loads(self.manager.state_path.read_text())
        self.assertTrue(saved[0]['closed'])
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(self.launch_count.read_text(), "start\n")

    def test_close_is_exact_idempotent_releases_quota_and_survives_restart(self):
        self.manager.max_terminals = 4
        first = self.create()
        second = self.create(key="other-fixture")
        other_ids = [second["id"]]
        for key in ("third-fixture", "fourth-fixture"):
            other = self.create(key=key)
            other_ids.append(other["id"])
            self.wait_for(self.manager, other["id"], b"FIXTURE_READY")
        self.wait_for(self.manager, first["id"], b"FIXTURE_READY")
        self.wait_for(self.manager, second["id"], b"FIXTURE_READY")
        with self.assertRaises(RuntimeError):
            self.create(key="over-limit")
        self.assertEqual(self.manager.close_terminal(first["id"]), {"ok": True})
        self.assertEqual(self.manager.close_terminal(first["id"]), {"ok": True})
        probe = subprocess.run([shutil.which("tmux"), "-L", self.socket, "has-session",
                                "-t", "=sb-" + first["id"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.assertNotEqual(probe.returncode, 0)
        rows = {row["id"]: row for row in self.manager.list()}
        self.assertFalse(rows[first["id"]]["alive"])
        self.assertTrue(rows[first["id"]]["closed"])
        self.assertTrue(all(rows[tid]["alive"] for tid in other_ids))
        self.assertFalse(self.manager.read(first["id"], wait=0)["alive"])
        with self.assertRaises(RuntimeError):
            self.manager.write(first["id"], "must-not-run\n")
        with self.assertRaises(RuntimeError):
            self.manager.resize(first["id"], 80, 24)
        with self.assertRaises(RuntimeError):
            self.create()
        third = self.create(key="new-explicit-fixture")
        self.assertTrue(third["alive"])
        self.manager.write(second["id"], "other-still-running\n")
        self.wait_for(self.manager, second["id"], b"FIXTURE_REPLY:other-still-running")
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        rows = {row["id"]: row for row in replacement.list()}
        self.assertFalse(rows[first["id"]]["alive"])
        self.assertTrue(all(rows[tid]["alive"] for tid in other_ids))
        self.assertTrue(rows[third["id"]]["alive"])
        self.assertFalse(replacement.read(first["id"], wait=0)["alive"])
        self.assertEqual(replacement.close_terminal(first["id"]), {"ok": True})
        self.assertEqual(self.launch_count.read_text(), "start\n" * 5)
        saved = json.loads((Path(self.directory.name) / "sessions.json").read_text())
        closed = next(row for row in saved if row["id"] == first["id"])
        self.assertTrue(closed["closed"])
        self.assertEqual(set(closed), {"id", "name", "key", "metadata", "closed"})
        self.assertNotIn("FIXTURE_READY", json.dumps(saved))

    def test_close_with_concurrent_input_does_not_deadlock_or_accept_later_input(self):
        created = self.create()
        self.wait_for(self.manager, created["id"], b"FIXTURE_READY")
        started = threading.Event()
        errors = []
        def writer():
            started.set()
            try:
                for _ in range(100):
                    self.manager.write(created["id"], "concurrent-fixture\n")
            except RuntimeError:
                pass  # Closing the selected terminal rejects in-flight/later input.
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        self.manager.close_terminal(created["id"])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(RuntimeError):
            self.manager.write(created["id"], "after-close\n")
        self.assertFalse(self.manager.read(created["id"], wait=20)["alive"])

    def test_immediate_exit_and_missing_executable_are_reported_as_failed(self):
        for key, command in (("exit", [sys.executable, "-c", "raise SystemExit(9)"]),
                             ("missing", ["/no-such-sessionholic-fixture"])):
            with self.assertRaisesRegex(RuntimeError, "CLI"):
                self.manager.create(key, command, self.directory.name, {"PATH": "/usr/bin:/bin"})
        self.assertTrue(all(not row["alive"] for row in self.manager.list()))
        self.assertEqual(list(Path(self.directory.name).glob("*.launch.json")), [])


if __name__ == "__main__":
    unittest.main()
