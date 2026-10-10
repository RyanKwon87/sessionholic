"""Installation names: public defaults, validation, remote propagation and rendered web files."""
import base64
import copy
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import instance  # noqa: E402
import server  # noqa: E402

# A synthetic installation; every name differs from the public one.
DEMO = {
    "version": 1,
    "brand": {
        "title": "데모 <보드>", "shortName": "데모 & 보드", "manifestName": "데모 · 보드",
        "description": "합성 \"설명\"", "publicMeta": False,
        "loginHeadline": "들어오세요 <지금>", "loginLead": "합성 안내 & 문구",
        "loginTokenHelp": "보드 기기의 `~/.config/demo-board/token`에\n있습니다 <확인>.",
        "logPrefix": "데모 보드", "serverVersion": "DemoBoard/2", "threadPrefix": "demo-board",
        "codexClientName": "demo-board",
    },
    "web": {"storagePrefix": "db:", "historyKey": "demoBoard", "cachePrefix": "demo-board-", "manifestId": "/"},
    "paths": {"configDir": "~/.config/demo-board", "stateDir": "~/.local/state/demo-board",
              "hostConfig": ".config/demo-board/config.json"},
    "names": {"cookie": "db_token", "tmuxSocket": "demo-board", "sshFallback": "demo-board"},
    "remote": {
        "runtimeDir": ".local/state/demo-board-runtime", "transfersDir": ".local/state/demo-board-transfers",
        "workspacesDir": ".local/share/demo-board/workspaces", "chatLedgerDir": ".demo-board",
        "attachmentsDir": ".demo-board-attachments", "attachStateDir": ".local/state/demo-board",
    },
}

# Every name the modules derive at import, printed as JSON by a separate interpreter.
PROBE = r'''
import json, re, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import instance, server, terminal, attachments, native_chat, transfer, transfer_worker, settings
import collector, events, launch
home = Path.home()
text = launch._handoff_text({"host": "h", "agent": "codex", "cwd": "/w"}, {"host": "h", "id": "x", "agent": "codex"},
                            [], {"available": False}, transfer={})
print(json.dumps({
    "origin": instance.ORIGIN,
    "stateDir": str(server.STATE_DIR), "tokenFile": str(server.TOKEN_FILE),
    "defaultHosts": str(server.DEFAULT_HOSTS), "configDir": str(server.CONFIG_DIR),
    "cookie": server.COOKIE, "serverVersion": server.Handler.server_version,
    "sshFallback": str(server.ssh_control_dir(home / ("x" * 90))),
    "logPrefix": server.LOG_PREFIX, "threadPrefix": server.THREAD_PREFIX,
    "terminalState": str(terminal.DEFAULT_STATE), "tmuxSocket": terminal.DEFAULT_SOCKET,
    "attachments": attachments.DIRECTORY, "ledger": str(native_chat.ledger_root(home)),
    "transfers": str(transfer_worker.root()),
    "workspaces": str(instance.remote_path("workspacesDir", home)),
    "attachState": str(instance.remote_path("attachStateDir", home)),
    "runtimeDir": re.search(r"root=pathlib\.Path\.home\(\)/'([^']*)'", transfer.BOOTSTRAP).group(1),
    "hostConfig": str(settings.config_path(home)), "watchLock": collector.WATCH_LOCK_NAME % 7,
    "codexClient": collector.NAMES["brand"]["codexClientName"], "eventsThread": events.BRAND["threadPrefix"],
    "handoffAttachments": re.search(r"적용하고, (\S+) 상대 경로", text).group(1),
}, ensure_ascii=False))
'''


def run_python(code, home, instance_file=None, *args):
    env = {key: value for key, value in os.environ.items() if key != instance.ENVIRONMENT}
    env["HOME"] = str(home)
    if instance_file is not None:
        env[instance.ENVIRONMENT] = str(instance_file)
    done = subprocess.run([sys.executable, "-c", code, str(ROOT)] + [str(arg) for arg in args],
                          capture_output=True, env=env, timeout=60)
    return done


def write_instance(folder, data, name="instance.json", mode=0o600):
    path = Path(folder) / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    path.chmod(mode)
    return path


