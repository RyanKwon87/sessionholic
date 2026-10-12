"""Optional agent usage (remaining limits) from helper commands on the board host.

Enabled only by the host settings' `usage` section (`provider: "helper"`). Each helper is
a local program that prints one JSON object with `schema_version: 1`:

- Codex: `<python> <resources>/<codexHelper> <codex> --json --accounts <key>...` prints
  `accounts: [{key, status, windows: {five_hour, weekly: {remaining, resets_at}}, credits}]`.
- Claude: `<python> <resources>/<claudeHelper> <claude>` prints
  `{status, usage: {windows: {five_hour, seven_day: {used_percentage, resets_at}}}}`.

Helpers run with a fixed PATH, HOME, USER and LANG only (no keys or tokens), no stdin,
stderr discarded, a 45 second limit and 64KiB of output. Reads happen while a board is
open: a page read refreshes after `intervalSeconds`, a manual refresh after
`manualIntervalSeconds`. A failed read keeps the last good numbers, marked as stale.
"""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

SCHEMA_VERSION = 1
TIMEOUT = 45
OUTPUT_LIMIT = 65536
STATUS = {"login_required": "로그인 필요", "mismatch": "계정 불일치", "unavailable": "조회 실패",
          "auth_changed": "인증 변경됨", "skipped": "조회 보류", "not_supported": "구독 잔량 미제공"}


