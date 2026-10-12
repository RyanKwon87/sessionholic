import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("public_check", Path(__file__).resolve().parents[1] / "scripts/check_public.py")
public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public)


class PublicTreeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "scripts").mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def allow(self, *names):
        (self.root / "scripts/public-files.json").write_text(json.dumps(["scripts/public-files.json", *names]))

    def test_unreviewed_files_and_missing_files_are_reported(self):
        self.allow("README.md", "LICENSE")
        (self.root / "README.md").write_text("Example")
        (self.root / "operating-data.json").write_text("{}")
        result = public.audit(self.root)
        self.assertEqual({item["category"] for item in result["findings"]},
                         {"not-in-public-allowlist", "missing-public-file"})

    def test_suspicious_value_is_never_returned_in_report(self):
        self.allow("README.md")
        token = "ghp_" + "SYNTHETIC" * 5
        (self.root / "README.md").write_text(token)
        result = public.audit(self.root)
        self.assertEqual(result["findings"], [{"file": "README.md", "category": "github-token"}])
        self.assertNotIn(token, json.dumps(result))

    def test_keys_in_strings_windows_homes_and_bare_tailnet_names_are_reported(self):
        names = ["key.json", "fixture.py", "win.md", "win-json.json", "examples.md", "tailnet.py"]
        self.allow(*names)
        body = "A" * 64
        (self.root / "key.json").write_text(json.dumps({"key": "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"}))
        (self.root / "fixture.py").write_text('KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\\nfixture-body\\n"')
        # Joined at run time, so this test file is no finding itself.
        person = "some" + "one"
        (self.root / "win.md").write_text("C:\\Users\\" + person + "\\Projects")
        (self.root / "win-json.json").write_text(json.dumps({"cwd": "C:\\Users\\" + person + "\\x"}))
        (self.root / "examples.md").write_text("C:\\Users\\example\\x C:/Users/fixture/y remote.tail-example.ts.net *.ts.net")
        (self.root / "tailnet.py").write_text('HOST = "board.' + "tail0a1b2c" + '.ts.net"')
        found = {(item["file"], item["category"]) for item in public.audit(self.root)["findings"]}
        self.assertEqual(found, {("key.json", "escaped-private-key"), ("win.md", "non-example-windows-home-path"),
                                 ("win-json.json", "non-example-windows-home-path"), ("tailnet.py", "non-example-tailnet-name")})

    def test_links_rejected_before_content_and_license_is_explicit(self):
        self.allow("README.md")
        outside = self.root / "private-value.txt"
        outside.write_text("do not read")
        (self.root / "README.md").symlink_to(outside)
        result = public.audit(self.root, release=True)
        self.assertIn({"file": "README.md", "category": "symlink"}, result["findings"])
        self.assertIn({"file": "LICENSE", "category": "owner-license-selection-required"}, result["findings"])


if __name__ == "__main__":
    unittest.main()
