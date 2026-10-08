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

    def test_doctor_detects_startup_permissions_and_links_without_mutation(self):
        cli.initialize(self.home)
        config = self.home / ".config/sessionholic"
        config.chmod(0o755)
        token = config / "token"
        token.write_text("private-token-content-must-not-be-read")
        token.chmod(0o644)
        state = self.home / ".local/state/sessionholic"
        state.parent.mkdir(parents=True)
        destination = self.home / "outside"
        destination.mkdir()
        state.symlink_to(destination, target_is_directory=True)
        with patch.object(cli.shutil, "which", return_value="/fixture/tool"), \
                patch("server.load_token") as token_reader:
            checks = cli.diagnose(self.home)
        errors = {row["name"] for row in checks if row["status"] == "error"}
        self.assertTrue({"config-dir", "token-file", "state-dir"}.issubset(errors))
        token_reader.assert_not_called()
        self.assertNotIn(token.read_text(), json.dumps(checks))
        self.assertEqual(config.stat().st_mode & 0o777, 0o755)
        self.assertEqual(token.stat().st_mode & 0o777, 0o644)
        self.assertTrue(state.is_symlink())
        self.assertEqual(list(destination.iterdir()), [])

    def test_doctor_empty_home_creates_nothing_and_empty_token_is_reported(self):
        with patch.object(cli.shutil, "which", return_value="/fixture/tool"):
            checks = cli.diagnose(self.home)
        self.assertFalse(any(row["status"] == "error" for row in checks))
        self.assertEqual(list(self.home.iterdir()), [])
        cli.initialize(self.home)
        token = self.home / ".config/sessionholic/token"
        token.touch(mode=0o600)
        with patch.object(cli.shutil, "which", return_value="/fixture/tool"):
            checks = cli.diagnose(self.home)
        self.assertEqual(next(row["status"] for row in checks if row["name"] == "token-file"), "error")
        self.assertEqual(token.read_bytes(), b"")

    def test_service_file_refuses_invalid_startup_paths_before_writing_plist(self):
        cli.initialize(self.home)
        (self.home / ".config/sessionholic").chmod(0o755)
        with patch.object(cli.sys, "platform", "darwin"), \
                patch.object(cli.shutil, "which", return_value="/fixture/tool"):
            with self.assertRaisesRegex(ValueError, "doctor"):
                cli.service_file(home=self.home)
        self.assertFalse((self.home / "Library/LaunchAgents" / (cli.LABEL + ".plist")).exists())

    def test_doctor_checks_directory_access_and_allows_read_only_config_with_token(self):
        cli.initialize(self.home)
        config = self.home / ".config/sessionholic"
        def access(path, mode):
            return not (Path(path) == config and mode & cli.os.W_OK)
        with patch.object(cli.shutil, "which", return_value="/fixture/tool"), \
                patch.object(cli.os, "access", side_effect=access):
            checks = cli.diagnose(self.home)
            self.assertEqual(next(row["status"] for row in checks if row["name"] == "config-dir"), "error")
            token = config / "token"
            token.write_text("fixture-token")
            token.chmod(0o400)
            checks = cli.diagnose(self.home)
            self.assertFalse(any(row["status"] == "error" for row in checks))
        self.assertEqual(token.stat().st_mode & 0o777, 0o400)

    def test_invalid_port_explains_the_error_and_serve_forwards_options(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(["service-file", "--port", "0"]), 1)
        self.assertIn("포트", output.getvalue())
        import server
        with patch.object(server, "main", return_value=0) as main:
            self.assertEqual(cli.main(["serve", "--", "--port", "9001"]), 0)
        main.assert_called_once_with(["--port", "9001"])

    def test_doctor_checks_input_receipts_without_reading_or_changing_them(self):
        folder = self.home / '.local/state/sessionholic/terminal-inputs'
        folder.mkdir(parents=True, mode=0o700)
        folder.parent.chmod(0o700)
        receipt = folder / 'receipts.sqlite3'
        receipt.write_bytes(b'fixture-private-receipt')
        receipt.chmod(0o644)
        with patch.object(cli.shutil, 'which', return_value='/fixture/tool'):
            checks = cli.diagnose(self.home)
        self.assertEqual(next(r['status'] for r in checks if r['name'] == 'terminal-input-file'), 'error')
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o644)
        self.assertEqual(receipt.read_bytes(), b'fixture-private-receipt')
        self.assertNotIn('fixture-private-receipt', json.dumps(checks))

    def test_cleanup_transfer_targets_only_requested_job_and_reports_pending(self):
        import server
        from transfer import Transfers
        with patch.object(server, 'load_hosts', return_value=[]), \
                patch.object(Transfers, 'cleanup_pending', create=True, return_value={'archiveCleanupPending': True}) as cleanup, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(['cleanup-transfer', 'a'*32, '--state-dir', str(self.home)])
        self.assertEqual(result, 1)
        cleanup.assert_called_once_with('a'*32)
        self.assertTrue(json.loads(output.getvalue())['archiveCleanupPending'])


if __name__ == "__main__":
    unittest.main()