class TempHome(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.temp = Path(self.directory.name).resolve()
        self.home = self.temp / "home"
        self.home.mkdir()

    def tearDown(self):
        self.directory.cleanup()


class InstanceNamesTest(TempHome):
    def expected(self, slug, cookie, socket, storage_names):
        home = self.home
        return {
            "stateDir": str(home / (".local/state/" + slug)), "tokenFile": str(home / (".config/" + slug + "/token")),
            "defaultHosts": str(home / (".config/" + slug + "/hosts.json")), "configDir": str(home / (".config/" + slug)),
            "cookie": cookie, "sshFallback": "/tmp/%s-%d" % (slug, os.getuid()), "tmuxSocket": socket,
            "terminalState": str(home / (".local/state/" + slug + "/terminals")),
            "attachments": "." + slug + "-attachments", "handoffAttachments": "." + slug + "-attachments",
            "ledger": str(home / ("." + slug)), "transfers": str(home / (".local/state/" + slug + "-transfers")),
            "workspaces": str(home / (".local/share/" + slug + "/workspaces")),
            "attachState": str(home / (".local/state/" + slug)),
            "hostConfig": str(home / (".config/" + slug + "/config.json")),
            "watchLock": slug + "-watch-7", "codexClient": slug, "eventsThread": slug, "threadPrefix": slug,
            **storage_names,
        }

    def test_without_an_instance_every_name_is_the_public_one(self):
        if os.path.lexists(str(ROOT / instance.FILENAME)):
            self.skipTest("an installation instance.json is present in this checkout")
        done = run_python(PROBE, self.home)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        names = json.loads(done.stdout)
        self.assertEqual(names.pop("origin"), "default")
        self.assertEqual(names.pop("runtimeDir"), ".local/state/sessionholic-transfer-runtime")
        self.assertEqual(names, self.expected("sessionholic", "sessionholic_token", "sessionholic", {
            "serverVersion": "Sessionholic/1", "logPrefix": "세션홀릭"}))

    def test_an_instance_file_names_every_path_socket_cookie_and_remote_folder(self):
        demo = copy.deepcopy(DEMO)
        # The fixture's runtime folder differs from "<slug>-transfer-runtime" on purpose.
        path = write_instance(self.temp, demo)
        done = run_python(PROBE, self.home, path)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        names = json.loads(done.stdout)
        self.assertEqual(names.pop("origin"), "environment")
        self.assertEqual(names.pop("runtimeDir"), ".local/state/demo-board-runtime")
        self.assertEqual(names, self.expected("demo-board", "db_token", "demo-board", {
            "serverVersion": "DemoBoard/2", "logPrefix": "데모 보드"}))
        self.assertNotIn("sessionholic", json.dumps(names))

    def test_installation_folder_file_is_used_without_the_environment_variable(self):
        copy_root = self.temp / "install"
        copy_root.mkdir()
        (copy_root / "instance.py").write_bytes((ROOT / "instance.py").read_bytes())
        write_instance(copy_root, DEMO)
        code = ("import sys;sys.path.insert(0,sys.argv[2]);import instance,json;"
                "print(json.dumps([instance.ORIGIN,instance.current()['names']['tmuxSocket']]))")
        done = run_python(code, self.home, None, copy_root)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        self.assertEqual(json.loads(done.stdout), ["file", "demo-board"])

    def test_current_is_read_only_and_paths_expand_the_given_home(self):
        with self.assertRaises(TypeError):
            instance.current()["names"]["tmuxSocket"] = "other"
        self.assertEqual(instance.path("stateDir", self.home), self.home / ".local/state/sessionholic")
        self.assertEqual(instance.path("hostConfig", self.home), self.home / ".config/sessionholic/config.json")
        self.assertEqual(instance.remote_path("transfersDir", self.home), self.home / ".local/state/sessionholic-transfers")
        parsed = instance.parse({"version": 1, "paths": {"configDir": "/srv/board-config"}})
        self.assertEqual((parsed["paths"]["tokenFile"], parsed["paths"]["defaultHosts"]),
                         ("/srv/board-config/token", "/srv/board-config/hosts.json"))


class InstanceValidationTest(TempHome):
    def broken(self, section, key, value):
        data = {"version": 1, section: {key: value}}
        with self.assertRaises(ValueError, msg="%s.%s=%r" % (section, key, value)):
            instance.parse(data)

    def test_unknown_keys_versions_and_values_fail_closed(self):
        for data in ({}, {"version": 2}, {"version": True}, {"version": "1"}, [], {"version": 1, "extra": {}},
                     {"version": 1, "brand": {"unknown": "x"}}, {"version": 1, "web": {"cacheName": "x-shell-v1"}},
                     {"version": 1, "names": []}):
            with self.assertRaises(ValueError, msg=repr(data)):
                instance.parse(data)
        cases = [
            ("brand", "title", ""), ("brand", "title", "줄\n바꿈"), ("brand", "title", 3),
            ("brand", "publicMeta", "false"), ("brand", "loginTokenHelp", "닫히지 않은 `코드"),
            ("brand", "loginTokenHelp", "\n".join("줄" for _ in range(7))), ("brand", "loginTokenHelp", "빈 줄\n\n"),
            ("brand", "serverVersion", "Bad Version\r\nX: 1"), ("brand", "threadPrefix", "-lead"),
            ("web", "storagePrefix", "has space"), ("web", "historyKey", "1key"), ("web", "manifestId", "relative"),
            ("web", "cachePrefix", "bad/prefix"),
            ("paths", "configDir", "relative/config"), ("paths", "stateDir", "~/../state"),
            ("paths", "tokenFile", "~/.config//token"), ("paths", "hostConfig", "/absolute/config.json"),
            ("paths", "hostConfig", "../config.json"), ("paths", "hostConfig", "~/.config/x.json"),
            ("names", "cookie", "a;b"), ("names", "tmuxSocket", "a b"), ("names", "sshFallback", "../x"),
            ("remote", "attachmentsDir", "a/b"), ("remote", "attachmentsDir", ".."),
            ("remote", "runtimeDir", "/tmp/runtime"), ("remote", "transfersDir", ".local/../x"),
        ]
        for section, key, value in cases:
            self.broken(section, key, value)

    def test_unsafe_files_fail_closed(self):
        valid = write_instance(self.temp, {"version": 1})
        self.assertEqual(instance.read(valid)["names"]["tmuxSocket"], "sessionholic")
        link = self.temp / "link.json"
        link.symlink_to(valid)
        writable = write_instance(self.temp, {"version": 1}, "writable.json", 0o622)
        large = write_instance(self.temp, {"version": 1, "brand": {"description": "x" * 400}}, "large.json")
        large.write_text(" " * (instance.SIZE_LIMIT + 1) + "{}")
        broken = self.temp / "broken.json"
        broken.write_text("{not json")
        broken.chmod(0o600)
        for path in (link, writable, large, broken, self.temp / "missing.json", self.temp):
            with self.assertRaises(ValueError, msg=str(path)):
                instance.read(path)

    def test_board_and_collector_refuse_to_start_with_an_invalid_instance(self):
        path = write_instance(self.temp, {"version": 1, "names": {"tmuxSocket": "bad socket"}})
        for code in ("import sys;sys.path.insert(0,sys.argv[1]);import server",
                     "import sys;sys.path.insert(0,sys.argv[1]);import collector"):
            done = run_python(code, self.home, path)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("ValueError", done.stderr.decode())
        missing = run_python("import sys;sys.path.insert(0,sys.argv[1]);import server", self.home,
                             self.temp / "missing.json")
        self.assertNotEqual(missing.returncode, 0)
        # The local collector runs with -I and still loads (and checks) the sibling module.
        done = subprocess.run([sys.executable, "-I", str(ROOT / "collector.py"), "read", "claude", "x"],
                              capture_output=True, timeout=60,
                              env={**{k: v for k, v in os.environ.items() if k != instance.ENVIRONMENT},
                                   "HOME": str(self.home), instance.ENVIRONMENT: str(path)})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ValueError", done.stderr.decode())


BOARD = r'''
import base64, json, sys
sys.path.insert(0, sys.argv[1])
import instance, server
from transfer import Transfers
runtime = Transfers([], sys.argv[2]).runtime()
again = Transfers([], sys.argv[2]).runtime()
print(json.dumps({"blob": instance.remote_blob(), "prelude": server.remote_prelude(),
                  "program": base64.b64encode(server.remote_program()).decode(),
                  "runtime": runtime["runtime"], "sha": runtime["sha256"], "same": again == runtime}))
'''

REMOTE = r'''
import json, sys
exec(sys.stdin.read())
import instance, settings
from pathlib import Path
print(json.dumps({"origin": instance.ORIGIN, "remote": dict(instance.current()["remote"]),
                  "socket": instance.current()["names"]["sshFallback"],
                  "client": instance.current()["brand"]["codexClientName"],
                  "hostConfig": str(settings.config_path(Path("/h")))}))
'''

ARCHIVE = r'''
import json, sys
sys.path.insert(0, sys.argv[2])
import instance, transfer_worker
from pathlib import Path
print(json.dumps({"origin": instance.ORIGIN, "remote": dict(instance.current()["remote"]),
                  "transfers": str(transfer_worker.root())}))
'''


class RemotePropagationTest(TempHome):
    """The board's names reach remote hosts only through what the board sends."""

    def board(self, instance_file):
        done = run_python(BOARD, self.home, instance_file, self.temp / "board-state")
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return json.loads(done.stdout)

    def test_blob_carries_only_the_remote_names(self):
        blob = json.loads(instance.remote_blob())
        self.assertEqual({key: sorted(value) for key, value in blob.items() if key != "version"},
                         {key: sorted(value) for key, value in instance.REMOTE_KEYS.items()})
        self.assertEqual(blob["remote"], dict(instance.current()["remote"]))

    def test_collector_stdin_and_helper_archive_use_the_board_names_not_the_remote_file(self):
        board_file = write_instance(self.temp, DEMO, "board.json")
        decoy = copy.deepcopy(DEMO)
        decoy["remote"] = {key: value.replace("demo-board", "decoy") for key, value in DEMO["remote"].items()}
        decoy["names"] = {"sshFallback": "decoy"}
        decoy_file = write_instance(self.temp, decoy, "decoy.json")
        board = self.board(board_file)
        public = self.board(None)
        self.assertEqual(json.loads(board["blob"])["remote"], DEMO["remote"])
        self.assertIn("m.INJECTED=", board["prelude"])
        self.assertTrue(board["prelude"].isascii(), "long stdin lines stay ASCII for Python 3.9")
        self.assertTrue(base64.b64decode(board["program"]).decode().startswith(
            "from __future__ import annotations\n" + board["prelude"]))
        # The remote interpreter (isolated, with its own instance file named) runs the prelude.
        remote = subprocess.run([sys.executable, "-I", "-c", REMOTE], input=board["prelude"].encode(),
                                capture_output=True, timeout=60,
                                env={"HOME": str(self.home), instance.ENVIRONMENT: str(decoy_file),
                                     "PATH": os.environ.get("PATH", "")})
        self.assertEqual(remote.returncode, 0, remote.stderr.decode())
        seen = json.loads(remote.stdout)
        self.assertEqual(seen["origin"], "injected")
        self.assertEqual(seen["remote"], DEMO["remote"])
        self.assertEqual((seen["socket"], seen["client"]), ("demo-board", "demo-board"))
        self.assertEqual(seen["hostConfig"], "/h/.config/demo-board/config.json")
        # The helper archive is reproducible, carries instance.py and the names, and its hash
        # follows the names.
        self.assertTrue(board["same"])
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(board["runtime"])))
        self.assertIn("instance.py", archive.namelist())
        self.assertEqual(archive.read("instance.json").decode(), board["blob"])
        self.assertEqual(hashlib.sha256(base64.b64decode(board["runtime"])).hexdigest(), board["sha"])
        self.assertNotEqual(board["sha"], public["sha"])
        self.assertEqual(json.loads(public["blob"])["remote"], dict(instance.defaults()["remote"]))
        helper = self.temp / ("worker-" + board["sha"] + ".pyz")
        helper.write_bytes(base64.b64decode(board["runtime"]))
        done = subprocess.run([sys.executable, "-I", "-c", ARCHIVE, str(ROOT), str(helper)], capture_output=True,
                              timeout=60, env={"HOME": str(self.home), instance.ENVIRONMENT: str(decoy_file),
                                               "PATH": os.environ.get("PATH", "")})
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        seen = json.loads(done.stdout)
        self.assertEqual(seen["origin"], "archive")
        self.assertEqual(seen["remote"], DEMO["remote"])
        self.assertEqual(seen["transfers"], str(self.home / ".local/state/demo-board-transfers"))

    def test_helper_archive_without_names_fails_closed(self):
        helper = self.temp / "worker.pyz"
        with zipfile.ZipFile(helper, "w") as archive:
            archive.writestr("instance.py", (ROOT / "instance.py").read_bytes())
        done = subprocess.run([sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);import instance",
                               str(helper)], capture_output=True, timeout=60, env={"HOME": str(self.home)})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("ValueError", done.stderr.decode())


