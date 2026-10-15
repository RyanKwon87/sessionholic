import base64
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

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
        row = {'id': 'a'*32, 'name': 'sb-'+'a'*32, 'key': 'fixture', 'metadata': {},
               'socket': self.manager.socket_name}
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
                       {"host": ["local"]}, {"cwd": "relative"}, {"cwd": "C:relative"},
                       {"cwd": "\\work"}, {"cwd": "/" + "a" * 4096},
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

    def fake_terminal(self):
        row = {"id": "b" * 32, "name": "sb-" + "b" * 32, "key": "fixture", "metadata": {}}
        stream = terminal._Terminal(row, 1024)
        self.manager.terminals[row["id"]] = stream
        self.addCleanup(self.manager.terminals.clear)
        return stream

    def test_new_session_sets_history_limit_in_the_same_tmux_call(self):
        self.assertEqual(terminal.HISTORY_LIMIT, 20000)
        with patch.object(self.manager, "_run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "시작하지"):
                self.manager.create("history", ["/bin/cat"], self.directory.name, {})
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:6], ("set-option", "-g", "history-limit", "20000", ";", "new-session"))

    def test_run_keeps_stdin_closed_unless_input_is_supplied(self):
        self.manager.tmux = "/fixture/tmux"
        with patch.object(terminal.subprocess, "run") as run:
            self.manager._run("has-session", "-t", "=sb-fixture")
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("input", run.call_args.kwargs)
            self.manager._run("load-buffer", "-", input=b"fixture")
            self.assertEqual(run.call_args.kwargs["input"], b"fixture")
            self.assertNotIn("stdin", run.call_args.kwargs)
            self.assertEqual(run.call_args.args[0][:5], ["/fixture/tmux", "-L", "sb-fixture-unused", "-f", "/dev/null"])

    def test_paste_and_scroll_validate_before_tmux(self):
        with patch.object(self.manager, "_run") as run:
            for text in ("", "a\0b", "x" * (terminal.INPUT_LIMIT + 1), "가" * 11000, b"bytes", None):
                with self.subTest(text=str(text)[:8]), self.assertRaises(ValueError):
                    self.manager.paste("a" * 32, text)
            for lines in (0, terminal.SCROLL_LIMIT + 1, -terminal.SCROLL_LIMIT - 1, True, 1.5, "5", None):
                with self.subTest(lines=lines), self.assertRaises(ValueError):
                    self.manager.scroll("a" * 32, lines)
            for call in (lambda: self.manager.paste("a" * 32, "ok"), lambda: self.manager.scroll("a" * 32, 5),
                         lambda: self.manager.scroll_end("a" * 32)):
                with self.assertRaises(KeyError):
                    call()
            with self.assertRaises(ValueError):
                self.manager.scroll("../x", 5)
            run.assert_not_called()

    def test_paste_uses_one_private_buffer_then_a_separate_enter(self):
        stream = self.fake_terminal()
        name = stream.record["name"]
        ok, failed = subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)
        with patch.object(self.manager, "_attach"), patch.object(terminal.time, "sleep") as sleep, \
                patch.object(self.manager, "_run", return_value=ok) as run:
            self.assertEqual(self.manager.paste(stream.record["id"], "한글 본문\n둘째 줄"), {"ok": True})
            self.manager.paste(stream.record["id"], "draft", submit=False)
        first, enter, draft = run.call_args_list
        buffer = first.args[2]
        self.assertRegex(buffer, r"^sh-[0-9a-f]{16}$")
        self.assertEqual(first.args, ("load-buffer", "-b", buffer, "-", ";",
                                      "paste-buffer", "-p", "-d", "-b", buffer, "-t", name))
        self.assertEqual(first.kwargs, {"input": "한글 본문\n둘째 줄".encode()})
        self.assertEqual(enter.args, ("send-keys", "-t", name, "Enter"))
        sleep.assert_called_once_with(0.3)
        self.assertEqual(draft.args[0], "load-buffer")
        self.assertNotEqual(draft.args[2], buffer)
        # tmux refused the paste: nothing reached the CLI, so no Enter either, and
        # a buffer that load-buffer may have filled is deleted rather than left behind.
        with patch.object(self.manager, "_attach"), patch.object(self.manager, "_run", return_value=failed) as run:
            with self.assertRaises(terminal.InputNotSent):
                self.manager.paste(stream.record["id"], "fixture")
        refused, cleanup = run.call_args_list
        self.assertEqual(refused.args[0], "load-buffer")
        self.assertEqual(cleanup.args, ("delete-buffer", "-b", refused.args[2]))
        # The text was pasted but Enter failed: this is not a pre-send failure.
        with patch.object(self.manager, "_attach"), patch.object(terminal.time, "sleep"), \
                patch.object(self.manager, "_run", side_effect=[ok, failed]):
            with self.assertRaises(RuntimeError) as caught:
                self.manager.paste(stream.record["id"], "fixture")
        self.assertNotIsInstance(caught.exception, terminal.InputNotSent)
        # A terminal shown in history first leaves copy-mode so the paste reaches the CLI.
        stream.scrolled = True
        with patch.object(self.manager, "_attach"), patch.object(terminal.time, "sleep"), \
                patch.object(self.manager, "_run", return_value=ok) as run:
            self.manager.paste(stream.record["id"], "fixture")
        self.assertEqual(run.call_args_list[0].args, ("send-keys", "-t", name, "-X", "cancel"))
        self.assertEqual(run.call_args_list[1].args[0], "load-buffer")
        self.assertFalse(stream.scrolled)
        for change in ({"alive": False}, {"closed": True}):
            with self.subTest(change=change):
                stream.alive = "alive" not in change
                stream.record["closed"] = "closed" in change
                with patch.object(self.manager, "_attach"), patch.object(self.manager, "_run") as run:
                    with self.assertRaises(terminal.InputNotSent):
                        self.manager.paste(stream.record["id"], "fixture")
                run.assert_not_called()

    @staticmethod
    def pane(**values):
        state = {"alternate": 0, "mouse": 0, "sgr": 0, "width": 120, "height": 30,
                 "inMode": 0, "position": 0, "history": 300}
        state.update(values)
        return state

    def test_scroll_drives_tmux_history_never_the_cli(self):
        stream = self.fake_terminal()
        tid, name = stream.record["id"], stream.record["name"]
        ok, failed = subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)
        history = {"ok": True, "mode": "history"}
        with patch.object(self.manager, "_run", return_value=ok) as run, \
                patch.object(self.manager, "_pane") as pane:
            # Below the live screen there is nothing to scroll to.
            pane.side_effect = [self.pane(), self.pane()]
            self.assertEqual(self.manager.scroll(tid, 5), {**history, "position": 0})
            run.assert_not_called()
            pane.side_effect = [self.pane(), self.pane(inMode=1, position=12)]
            self.assertEqual(self.manager.scroll(tid, -12), {**history, "position": 12})
            self.assertEqual(run.call_args.args, ("copy-mode", "-e", "-t", name, ";",
                                                  "send-keys", "-t", name, "-X", "-N", "12", "scroll-up"))
            self.assertTrue(stream.scrolled)
            pane.side_effect = [self.pane(inMode=1, position=12), self.pane(inMode=1, position=5)]
            self.assertEqual(self.manager.scroll(tid, 7), {**history, "position": 5})
            self.assertEqual(run.call_args.args, ("send-keys", "-t", name, "-X", "-N", "7", "scroll-down"))
            self.assertEqual(self.manager.scroll_end(tid), {"ok": True})
            self.assertEqual(run.call_args.args, ("send-keys", "-t", name, "-X", "cancel"))
            self.assertFalse(stream.scrolled)
            calls = run.call_count
            self.manager.scroll_end(tid)
            self.assertEqual(run.call_count, calls)
            # The live pane, not the in-memory flag, decides: a pane left in copy-mode by an
            # earlier gateway still scrolls down, and a stale flag is cleared without keys.
            pane.side_effect = [self.pane(inMode=1, position=3), self.pane()]
            self.assertEqual(self.manager.scroll(tid, 3), {**history, "position": 0})
            self.assertEqual(run.call_args.args, ("send-keys", "-t", name, "-X", "-N", "3", "scroll-down"))
            stream.scrolled = True
            calls = run.call_count
            pane.side_effect = [self.pane(), self.pane()]
            self.assertEqual(self.manager.scroll(tid, 3), {**history, "position": 0})
            self.assertEqual(run.call_count, calls)
            self.assertFalse(stream.scrolled)
            # An empty history never enters copy-mode, which would only trap later input.
            pane.side_effect = [self.pane(history=0)]
            self.assertEqual(self.manager.scroll(tid, -3), {**history, "position": 0})
            self.assertEqual(run.call_count, calls)
            self.assertFalse(stream.scrolled)
        with patch.object(self.manager, "_run", return_value=failed), \
                patch.object(self.manager, "_pane", return_value=self.pane()):
            with self.assertRaisesRegex(RuntimeError, "스크롤"):
                self.manager.scroll(tid, -3)
            self.assertFalse(stream.scrolled)
        stream.record["closed"] = True
        with patch.object(self.manager, "_run") as run, patch.object(self.manager, "_pane") as pane:
            with self.assertRaises(RuntimeError):
                self.manager.scroll(tid, -1)
            run.assert_not_called()
            pane.assert_not_called()

    def test_full_screen_mouse_app_receives_wheel_events_instead_of_history(self):
        stream = self.fake_terminal()
        tid, name = stream.record["id"], stream.record["name"]
        ok, failed = subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)
        app = {"ok": True, "mode": "app", "position": None}
        cases = [  # lines, pane, expected one -l argument
            (-7, self.pane(alternate=1, mouse=1, sgr=1), "\x1b[<64;60;15M" * 3),
            (1, self.pane(alternate=1, mouse=1, sgr=1, width=81, height=25), "\x1b[<65;40;12M"),
            (200, self.pane(alternate=1, mouse=1, sgr=1), "\x1b[<65;60;15M" * 40),
            (-3, self.pane(alternate=1, mouse=1, width=1, height=1), "\x1b[M" + chr(96) + chr(33) + chr(33)),
            (6, self.pane(alternate=1, mouse=1, width=600, height=40), ("\x1b[M" + chr(97) + chr(255) + chr(52)) * 2),
        ]
        for lines, state, expected in cases:
            with self.subTest(lines=lines, state=state):
                with patch.object(self.manager, "_run", return_value=ok) as run, \
                        patch.object(self.manager, "_pane", return_value=state) as pane:
                    self.assertEqual(self.manager.scroll(tid, lines), app)
                self.assertEqual([call.args for call in run.call_args_list], [("send-keys", "-t", name, "-l", expected)])
                pane.assert_called_once_with(stream)
        # A pane stuck in copy-mode is released first so the app itself gets the wheel.
        stream.scrolled = True
        with patch.object(self.manager, "_run", return_value=ok) as run, \
                patch.object(self.manager, "_pane", return_value=self.pane(alternate=1, mouse=1, sgr=1, inMode=1)):
            self.assertEqual(self.manager.scroll(tid, -3), app)
        self.assertEqual([call.args for call in run.call_args_list],
                         [("send-keys", "-t", name, "-X", "cancel"), ("send-keys", "-t", name, "-l", "\x1b[<64;60;15M")])
        self.assertFalse(stream.scrolled)
        # A held run's client draws on the main screen: its mouse modes alone mean the wheel.
        stream.record["metadata"] = {"holdName": "sh-0123456789abcdef"}
        with patch.object(self.manager, "_run", return_value=ok) as run, \
                patch.object(self.manager, "_pane", return_value=self.pane(alternate=0, mouse=1, sgr=1)):
            self.assertEqual(self.manager.scroll(tid, -3), app)
        self.assertEqual(run.call_args.args, ("send-keys", "-t", name, "-l", "\x1b[<64;60;15M"))
        stream.record["metadata"] = {}
        # The alternate screen without mouse reporting keeps the tmux history path.
        with patch.object(self.manager, "_run", return_value=ok) as run, \
                patch.object(self.manager, "_pane", side_effect=[self.pane(alternate=1), self.pane(inMode=1, position=3)]):
            self.assertEqual(self.manager.scroll(tid, -3), {"ok": True, "mode": "history", "position": 3})
        self.assertEqual(run.call_args.args[0], "copy-mode")
        with patch.object(self.manager, "_run", return_value=failed), \
                patch.object(self.manager, "_pane", return_value=self.pane(alternate=1, mouse=1, sgr=1)):
            with self.assertRaisesRegex(RuntimeError, "스크롤"):
                self.manager.scroll(tid, -3)

    def test_pane_state_is_one_captured_display_query(self):
        stream = self.fake_terminal()
        self.manager.tmux = "/fixture/tmux"
        with patch.object(terminal.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b"1|1|0|100|30|0||0\n")
            self.assertEqual(self.manager._pane(stream), {"alternate": 1, "mouse": 1, "sgr": 0, "width": 100,
                                                          "height": 30, "inMode": 0, "position": 0, "history": 0})
            self.assertEqual(run.call_args.args[0][5:], ["display-message", "-p", "-t", stream.record["name"],
                                                         terminal.PANE_FORMAT])
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            for done in (subprocess.CompletedProcess([], 1, stdout=b"1|1|0|100|30|0||0"),
                         subprocess.CompletedProcess([], 0, stdout=b"1|1|0"),
                         subprocess.CompletedProcess([], 0, stdout=b"x|1|0|100|30|0||0")):
                run.return_value = done
                with self.subTest(done=done), self.assertRaisesRegex(RuntimeError, "상태"):
                    self.manager._pane(stream)
            run.side_effect = subprocess.TimeoutExpired("tmux", 10)
            with self.assertRaisesRegex(RuntimeError, "상태"):
                self.manager._pane(stream)

    def test_send_keys_presses_only_named_navigation_keys_in_one_call(self):
        stream = self.fake_terminal()
        tid, name = stream.record["id"], stream.record["name"]
        ok, failed = subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1)
        with patch.object(self.manager, "_run", return_value=ok) as run:
            for keys in ([], ["Left"], ["enter"], ["Down", "C-c"], ["Enter "], "Enter", ("Up", 1),
                         [["Up"]], ["Down"] * (terminal.KEY_LIMIT + 1), None, {"Up": 1}):
                with self.subTest(keys=keys), self.assertRaises(ValueError):
                    self.manager.send_keys(tid, keys)
            with self.assertRaises(KeyError):
                self.manager.send_keys("c" * 32, ["Enter"])
            with self.assertRaises(ValueError):
                self.manager.send_keys("../x", ["Enter"])
            run.assert_not_called()
            self.assertEqual(self.manager.send_keys(tid, ["Down", "Down", "Enter"]), {"ok": True})
            self.assertEqual(run.call_args_list[-1].args, ("send-keys", "-t", name, "Down", "Down", "Enter"))
            self.manager.send_keys(tid, ("Up",) * terminal.KEY_LIMIT)
            self.assertEqual(run.call_count, 2)
            # A terminal shown in history first leaves copy-mode so the keys reach the CLI.
            stream.scrolled = True
            self.manager.send_keys(tid, ["Enter"])
            self.assertEqual([call.args for call in run.call_args_list[-2:]],
                             [("send-keys", "-t", name, "-X", "cancel"), ("send-keys", "-t", name, "Enter")])
            self.assertFalse(stream.scrolled)
        with patch.object(self.manager, "_run", return_value=failed):
            with self.assertRaises(terminal.InputNotSent):
                self.manager.send_keys(tid, ["Enter"])
        for change in ({"alive": False}, {"closed": True}):
            with self.subTest(change=change):
                stream.alive = "alive" not in change
                stream.record["closed"] = "closed" in change
                with patch.object(self.manager, "_run") as run:
                    with self.assertRaises(RuntimeError):
                        self.manager.send_keys(tid, ["Enter"])
                run.assert_not_called()

    def test_new_attach_client_and_its_first_input_leave_stale_copy_mode(self):
        stream = self.fake_terminal()
        tid, name = stream.record["id"], stream.record["name"]
        cancel = ("send-keys", "-t", name, "-X", "cancel")
        process = Mock(pid=0)
        process.poll.return_value = None
        # Popen is a fixture: no installed tmux is needed, only a path to name in its command.
        with patch.object(self.manager, "tmux", "/usr/bin/false"), \
                patch.object(self.manager, "_exists", return_value=True), patch.object(self.manager, "_configure"), \
                patch.object(self.manager, "_run", side_effect=RuntimeError("fixture tmux")) as run, \
                patch.object(terminal.subprocess, "Popen", return_value=process), \
                patch.object(terminal.threading, "Thread") as thread:
            self.manager._attach(stream)
        self.addCleanup(os.close, stream.fd)
        # The cancel result is ignored: outside copy-mode tmux reports "not in a mode".
        self.assertEqual([call.args for call in run.call_args_list], [cancel])
        thread.return_value.start.assert_called_once()
        self.assertTrue(stream.attached_fresh)
        ok = subprocess.CompletedProcess([], 0)
        with patch.object(self.manager, "_run", return_value=ok) as run:
            self.manager.send_keys(tid, ["Enter"])
            self.manager.send_keys(tid, ["Down"])
            # A running attach client is reused, so nothing is cancelled again.
            self.manager._attach(stream)
        self.assertEqual([call.args for call in run.call_args_list],
                         [cancel, ("send-keys", "-t", name, "Enter"), ("send-keys", "-t", name, "Down")])
        self.assertFalse(stream.attached_fresh)


