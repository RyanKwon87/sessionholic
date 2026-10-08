import importlib.util
import contextlib
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_cli", ROOT / "scripts/sessionholic.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_init_empty_home_and_preserves_existing_settings(self):
        self.assertEqual(set(cli.initialize(self.home)), {"config.json", "hosts.json"})
        config = self.home / ".config/sessionholic/config.json"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        config.write_text('{"version":1,"projects":[]}\n')
        self.assertEqual(cli.initialize(self.home), [])
        self.assertEqual(config.read_text(), '{"version":1,"projects":[]}\n')

    def test_init_rejects_symlink_without_writing_target(self):
        external = self.home / "external"
        external.mkdir()
        (self.home / ".config").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            cli.initialize(self.home)
        self.assertEqual(list(external.iterdir()), [])

    def test_service_file_is_inert_private_and_never_overwrites(self):
        cli.initialize(self.home)
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli, "diagnose", return_value=[]):
            output = cli.service_file(home=self.home)
            value = plistlib.loads(output.read_bytes())
            self.assertEqual(value["Label"], cli.LABEL)
            self.assertIn(str(self.home / ".config/sessionholic/hosts.json"), value["ProgramArguments"])
            self.assertEqual(value["Umask"], 0o077)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                cli.service_file(home=self.home)

    def test_doctor_validates_empty_home_without_invoking_tools_or_credentials(self):
        cli.initialize(self.home)
        with patch.object(cli.shutil, "which", return_value="/example/bin/tool"):
            checks = cli.diagnose(self.home)
        self.assertFalse(any(row["status"] == "error" for row in checks))
        config = self.home / ".config/sessionholic/config.json"
        config.write_text('{"version":1,"unexpectedSecret":"do-not-print-this"}')
        checks = cli.diagnose(self.home)
        self.assertEqual(next(row["status"] for row in checks if row["name"] == "config"), "error")
        self.assertNotIn("do-not-print-this", json.dumps(checks))

    def test_service_rejects_invalid_identity_before_writing_plist(self):
        cli.initialize(self.home)
        identity = self.home / "tailscale-user"
        identity.write_text("")
        identity.chmod(0o600)
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli, "diagnose", return_value=[]):
            with self.assertRaises(ValueError):
                cli.service_file(home=self.home, tailscale_user_file=identity)
            identity.write_text("example@example.com")
            identity.chmod(0o644)
            with self.assertRaises(ValueError):
                cli.service_file(home=self.home, tailscale_user_file=identity)
        self.assertFalse((self.home / "Library/LaunchAgents" / (cli.LABEL + ".plist")).exists())

    def test_invalid_port_explains_the_error_and_serve_forwards_options(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(["service-file", "--port", "0"]), 1)
        self.assertIn("포트", output.getvalue())
        import server
        with patch.object(server, "main", return_value=0) as main:
            self.assertEqual(cli.main(["serve", "--", "--port", "9001"]), 0)
        main.assert_called_once_with(["--port", "9001"])


if __name__ == "__main__":
    unittest.main()