WEB = ROOT / "web"


class RenderTest(unittest.TestCase):
    """index.html, manifest.webmanifest and sw.js carry the installation's names."""

    @classmethod
    def setUpClass(cls):
        cls.demo = instance.parse(copy.deepcopy(DEMO))
        cls.page = (WEB / "index.html").read_text(encoding="utf-8")
        cls.manifest = (WEB / "manifest.webmanifest").read_text(encoding="utf-8")
        cls.worker = (WEB / "sw.js").read_text(encoding="utf-8")

    @staticmethod
    def meta(page):
        content = re.search(r'<meta name="sessionholic-instance" content="([^"]*)" />', page).group(1)
        return json.loads(html.unescape(content))

    def test_public_names_leave_the_files_as_they_are_except_the_filled_names_and_cache(self):
        rendered = server.render_page(self.page)
        self.assertEqual(re.sub(r'(name="sessionholic-instance" content=")[^"]*', r"\1{}", rendered), self.page)
        self.assertEqual(self.meta(rendered), {
            "title": "세션홀릭 - 에이전트 세션 매니저", "shortName": "세션홀릭", "storagePrefix": "sessionholic:",
            "historyKey": "sessionholic", "attachmentsDir": ".sessionholic-attachments"})
        self.assertEqual(server.render_manifest(self.manifest), self.manifest)
        worker = server.render_service_worker(self.worker, "sessionholic-shell-0a1b2c3d")
        changed = [(a, b) for a, b in zip(self.worker.splitlines(), worker.splitlines()) if a != b]
        self.assertEqual(changed, [('const CACHE = "sessionholic-shell-v13";', 'const CACHE = "sessionholic-shell-0a1b2c3d";')])
        # The meta precedes theme-init.js so the stored look applies before the first paint.
        self.assertLess(rendered.index('name="sessionholic-instance"'), rendered.index('src="/theme-init.js"'))

    def test_installation_names_are_escaped_text(self):
        rendered = server.render_page(self.page, self.demo)
        self.assertIn("<title>데모 &lt;보드&gt;</title>", rendered)
        self.assertIn('<meta name="apple-mobile-web-app-title" content="데모 &amp; 보드" />', rendered)
        self.assertIn('↗</span> 데모 &amp; 보드</div>', rendered)
        self.assertIn('<span class="home-label">데모 &amp; 보드</span>', rendered)
        self.assertIn("<h1>들어오세요 &lt;지금&gt;</h1>", rendered)
        self.assertIn("<p>합성 안내 &amp; 문구</p>", rendered)
        self.assertIn("<p>\n            보드 기기의 <code>~/.config/demo-board/token</code>에\n"
                      "            있습니다 &lt;확인&gt;.\n          </p>", rendered)
        self.assertNotIn('name="application-name"', rendered)
        self.assertNotIn('name="description"', rendered)
        self.assertNotIn("세션홀릭", rendered)
        self.assertEqual(self.meta(rendered), {"title": "데모 <보드>", "shortName": "데모 & 보드", "storagePrefix": "db:",
                                               "historyKey": "demoBoard", "attachmentsDir": ".demo-board-attachments"})
        manifest = json.loads(server.render_manifest(self.manifest, self.demo))
        self.assertEqual((manifest["id"], manifest["name"], manifest["short_name"], manifest["description"]),
                         ("/", "데모 · 보드", "데모 & 보드", '합성 "설명"'))
        self.assertEqual(manifest["icons"], json.loads(self.manifest)["icons"])
        worker = server.render_service_worker(self.worker, "demo-board-shell-0a1b2c3d", self.demo)
        self.assertIn('const CACHE = "demo-board-shell-0a1b2c3d";', worker)
        self.assertIn('.filter((key) => key.startsWith("demo-board-") && key !== CACHE)', worker)
        self.assertIn("// A private PWA caches only public static assets.", worker)
        # Only the protocol meta name (sessionholic-assets) keeps the public name.
        self.assertEqual(re.findall(r"sessionholic-\w+", worker), ["sessionholic-assets"])

    def test_a_changed_name_without_its_place_in_the_file_stops_rendering(self):
        title_only = instance.parse({"version": 1, "brand": {"title": "다른 이름"}})
        self.assertEqual(server.render_page("<p>no title</p>"), "<p>no title</p>")
        with self.assertRaises(ValueError):
            server.render_page("<p>no title</p>", title_only)
        with self.assertRaises(ValueError):
            server.render_page(self.page.replace('    <meta name="sessionholic-instance" content="{}" />\n', ""),
                               self.demo)
        with self.assertRaises(ValueError):
            server.render_manifest('{"name": "x"}', self.demo)
        with self.assertRaises(ValueError):
            server.render_service_worker("self.addEventListener('fetch', () => {});\n", "demo-board-shell-00000000",
                                         self.demo)
        # The public names need no place: a custom page stays as it is.
        self.assertEqual(server.render_service_worker("// worker\n", "sessionholic-shell-00000000"), "// worker\n")

    def test_the_shipped_files_have_every_place(self):
        server.StaticAssets(WEB, names=self.demo).check()
        server.StaticAssets(WEB).check()