class UsageMeter:
    """Remaining limits from configured helpers, read only while a board is open."""

    def __init__(self, config, runner=None, clock=time.time, threaded=True, home=None):
        self.resources, self.python = Path(config["resources"]), Path(config["python"])
        self.path, self.home = config["path"], str(home if home is not None else Path.home())
        self.codex_helper, self.codex = config.get("codexHelper"), config.get("codex")
        self.claude_helper, self.claude = config.get("claudeHelper"), config.get("claude")
        self.accounts = tuple((row["key"], row["label"]) for row in config.get("codexAccounts", ()))
        self.interval, self.manual_interval = config["intervalSeconds"], config["manualIntervalSeconds"]
        self.runner = runner or self.run_helper
        self.clock, self.threaded = clock, threaded
        self.lock = threading.Lock()
        self.providers, self.fetched_at, self.error = [], None, None
        self.attempted, self.refreshing = 0.0, False

    def helpers(self):
        return [self.resources / name for name in (self.codex_helper, self.claude_helper) if name]

    def available(self):
        return all(path.is_file() for path in self.helpers() + [self.python])

    def snapshot(self, refresh=False, force=False):
        with self.lock:
            now = self.clock()
            due = now - self.attempted >= (self.manual_interval if force else self.interval)
            start = (refresh or force) and due and not self.refreshing and self.available()
            if start:
                self.refreshing, self.attempted = True, now
            result = self.public()
        if start:
            if self.threaded:
                threading.Thread(target=self.collect, daemon=True).start()
            else:
                self.collect()
                with self.lock:
                    result = self.public()
        return result

    def public(self):
        return {"available": self.available(), "refreshing": self.refreshing, "fetchedAt": self.fetched_at,
                "error": self.error, "providers": copy.deepcopy(self.providers)}

    def run_helper(self, argv):
        try:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            user = os.environ.get("USER", "")
        # A fixed PATH (the CLIs may be scripts) and no API keys or tokens.
        env = {"PATH": self.path, "HOME": self.home, "USER": user, "LANG": "en_US.UTF-8"}
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=TIMEOUT, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if len(done.stdout) > OUTPUT_LIMIT:
            return None
        try:
            value = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None
        return value if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION else None

    def collect(self):
        try:
            results = []
            if self.codex_helper:
                codex = self.runner([str(self.python), str(self.resources / self.codex_helper), str(self.codex),
                                     "--json", "--accounts"] + [key for key, _ in self.accounts])
                results.append(self.codex_provider(codex))
            if self.claude_helper:
                claude = self.runner([str(self.python), str(self.resources / self.claude_helper), str(self.claude)])
                results.append(self.claude_provider(claude))
            previous = {item["id"]: item for item in self.providers}
            providers = []
            for item in results:
                if item is None:
                    continue
                if item["status"] != "ready" and previous.get(item["id"], {}).get("status") in ("ready", "stale"):
                    # Keep the last good numbers, clearly marked as earlier ones.
                    kept = copy.deepcopy(previous[item["id"]])
                    kept["status"], kept["note"] = "stale", "이전 조회 값입니다. 최신 조회에 실패했습니다."
                    item = kept
                providers.append(item)
            with self.lock:
                self.providers = providers
                self.fetched_at = int(self.clock())
                self.error = None if providers else "사용량을 확인하지 못했습니다."
        except Exception:
            with self.lock:
                self.error = "사용량을 확인하지 못했습니다."
        finally:
            with self.lock:
                self.refreshing = False

    @staticmethod
    def remaining(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return max(0, min(100, int(round(value))))

    @staticmethod
    def reset(value):
        return value if isinstance(value, str) and len(value) <= 40 and re.fullmatch(r"[0-9T:.+\-Z]+", value) else None

    @staticmethod
    def credits(value):
        if not isinstance(value, dict):
            return None
        if value.get("unlimited") is True:
            return "무제한"
        try:
            return f"{int(round(float(value.get('balance')))):,}"
        except (TypeError, ValueError, OverflowError):
            return None

    def codex_provider(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            return {"id": "codex", "short": "CX", "label": "Codex", "remaining": None, "status": "error",
                    "note": "Codex 사용량을 확인하지 못했습니다.", "rows": []}
        accounts = {item.get("key"): item for item in data["accounts"] if isinstance(item, dict)}
        rows, summary, status = [], None, "error"
        # The summary follows the first configured account.
        for index, (key, name) in enumerate(self.accounts):
            account = accounts.get(key) or {}
            ready = account.get("status") == "ready"
            windows = account.get("windows") if ready and isinstance(account.get("windows"), dict) else {}
            entries = [{"label": label, "remaining": self.remaining((windows.get(field) or {}).get("remaining")),
                        "resetsAt": self.reset((windows.get(field) or {}).get("resets_at"))}
                       for field, label in (("five_hour", "5시간"), ("weekly", "주간"))]
            rows.append({"name": name, "status": "ready" if ready else "error",
                         "note": None if ready else STATUS.get(account.get("status"), "조회 실패"),
                         "credits": self.credits(account.get("credits")) if ready else None,
                         "windows": entries if ready else []})
            if index == 0 and ready:
                status = "ready"
                values = [entry["remaining"] for entry in entries if entry["remaining"] is not None]
                summary = min(values) if values else None
        return {"id": "codex", "short": "CX", "label": "Codex", "remaining": summary, "status": status,
                "note": None, "rows": rows}

    def claude_provider(self, data):
        base = {"id": "claude", "short": "CC", "label": "Claude", "rows": []}
        if not isinstance(data, dict):
            return {**base, "remaining": None, "status": "error", "note": "Claude 사용량을 확인하지 못했습니다."}
        if data.get("status") != "ready":
            return {**base, "remaining": None, "status": "error",
                    "note": STATUS.get(data.get("status"), "조회 실패")}
        windows = ((data.get("usage") or {}).get("windows")) or {}
        entries = []
        for field, label in (("five_hour", "5시간"), ("seven_day", "7일")):
            window = windows.get(field)
            used = self.remaining((window or {}).get("used_percentage"))
            entries.append({"label": label, "remaining": None if used is None else 100 - used,
                            "resetsAt": self.reset((window or {}).get("resets_at"))})
        values = [entry["remaining"] for entry in entries if entry["remaining"] is not None]
        return {**base, "remaining": min(values) if values else None, "status": "ready", "note": None,
                "rows": [{"name": "Claude Code", "status": "ready", "note": None, "credits": None, "windows": entries}]}
