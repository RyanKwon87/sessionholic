#!/usr/bin/env python3
"""Finite public-tree checks. Reports locations and categories, never matched values.

This guard supplements review; it cannot prove that arbitrary text contains no
private information. Release archives must be built from the reviewed Git commit.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", "venv", ".DS_Store"}
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\r?\n"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "provider-token": re.compile(r"\bsk-(?:proj-|ant-api\d{2}-|ant-oat\d{2}-)[A-Za-z0-9_-]{32,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{24,}\b"),
}
HOME_PATH = re.compile(r"/Users/([A-Za-z0-9._-]+)/")
EXAMPLE_USERS = {"example", "test", "user", "u", "sample", "demo", "runner", "local", "source", "other"}
TAILNET_URL = re.compile(r"https?://[a-zA-Z0-9.-]+\.ts\.net(?=[:/\s\"'`]|$)")


def checked_files(root):
    """Git decides what is public after initialization; exports use the allowlist."""
    if (root / ".git").exists():
        result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                 "--exclude-standard", "-z"], capture_output=True, check=True)
        return sorted(set(result.stdout.decode().split("\0")) - {""})
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and not any(part in SKIP for part in path.relative_to(root).parts)
                  and path.suffix != ".pyc")


def audit(root=ROOT, release=False):
    manifest = root / "scripts/public-files.json"
    allowed = set(json.loads(manifest.read_text()))
    files = checked_files(root)
    findings = []
    for name in files:
        path = root / name
        if name not in allowed:
            findings.append({"file": name, "category": "not-in-public-allowlist"})
            continue
        if path.is_symlink():
            findings.append({"file": name, "category": "symlink"})
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            findings.append({"file": name, "category": "unexpected-large-file"})
            continue
        data = path.read_bytes()
        if path.suffix in (".png", ".jpg", ".jpeg"):
            if not (data.startswith(b"\x89PNG\r\n\x1a\n") if path.suffix == ".png" else data.startswith(b"\xff\xd8\xff")):
                findings.append({"file": name, "category": "invalid-image"})
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append({"file": name, "category": "unexpected-binary"})
            continue
        for category, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                findings.append({"file": name, "category": category})
        if any(match.group(1) not in EXAMPLE_USERS for match in HOME_PATH.finditer(content)):
            findings.append({"file": name, "category": "non-example-home-path"})
        if any("example" not in match.group(0) for match in TAILNET_URL.finditer(content)):
            findings.append({"file": name, "category": "non-example-tailnet-url"})
    for name in sorted(allowed - set(files)):
        findings.append({"file": name, "category": "missing-public-file"})
    if release and not (root / "LICENSE").is_file():
        findings.append({"file": "LICENSE", "category": "owner-license-selection-required"})
    return {"checkedFiles": len(files), "findings": findings,
            "releaseLicensePresent": (root / "LICENSE").is_file()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="공개 파일 허용 목록·흔한 비밀값·개인 경로 검사")
    parser.add_argument("--release", action="store_true", help="공개 라이선스 파일도 필수 확인")
    args = parser.parse_args(argv)
    try:
        result = audit(release=args.release)
    except (ValueError, OSError, subprocess.SubprocessError):
        print("공개 파일 목록을 읽지 못했습니다. Git 상태와 허용 목록을 확인하세요.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
