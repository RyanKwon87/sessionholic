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
import launch  # noqa: E402


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sb-", dir="/tmp")
        self.home = Path(self.temp.name).resolve()
        self.cwd = self.home / "project with spaces; $(false)"
        self.cwd.mkdir()
        self.state = self.home / "state"
        self.binary = patch.object(launch, "_binary", side_effect=lambda agent, home: "/native/" + agent)
        self.binary.start()
        self.git = patch.object(launch, "_git_metadata", return_value={"available": False})
        self.git_mock = self.git.start()
        config = self.home / ".config/sessionholic/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"version": 1, "environments": {"isolated": {
            "label": "격리 환경", "variables": {"GH_CONFIG_DIR": "~/.config/fixture-git/gh", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_SSH_COMMAND": "ssh -o IdentityAgent=none"}}}, "profiles": [
            {"agent": "codex", "home": key, "environment": "isolated", "label": "Fixture"}
            for key in (".codex-isolated", ".codex-isolated-alpha", ".codex-isolated-beta")],
            "projects": [{"id": "isolated", "label": "Isolated", "root": str(self.home / "Isolated"), "environment": "isolated"}]}))
        self.sockets = []
        self.profile("codex", ".codex")
        self.profile("codex", ".codex-beta")
        self.profile("claude", ".claude")
        self.source = {"host": "local", "agent": "codex", "id": str(uuid.uuid4()),
                       "home": ".codex", "cwd": str(self.cwd), "phase": "idle"}
        self.messages = [{"role": "user", "text": "현재 작업을 이어서 검증해줘.", "ts": 1},
                         {"role": "assistant", "text": "변경 파일을 확인했습니다.", "ts": 2}]

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        self.git.stop()
        self.binary.stop()
        self.temp.cleanup()

    def profile(self, agent, key, auth=True):
        directory = self.home / key
        directory.mkdir(parents=True, exist_ok=True)
        if agent == "codex" and auth:
            (directory / "auth.json").write_text("fixture: do not read")
        return {"id": "claude:default" if agent == "claude" else "codex:" + key,
                "agent": agent, "home": key, "host": "local", "available": True}

    def daemon(self, key):
        path = self.home / key / launch.SOCKET_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        self.sockets.append(sock)
        return path

    def build(self, target, source=None, messages=None):
        return launch.build_launch(self.source if source is None else source, target,
                                   self.messages if messages is None else messages,
                                   self.state, home=self.home)

    def test_discovery_never_opens_auth_or_runs_native_cli(self):
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path.name, "auth.json")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded), patch.object(subprocess, "run") as run:
            profiles = launch.discover_profiles(self.home)
        run.assert_not_called()
        by_id = {row["id"]: row for row in profiles}
        self.assertTrue(by_id["codex:.codex"]["available"])
        self.assertNotIn("codex:.codex-missing", by_id)
        self.assertNotIn("codex:.codex-isolated-alpha", by_id)
        self.assertEqual(by_id["claude:default"]["label"], "기본 환경 · 기본 Claude")
        self.assertNotIn("fixture: do not read", json.dumps(profiles))

    def test_missing_saved_login_cannot_be_overridden_by_target_flag(self):
        target = self.profile("codex", ".codex-missing", auth=False)
        with self.assertRaisesRegex(ValueError, "로그인 파일"):
            self.build(target)

    def test_profile_scope_describes_environment_not_login_identity(self):
        for key in (".codex-isolated", ".codex-isolated-beta", ".codex-default",
                    ".codex-isolatedish", ".codex-router/0123456789abcdef"):
            self.profile("codex", key)
        profiles = {row["home"]: row for row in launch.discover_profiles(self.home)}
        self.assertEqual(profiles[".codex"]["label"], "기본 환경 · 기본 Codex")
        isolated = profiles[".codex-isolated-beta"]
        standard = profiles[".codex-beta"]
        self.assertEqual(isolated["scope"], "isolated")
        self.assertEqual(isolated["accountLabel"], "Fixture")
        self.assertEqual(standard["scope"], "default")
        self.assertNotEqual(standard["id"], isolated["id"])
        self.assertEqual(profiles[".codex-isolatedish"]["scope"], "default")
        self.assertEqual(profiles[".codex-router/0123456789abcdef"]["scope"], "default")
        for profile in profiles.values():
            self.assertIs(profile["identityVerified"], False)
            self.assertIn("확인하지", profile["identityReason"])

    def test_profile_scope_agrees_with_launch_environment_and_keeps_exact_home(self):
        environment = self.home / "Isolated/work"
        environment.mkdir(parents=True)
        self.profile("codex", ".codex-isolated-beta")
        profiles = {row["home"]: row for row in launch.discover_profiles(self.home)}
        target = dict(profiles[".codex-isolated-beta"], host="local")
        result = self.build(target, dict(self.source, cwd=str(environment)))
        self.assertEqual(target["scope"], "isolated")
        self.assertIn("GH_CONFIG_DIR", result["env"])
        self.assertEqual(result["env"]["CODEX_HOME"], str(self.home / target["home"]))
        standard = dict(profiles[".codex-beta"], host="local")
        # Metadata never authorizes a change across the original home boundary.
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build(dict(standard, scope="isolated"), dict(self.source, cwd=str(environment)))

    def test_profile_metadata_rejects_unrecognized_agent_and_unsafe_home(self):
        for agent, key in (("other", ".codex"), ("codex", "../.codex"),
                           ("claude", ".claude-other"), ("codex", None)):
            with self.subTest(agent=agent, key=key), self.assertRaises(ValueError):
                launch.profile_metadata(agent, key)

    def test_codex_attach_uses_exact_id_home_and_local_daemon(self):
        path = self.daemon(".codex")
        with patch.object(subprocess, "run") as run:
            result = self.build(self.profile("codex", ".codex"))
        run.assert_not_called()
        self.assertEqual(result["mode"], "attach")
        self.assertEqual(result["argv"], ["/native/codex", "resume", self.source["id"],
                                         "--remote", "unix://" + str(path), "--cd", str(self.cwd)])
        self.assertEqual(result["env"]["CODEX_HOME"], str(self.home / ".codex"))
        self.assertNotIn("--no-daemon", result["argv"])
        self.assertNotIn("--last", result["argv"])
        self.assertFalse(self.state.exists())

    def test_codex_native_private_socket_symlink_is_supported(self):
        path = self.home / ".codex" / launch.SOCKET_REL
        path.parent.mkdir(parents=True)
        private = self.home / "runtime"
        private.mkdir(mode=0o700)
        endpoint = private / "daemon.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(endpoint))
        self.sockets.append(sock)
        path.symlink_to(endpoint)
        result = self.build(self.profile("codex", ".codex"))
        self.assertIn("unix://" + str(path), result["argv"])
        private.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "socket"):
            self.build(self.profile("codex", ".codex"))

    def test_codex_attach_rejects_name_or_missing_daemon(self):
        target = self.profile("codex", ".codex")
        with self.assertRaisesRegex(ValueError, "socket"):
            self.build(target)
        self.daemon(".codex")
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.build(target, dict(self.source, id="--last"))

    def test_claude_background_attach_never_uses_resume_or_session_name(self):
        source = dict(self.source, agent="claude", home=".claude", kind="background",
                      id="a1b2c3d4", sessionId=str(uuid.uuid4()), title="ambiguous title")
        result = self.build(self.profile("claude", ".claude"), source)
        self.assertEqual(result["argv"], ["/native/claude", "attach", "a1b2c3d4"])
        with self.assertRaisesRegex(ValueError, "interactive"):
            self.build(self.profile("claude", ".claude"), dict(source, kind="interactive"))

    def test_account_switch_creates_private_handoff_and_new_native_session(self):
        result = self.build(self.profile("codex", ".codex-beta"))
        path = Path(result["handoffPath"])
        self.assertEqual(result["mode"], "handoff")
        self.assertEqual(result["argv"][:3], ["/native/codex", "--cd", str(self.cwd)])
        self.assertIn(str(path), result["argv"][3])
        self.assertNotIn("resume", result["argv"])
        self.assertEqual(result["env"]["CODEX_HOME"], str(self.home / ".codex-beta"))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text()
        self.assertIn(self.source["id"], text)
        self.assertIn("현재 작업을 이어서", text)
        self.assertIn("새 세션의 승인으로 승계하지", text)
        self.assertEqual(list(self.state.glob("*.tmp")), [])
        self.assertTrue((self.home / ".codex/auth.json").exists())

    def test_tool_switch_uses_one_literal_prompt_argument(self):
        result = self.build(self.profile("claude", ".claude"))
        self.assertEqual(len(result["argv"]), 2)
        self.assertEqual(result["argv"][0], "/native/claude")
        self.assertEqual(result["cwd"], str(self.cwd))
        self.assertNotIn("CODEX_HOME", result["env"])

    def test_inherited_credentials_and_session_selectors_are_removed(self):
        inherited = {"OPENAI_API_KEY": "fixture", "CODEX_ACCESS_TOKEN": "fixture",
                     "CODEX_THREAD_ID": "other", "ANTHROPIC_API_KEY": "fixture",
                     "CLAUDE_CODE_OAUTH_TOKEN": "fixture", "CLAUDE_CONFIG_DIR": "/other",
                     "CLAUDECODE": "1", "BASH_ENV": "/unexpected", "FIXTURE_LAUNCH_ID": "old"}
        with patch.dict(os.environ, inherited):
            result = self.build(self.profile("claude", ".claude"))
        self.assertTrue(set(inherited).isdisjoint(result["env"]))

    def test_environment_to_default_environment_is_blocked_and_environment_target_isolated(self):
        environment = self.home / "Isolated/work"
        environment.mkdir(parents=True)
        source = dict(self.source, home=".codex-isolated-alpha", cwd=str(environment))
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build(self.profile("codex", ".codex-beta"), source)
        result = self.build(self.profile("codex", ".codex-isolated-beta"), source)
        self.assertEqual(result["env"]["GH_CONFIG_DIR"], str(self.home / ".config/fixture-git/gh"))
        self.assertEqual(result["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertIn("IdentityAgent=none", result["env"]["GIT_SSH_COMMAND"])

    def test_existing_legacy_environment_session_can_attach_its_exact_home(self):
        environment = self.home / "Isolated/work"
        environment.mkdir(parents=True)
        self.daemon(".codex")
        result = self.build(self.profile("codex", ".codex"), dict(self.source, cwd=str(environment)))
        self.assertEqual(result["mode"], "attach")

    def test_cross_host_and_missing_destination_never_infer_or_copy(self):
        for target in (dict(self.profile("codex", ".codex-beta"), host="remote"),
                       {key: value for key, value in self.profile("codex", ".codex-beta").items() if key != "host"}):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "호스트"):
                self.build(target)
        self.git_mock.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_invalid_home_profile_pair_and_cwd_are_rejected(self):
        for target in (dict(self.profile("codex", ".codex"), home="../other"),
                       dict(self.profile("codex", ".codex"), id="codex:.codex-beta")):
            with self.assertRaises(ValueError):
                self.build(target)
        for cwd in ("relative", str(self.home / "missing"), "/tmp\nother"):
            with self.assertRaises(ValueError):
                self.build(self.profile("claude", ".claude"), dict(self.source, cwd=cwd))

    def test_active_or_unknown_source_cannot_handoff(self):
        for phase in ("working", "needs_input", "unknown", None):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                self.build(self.profile("claude", ".claude"), dict(self.source, phase=phase))
        self.assertFalse(self.state.exists())

    def test_empty_handoff_does_not_start_an_unrelated_session(self):
        for messages in ([], [{"role": "user", "text": ""}], [{"role": "user", "text": 5}]):
            with self.assertRaisesRegex(ValueError, "대화 기록"):
                self.build(self.profile("claude", ".claude"), messages=messages)

    def test_handoff_has_message_and_utf8_size_limits(self):
        messages = [{"role": "user", "text": "row-%03d " % index + "한" * 5000} for index in range(100)]
        result = self.build(self.profile("claude", ".claude"), messages=messages)
        path = Path(result["handoffPath"])
        self.assertLessEqual(path.stat().st_size, launch.MAX_HANDOFF_BYTES)
        text = path.read_text()
        data = json.loads(text[text.index("{\n"):])
        self.assertLessEqual(len(data["messages"]), 80)
        self.assertTrue(data["truncated"])
        self.assertGreater(data["omittedMessages"], 0)
        self.assertIn("row-099", data["messages"][-1]["text"])
        self.assertNotIn("row-000", text)

    def test_default_environment_profile_is_a_environment_boundary(self):
        source = dict(self.source, home=".codex-isolated")
        with self.assertRaisesRegex(ValueError, "환경"):
            self.build(self.profile("codex", ".codex-beta"), source)
        result = self.build(self.profile("codex", ".codex-isolated-beta"), source)
        self.assertIn("GH_CONFIG_DIR", result["env"])

    def test_handoff_scrubs_email_tokens_and_private_keys(self):
        secrets = ["person@example.invalid", "sk-proj-" + "a" * 48,
                   "sk-ant-api03-" + "b" * 48, "ghp_" + "c" * 40,
                   "github_pat_" + "d" * 50, "AKIA" + "A" * 16,
                   "Bearer " + "e" * 30, "aws_secret_access_key=" + "f" * 40,
                   "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-body\n-----END OPENSSH PRIVATE KEY-----"]
        messages = [{"role": "user", "text": "확인할 원문: " + "\n".join(secrets)}]
        result = self.build(self.profile("claude", ".claude"), messages=messages)
        text = Path(result["handoffPath"]).read_text()
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)
        self.assertIn('"redacted": true', text)
        self.assertIn('"redactedFields": 1', text)

    def test_state_symlink_is_rejected(self):
        other = self.home / "other"
        other.mkdir()
        self.state.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "심볼릭"):
            self.build(self.profile("claude", ".claude"))
        self.assertEqual(list(other.iterdir()), [])

    def test_git_collects_metadata_and_stat_without_patch_contents(self):
        self.git.stop()
        env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")

        def git(*args):
            subprocess.run(["git", *args], cwd=str(self.cwd), env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        git("init")
        changed = self.cwd / "work.txt"
        changed.write_text("initial\n")
        git("add", "work.txt")
        git("commit", "-m", "fixture")
        changed.write_text("PRIVATE_FILE_BODY_NOT_FOR_HANDOFF\n")
        result = self.build(self.profile("claude", ".claude"))
        text = Path(result["handoffPath"]).read_text()
        self.assertIn('"available": true', text)
        self.assertIn("work.txt", text)
        self.assertIn("diffStat", text)
        self.assertNotIn("PRIVATE_FILE_BODY_NOT_FOR_HANDOFF", text)


if __name__ == "__main__":
    unittest.main()
