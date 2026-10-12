from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_programs as lp  # noqa: E402

LAUNCHER = ["/bin/zsh", "-lic", "zz"]
FIRST, SECOND = "a" * 32, "b" * 32


def started(text):
    return int(time.mktime(time.strptime(text, lp.LSTART)))


PS = """  501   501     1 Mon Oct 12 15:16:50 2026     /usr/sbin/sshd
  501 57597   501 Mon Oct 12 15:16:52 2026     /bin/zsh -lic zz
  501 57608 57597 Mon Oct 12 15:16:59 2026     /bin/zsh -c _zz_run
  501 57623 57608 Mon Oct 12 15:17:00 2026     /Users/example/.local/bin/claude agents --dangerously-skip-permissions --cwd /Users/example/My Project --mcp-config /x.json
  501 57635 57623 Mon Oct 12 15:17:00 2026     /Users/example/.local/bin/claude daemon run --origin transient
  501 67233   501 Mon Oct 12 17:14:22 2026     /bin/zsh -lic zz
  501 67255 67233 Mon Oct 12 17:14:29 2026     node /opt/homebrew/bin/codex --cd /Users/example/other
  501 67260 67255 Mon Oct 12 17:14:29 2026     /opt/homebrew/lib/node_modules/@openai/codex/vendor/codex --cd /Users/example/other
  501 70000   501 Mon Oct 12 17:30:00 2026     /bin/zsh -lic zz
  501 70001   501 Mon Oct 12 17:30:02 2026     /bin/zsh -lic zz
  502 80000     1 Mon Oct 12 18:00:00 2026     /bin/zsh -lic zz
  502 80001 80000 Mon Oct 12 18:00:01 2026     claude agents --cwd /Users/other/secret
  501 90000   501 Mon Oct 12 19:00:00 2026     /bin/zsh /Users/example/bin/zz-launcher
  501 90001 90000 Mon Oct 12 19:00:03 2026     claude
not a process line
"""


class TableTest(unittest.TestCase):
    def test_ps_rows_are_parsed_in_the_c_locale_and_bad_lines_are_skipped(self):
        commands = []
        rows = lp.table(lambda command: commands.append(command) or PS, uid=501)
        self.assertEqual(commands, [[lp.PS, "-axww", "-o", "uid=,pid=,ppid=,lstart=,command="]])
        self.assertEqual(len(rows), 12, "another user's processes are never read")
        self.assertEqual(rows[1], (57597, 501, started("Mon Oct 12 15:16:52 2026"), "/bin/zsh -lic zz"))


