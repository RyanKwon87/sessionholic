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
