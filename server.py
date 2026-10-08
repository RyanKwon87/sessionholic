#!/usr/bin/env python3
"""Session board server: poll each host's collector and serve a token-protected page.

Binds to 127.0.0.1 only. Remote access goes through Tailscale Serve, never a public funnel.
Standard library only and Python 3.9 compatible so it can later run on POSIX host as is.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import shutil
import hmac
import ipaddress
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse, unquote
import settings

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
COLLECTOR = HERE / "collector.py"
DEFAULT_HOSTS = Path.home() / ".config/sessionholic/hosts.json"
STATE_DIR = Path.home() / ".local/state/sessionholic"
TOKEN_FILE = Path.home() / ".config/sessionholic/token"
COOKIE = "sessionholic_token"
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
PYTHON_PATH = re.compile(r"^/[A-Za-z0-9_./-]{1,120}$")
SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
HOME_KEY = re.compile(r"^\.codex(-[A-Za-z0-9_-]{1,40})?$|^\.codex-router/[0-9a-f]{16}$")
STATIC = re.compile(r"^(?:vendor/)?[a-z0-9_-]{1,40}\.(html|js|css|svg|png|webmanifest)$")
REMOTE_PATH = "/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".webmanifest": "application/manifest+json"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                               "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def load_hosts(path):
    hosts = []
    path = settings.safe_path(path, allow_missing=True)
    if not Path(path).exists() and Path(path) == DEFAULT_HOSTS:
        return [{"name": "local", "label": "이 기기", "local": True, "python": "/usr/bin/python3", "ssh": None}]
    if Path(path).is_symlink() or Path(path).stat().st_size > 65536:
        raise ValueError("기기 설정 파일이 안전하지 않거나 너무 큽니다.")
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("기기 설정은 1~16개 항목의 JSON 목록이어야 합니다.")
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {"name", "label", "local", "ssh", "python"}
                or type(row.get("local", False)) is not bool
                or not isinstance(row.get("name"), str)
                or not isinstance(row.get("python") or "/usr/bin/python3", str)
                or not isinstance(row.get("label", row["name"]), str)
                or len(row.get("label", row["name"]).encode()) > 256):
            raise ValueError("기기 설정 형식이 올바르지 않습니다.")
        name, python = row.get("name", ""), row.get("python") or "/usr/bin/python3"
        if not NAME.fullmatch(name):
            raise ValueError("기기 이름이 올바르지 않습니다.")
        if not row.get("local") and (not isinstance(row.get("ssh"), str) or not NAME.fullmatch(row["ssh"])):
            raise ValueError("SSH 별칭이 올바르지 않습니다.")
        if not PYTHON_PATH.fullmatch(python):
            raise ValueError("Python 실행 경로가 올바르지 않습니다.")
        hosts.append({"name": name, "label": row.get("label") or name, "local": bool(row.get("local")),
                      "ssh": row.get("ssh"), "python": python})
    if len({h["name"] for h in hosts}) != len(hosts):
        raise ValueError("기기 이름이 중복됩니다.")
    if sum(bool(h["local"]) for h in hosts) > 1:
        raise ValueError("로컬 기기는 한 항목만 등록할 수 있습니다.")
    return hosts


def collector_command(host, args):
    """Local hosts run the file; SSH hosts read it from stdin so nothing is left on the remote."""
    if host["local"]:
        return [sys.executable, "-I", str(COLLECTOR)] + list(args)
    remote = (f"PATH={REMOTE_PATH} {shlex.quote(host['python'])} -I - "
              + " ".join(shlex.quote(a) for a in args))
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5",
            "--", host["ssh"], remote]


def run_collector(host, args, timeout):
    stdin = None
    if not host["local"]:
        # Remote stdin remains a single self-contained program, with settings
        # imported from this reviewed bundle rather than a remote Python path.
        module = (HERE / "settings.py").read_text(encoding="utf-8")
        prefix = "import types,sys\nm=types.ModuleType('settings')\nexec(" + repr(module) + ",m.__dict__)\nsys.modules['settings']=m\n"
        stdin = (b"from __future__ import annotations\n" + prefix.encode()
                 + COLLECTOR.read_bytes().replace(b"from __future__ import annotations\n", b"", 1))
    try:
        done = subprocess.run(collector_command(host, args), input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("응답 시간 초과") from None
    except OSError:
        raise RuntimeError("수집기를 실행하지 못했습니다") from None
    if done.returncode != 0:
        raise RuntimeError("SSH 연결 실패" if done.returncode == 255 else f"수집기 오류 (exit {done.returncode})")
    try:
        result = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError("수집기 응답 형식 오류") from None
    if not isinstance(result, dict):
        raise RuntimeError("수집기 응답 형식 오류")
    return result


def scrub_payload(value):
    from launch import scrub_text
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_text(item) if key in ("text", "title", "snippet", "account", "error") and isinstance(item, str)
                else scrub_payload(item) for key, item in value.items()}
    return value


class Board:
    """Visible-client driven collection. A failed host stays paused until explicit retry."""

    def __init__(self, hosts, interval, runner=run_collector, state_dir=None):
        self.hosts, self.interval, self.runner = hosts, max(interval, 60), runner
        self.lock = threading.RLock()
        self.collect_lock = threading.Lock()
        self.state = {h["name"]: {"name": h["name"], "label": h["label"], "ok": False,
                     "error": "아직 수집하지 않았어요", "data": None, "fetchedAt": None,
                     "durationMs": None, "paused": False, "refreshing": False} for h in hosts}
        self.reads, self.attempted = {}, {}
        self.state_dir = Path(state_dir) if state_dir else None
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                cached = json.loads((self.state_dir / "snapshot.json").read_text())
                for row in cached.get("hosts", []):
                    if row["name"] in self.state and row.get("data"):
                        self.state[row["name"]].update(data=row["data"], fetchedAt=row.get("fetchedAt"),
                            error="저장된 목록 · 연결 확인 전")
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                saved_reads = json.loads((self.state_dir / "conversations.json").read_text())
                for entry in saved_reads[-128:]:
                    self.reads[tuple(entry["key"])] = (entry["at"], entry["data"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def start(self):
        # Kept for old callers; no background polling without a visible client.
        return None

    def poll(self, host):
        started = time.time()
        with self.collect_lock:
            with self.lock:
                self.state[host["name"]]["refreshing"] = True
                self.attempted[host["name"]] = started
            try:
                data = scrub_payload(self.runner(host, ["snapshot"], 45))
                update = {"ok": True, "paused": False, "error": None, "data": data,
                          "fetchedAt": int(time.time())}
            except RuntimeError as exc:
                update = {"ok": False, "paused": True, "error": str(exc)}
            update.update(durationMs=int((time.time() - started) * 1000), refreshing=False)
            with self.lock:
                self.state[host["name"]].update(update)
                if self.state_dir:
                    private_json(self.state_dir / "snapshot.json", self.snapshot())
            return update["ok"]

    def request_refresh(self, host_name=None, force=False):
        if host_name is not None and host_name not in self.state:
            raise ValueError("등록되지 않은 기기입니다.")
        queued = []
        with self.lock:
            now = time.time()
            for host in self.hosts:
                row = self.state[host["name"]]
                if host_name and host["name"] != host_name:
                    continue
                delay = 5 if force else self.interval
                if row["refreshing"] or now - self.attempted.get(host["name"], 0) < delay:
                    continue
                if row["paused"] and not force:
                    continue
                row["refreshing"] = True
                queued.append(host)
        def collect():
            for host in queued:
                self.poll(host)
        if queued:
            threading.Thread(target=collect, daemon=True).start()
        return bool(queued)

    def snapshot(self):
        with self.lock:
            return {"serverTime": int(time.time()), "interval": self.interval,
                    "hosts": copy.deepcopy(list(self.state.values()))}

    def source(self, ref):
        if not isinstance(ref, dict):
            raise ValueError("작업을 선택해 주세요.")
        host = self.state.get(ref.get("host"))
        if not host:
            raise ValueError("등록되지 않은 기기입니다.")
        agent = ref.get("agent")
        if agent not in ("claude", "codex"):
            raise ValueError("지원하지 않는 에이전트입니다.")
        with self.lock:
            for row in (host.get("data") or {}).get(agent, []):
                if row.get("id") == ref.get("id") and (agent != "codex" or row.get("home") == ref.get("home")):
                    return {**copy.deepcopy(row), "host": host["name"], "hostLabel": host["label"],
                            "online": host["ok"], "fetchedAt": host["fetchedAt"]}
        raise ValueError("목록에서 작업을 다시 선택해 주세요.")

    def read(self, host_name, agent, session_id, home, fresh=False):
        host = next((h for h in self.hosts if h["name"] == host_name), None)
        if host is None or agent not in ("claude", "codex") or not SESSION_ID.fullmatch(session_id or ""):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        if agent == "codex" and not HOME_KEY.fullmatch(home or ""):
            raise ValueError("Codex home 형식이 올바르지 않습니다.")
        key = (host_name, agent, session_id, home)
        with self.lock:
            cached = self.reads.get(key)
            paused = self.state[host_name]["paused"]
            if cached and not fresh and (paused or time.time() - cached[0] < 60):
                return {**cached[1], "fetchedAt": int(cached[0]), "stale": not self.state[host_name]["ok"]}
            if paused and not fresh:
                raise RuntimeError("기기 연결을 다시 확인한 뒤 대화를 읽어 주세요.")
        args = ["read", agent, session_id] + ([home] if agent == "codex" else [])
        result = scrub_payload(self.runner(host, args, 30))
        result["messages"] = result.get("messages", [])[-80:]
        fetched = time.time()
        with self.lock:
            if len(self.reads) >= 128 and key not in self.reads:
                self.reads.pop(next(iter(self.reads)))
            if not result.get("error"):
                self.reads[key] = (fetched, result)
                if self.state_dir:
                    private_json(self.state_dir / "conversations.json", [
                        {"key": list(k), "at": v[0], "data": v[1]} for k, v in self.reads.items()])
        return {**result, "fetchedAt": int(fetched), "stale": False}


def private_json(path, value):
    path = Path(path)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("상태 경로는 심볼릭 링크를 사용할 수 없습니다.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise ValueError("상태 경로 소유자를 확인해 주세요.")
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + "." + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class LaunchFailure(RuntimeError):
    def __init__(self, recovery):
        super().__init__("대상 실행 준비 뒤 터미널 연결을 완료하지 못했습니다. 대상 작업이 이미 시작됐을 수 있습니다.")
        self.recovery = recovery


class Workflow:
    """Only server-discovered identities become native terminal launch commands."""
    def __init__(self, board, terminals, state_dir=STATE_DIR):
        self.board, self.terminals, self.state_dir = board, terminals, Path(state_dir)
        self.plans, self.requests = {}, {}
        self.lock = threading.RLock()
        from transfer import Transfers
        self.transfers = Transfers(board.hosts, state_dir)

    def profiles(self):
        from launch import discover_profiles
        return discover_profiles()

    def capabilities(self):
        from launch import profile_metadata
        profiles = self.profiles()
        hosts = []
        for host in self.board.hosts:
            status = self.board.state[host["name"]]
            accounts = profiles if host["local"] else []
            if not host["local"] and status["ok"]:
                try:
                    accounts = self.transfers.profiles(host)
                except (RuntimeError, OSError, ValueError):
                    # Existing same-account attach stays available if transfer setup is unavailable.
                    accounts = []
            if not host["local"]:
                for agent in ("claude", "codex"):
                    for row in (status.get("data") or {}).get(agent, []):
                        key = "claude:default" if agent == "claude" else "codex:" + row["home"]
                        if not any(p["id"] == key for p in accounts):
                            home = row.get("home", ".claude")
                            try:
                                presentation = profile_metadata(agent, home)
                            except ValueError:
                                presentation = {"label": row.get("account") or "현재 계정 설정"}
                            accounts.append({**presentation, "id": key, "agent": agent,
                                             "home": home, "available": True})
            hosts.append({"name": host["name"], "label": host["label"], "online": status["ok"],
                "agents": [{"id": agent, "label": "Claude Code" if agent == "claude" else "Codex",
                    "accounts": [{k: p.get(k) for k in ("id", "label", "available", "reason", "home",
                        "scope", "scopeLabel", "scopeDescription", "accountKey", "accountLabel",
                        "identityVerified", "identityReason", "environment")} for p in accounts if p["agent"] == agent]}
                    for agent in ("claude", "codex")]})
        return {"hosts": hosts, "terminal": {"available": bool(shutil.which("tmux", path=collector_path())),
                "reason": "터미널 연결에는 tmux가 필요합니다."}}

    @staticmethod
    def identity(row):
        return {k: row.get(k) for k in ("host", "agent", "id", "home")}

    def plan(self, payload):
        source = self.board.source(payload.get("source"))
        target = payload.get("target")
        if not isinstance(target, dict) or target.get("agent") not in ("claude", "codex"):
            raise ValueError("이어갈 에이전트와 계정을 선택해 주세요.")
        target = {key: target.get(key) for key in ("host", "agent", "account")}
        if any(not isinstance(value, str) for value in target.values()):
            raise ValueError("기기·에이전트·계정을 선택해 주세요.")
        host = next((h for h in self.board.hosts if h["name"] == target.get("host")), None)
        if host is None:
            raise ValueError("등록되지 않은 기기입니다.")
        current = "claude:default" if source["agent"] == "claude" else "codex:" + source["home"]
        same = source["host"] == target["host"] and source["agent"] == target["agent"] and current == target.get("account")
        cross_host = source["host"] != target["host"]
        source_host = next(h for h in self.board.hosts if h["name"] == source["host"])
        allowed, reason, profile = True, None, None
        warnings, transfer_preview = [], None
        if cross_host:
            try:
                choices = self.profiles() if host["local"] else self.transfers.profiles(host, refresh=True)
                profile = next((p for p in choices if p["id"] == target["account"] and p["agent"] == target["agent"]), None)
                if not profile or not profile.get("available"):
                    allowed, reason = False, (profile or {}).get("reason") or "대상 기기의 기존 계정을 확인해 주세요."
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc)
            if not self.board.state[host["name"]]["ok"]:
                allowed, reason = False, "대상 기기의 연결을 확인한 뒤 다시 시도해 주세요."
        elif host["local"]:
            profile = next((p for p in self.profiles() if p["id"] == target.get("account") and p["agent"] == target["agent"]), None)
            if not profile or not profile.get("available"):
                allowed, reason = False, (profile or {}).get("reason") or "이 계정의 저장된 로그인을 확인해 주세요."
        elif same:
            profile = {"id": current, "agent": source["agent"], "home": source.get("home", ".claude"), "label": source.get("account") or "현재 계정", "available": True}
        else:
            allowed, reason = False, "원격 기기의 계정 전환은 해당 기기에 실행 연결을 설치한 뒤 사용할 수 있어요."
        if not source["online"]:
            allowed, reason = False, "원본 기기의 연결을 확인한 뒤 다시 시도해 주세요."
        if not same and not cross_host and source.get("phase") in ("working", "needs_input"):
            allowed, reason = False, "진행 중인 실행이나 승인 요청을 원래 터미널에서 마무리한 뒤 전환해 주세요."
        if same and source["agent"] == "claude" and source.get("kind") != "background":
            allowed, reason = False, "이 Claude 세션은 기존 터미널에서 실행 중입니다. 백그라운드 세션만 직접 연결할 수 있어요."
        source_environment = None
        if source_host["local"]:
            from launch import source_environment as resolve_environment
            source_environment = resolve_environment(source)
        if not same and not cross_host and profile and source_environment != profile.get("environment", "default"):
            allowed, reason = False, "원본과 대상의 실행 환경 경계가 다릅니다."
        if not shutil.which("tmux", path=collector_path()):
            allowed, reason = False, "이 기기에 tmux를 설치한 뒤 터미널을 열 수 있어요."
        mode = "transfer" if cross_host else ("attach" if same else "handoff")
        summary = ("코드·미커밋 변경·최근 대화를 대상 기기로 넘겨 이어갑니다." if cross_host else
                   "기존 작업의 터미널에 연결합니다." if same else "작업 맥락을 자동으로 넘겨 새 터미널을 준비합니다.")
        route_key = hashlib.sha256(json.dumps([self.identity(source), target], sort_keys=True).encode()).hexdigest()
        existing = next((item for item in self.terminals.list() if item.get("alive") and item.get("key", "").split(":")[0] == route_key), None)
        if allowed and existing:
            mode, summary = "reconnect", "이미 열어 둔 터미널에 다시 연결합니다."
            if not same:
                warnings.append("이전 인계 뒤 원본에 추가된 대화는 다시 전달하지 않습니다. 새 요청은 연결한 터미널에서 입력해 주세요.")
        elif cross_host and allowed:
            try:
                transfer_preview = self.transfers.preview(source, target, source_host, host)
                source_environment = transfer_preview.get("workspace", {}).get("sourceEnvironment")
                if not isinstance(source_environment, str) or source_environment != profile.get("environment", "default"):
                    allowed, reason = False, "원본과 대상의 실행 환경 경계가 다릅니다."
                warnings.extend(transfer_preview.get("workspace", {}).get("warnings", []))
                warnings.append("진행 중인 응답을 중단한 뒤 이전합니다. 승인 대기와 프로세스 자체는 새 기기로 이동하지 않습니다.")
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc)
        elif not same:
            warnings.append("최근 대화와 파일 변경 상태를 넘겨 새 실행을 준비합니다. 기존 프로세스·승인 상태는 이어지지 않습니다.")
        plan = {"id": secrets.token_hex(16), "mode": mode, "allowed": allowed,
                "reason": reason, "summary": summary,
                "warnings": warnings, "source": self.identity(source), "target": target,
                "expiresAt": int(time.time()) + 180}
        if transfer_preview is not None:
            plan["transfer"] = transfer_preview
        with self.lock:
            self.plans = {k: v for k, v in self.plans.items() if v[0]["expiresAt"] > time.time()}
            if len(self.plans) >= 64:
                self.plans.pop(next(iter(self.plans)))
            self.plans[plan["id"]] = (plan, source, profile)
        return plan

    def launch(self, plan_id, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ValueError("실행 요청 식별자가 필요합니다.")
        with self.lock:
            if request_id in self.requests:
                previous_plan, result = self.requests[request_id]
                if previous_plan != plan_id:
                    raise ValueError("이미 사용한 요청입니다.")
                if isinstance(result, LaunchFailure):
                    raise result
                return result
            saved = self.plans.get(plan_id)
            if not saved or saved[0]["expiresAt"] <= time.time():
                raise ValueError("선택이 만료됐어요. 실행 설정을 다시 확인해 주세요.")
            plan, source, profile = saved
            if not plan["allowed"]:
                raise ValueError(plan["reason"])
            key_base = hashlib.sha256(json.dumps([plan["source"], plan["target"]], sort_keys=True).encode()).hexdigest()
            previous = [item for item in self.terminals.list() if item.get("key", "").split(":")[0] == key_base]
            live = next((item for item in reversed(previous) if item.get("alive")), None)
            if live:
                result = {"terminal": live, "mode": plan["mode"], "reused": True}
                self.requests[request_id] = (plan_id, result)
                return result
            if plan["mode"] == "reconnect":
                raise RuntimeError("열어 둔 터미널이 종료됐어요. 실행 설정을 다시 확인해 주세요.")
            # A new explicit launch after a completed CLI is separate from reconnect.
            key = key_base + (":" + plan_id if previous else "")
            with self.terminals.reserve(key):
                # Re-validate cached identity, and refresh original status at the transition boundary.
                host = next(h for h in self.board.hosts if h["name"] == source["host"])
                if not self.board.poll(host):
                    raise RuntimeError("원본 기기에 연결할 수 없어 실행하지 않았어요.")
                fresh = self.board.source(plan["source"])
                if plan["mode"] == "handoff" and fresh.get("phase") in ("working", "needs_input"):
                    raise ValueError("원래 작업이 실행 중이거나 승인을 기다리고 있어요. 마무리한 뒤 다시 전환해 주세요.")
                source = fresh
                if plan["mode"] == "transfer":
                    target_host = next(h for h in self.board.hosts if h["name"] == plan["target"]["host"])
                    def read_for_transfer():
                        result = self.board.read(source["host"], source["agent"], source.get("sessionId") or source["id"], source.get("home"), fresh=True)
                        if result.get("error"):
                            raise RuntimeError("중단 후 최근 대화를 읽지 못해 이전을 중단했습니다.")
                        return result.get("messages") or []
                    spec = self.transfers.execute(source, plan["target"], host, target_host, profile, request_id, read_for_transfer)
                elif host["local"]:
                    from launch import build_launch
                    messages = []
                    if plan["mode"] == "handoff":
                        result = self.board.read(source["host"], source["agent"], source.get("sessionId") or source["id"], source.get("home"), fresh=True)
                        if result.get("error"):
                            raise RuntimeError("원본 대화를 읽지 못해 인계를 중단했어요.")
                        messages = result.get("messages") or []
                    spec = build_launch(source, {**profile, "host": source["host"]}, messages, self.state_dir)
                    from managed_launch import bind
                    spec = bind(spec, profile, source["host"], receipt_path=self.state_dir / "native-launches" / (key_base + ".json"))
                else:
                    spec = self.remote_attach(host, source)
                metadata = {"title": source.get("title") or "작업", "host": plan["target"]["host"], "agent": profile["agent"],
                            "account": profile["label"], "sourceKey": json.dumps(plan["source"], sort_keys=True), "mode": plan["mode"]}
                if spec.get("connectionMode"):
                    metadata.update(mode=spec["connectionMode"], launchMode=plan["mode"])
                native_source = spec.get("nativeSource")
                if plan["mode"] == "attach":
                    native_source = {**self.identity(source), "cwd": source.get("cwd"), "kind": source.get("kind")}
                if native_source:
                    metadata["nativeSource"] = native_source
                if spec.get("transferId"):
                    metadata.update(transferId=spec["transferId"], destinationCwd=spec["destinationCwd"], sourceHost=source["host"])
                try:
                    terminal = self.terminals.create(key, spec["argv"], spec["cwd"], spec["env"], metadata)
                    if spec.get("nativeReceiptPath"):
                        from managed_launch import mark_attached
                        mark_attached(spec, terminal["id"])
                    if spec.get("transferId"):
                        self.transfers.mark_started(spec["transferId"], terminal["id"])
                except (RuntimeError, ValueError, OSError) as exc:
                    # Preparation can start a native turn before the TUI connects.
                    # Retain an explicit recovery result instead of rerunning it.
                    recovery = {"targetStarted": None, "host": plan["target"]["host"],
                                "destinationCwd": spec.get("destinationCwd") or spec["cwd"]}
                    if native_source:
                        recovery["nativeSource"] = native_source
                    try:
                        opened = next((t for t in self.terminals.list() if t.get("key") == key), None)
                    except (RuntimeError, OSError):
                        opened = None
                    if opened:
                        recovery["terminal"] = opened
                    failure = LaunchFailure(recovery)
                    self.requests[request_id] = (plan_id, failure)
                    raise failure from exc
                result = {"terminal": terminal, "mode": plan["mode"]}
                self.requests[request_id] = (plan_id, result)
                if len(self.requests) > 256:
                    self.requests.pop(next(iter(self.requests)))
                return result

    def remote_attach(self, host, source):
        # The owning helper resolves binaries and explicit environment settings.
        # No configuration values or credentials cross back into an SSH command.
        profiles = self.transfers.profiles(host, refresh=True)
        identity = "claude:default" if source["agent"] == "claude" else "codex:" + source["home"]
        profile = next((p for p in profiles if p.get("id") == identity and p.get("available")), None)
        if profile is None:
            raise ValueError("원격 기기의 원본 실행 프로필을 확인하지 못했습니다.")
        selector = {key: source.get(key) for key in ("host", "agent", "id", "home", "cwd", "kind", "sessionId", "phase")}
        import collector
        return {"argv": self.transfers.command(host, ["-I", self.transfers.runtime_paths[host["name"]],
                "attach", json.dumps(selector, ensure_ascii=False)], tty=True),
                "cwd": str(Path.home()), "env": collector.tool_env()}


def collector_path():
    return os.pathsep.join(["/opt/homebrew/bin", str(Path.home() / ".local/bin"), "/usr/local/bin", os.environ.get("PATH", "")])


def load_token(path=TOKEN_FILE):
    path = settings.safe_path(path, private=True, allow_missing=True)
    settings.safe_path(path.parent, directory=True, private=True, allow_missing=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as incoming:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("접속 토큰의 소유자와 비공개 권한을 확인해 주세요.")
        value = incoming.read(513).strip()
    if not value or len(value) > 512 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("접속 토큰 파일 형식이 올바르지 않습니다.")
    return value


def load_tailscale_user(path):
    """Read one private, owner-controlled Serve login; never log its contents."""
    path = settings.safe_path(path, private=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Tailscale 사용자 파일은 심볼릭 링크를 사용할 수 없습니다.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
                    or not stat.S_ISREG(metadata.st_mode)):
                raise ValueError("Tailscale 사용자 파일은 현재 사용자 소유의 비공개 일반 파일이어야 합니다.")
            raw = stream.read(4097)
            value = raw.strip()
    except (OSError, UnicodeError):
        raise ValueError("Tailscale 사용자 파일을 읽지 못했습니다.") from None
    if not value or len(raw) > 4096 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Tailscale 사용자 파일에는 허용할 로그인 하나를 적어 주세요.")
    return value


def host_allowed(value):
    """Reject DNS-rebinding style Host headers; only loopback and tailnet names are served."""
    value = value or ""
    host = value.split("]")[0] + "]" if value.startswith("[") else value.rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "[::1]") or host.endswith(".ts.net")


class Handler(BaseHTTPRequestHandler):
    server_version = "Sessionholic/1"

    # ---------- plumbing ----------
    def log_message(self, fmt, *args):
        # Log the path only; query strings may carry a one-time login code.
        sys.stderr.write("%s %s %s\n" % (self.log_date_time_string(), self.command, urlparse(self.path).path))

    def send_body(self, code, body, content_type, extra=None):
        self.send_response(code)
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, data, extra=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_body(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

    def tailscale_authorized(self):
        """Trust identity headers only from the opted-in loopback HTTPS Serve route.

        The local Serve process and other processes of this OS user are trusted.
        Serve replaces client-supplied Tailscale identity headers before proxying.
        """
        expected = self.server.tailscale_user
        if expected is None:
            return False
        for name in ("Host", "X-Forwarded-Proto", "Tailscale-User-Login"):
            if len(self.headers.get_all(name, [])) != 1:
                return False
        try:
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
            hostname = urlparse("//" + self.headers["Host"]).hostname or ""
        except ValueError:
            return False
        return (local and hostname.endswith(".ts.net")
                and self.headers["X-Forwarded-Proto"] == "https"
                and hmac.compare_digest(self.headers["Tailscale-User-Login"].encode(), expected.encode()))

    def authorized(self):
        if self.tailscale_authorized():
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return False
        name = self.server.cookie_name
        given = jar[name].value if name in jar else ""
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:].encode(), self.server.token.encode())
        with self.server.auth_lock:
            return self.server.sessions.get(given, 0) > time.time()

    def session_cookie(self, clear=False):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        value = "" if clear else secrets.token_urlsafe(32)
        if not clear:
            with self.server.auth_lock:
                now = time.time()
                self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
                if len(self.server.sessions) >= 64:
                    self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[value] = now + 86400
        return f"{self.server.cookie_name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if clear else 86400}{secure}"

    def origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site")
        parsed = urlparse(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("JSON 요청만 사용할 수 있습니다.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("요청 크기가 올바르지 않습니다.") from None
        if not 0 < length <= 65536:
            raise ValueError("요청이 비었거나 너무 큽니다.")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ValueError("요청 형식이 올바르지 않습니다.") from None
        if not isinstance(data, dict):
            raise ValueError("객체 형식의 요청이 필요합니다.")
        return data

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not host_allowed(self.headers.get("Host")):
            return self.send_body(421, b"misdirected", "text/plain; charset=utf-8")
        url = urlparse(self.path)
        if url.path == "/login":
            return self.login_once(parse_qs(url.query).get("once", [""])[0])
        if url.path.startswith("/api/"):
            if not self.authorized():
                return self.send_json(401, {"error": "로그인이 필요합니다."})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/api/snapshot":
                    if query.get("refresh") == "1":
                        self.server.board.request_refresh()
                    return self.send_json(200, self.server.board.snapshot())
                if url.path == "/api/read":
                    result = self.server.board.read(query.get("host"), query.get("agent"), query.get("id"), query.get("home"))
                    return self.send_json(200, result)
                if url.path == "/api/capabilities":
                    result = self.server.workflow.capabilities()
                    return self.send_json(200, {**result, "csrfToken": self.server.csrf,
                                               "authMode": "tailscale" if self.tailscale_authorized() else "token"})
                if url.path == "/api/terminals":
                    records = self.server.terminals.list()
                    result = {"terminals": records}
                    try:
                        self.server.terminal_inputs.prune_all_closed(records)
                    except (ValueError, RuntimeError, OSError):
                        result["inputCleanupPending"] = True
                    return self.send_json(200, result)
                match = re.fullmatch(r"/api/terminal/([a-f0-9]{32})/events", url.path)
                if match:
                    after = int(query.get("after", "0"))
                    wait = min(max(float(query.get("wait", "20")), 0), 20)
                    if after < 0:
                        raise ValueError("출력 위치가 올바르지 않습니다.")
                    result = self.server.terminals.read(match[1], after, wait, epoch=query.get("epoch"))
                    return self.send_json(200, result)
            except (ValueError, KeyError) as exc:
                return self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "터미널을 찾지 못했어요."})
            except RuntimeError as exc:
                return self.send_json(409, {"error": str(exc)})
            except (OSError, ImportError):
                return self.send_json(503, {"error": "실행 연결을 준비하지 못했어요. 설정을 확인해 주세요."})
            return self.send_json(404, {"error": "없는 경로입니다."})
        return self.static(url.path)

    def do_POST(self):
        from chat import SendRejected
        path = urlparse(self.path).path
        not_started = {"dispatchState": "not_started"} if path == "/api/chat/send" else {}
        if not host_allowed(self.headers.get("Host")):
            if not_started:
                return self.send_json(421, {"error": "접속 주소를 확인해 주세요.", **not_started})
            return self.send_body(421, b"misdirected", "text/plain; charset=utf-8")
        if not self.origin_allowed():
            return self.send_json(403, {"error": "이 보드에서 요청해 주세요.", **not_started})
        if path == "/api/chat/upload":
            if not self.authorized():
                return self.send_json(401, {"error": "로그인이 필요합니다."})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
                return self.send_json(403, {"error": "화면을 새로 열고 다시 시도해 주세요."})
            try:
                from chat import MAX_FILE
                length = int(self.headers.get("Content-Length") or 0)
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_FILE:
                    raise ValueError("첨부 파일은 20MiB까지 보낼 수 있습니다.")
                if self.headers.get_content_type() != "application/octet-stream":
                    raise ValueError("파일 전송 형식이 올바르지 않습니다.")
                raw_source, raw_name = self.headers.get("X-Chat-Source", ""), self.headers.get("X-File-Name", "")
                if len(raw_source) > 4096 or len(raw_name) > 2048:
                    raise ValueError("파일 정보가 너무 깁니다.")
                source = json.loads(unquote(raw_source))
                # Validate the destination before reading an upload body.
                self.server.chat.source(source)
                self.connection.settimeout(30)
                if not self.server.upload_gate.acquire(blocking=False):
                    raise RuntimeError("다른 파일을 전송 중입니다.")
                try:
                    content = self.rfile.read(length)
                    if len(content) != length:
                        raise ValueError("파일을 끝까지 받지 못했습니다.")
                    return self.send_json(200, self.server.chat.upload(source, unquote(raw_name), content))
                finally:
                    self.server.upload_gate.release()
            except (ValueError, KeyError):
                return self.send_json(400, {"error": "파일 또는 대상 정보가 올바르지 않습니다. 20MiB 이하 파일을 다시 선택해 주세요."})
            except (RuntimeError, OSError):
                return self.send_json(409, {"error": "파일 전송을 확인하지 못했습니다. 기기 연결을 확인해 주세요."})
        try:
            data = self.json_body()
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc), **not_started})
        if path == "/login":
            token = str(data.get("token") or "")
            if not token or not hmac.compare_digest(token.encode(), self.server.token.encode()):
                time.sleep(0.5)
                return self.send_json(403, {"error": "토큰이 맞지 않습니다."})
            return self.send_json(200, {"ok": True, "csrfToken": self.server.csrf}, {"Set-Cookie": self.session_cookie()})
        if not self.authorized():
            return self.send_json(401, {"error": "로그인이 필요합니다.", **not_started})
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            return self.send_json(403, {"error": "보안 연결을 갱신한 뒤 다시 시도해 주세요.",
                                        "code": "csrf_expired", **not_started})
        try:
            if path == "/logout":
                jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
                if self.server.cookie_name in jar:
                    with self.server.auth_lock:
                        self.server.sessions.pop(jar[self.server.cookie_name].value, None)
                return self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie(clear=True)})
            if path == "/api/refresh":
                self.server.board.request_refresh(data.get("host"), force=True)
                return self.send_json(200, self.server.board.snapshot())
            if path == "/api/chat/state":
                return self.send_json(200, self.server.chat.state(data))
            if path == "/api/chat/send":
                return self.send_json(200, self.server.chat.send(data))
            if path == "/api/plan":
                return self.send_json(200, self.server.workflow.plan(data))
            if path == "/api/launch":
                return self.send_json(200, self.server.workflow.launch(data.get("planId"), data.get("requestId")))
            match = re.fullmatch(r"/api/terminal/([a-f0-9]{32})/(input|resize|detach|close)", path)
            if match:
                tid, action = match.groups()
                if action == "input":
                    request_id = data.get("requestId")
                    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
                        raise ValueError("입력 요청 식별자가 필요합니다.")
                    text = data.get("text")
                    if not isinstance(text, str) or len(text.encode()) > 32768:
                        raise ValueError("입력이 비었거나 너무 깁니다.")
                    self.server.terminal_inputs.submit(tid, request_id, text, self.server.terminals.write)
                elif action == "resize":
                    self.server.terminals.resize(tid, data.get("cols"), data.get("rows"))
                elif action == "close":
                    self.server.terminals.close_terminal(tid)
                    try:
                        self.server.terminal_inputs.prune_closed(tid, self.server.terminals.list())
                    except (ValueError, RuntimeError, OSError):
                        # Closing the CLI succeeded even if optional receipt cleanup failed.
                        return self.send_json(200, {"ok": True, "inputCleanupPending": True})
                else:
                    self.server.terminals.detach(tid)
                return self.send_json(200, {"ok": True})
        except SendRejected as exc:
            return self.send_json(exc.status, {"error": str(exc), "dispatchState": "not_started"})
        except LaunchFailure as exc:
            return self.send_json(409, {"error": str(exc), "recovery": exc.recovery})
        except (ValueError, KeyError) as exc:
            return self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "터미널을 찾지 못했어요."})
        except RuntimeError as exc:
            return self.send_json(409, {"error": str(exc)})
        except (OSError, ImportError):
            return self.send_json(503, {"error": "실행 연결을 준비하지 못했어요. 원래 작업은 유지됩니다."})
        return self.send_json(404, {"error": "없는 경로입니다."})

    def login_once(self, code):
        """One-time code from `--open`; the long-lived token itself never appears in a URL."""
        with self.server.once_lock:
            expected, expires = self.server.once
            valid = bool(code) and bool(expected) and time.time() < expires and hmac.compare_digest(code, expected)
            if valid:
                self.server.once = ("", 0)
        if not valid:
            return self.send_body(403, "로그인 링크가 만료됐습니다. 토큰으로 로그인하세요.".encode(), "text/plain; charset=utf-8")
        self.send_response(303)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Set-Cookie", self.session_cookie())
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def static(self, path):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = WEB / name
        if not STATIC.match(name) or not target.is_file():
            return self.send_body(404, b"not found", "text/plain; charset=utf-8")
        extra = {"Cache-Control": "no-cache"}
        if name == "sw.js":
            extra["Service-Worker-Allowed"] = "/"
        self.send_body(200, target.read_bytes(), TYPES[target.suffix], extra)


class BoardHTTPServer(ThreadingHTTPServer):
    def server_close(self):
        manager = getattr(self, "terminals", None)
        if manager is not None and hasattr(manager, "close"):
            manager.close()
        super().server_close()


def make_server(bind, port, board, token, terminals=None, workflow=None, state_dir=STATE_DIR, max_terminals=4,
                tailscale_user=None):
    if bind not in ("127.0.0.1", "::1"):
        raise ValueError("서버는 loopback에서만 실행할 수 있습니다.")
    if tailscale_user is not None and (not isinstance(tailscale_user, str) or not tailscale_user
            or len(tailscale_user) > 4096 or any(char.isspace() or ord(char) < 32 for char in tailscale_user)):
        raise ValueError("허용할 Tailscale 로그인 하나가 필요합니다.")
    from terminal import TerminalManager
    server = BoardHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.board, server.token, server.tailscale_user = board, token, tailscale_user
    server.cookie_name = COOKIE + "_" + str(server.server_address[1])
    server.once, server.once_lock = ("", 0), threading.Lock()
    server.sessions, server.auth_lock = {}, threading.Lock()
    server.csrf = secrets.token_urlsafe(32)
    server.upload_gate = threading.BoundedSemaphore(2)
    try:
        server.terminals = terminals if terminals is not None else TerminalManager(state_dir=Path(state_dir) / "terminals", max_terminals=max_terminals)
        from terminal_input import TerminalInputs
        server.terminal_inputs = TerminalInputs(state_dir)
    except BaseException:
        server.server_close()
        raise
    server.workflow = workflow if workflow is not None else Workflow(board, server.terminals, state_dir)
    from chat import Chat
    server.chat = Chat(board, server.workflow, state_dir)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="세션홀릭 - 에이전트 세션 매니저")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--hosts", default=str(DEFAULT_HOSTS))
    parser.add_argument("--interval", type=int, default=60, help="보이는 화면의 기기별 최소 수집 간격(초)")
    parser.add_argument("--max-terminals", type=int, choices=range(1, 5), default=4, help="동시에 유지할 보드 터미널 수(1~4)")
    parser.add_argument("--local-only", action="store_true", help="이 기기만 수집·검증")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--tailscale-user-file", type=Path,
                        help="비공개 파일에 적힌 Tailscale 사용자에게 HTTPS Serve 자동 인증 허용")
    parser.add_argument("--open", action="store_true", help="개인 브라우저에서 주소를 직접 여세요")
    args = parser.parse_args(argv)
    if args.bind != "127.0.0.1":
        parser.error("127.0.0.1만 허용합니다. 원격 접속은 Tailscale Serve를 사용하세요.")
    try:
        settings.load()
        settings.safe_path(TOKEN_FILE.parent, directory=True, private=True, allow_missing=True)
        args.state_dir = settings.safe_path(args.state_dir, directory=True, private=True, allow_missing=True)
        for filename in ("snapshot.json", "conversations.json"):
            settings.safe_path(args.state_dir / filename, private=True, allow_missing=True)
        tailscale_user = load_tailscale_user(args.tailscale_user_file) if args.tailscale_user_file else None
        hosts = load_hosts(args.hosts)
        token = load_token()
    except ValueError as exc:
        parser.error(str(exc))
    if args.local_only:
        hosts = [h for h in hosts if h["local"]]
    board = Board(hosts, max(args.interval, 60), state_dir=args.state_dir)
    httpd = make_server(args.bind, args.port, board, token, state_dir=args.state_dir,
                        max_terminals=args.max_terminals, tailscale_user=tailscale_user)
    print(f"세션홀릭: http://127.0.0.1:{args.port}/ · 화면을 열 때만 수집합니다.", file=sys.stderr)
    if args.open:
        print("개인 Chrome 프로필에서 위 주소를 여세요.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
