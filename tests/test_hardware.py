import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hardware  # noqa: E402
import hardware_monitor  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    94167.
Pages active:                                 711969.
Pages inactive:                               705509.
Pages wired down:                             223291.
Pages occupied by compressor:                  10000.
"Translation faults":                     3615555772.
"""


def reading(busy, total, **fields):
    return {"schema": 1, "system": "macos", "cpu": {"model": "Fixture M1", "cores": 8, "load": [1.5, 1.25, 1.0],
            "ticks": {"busy": busy, "total": total}}, "memory": {"total": 16 * 2 ** 30, "used": 4 * 2 ** 30},
            "disk": {"total": 1000, "free": 250}, "battery": None, "uptimeSeconds": 3600, **fields}


class ReadingTest(unittest.TestCase):
    def test_mac_outputs_are_read_as_activity_monitor_and_pmset_report_them(self):
        self.assertEqual(hardware.parse_vm_stat(VM_STAT), (711969 + 223291 + 10000) * 16384)
        self.assertIsNone(hardware.parse_vm_stat("no header"))
        cases = {
            "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true":
                {"percent": 80, "charging": False, "power": "ac"},
            "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t42%; charging; 1:02 remaining present: true":
                {"percent": 42, "charging": True, "power": "ac"},
            "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t55%; discharging; 3:20 remaining present: true":
                {"percent": 55, "charging": False, "power": "battery"},
            "Now drawing from 'AC Power'": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(hardware.parse_pmset(text), expected)
        self.assertEqual(hardware.parse_boottime("{ sec = 1000, usec = 5 } Sun Oct  4", 4600), 3600)
        self.assertIsNone(hardware.parse_boottime("", 1))

    def test_a_mac_reading_uses_only_fixed_system_commands_and_the_kernel_ticks(self):
        seen = []
        outputs = {"hw.memsize": "17179869184\n", "machdep.cpu.brand_string": "Fixture M1\n",
                   "kern.boottime": "{ sec = 1000, usec = 0 }\n"}

        def command(argv):
            seen.append(argv)
            if argv[0] == "/usr/bin/vm_stat":
                return VM_STAT
            if argv[0] == "/usr/bin/pmset":
                return ""
            return outputs[argv[-1]]
        result = hardware.macos(command=command, ticks=lambda: {"busy": 5, "total": 20}, now=lambda: 4600)
        self.assertEqual(result["system"], "macos")
        self.assertEqual(result["cpu"]["model"], "Fixture M1")
        self.assertEqual(result["cpu"]["ticks"], {"busy": 5, "total": 20})
        self.assertEqual(result["memory"], {"total": 17179869184, "used": (711969 + 223291 + 10000) * 16384})
        self.assertIsNone(result["battery"])
        self.assertEqual(result["uptimeSeconds"], 3600)
        self.assertTrue(all(argv[0].startswith("/usr/") for argv in seen), seen)

    @unittest.skipUnless(sys.platform == "darwin", "a real Mac reading")
    def test_this_mac_reads_itself_as_one_json_line(self):
        done = subprocess.run([sys.executable, "-I", str(Path(hardware.__file__))], capture_output=True, timeout=30)
        result = json.loads(done.stdout)
        self.assertEqual(result["system"], "macos")
        self.assertGreater(result["memory"]["total"], 0)
        self.assertGreaterEqual(result["cpu"]["ticks"]["total"], result["cpu"]["ticks"]["busy"])
        self.assertEqual(done.stdout.count(b"\n"), 1)


class MonitorTest(unittest.TestCase):
    def monitor(self, outputs, hosts=None):
        clock = [1000.0]
        calls = []

        def runner(host):
            calls.append(host["name"])
            value = outputs.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        hosts = hosts or [{"name": "laptop", "label": "노트북"}]
        return hardware_monitor.HardwareMonitor(hosts, runner, interval=30, manual_interval=10,
                                                clock=lambda: clock[0], threaded=False), clock, calls

    def test_cpu_use_is_the_busy_share_between_two_readings_and_ticks_stay_private(self):
        monitor, clock, calls = self.monitor([reading(100, 1000), reading(400, 2000)])
        self.assertEqual(monitor.snapshot()["hosts"][0]["status"], "pending", "nothing is read without a look")
        self.assertEqual(calls, [])
        first = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual((first["status"], first["cpu"]["percent"]), ("ready", None), "one reading has no share yet")
        self.assertNotIn("ticks", first["cpu"])
        self.assertEqual(first["memory"]["percent"], 25)
        self.assertEqual(first["disk"]["percent"], 75)
        monitor.snapshot(refresh=True)
        self.assertEqual(len(calls), 1, "spaced by the interval")
        clock[0] += 30
        second = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual(second["cpu"]["percent"], 30)

    def test_cpu_use_is_not_shown_across_a_restart_or_after_a_long_gap(self):
        monitor, clock, _ = self.monitor([reading(100, 1000), reading(150, 1200, uptimeSeconds=60),
                                          reading(250, 1400, uptimeSeconds=90), reading(900, 9000, uptimeSeconds=4000)])
        monitor.snapshot(refresh=True)
        clock[0] += 30
        restarted = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertIsNone(restarted["cpu"]["percent"], "ticks before a restart are not compared")
        clock[0] += 30
        self.assertEqual(monitor.snapshot(refresh=True)["hosts"][0]["cpu"]["percent"], 50)
        clock[0] += 3601
        self.assertIsNone(monitor.snapshot(refresh=True)["hosts"][0]["cpu"]["percent"], "an hour's average is not now")
        # A Windows power line it cannot tell is neither mains nor battery.
        unknown = hardware_monitor.normalize(reading(1, 2, battery={"percent": 50, "charging": False, "power": None}))
        self.assertEqual(unknown["battery"], {"percent": 50, "charging": False, "power": None})

    def test_a_failure_keeps_the_last_numbers_marked_and_waits_longer(self):
        monitor, clock, calls = self.monitor([reading(1, 10), RuntimeError("SSH 연결 실패"), RuntimeError("x"), reading(2, 20)])
        monitor.snapshot(refresh=True)
        clock[0] += 30
        stale = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["memory"]["total"], 16 * 2 ** 30)
        self.assertIn("연결하지 못했습니다", stale["error"])
        clock[0] += 30
        monitor.snapshot(refresh=True)
        self.assertEqual(len(calls), 2, "a failing machine waits interval × 2")
        monitor.snapshot(force=True)
        self.assertEqual(len(calls), 3, "a manual refresh (a machine that just woke) skips the backoff")
        clock[0] += 60
        monitor.snapshot(refresh=True)
        self.assertEqual(len(calls), 3, "then interval × 4")
        clock[0] += 60
        recovered = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual((recovered["status"], recovered["error"]), ("ready", None))
        self.assertEqual(len(calls), 4)

    def test_an_unreachable_machine_is_offline_and_bad_output_is_refused(self):
        monitor, clock, _ = self.monitor([RuntimeError("SSH 연결 실패"), {"schema": 1, "system": "linux"}])
        offline = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual(offline["status"], "offline")
        self.assertNotIn("cpu", offline)
        clock[0] += 60
        refused = monitor.snapshot(refresh=True)["hosts"][0]
        self.assertEqual((refused["status"], refused["error"]), ("offline", "기기 정보를 읽지 못했습니다."))
        bad = reading(5, 1, memory={"total": 10, "used": 20}, battery={"percent": 300}, uptimeSeconds=-1)
        normalized = hardware_monitor.normalize(bad)
        self.assertIsNone(normalized["cpu"]["ticks"], "busy above total is no reading")
        self.assertIsNone(normalized["memory"])
        self.assertIsNone(normalized["battery"])
        self.assertIsNone(normalized["uptimeSeconds"])

    def test_a_manual_refresh_reads_again_sooner(self):
        monitor, clock, calls = self.monitor([reading(1, 10), reading(2, 20)])
        monitor.snapshot(refresh=True)
        clock[0] += 10
        monitor.snapshot(refresh=True)
        self.assertEqual(len(calls), 1)
        monitor.snapshot(force=True)
        self.assertEqual(len(calls), 2)


class HostsTest(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.json"
            path.write_text(json.dumps(rows))
            return server.load_hosts(path)

    def test_a_monitoring_only_machine_is_marked_and_never_the_only_or_local_one(self):
        local = {"name": "board", "label": "보드", "local": True}
        watched = {"name": "office", "label": "사무실", "ssh": "office", "monitorOnly": True}
        hosts = self.load([local, watched])
        self.assertNotIn("monitorOnly", hosts[0])
        self.assertTrue(hosts[1]["monitorOnly"])
        for rows in ([watched], [local, {**watched, "monitorOnly": "yes"}], [{**local, "monitorOnly": True}, watched]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.load(rows)

    def test_a_remote_reading_sends_only_the_helper_over_ssh(self):
        host = {"name": "office", "label": "사무실", "local": False, "ssh": "office", "python": "/usr/bin/python3"}
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(reading(1, 2)).encode())
        with mock.patch.object(server.subprocess, "run", return_value=done) as run:
            self.assertEqual(server.run_hardware(host)["system"], "macos")
        self.assertEqual(run.call_args.kwargs["input"], server.HARDWARE.read_bytes())
        self.assertEqual(run.call_args.args[0][:2], ["ssh", "-o"])
        for code, kind in ((255, RuntimeError), (1, RuntimeError)):
            with mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout=b"")), \
                    self.assertRaises(kind):
                server.run_hardware(host)
        with mock.patch.object(server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=b'{"error":"X"}')), \
                self.assertRaises(ValueError):
            server.run_hardware(host)

    def test_the_settings_section_is_opt_in_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / ".config/sessionholic"
            folder.mkdir(parents=True, mode=0o700)
            path = folder / "config.json"

            def load(section):
                path.write_text(json.dumps({"version": 2, "profiles": [], "environments": {}, "projects": [],
                                            **({"hardware": section} if section is not None else {})}))
                path.chmod(0o600)
                return settings.load(home)["hardware"]
            self.assertIsNone(load(None))
            self.assertEqual(load({}), {"intervalSeconds": 30, "manualIntervalSeconds": 10})
            for bad in ({"intervalSeconds": 10}, {"manualIntervalSeconds": 1}, {"other": 1}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    load(bad)


class RouteTest(unittest.TestCase):
    TOKEN = "fixture-hardware-token"

    def serve(self, hardware):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        board = server.Board([{"name": "local", "label": "로컬", "local": True}], 60,
                             runner=lambda host, args, timeout: {"codex": [], "claude": [], "errors": []})
        httpd = server.make_server("127.0.0.1", 0, board, self.TOKEN, state_dir=temp.name, push=False, hardware=hardware)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, path, body, {"Authorization": "Bearer " + self.TOKEN, **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_routes_exist_only_with_a_monitor_and_need_a_login(self):
        self.assertEqual(self.request(self.serve(None), "GET", "/api/hardware")[0], 404)
        monitor = mock.Mock()
        monitor.snapshot.return_value = {"available": True, "refreshing": False, "hosts": []}
        port = self.serve(monitor)
        status, data = self.request(port, "GET", "/api/hardware")
        self.assertEqual((status, json.loads(data)["available"]), (200, True))
        self.assertEqual(monitor.snapshot.call_args.kwargs, {"refresh": True})
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/api/hardware")
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import shutil
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


if __name__ == "__main__":
    unittest.main()