class StaticRenderTest(unittest.TestCase):
    """Served bytes, hashes and the automatic cache name follow the rendered files."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.web = Path(self.directory.name) / "web"
        self.web.mkdir()
        for name in ("index.html", "manifest.webmanifest", "sw.js"):
            (self.web / name).write_bytes((WEB / name).read_bytes())
        (self.web / "app.js").write_text("console.log('one');\n" * 100)
        self.demo = instance.parse(copy.deepcopy(DEMO))

    def tearDown(self):
        self.directory.cleanup()

    def test_cache_name_tracks_assets_and_rendered_hashes_are_served(self):
        assets = server.StaticAssets(self.web, names=self.demo)
        worker = assets.file("sw.js")
        sources = assets.sources()
        name = re.search(r'const CACHE = "([^"]+)";', worker["body"].decode()).group(1)
        self.assertRegex(name, r"^demo-board-shell-[0-9a-f]{8}$")
        self.assertEqual(name, server.shell_cache_name(sources, self.demo))
        self.assertEqual(worker["sha8"], hashlib.sha256(worker["body"]).hexdigest()[:8])
        self.assertEqual(worker["etag"], hashlib.sha256(worker["body"]).hexdigest()[:16])
        manifest = assets.manifest()
        rendered_manifest = assets.file("manifest.webmanifest")
        self.assertEqual(json.loads(rendered_manifest["body"])["id"], "/")
        self.assertEqual(manifest["/manifest.webmanifest"], rendered_manifest["sha8"])
        self.assertEqual(manifest["/sw.js"], worker["sha8"])
        self.assertNotEqual(manifest["/sw.js"], sources["/sw.js"])
        page = assets.index()["body"].decode()
        self.assertIn('href="/manifest.webmanifest?v=%s"' % rendered_manifest["sha8"], page)
        self.assertIn("<title>데모 &lt;보드&gt;</title>", page)
        # Any asset change gives the worker a new cache name, so older caches are dropped.
        app = self.web / "app.js"
        app.write_text("console.log('two');\n" * 100)
        stamp = app.stat().st_mtime_ns + 1_000_000_000
        os.utime(app, ns=(stamp, stamp))
        again = re.search(r'const CACHE = "([^"]+)";', assets.file("sw.js")["body"].decode()).group(1)
        self.assertRegex(again, r"^demo-board-shell-[0-9a-f]{8}$")
        self.assertNotEqual(again, name)
        public = server.StaticAssets(self.web).file("sw.js")["body"].decode()
        self.assertRegex(public, r'const CACHE = "sessionholic-shell-[0-9a-f]{8}";')
        self.assertIn('key.startsWith("sessionholic-")', public)


if __name__ == "__main__":
    unittest.main()