class ProgramTest(unittest.TestCase):
    def test_agent_clis_are_named_and_claude_helpers_are_not(self):
        cases = {
            "/Users/example/.local/bin/claude agents --cwd /Users/example/project": {"program": "claude-agents", "cwd": "/Users/example/project"},
            "/Users/example/.local/bin/claude agents --cwd=relative": {"program": "claude-agents", "cwd": None},
            "claude": {"program": "claude", "cwd": None},
            "/x/claude daemon run": None, "claude bg-pty-host --bg-pty-host x": None, "claude bg-spare": None,
            "node /opt/homebrew/bin/codex resume 019a": {"program": "codex", "cwd": None},
            "/opt/homebrew/bin/codex app-server --listen unix://": None,
            "/bin/zsh -lic zz": None, "": None,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(lp.program_of(command), expected)

    def test_each_terminal_names_its_own_launcher_and_the_first_cli_below_it(self):
        rows = lp.table(lambda command: PS, uid=501)
        terminals = [{"id": FIRST, "createdAt": started("Mon Oct 12 15:16:51 2026")},
                     {"id": SECOND, "createdAt": started("Mon Oct 12 17:14:21 2026")},
                     {"id": "c" * 32, "createdAt": started("Mon Oct 12 17:30:00 2026")},
                     {"id": "d" * 32, "createdAt": started("Mon Oct 12 09:00:00 2026")},
                     {"id": "f" * 32, "createdAt": started("Mon Oct 12 18:00:00 2026")},
                     {"id": "../x", "createdAt": 1}, {"id": "e" * 32, "createdAt": "soon"}]
        result = lp.describe(terminals, LAUNCHER, rows)
        self.assertEqual(result[FIRST], {"program": "claude-agents", "cwd": "/Users/example/My Project", "pid": 57623})
        self.assertEqual(result[SECOND], {"program": "codex", "cwd": None, "pid": 67255})
        # Two launchers in the same window, or none: no guess.
        self.assertEqual(result["c" * 32], {"program": None})
        self.assertEqual(result["d" * 32], {"program": None})
        self.assertEqual(result["f" * 32], {"program": None}, "another user's launcher never names this terminal")
        self.assertEqual(set(result), {FIRST, SECOND, "c" * 32, "d" * 32, "f" * 32})
        # Only behind an interpreter: another command ending in the launcher path never matches.
        other = lp.describe([{"id": "a" * 32, "createdAt": started("Mon Oct 12 19:00:00 2026")}], ["bin/zz-launcher"],
                            [(1, 0, started("Mon Oct 12 19:00:00 2026"), "vim /Users/example/bin/zz-launcher"),
                             (2, 1, started("Mon Oct 12 19:00:01 2026"), "claude")])
        self.assertEqual(other["a" * 32], {"program": None})
        self.assertEqual(lp.program_of("claude agents --cwd /Users/example/p -- extra"), {"program": "claude-agents", "cwd": "/Users/example/p"})
        # A script launcher appears behind its interpreter.
        script = lp.describe([{"id": "a" * 32, "createdAt": started("Mon Oct 12 19:00:00 2026")}], ["/Users/example/bin/zz-launcher"], rows)
        self.assertEqual(script["a" * 32], {"program": "claude", "cwd": None, "pid": 90001})

    def test_terminals_opened_close_together_never_share_a_launcher(self):
        opened = started("Mon Oct 12 15:16:51 2026")
        first, second = {"id": FIRST, "createdAt": opened}, {"id": SECOND, "createdAt": opened + 10}
        launcher = lambda pid, after: (pid, 1, opened + after, "/bin/zsh -lic zz")
        program = lambda pid, parent, after: (pid, parent, opened + after, "claude")
        # Each launcher started after its own terminal: both are named, though the windows overlap.
        rows = [launcher(10, 1), program(11, 10, 2), launcher(20, 11), program(21, 20, 12)]
        self.assertEqual(lp.describe([first, second], LAUNCHER, rows),
                         {FIRST: {"program": "claude", "cwd": None, "pid": 11}, SECOND: {"program": "claude", "cwd": None, "pid": 21}})
        # The first one's launcher is gone: the second one's is not given to it as well.
        self.assertEqual(lp.describe([first, second], LAUNCHER, rows[2:]),
                         {FIRST: {"program": None}, SECOND: {"program": "claude", "cwd": None, "pid": 21}})
        # Two terminals opened in the same second: their launchers name neither.
        self.assertEqual(lp.describe([first, {**second, "createdAt": opened}], LAUNCHER, rows[2:]),
                         {FIRST: {"program": None}, SECOND: {"program": None}})

    def test_a_host_without_a_launcher_reports_nothing(self):
        with patch("launch.new_session", side_effect=ValueError("no launcher")):
            self.assertEqual(lp.launcher_programs({"terminals": []}), {"terminals": {}})
        with patch("launch.new_session", return_value={"argv": LAUNCHER}), self.assertRaises(ValueError):
            lp.launcher_programs({"terminals": "x"})


class WorkflowTest(unittest.TestCase):
    def test_reads_run_in_the_background_once_a_minute_per_host_and_save_the_program(self):
        import server

        class Terminals:
            def __init__(self):
                self.saved, self.rows = [], [
                    {"id": FIRST, "mode": "new-session", "alive": True, "host": "laptop", "createdAt": 100},
                    {"id": SECOND, "mode": "attach", "alive": True, "host": "laptop", "createdAt": 100},
                    {"id": "c" * 32, "mode": "new-session", "alive": True, "host": "pc", "createdAt": 100},
                ]

            def list(self):
                return self.rows

            def set_program(self, identifier, info):
                self.saved.append((identifier, info))
                return True

        class Board:
            hosts = [{"name": "laptop", "label": "노트북"}, {"name": "pc", "label": "PC", "platform": "windows"}]
            state = {"laptop": {"ok": True}, "pc": {"ok": True}}

        with tempfile.TemporaryDirectory() as directory:
            terminals = Terminals()
            flow = server.Workflow(Board(), terminals, directory)
            done = threading.Event()
            calls = []

            def rpc(host, op, args, timeout):
                calls.append((host["name"], op, args))
                done.set()
                return {"terminals": {FIRST: {"program": "claude-agents", "cwd": "/p"}, "f" * 32: {"program": "codex"}}}
            with patch.object(flow.transfers, "rpc", side_effect=rpc):
                flow.describe_launchers()
                self.assertTrue(done.wait(3))
                for _ in range(50):
                    if terminals.saved:
                        break
                    time.sleep(0.02)
                flow.describe_launchers()
        self.assertEqual(calls, [("laptop", "launcher_programs", {"terminals": [{"id": FIRST, "createdAt": 100}]})])
        self.assertEqual(terminals.saved, [(FIRST, {"program": "claude-agents", "cwd": "/p"})])


if __name__ == "__main__":
    unittest.main()