class ListingTest(unittest.TestCase):
    """A listing asks tmux once, not once per terminal while the lock is held, and closed
    rows neither keep their output nor pile up for good."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = terminal.TerminalManager(state_dir=self.directory.name, socket_name="sb-fixture-listing")

    def tearDown(self):
        self.manager.close()
        self.directory.cleanup()

    def add(self, letter, closed=False, reason=None, closed_at=float("-inf"), output=b""):
        row = {"id": letter * 32, "name": "sb-" + letter * 32, "key": "fixture-" + letter, "metadata": {},
               "socket": self.manager.socket_name}
        if closed:
            row.update(closed=True, closedReason=reason)
        instance = terminal._Terminal(row, 1024)
        if closed:
            instance.closed_at = closed_at
        instance.output = bytearray(output)
        self.manager.terminals[row["id"]] = instance
        return instance

    def test_a_listing_asks_tmux_once_for_every_terminal(self):
        for letter in "abc":
            self.add(letter)
        with patch.object(self.manager, "_live_names", return_value={"sb-" + "a" * 32}) as names, \
                patch.object(self.manager, "_run") as run:
            rows = {row["id"][0]: (row["alive"], row["closed"]) for row in self.manager.list()}
        names.assert_called_once()
        run.assert_not_called()
        self.assertEqual(rows, {"a": (True, False), "b": (False, True), "c": (False, True)})

    def test_an_unclear_tmux_answer_asks_each_terminal_as_before(self):
        for letter in "ab":
            self.add(letter)
        ok = Mock(returncode=0)
        with patch.object(self.manager, "_live_names", return_value=None), \
                patch.object(self.manager, "_run", return_value=ok) as run:
            self.assertEqual([row["alive"] for row in self.manager.list()], [True, True])
        self.assertEqual([call.args[0] for call in run.call_args_list], ["has-session", "has-session"])

    def test_nothing_to_check_asks_tmux_nothing(self):
        self.add("a", closed=True, reason="user")
        with patch.object(self.manager, "_live_names") as names:
            self.manager.list()
        names.assert_not_called()

    def test_live_names_reads_one_list_and_no_server_is_no_sessions(self):
        done = lambda code, out=b"", err=b"": subprocess.CompletedProcess([], code, out, err)
        with patch.object(terminal.subprocess, "run", return_value=done(0, b"sb-one\nsb-two\n")) as run:
            self.assertEqual(self.manager._live_names(), {"sb-one", "sb-two"})
        self.assertIn("list-sessions", run.call_args.args[0])
        with patch.object(terminal.subprocess, "run", return_value=done(1, err=b"no server running on /tmp/x")):
            self.assertEqual(self.manager._live_names(), set())
        with patch.object(terminal.subprocess, "run", return_value=done(1, err=b"error connecting to /tmp/x (No such file or directory)")):
            self.assertEqual(self.manager._live_names(), set())
        with patch.object(terminal.subprocess, "run", return_value=done(1, err=b"protocol version mismatch")):
            self.assertIsNone(self.manager._live_names())
        with patch.object(terminal.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 10)):
            self.assertIsNone(self.manager._live_names())

    def test_closed_rows_free_their_output_and_only_the_newest_are_kept(self):
        self.add("f")
        for letter in "abcd":
            self.add(letter, closed=True, reason="connection_ended", output=b"old screen")
        for letter in "12":
            self.add(letter, closed=True, reason="user", output=b"closed by hand")
        recent = self.add("e", closed=True, reason="released", closed_at=time.monotonic(), output=b"just closed")
        with patch.object(terminal, "CLOSED_KEEP", 2), patch.object(terminal, "USER_CLOSED_KEEP", 1), \
                patch.object(self.manager, "_live_names", return_value={"sb-" + "f" * 32}):
            ids = [row["id"][0] for row in self.manager.list()]
        # The open row, the two newest other closes, the newest own close and the one closed moments ago.
        self.assertEqual(ids, ["f", "c", "d", "2", "e"])
        self.assertEqual([row["id"][0] for row in json.loads(self.manager.state_path.read_text())], ids)
        self.assertEqual(bytes(self.manager.terminals["c" * 32].output), b"")
        self.assertEqual(self.manager.terminals["c" * 32].read(0, 0)["data"], "")
        self.assertEqual(bytes(recent.output), b"just closed", "still readable right after closing")

    def test_another_thread_never_reads_a_listings_answer(self):
        row = self.add("a")
        seen = []
        def names():
            # While this listing holds its answer, a reader thread asks on its own.
            other = threading.Thread(target=lambda: seen.append(self.manager._exists(row)))
            other.start(); other.join()
            return {"sb-" + "a" * 32}
        with patch.object(self.manager, "_live_names", side_effect=names), \
                patch.object(self.manager, "_run", return_value=Mock(returncode=1)) as run:
            self.assertTrue(self.manager.list()[0]["alive"])
        self.assertEqual(seen, [False], "the other thread got tmux's own answer")
        self.assertEqual(run.call_args.args[0], "has-session")

    def test_the_oldest_closes_are_forgotten_first_whatever_order_they_were_opened(self):
        for letter, closed_at in (("a", 300), ("b", 100), ("c", 200)):
            self.add(letter, closed=True, reason="user").record["closedAt"] = closed_at
        with patch.object(terminal, "USER_CLOSED_KEEP", 2):
            self.assertEqual([row["id"][0] for row in self.manager.list()], ["a", "c"])
        self.assertEqual(self.manager.take_forgotten(), [{"id": "b" * 32, "closed": True}])
        self.assertEqual(self.manager.take_forgotten(), [])
        self.manager.restore_forgotten([{"id": "b" * 32, "closed": True}])
        self.assertEqual([row["id"] for row in self.manager.take_forgotten()], ["b" * 32])

    def test_rows_not_saved_as_forgotten_are_kept(self):
        for letter in "abc":
            self.add(letter, closed=True, reason="connection_ended")
        with patch.object(terminal, "CLOSED_KEEP", 1), \
                patch.object(self.manager, "_save", side_effect=OSError("fixture full disk")):
            self.assertEqual([row["id"][0] for row in self.manager.list()], ["a", "b", "c"])


class SocketRecordTest(unittest.TestCase):
    """A board started on another tmux socket must never tombstone live sessions (R1)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.directory.cleanup()

    def rows(self, **extra):
        return [{"id": char * 32, "name": "sb-" + char * 32, "key": "fixture-" + char, "metadata": {}, **extra}
                for char in "ab"]

    def manager(self, socket="sb-fixture-current"):
        # tmux is a program that always fails: no session exists on this socket.
        manager = terminal.TerminalManager(state_dir=self.state, socket_name=socket, tmux="/usr/bin/false")
        self.managers.append(manager)
        return manager

    def saved(self):
        return json.loads((self.state / "sessions.json").read_text())

    def test_default_socket_is_the_instance_name(self):
        manager = terminal.TerminalManager(state_dir=self.state, tmux="/usr/bin/false")
        self.managers.append(manager)
        self.assertEqual(manager.socket_name, terminal.DEFAULT_SOCKET)
        self.assertEqual(terminal.DEFAULT_SOCKET, terminal.instance.current()["names"]["tmuxSocket"])

    def test_rows_saved_without_a_socket_are_neither_given_this_socket_nor_closed_when_not_found(self):
        # M3: the first start after sockets were recorded may run on a new socket name while
        # these rows still live on the old one; not finding them here proves nothing.
        (self.state / "sessions.json").write_text(json.dumps(self.rows()))
        before = (self.state / "sessions.json").read_bytes()
        manager = self.manager()
        self.assertEqual((self.state / "sessions.json").read_bytes(), before, "nothing is written at load")
        # Still running on another live tmux server of this user (an earlier socket name).
        manager._elsewhere = lambda terminal: True
        listed = manager.list()
        manager.list()
        self.assertEqual([(row["alive"], row["closed"]) for row in listed], [(False, False)] * 2)
        self.assertEqual((self.state / "sessions.json").read_bytes(), before)
        self.assertTrue(all("socket" not in row and "closed" not in row for row in self.saved()))
        # An explicit close or reopen cannot tombstone it either.
        with self.assertRaisesRegex(RuntimeError, "소켓 기록 없이"):
            manager.close_terminal("a" * 32)
        with self.assertRaisesRegex(RuntimeError, "소켓 기록 없이"):
            manager.create("fixture-a", ["/bin/echo"], self.directory.name, {})
        self.assertEqual((self.state / "sessions.json").read_bytes(), before)
        # Found on this socket, the row joins it (and is judged like any other from then on).
        with patch.object(manager, "_exists", side_effect=lambda t: t.record["id"] == "a" * 32):
            listed = manager.list()
        self.assertEqual([(row["alive"], row["closed"]) for row in listed], [(True, False), (False, False)])
        self.assertEqual([row.get("socket") for row in self.saved()], ["sb-fixture-current", None])
        self.assertTrue(all("closed" not in row for row in self.saved()))
        # A failed save leaves the row unrecorded.
        with patch.object(manager, "_exists", return_value=True), \
                patch.object(manager, "_save", side_effect=OSError("fixture full disk")):
            with self.assertRaises(OSError):
                manager.list()
        self.assertNotIn("socket", manager.terminals["b" * 32].record)

    def test_an_unrecorded_row_no_live_tmux_server_has_is_closed_once_looked_for(self):
        # Old rows from before sockets were recorded stayed listed as open forever.
        (self.state / "sessions.json").write_text(json.dumps(self.rows()))
        manager = self.manager()
        socket_dir = Path(self.directory.name) / "tmux"
        socket_dir.mkdir()
        manager.socket_dir = socket_dir
        # A file left by an ended server (no server answers) and a live server that lacks it.
        dead = socket_dir / "sb-fixture-old"
        live = socket_dir / "sb-fixture-live"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(live)); server.listen(8)
        self.addCleanup(server.close)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); probe.bind(str(dead)); probe.close()
        asked = []
        real = subprocess.run
        answers = {"=sb-" + "a" * 32: (1, b"can't find session: sb-a"), "=sb-" + "b" * 32: (0, b"")}
        def run(argv, **kwargs):
            if argv[1:3] == ["-L", "sb-fixture-live"]:
                asked.append(argv[-1])
                code, said = answers[argv[-1]]
                return subprocess.CompletedProcess(argv, code, stderr=said)
            return real(argv, **kwargs)
        with patch.object(terminal.subprocess, "run", side_effect=run):
            listed = manager.list()
            # Looked for once per start.
            manager.list()
        self.assertEqual(asked, ["=sb-" + "a" * 32, "=sb-" + "b" * 32])
        self.assertEqual([(row["alive"], row["closed"]) for row in listed], [(False, True), (False, False)])
        saved = {row["id"][0]: row for row in self.saved()}
        self.assertEqual((saved["a"]["closedReason"], saved["a"]["socket"]), ("connection_ended", "sb-fixture-current"))
        self.assertNotIn("socket", saved["b"], "found on the live server: left as it was")
        # An answer that is not about the session (another tmux version): nothing is told, and
        # no row is looked for again until LOOK_AGAIN_SECONDS passed (a server that does not
        # answer holds the listing for its timeout).
        manager.close()
        (self.state / "sessions.json").write_text(json.dumps(self.rows()))
        other = self.manager()
        other.socket_dir = socket_dir
        asked.clear()
        answers["=sb-" + "a" * 32] = (1, b"protocol version mismatch")
        answers["=sb-" + "b" * 32] = (1, b"protocol version mismatch")
        clock = [1000.0]
        with patch.object(terminal.subprocess, "run", side_effect=run), \
                patch.object(terminal.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual([row["closed"] for row in other.list()], [False, False])
            other.list()
            self.assertEqual(len(asked), 1)
            clock[0] += terminal.LOOK_AGAIN_SECONDS
            other.list()
            self.assertEqual(len(asked), 2)
        # A tmux program gone between the listing and the look tells nothing either.
        clock[0] += terminal.LOOK_AGAIN_SECONDS
        def gone(argv, **kwargs):
            if argv[1:3] == ["-L", "sb-fixture-live"]:
                raise FileNotFoundError(argv[0])
            return real(argv, **kwargs)
        with patch.object(terminal.subprocess, "run", side_effect=gone), \
                patch.object(terminal.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual([row["closed"] for row in other.list()], [False, False])
        # No socket folder at all: no tmux server of this user runs, so it ended.
        clock[0] += terminal.LOOK_AGAIN_SECONDS
        other.socket_dir = Path(self.directory.name) / "missing"
        with patch.object(terminal.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual([row["closed"] for row in other.list()], [True, True])

    def test_rows_of_another_socket_are_not_closed_by_listing_closing_or_reopening(self):
        (self.state / "sessions.json").write_text(json.dumps(self.rows(socket="sb-fixture-old")))
        before = (self.state / "sessions.json").read_bytes()
        manager = self.manager()
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            listed = manager.list()
            manager.list()
        self.assertEqual([(row["alive"], row["closed"]) for row in listed], [(False, False)] * 2)
        self.assertEqual((self.state / "sessions.json").read_bytes(), before)
        warnings = log.getvalue().splitlines()
        self.assertEqual(len(warnings), 2, "one warning per terminal, not per listing")
        self.assertIn("sb-fixture-old", warnings[0])
        with self.assertRaisesRegex(RuntimeError, "다른 tmux 소켓"):
            manager.close_terminal("a" * 32)
        with self.assertRaisesRegex(RuntimeError, "다른 tmux 소켓"):
            manager.create("fixture-a", ["/bin/echo"], self.directory.name, {})
        self.assertEqual((self.state / "sessions.json").read_bytes(), before)
        # Back on the recorded socket, the same rows are judged normally again.
        manager.close()
        self.managers.remove(manager)
        again = self.manager("sb-fixture-old")
        self.assertEqual([row["closed"] for row in again.list()], [True, True])

    def test_rows_of_the_current_socket_that_ended_are_still_closed(self):
        (self.state / "sessions.json").write_text(json.dumps(self.rows(socket="sb-fixture-current")))
        manager = self.manager()
        self.assertEqual([row["closed"] for row in manager.list()], [True, True])
        self.assertEqual([row["closed"] for row in self.saved()], [True, True])

    def test_invalid_socket_records_are_rejected(self):
        for value in (5, "", "bad socket", "../x"):
            (self.state / "sessions.json").write_text(json.dumps(self.rows(socket=value)))
            with self.assertRaisesRegex(RuntimeError, "상태 파일 형식"):
                self.manager()


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
        # tmux keeps the socket file of an ended server.
        (terminal.socket_dir() / self.socket).unlink(missing_ok=True)
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

    def connection(self, key, mode, idle=True, **metadata):
        created = self.manager.create(key, [sys.executable, "-u", "-c", self.code, str(self.launch_count)],
                                      self.directory.name, {"PATH": os.environ.get("PATH", "")},
                                      {"agent": "fixture", "host": "local", "mode": mode, **metadata})
        if idle:
            self.manager.terminals[created["id"]].touched = time.monotonic() - terminal.RELEASE_IDLE - 1
        return created

    def test_an_idle_connection_to_a_session_is_released_for_a_new_terminal(self):
        old = self.connection("old", "attach")
        changes = []
        self.manager.add_listener(lambda: changes.append(1))
        with self.manager.reserve("next"):
            new = self.connection("next", "attach", idle=False)
        rows = {row["id"]: row for row in self.manager.list()}
        self.assertEqual((rows[old["id"]]["closed"], rows[old["id"]]["closedReason"]), (True, "released"))
        self.assertFalse(rows[old["id"]]["alive"])
        self.assertTrue(rows[new["id"]]["alive"])
        self.assertTrue(changes)
        # The tombstone survives a gateway restart and the session's next connection uses a new key.
        self.manager.close()
        reloaded = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket, max_terminals=1)
        self.managers.append(reloaded)
        self.assertEqual({row["key"]: row["closedReason"] for row in reloaded.list()}, {"old": "released", "next": None})

    def test_recent_owned_or_launching_terminals_are_never_released(self):
        self.manager.max_terminals = 4
        recent = self.connection("recent", "attach", idle=False)
        owned = self.connection("owned", "new-session")
        handoff = self.connection("handoff", "attach", launchMode="handoff")
        with self.manager.reserve("launching"):
            self.connection("launching", "attach")
            with self.assertRaisesRegex(RuntimeError, "열린 터미널 4개가 가득 찼습니다"):
                with self.manager.reserve("next"):
                    self.fail("a terminal that must stay open was released")
        alive = {row["key"]: row["alive"] for row in self.manager.list()}
        self.assertEqual(alive, {"recent": True, "owned": True, "handoff": True, "launching": True})

    def test_a_connection_loaded_after_a_restart_is_not_released_at_once(self):
        self.connection("old", "attach")
        self.manager.close()
        self.manager = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket, max_terminals=1)
        self.managers.append(self.manager)
        with self.assertRaisesRegex(RuntimeError, "가득"):
            with self.manager.reserve("next"):
                self.fail("a connection loaded after a restart was released before its browser read again")

    def test_a_connection_unused_for_ten_minutes_closes_even_with_room(self):
        self.manager.max_terminals = 5
        old = self.connection("old", "attach", agent="claude")
        self.manager.terminals[old["id"]].touched = time.monotonic() - terminal.CLOSE_IDLE - 1
        codex = self.connection("codex", "attach", agent="codex")
        self.manager.terminals[codex["id"]].touched = time.monotonic() - terminal.CLOSE_IDLE - 1
        recent = self.connection("recent", "attach", agent="claude")
        owned = self.connection("owned", "new-session")
        self.manager.terminals[owned["id"]].touched = time.monotonic() - terminal.CLOSE_IDLE - 1
        changes = []
        self.manager.add_listener(lambda: changes.append(1))
        rows = {row["key"]: row for row in self.manager.list()}
        self.assertEqual((rows["old"]["alive"], rows["old"]["closedReason"]), (False, "released"))
        self.assertTrue(rows["recent"]["alive"])
        self.assertTrue(rows["owned"]["alive"], "a terminal running its own CLI never closes by itself")
        self.assertTrue(rows["codex"]["alive"], "a Codex connection is never closed by the board")
        self.assertTrue(changes)
        self.assertIsNotNone(recent)

    def test_a_new_session_terminal_records_what_it_runs_and_announces_only_a_change(self):
        self.manager.max_terminals = 4
        launched = self.connection("launched", "new-session", idle=False)
        attached = self.connection("attached", "attach", idle=False)
        changes = []
        self.manager.add_listener(lambda: changes.append(1))
        self.assertTrue(self.manager.set_program(launched["id"], {"program": "claude-agents", "cwd": "/Users/example/project"}))
        self.assertEqual(len(changes), 1)
        self.assertTrue(self.manager.set_program(launched["id"], {"program": "claude-agents", "cwd": "/Users/example/project"}))
        self.assertEqual(len(changes), 1, "the same program read again is saved without an announcement")
        row = next(item for item in self.manager.list() if item["key"] == "launched")
        self.assertEqual((row["program"], row["programCwd"]), ("claude-agents", "/Users/example/project"))
        self.assertIsInstance(row["programAt"], int)
        self.assertFalse(self.manager.set_program(attached["id"], {"program": "codex"}))
        self.manager.set_program(launched["id"], {"program": "codex", "cwd": "relative"})
        self.assertIsNone(next(item for item in self.manager.list() if item["key"] == "launched")["programCwd"])
        with self.assertRaises(ValueError):
            self.manager.set_program(launched["id"], {"program": "rm -rf"})
        # Its launcher named with nothing below it: the menu waits. An SSH connection is a program.
        self.manager.set_program(launched["id"], {"program": None, "launcher": True})
        row = next(item for item in self.manager.list() if item["key"] == "launched")
        self.assertEqual((row["program"], row["programMenu"]), (None, True))
        self.manager.set_program(launched["id"], {"program": "ssh", "cwd": None})
        row = next(item for item in self.manager.list() if item["key"] == "launched")
        self.assertEqual((row["program"], row["programMenu"]), ("ssh", False))
        self.manager.set_program(launched["id"], {"program": None})
        self.assertFalse(next(item for item in self.manager.list() if item["key"] == "launched")["programMenu"])

    def test_unused_launcher_menus_and_agent_views_close_and_nothing_else_does(self):
        self.manager.max_terminals = 12

        def launcher(key, info, idle):
            created = self.connection(key, "new-session", idle=False)
            if info is not None:
                self.manager.set_program(created["id"], info)
            self.manager.terminals[created["id"]].touched = time.monotonic() - idle
            return created
        old = terminal.LAUNCHER_AGENTS_IDLE + 1
        menu = launcher("menu", {"program": None, "launcher": True}, terminal.LAUNCHER_MENU_IDLE + 1)
        launcher("fresh-menu", {"program": None, "launcher": True}, terminal.LAUNCHER_MENU_IDLE - 30)
        typed = launcher("typed", {"program": None, "launcher": True}, 0)
        self.manager.write(typed["id"], "1\n")  # A menu choice, through the board.
        self.manager.terminals[typed["id"]].touched = time.monotonic() - old
        agents = launcher("agents", {"program": "claude-agents", "cwd": None}, old)
        launcher("recent-agents", {"program": "claude-agents", "cwd": None}, terminal.LAUNCHER_MENU_IDLE + 1)
        for program in ("claude", "codex", "ssh"):
            launcher(program, {"program": program, "cwd": None}, old)
        launcher("unnamed", {"program": None}, old)  # No launcher named on its host.
        launcher("unread", None, old)  # Its program never read.
        stale = launcher("stale", {"program": "claude-agents", "cwd": None}, old)
        self.manager.terminals[stale["id"]].record["metadata"]["programAt"] = int(time.time()) - terminal.LAUNCHER_PROGRAM_FRESH - 1
        attach = self.connection("attach", "attach")
        self.manager.terminals[attach["id"]].touched = time.monotonic() - old
        self.assertEqual(set(self.manager.idle_launchers()), {menu["id"], agents["id"]})
        changes = []
        self.manager.add_listener(lambda: changes.append(1))
        row = self.manager.release_launcher(menu["id"])
        self.assertEqual((row["closed"], row["closedReason"], row["host"]), (True, "released", "local"))
        self.assertTrue(changes)
        self.assertIsNone(self.manager.release_launcher(menu["id"]), "already closed")
        self.manager.read(agents["id"], 0, 0)  # Looked at again: no longer unused.
        self.assertIsNone(self.manager.release_launcher(agents["id"]))
        rows = {item["key"]: item for item in self.manager.list()}
        self.assertFalse(rows["menu"]["alive"])
        for key in ("fresh-menu", "typed", "agents", "recent-agents", "claude", "codex", "ssh", "unnamed", "unread", "stale", "attach"):
            self.assertTrue(rows[key]["alive"], key)
        # Typing is kept across a restart: a chosen menu is never taken for an untouched one.
        self.manager.close()
        self.manager = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket, max_terminals=12)
        self.managers.append(self.manager)
        self.assertTrue(next(item for item in self.manager.list() if item["key"] == "typed")["typed"])
        self.assertEqual(self.manager.idle_launchers(), [], "a restart counts unused time again")

    def test_an_unused_codex_connection_is_named_for_a_session_check_and_ended_when_asked(self):
        self.manager.max_terminals = 5
        codex = self.connection("codex", "attach", agent="codex")
        recent = self.connection("recent", "attach", idle=False, agent="codex")
        claude = self.connection("claude", "attach", agent="claude")
        launched = self.connection("launched", "new-session", agent="codex")
        for item in (codex, claude, launched):
            self.manager.terminals[item["id"]].touched = time.monotonic() - terminal.CODEX_ATTACH_IDLE - 1
        self.assertEqual([row["id"] for row in self.manager.idle_codex_attaches()], [codex["id"]])
        self.assertIsNone(self.manager.release_codex_attach(recent["id"]))
        row = self.manager.release_codex_attach(codex["id"])
        self.assertEqual((row["closed"], row["closedReason"]), (True, "released"))
        self.assertIsNone(self.manager.release_codex_attach(codex["id"]), "already closed")
        alive = {item["key"]: item["alive"] for item in self.manager.list()}
        self.assertEqual((alive["codex"], alive["recent"], alive["launched"]), (False, True, True))

    def test_a_codex_connection_is_never_released_for_room(self):
        self.connection("codex", "attach", agent="codex")
        with self.assertRaisesRegex(RuntimeError, "가득"):
            with self.manager.reserve("next"):
                self.fail("a Codex connection was released for room")
        self.assertTrue(next(row for row in self.manager.list() if row["key"] == "codex")["alive"])

    def test_a_browser_read_keeps_a_connection_from_being_released(self):
        old = self.connection("old", "attach")
        self.manager.read(old["id"], 0, 0)
        with self.assertRaisesRegex(RuntimeError, "가득"):
            with self.manager.reserve("next"):
                self.fail("a connection a browser just read was released")
        self.assertTrue(next(row for row in self.manager.list() if row["id"] == old["id"])["alive"])

    def test_a_terminal_records_its_native_session_once_and_keeps_it(self):
        created = self.create()
        changes = []
        self.manager.add_listener(lambda: changes.append(1))
        native = {"host": "local", "agent": "codex", "id": "019a0000-0000-7000-8000-000000000001",
                  "home": ".codex", "cwd": "/tmp/work"}
        with self.assertRaises(ValueError):
            self.manager.link_native(created["id"], {**native, "id": "not-a-uuid"})
        self.assertTrue(self.manager.link_native(created["id"], native))
        self.assertFalse(self.manager.link_native(created["id"], {**native, "id": native["id"][:-1] + "2"}))
        self.assertEqual(changes, [1])
        listed = next(row for row in self.manager.list() if row["id"] == created["id"])
        self.assertEqual(listed["nativeSource"], native)
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        self.assertEqual(next(row for row in replacement.list() if row["id"] == created["id"])["nativeSource"], native)

    def test_windows_native_cwd_survives_terminal_creation_and_restart(self):
        native = {"host": "windows-fixture", "agent": "codex", "id": "019a0000-0000-7000-8000-000000000001",
                  "home": ".codex", "cwd": r"\\?\C:\Users\example\Work"}
        created = self.manager.create("windows-metadata", [sys.executable, "-u", "-c", self.code, str(self.launch_count)],
                                      self.directory.name, {"PATH": os.environ.get("PATH", "")},
                                      {"host": native["host"], "nativeSource": native})
        self.wait_for(self.manager, created["id"], b"FIXTURE_READY")
        self.assertEqual(created["nativeSource"], native)
        self.manager.close()
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        self.assertEqual(replacement.list()[0]["nativeSource"], native)

    def test_a_row_saved_without_a_socket_joins_the_socket_it_is_found_on(self):
        created = self.create()
        self.wait_for(self.manager, created["id"], b"FIXTURE_READY")
        self.manager.close()
        state = Path(self.directory.name) / "sessions.json"
        rows = json.loads(state.read_text())
        for row in rows:
            row.pop("socket")
        state.write_text(json.dumps(rows))
        # Another socket name first: not found there, nothing is recorded (M3).
        other = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket + "-other")
        self.assertEqual([(row["alive"], row["closed"]) for row in other.list()], [(False, False)])
        other.close()
        self.assertNotIn("socket", json.loads(state.read_text())[0])
        replacement = terminal.TerminalManager(state_dir=self.directory.name, socket_name=self.socket)
        self.managers.append(replacement)
        self.assertEqual([(row["alive"], row["closed"]) for row in replacement.list()], [(True, False)])
        self.assertEqual(json.loads(state.read_text())[0]["socket"], self.socket)
        replacement.write(created["id"], "after-adoption\n")
        self.wait_for(replacement, created["id"], b"FIXTURE_REPLY:after-adoption")

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
        self.assertEqual(set(closed), {"id", "name", "key", "metadata", "closed", "closedReason", "closedAt", "socket"})
        self.assertIs(type(closed["closedAt"]), int)
        self.assertEqual(closed["closedReason"], "user")
        self.assertEqual(closed["socket"], self.socket)
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

    def tmux(self, *args):
        return subprocess.run([shutil.which("tmux"), "-L", self.socket] + list(args),
                              capture_output=True, text=True, timeout=5)

    def mode(self, terminal_id):
        done = self.tmux("display-message", "-p", "-t", "sb-" + terminal_id, "#{pane_in_mode} #{scroll_position}")
        self.assertEqual(done.returncode, 0)
        return done.stdout.strip()

    def output_since(self, terminal_id, after):
        return base64.b64decode(self.manager.read(terminal_id, after, 0)["data"])

    def test_history_limit_paste_and_scroll_use_real_tmux(self):
        code = ("import sys\n"
                "for i in range(300): print('HISTORY-%03d' % i)\n"
                "print('FIXTURE_READY', flush=True)\n"
                "for line in sys.stdin:\n"
                " print('FIXTURE_REPLY:' + line.strip(), flush=True)\n")
        created = self.manager.create("scroll-fixture", [sys.executable, "-u", "-c", code], self.directory.name,
                                      {"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"},
                                      {"agent": "fixture", "host": "local"})
        tid = created["id"]
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_READY")
        self.assertEqual(self.tmux("show-options", "-g", "-v", "history-limit").stdout.strip(), "20000")
        self.assertEqual(self.tmux("display-message", "-p", "-t", "sb-" + tid, "#{history_limit}").stdout.strip(), "20000")

        self.assertEqual(self.manager.paste(tid, "pasted-fixture"), {"ok": True})
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_REPLY:pasted-fixture", cursor)
        before = cursor
        self.manager.paste(tid, "line-one\nline-two")
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_REPLY:line-two", cursor)
        self.assertIn(b"FIXTURE_REPLY:line-one", self.output_since(tid, before))
        # paste-buffer -d removed every private buffer.
        buffers = self.tmux("list-buffers", "-F", "#{buffer_name}").stdout.split()
        self.assertFalse([name for name in buffers if name.startswith("sh-")])
        # Without submit the text waits in the CLI input until the user's own Enter.
        before = cursor
        self.manager.paste(tid, "draft-fixture", submit=False)
        time.sleep(0.6)
        self.assertNotIn(b"FIXTURE_REPLY:draft-fixture", self.output_since(tid, before))
        self.manager.write(tid, "\n")
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_REPLY:draft-fixture", cursor)

        self.assertEqual(self.manager.scroll(tid, -10), {"ok": True, "mode": "history", "position": 10})
        self.assertEqual(self.mode(tid), "1 10")
        self.assertEqual(self.manager.scroll(tid, 4), {"ok": True, "mode": "history", "position": 6})
        self.assertEqual(self.mode(tid), "1 6")
        # copy-mode -e leaves history once the view is back at the live screen.
        self.manager.scroll(tid, 6)
        self.assertEqual(self.mode(tid).split()[0], "0")
        self.manager.scroll(tid, 5)
        self.assertFalse(self.manager.terminals[tid].scrolled)
        self.assertEqual(self.mode(tid).split()[0], "0")
        # Typed input and pasted input both leave history first and reach the CLI.
        self.manager.scroll(tid, -3)
        self.assertEqual(self.mode(tid), "1 3")
        self.manager.write(tid, "typed-after-scroll\n")
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_REPLY:typed-after-scroll", cursor)
        self.assertEqual(self.mode(tid).split()[0], "0")
        self.manager.scroll(tid, -3)
        self.manager.paste(tid, "pasted-after-scroll")
        cursor = self.wait_for(self.manager, tid, b"FIXTURE_REPLY:pasted-after-scroll", cursor)
        self.assertEqual(self.mode(tid).split()[0], "0")
        self.manager.scroll(tid, -5)
        self.assertEqual(self.manager.scroll_end(tid), {"ok": True})
        self.assertEqual(self.mode(tid).split()[0], "0")

        self.manager.close_terminal(tid)
        with self.assertRaises(terminal.InputNotSent):
            self.manager.paste(tid, "after-close")
        with self.assertRaises(RuntimeError):
            self.manager.scroll(tid, -1)

    def test_full_screen_mouse_app_gets_wheel_and_menu_keys_through_real_tmux(self):
        record = Path(self.directory.name) / "received.bin"
        # Like a remote TUI over ssh: alternate screen, mouse reporting, raw input.
        code = ("import os,sys,tty\n"
                "out=open(sys.argv[1],'ab',buffering=0)\n"
                "tty.setraw(0)\n"
                "os.write(1,b'\\x1b[?1049h\\x1b[?1000h\\x1b[?1006hRECORDER_READY')\n"
                "while True:\n"
                " data=os.read(0,1024)\n"
                " if not data: break\n"
                " out.write(data)\n")
        created = self.manager.create("wheel-fixture", [sys.executable, "-u", "-c", code, str(record)],
                                      self.directory.name, {"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"},
                                      {"agent": "fixture", "host": "local"})
        tid = created["id"]
        self.wait_for(self.manager, tid, b"RECORDER_READY")
        deadline = time.monotonic() + 5
        while self.tmux("display-message", "-p", "-t", "sb-" + tid,
                        "#{alternate_on}|#{mouse_any_flag}|#{mouse_sgr_flag}").stdout.strip() != "1|1|1":
            if time.monotonic() > deadline:
                self.fail("fixture app did not enable alternate screen and mouse reporting")
            time.sleep(0.05)
        size = self.tmux("display-message", "-p", "-t", "sb-" + tid, "#{pane_width} #{pane_height}").stdout.split()
        x, y = max(int(size[0]) // 2, 1), max(int(size[1]) // 2, 1)

        def received(expected):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                data = record.read_bytes() if record.exists() else b""
                if expected in data:
                    return data
                time.sleep(0.05)
            self.fail("fixture app did not receive the expected input")

        self.assertEqual(self.manager.scroll(tid, -3), {"ok": True, "mode": "app", "position": None})
        wheel = ("\x1b[<64;%d;%dM" % (x, y)).encode()
        self.assertEqual(received(wheel), wheel)
        self.assertEqual(self.mode(tid).split()[0], "0")
        self.manager.scroll(tid, 6)
        self.assertEqual(received(("\x1b[<65;%d;%dM" % (x, y)).encode() * 2), wheel + ("\x1b[<65;%d;%dM" % (x, y)).encode() * 2)
        before = len(record.read_bytes())
        self.assertEqual(self.manager.send_keys(tid, ["Down", "Up", "Enter"]), {"ok": True})
        keys = received(b"\r")[before:]
        self.assertIn(keys, (b"\x1b[B\x1b[A\r", b"\x1bOB\x1bOA\r"))

    def test_ctrl_b_reaches_the_cli_instead_of_the_boards_own_tmux_client(self):
        record = Path(self.directory.name) / "prefix.bin"
        code = ("import os,sys,tty\n"
                "out=open(sys.argv[1],'ab',buffering=0)\n"
                "tty.setraw(0)\n"
                "os.write(1,b'RECORDER_READY')\n"
                "while True:\n"
                " data=os.read(0,1024)\n"
                " if not data: break\n"
                " out.write(data)\n")
        created = self.manager.create("prefix-fixture", [sys.executable, "-u", "-c", code, str(record)],
                                      self.directory.name, {"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"},
                                      {"agent": "fixture", "host": "local"})
        tid = created["id"]
        self.wait_for(self.manager, tid, b"RECORDER_READY")
        self.manager.write(tid, "\x02d\x02%")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (record.read_bytes() if record.exists() else b"") != b"\x02d\x02%":
            time.sleep(0.05)
        self.assertEqual(record.read_bytes(), b"\x02d\x02%")
        self.assertTrue(self.manager.terminals[tid].alive if hasattr(self.manager.terminals[tid], "alive") else True)
        self.assertEqual(self.tmux("list-clients", "-t", "sb-" + tid, "-F", "#{client_session}").stdout.strip(), "sb-" + tid,
                         "the board's client is still attached")

    def test_empty_history_never_traps_input_in_copy_mode(self):
        created = self.create()
        tid = created["id"]
        self.wait_for(self.manager, tid, b"FIXTURE_READY")
        self.assertEqual(self.manager.scroll(tid, -3), {"ok": True, "mode": "history", "position": 0})
        self.assertEqual(self.mode(tid).split()[0], "0")
        self.assertFalse(self.manager.terminals[tid].scrolled)
        self.manager.write(tid, "typed-after-empty-scroll\n")
        self.wait_for(self.manager, tid, b"FIXTURE_REPLY:typed-after-empty-scroll")

    def test_immediate_exit_and_missing_executable_are_reported_as_failed(self):
        for key, command in (("exit", [sys.executable, "-c", "raise SystemExit(9)"]),
                             ("missing", ["/no-such-sessionholic-fixture"])):
            with self.assertRaisesRegex(RuntimeError, "CLI"):
                self.manager.create(key, command, self.directory.name, {"PATH": "/usr/bin:/bin"})
        self.assertTrue(all(not row["alive"] for row in self.manager.list()))
        self.assertEqual(list(Path(self.directory.name).glob("*.launch.json")), [])


if __name__ == "__main__":
    unittest.main()
