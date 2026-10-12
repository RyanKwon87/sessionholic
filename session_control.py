"""Exact native session controls, including sessions started outside this board.

Preview signs a short-lived target revision. Execute resolves the native target
again and records a pending receipt *before* the one permitted mutation. A lost
reply is never grounds for sending another signal or interrupt. Transcripts,
credentials and arbitrary client-supplied PIDs are not used for identification.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import time

import collector
import instance
import launch
import settings
import transfer_native as native

REQUEST = re.compile(r"[A-Za-z0-9_-]{8,100}\Z")
PROOF_TTL = 45
MAX_REGISTRY = 256
UNAVAILABLE = "원본 실행 상태를 확인하지 못했습니다. 연결을 복구한 뒤 다시 확인해 주세요."


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _private_read(path, limit=65536, private=False):
    """Open only an owned, unlinked, bounded ordinary file; never follow links."""
    path = Path(path)
    if path.parent.resolve() != path.parent.absolute():
        raise ValueError("세션 제어 저장 경로가 안전하지 않습니다.")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022) or info.st_size > limit):
            raise ValueError("세션 제어 파일이 안전하지 않습니다.")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("세션 제어 파일 크기가 올바르지 않습니다.")
    return value


def _save(path, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ps(args, deadline):
    result = subprocess.run(["/bin/ps"] + args, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=min(1.5, native._remaining(deadline)), check=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"})
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""


def _darwin_info(pid):
    """Kernel start time includes microseconds, so PID reuse does not match."""
    class BsdInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "rfu")]
        _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")]
        _fields_ += [("nice", ctypes.c_int32), ("startSec", ctypes.c_uint64), ("startUsec", ctypes.c_uint64)]
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    row = BsdInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(row), ctypes.sizeof(row)) != ctypes.sizeof(row):
        return None
    executable = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, executable, len(executable)) <= 0 and row.status != 5:
        return None
    return {"pid": pid, "uid": row.uid, "ppid": row.ppid,
            "start": str(row.startSec) + "." + str(row.startUsec),
            "exe": os.fsdecode(executable.value), "zombie": row.status == 5}


def _process_info(pid, deadline, start_label=True):
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        return None
    try:
        if sys.platform == "darwin":
            row = _darwin_info(pid)
        elif sys.platform.startswith("linux"):
            root = Path("/proc") / str(pid)
            data = (root / "stat").read_text().rsplit(")", 1)[1].split()
            row = {"pid": pid, "uid": root.stat().st_uid, "ppid": int(data[1]),
                   "start": data[19], "exe": str((root / "exe").resolve(strict=True)), "zombie": data[0] == "Z"}
        else:
            return None
        if not row or row["uid"] != os.getuid() or row["zombie"]:
            return None
        row["procStart"] = _ps(["-o", "lstart=", "-p", str(pid)], deadline).strip() if start_label else ""
        return row if row["procStart"] or not start_label else None
    except (OSError, ValueError, IndexError):
        return None


def _process_args(pid):
    """Only argv and the two home selectors survive this local kernel read.

    The environment is never returned, persisted or logged. Reading the kernel
    avoids `ps e` output containing unrelated credentials.
    """
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
        size = ctypes.c_size_t(1024 * 1024)
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buffer.raw[:size.value]
        if len(raw) < 4:
            return None
        argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        if not 1 <= argc <= 4096:
            return None
        offset = raw.find(b"\0", 4)
        if offset < 0:
            return None
        offset += 1
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        pieces = raw[offset:].split(b"\0")
        args, environment = pieces[:argc], pieces[argc:]
    elif sys.platform.startswith("linux"):
        root = Path("/proc") / str(pid)
        args = (root / "cmdline").read_bytes().split(b"\0")
        environment = (root / "environ").read_bytes().split(b"\0")
    else:
        return None
    homes = {}
    for entry in environment:
        key, separator, value = entry.partition(b"=")
        if separator and key in (b"HOME", b"CODEX_HOME"):
            homes[os.fsdecode(key)] = os.fsdecode(value)
    return [os.fsdecode(value) for value in args if value], homes


def _agent_executable(row, agent, home):
    binary = launch._binary(agent, home)
    if not binary or not row:
        return False
    actual, expected = Path(row["exe"]), Path(binary).resolve()
    if actual == expected:
        return True
    # Official npm Codex launcher invokes a bundled native binary. Node itself
    # and arbitrary wrapper scripts are deliberately not signal targets.
    return (agent == "codex" and actual.name == "codex" and "@openai" in actual.parts
            and ("codex" in actual.parts or any(part.startswith("codex-") for part in actual.parts)))


def _codex_resume_argument(args, sid):
    # Explicit positional UUID only; --last, a title, a fork and remote endpoints
    # cannot prove which local native thread the process currently owns.
    if any(arg in ("--last", "--remote", "fork") or arg.startswith("--remote=") for arg in args):
        return False
    return any(args[index:index + 2] == ["resume", sid] for index in range(1, len(args) - 1))


def _codex_process(identity, home, deadline):
    rows = _ps(["-axo", "pid=,uid=,comm="], deadline).splitlines()
    matches = []
    examined = 0
    for line in rows:
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or parts[1] != str(os.getuid()):
            continue
        if Path(parts[2]).name != "codex":
            continue
        examined += 1
        if examined > MAX_REGISTRY:
            return None
        row = _process_info(int(parts[0]), deadline)
        if not _agent_executable(row, "codex", home):
            continue
        try:
            binding = _process_args(row["pid"])
        except (OSError, ValueError):
            continue
        if binding is None:
            continue
        args, homes = binding
        configured = homes.get("CODEX_HOME") or str(Path(homes.get("HOME") or "") / ".codex")
        if (not Path(configured).is_absolute() or Path(configured).absolute() != home / identity["home"]
                or not _codex_resume_argument(args, identity["id"])):
            continue
        matches.append({**row, "binding": "codex-resume-argv"})
    return matches[0] if len(matches) == 1 else None


def _claude_process(row, home, deadline):
    sid = row.get("sessionId")
    if not isinstance(sid, str) or not collector.SESSION_ID.fullmatch(sid):
        return None
    folder = home / ".claude/sessions"
    try:
        info = folder.stat()
        if (folder.resolve() != folder.absolute() or info.st_uid != os.getuid() or info.st_mode & 0o022):
            return None
        candidates = list(folder.glob("[0-9]*.json"))
        if len(candidates) > MAX_REGISTRY:
            return None
    except OSError:
        return None
    matches = []
    for path in candidates:
        if not path.stem.isdigit() or str(int(path.stem)) != path.stem:
            continue
        try:
            record = json.loads(_private_read(path))
            if (not isinstance(record, dict) or record.get("sessionId") != sid
                    or record.get("kind") != "interactive" or record.get("cwd") != row.get("cwd")
                    or record.get("pidDomain", "darwin") != "darwin" and sys.platform == "darwin"):
                continue
            process = _process_info(int(path.stem), deadline)
            if (not _agent_executable(process, "claude", home)
                    or not record.get("procStart") or record["procStart"] != process["procStart"]
                    or record.get("pid", process["pid"]) != process["pid"]
                    or type(row.get("pid")) is int and row["pid"] != process["pid"]):
                continue
            matches.append({**process, "binding": "claude-session-registry", "sessionId": sid})
        except (OSError, ValueError):
            continue
    return matches[0] if len(matches) == 1 else None


def _process_key(row):
    return {key: row[key] for key in ("pid", "uid", "start", "exe", "binding") if key in row} if row else None


def _process_ended(expected, current, deadline=None):
    """An unreadable process is not evidence of exit; require gone/replaced/zombie."""
    if current is not None:
        return current.get("start") != expected["start"]
    try:
        if sys.platform == "darwin":
            raw = _darwin_info(expected["pid"])
            if raw:
                return raw["start"] != expected["start"] or raw["zombie"]
            # libproc may omit a zombie whose parent has not reaped it yet.
            status = _ps(["-o", "stat=", "-p", str(expected["pid"])],
                         deadline or time.monotonic() + 0.5).strip()
            if status.startswith("Z"):
                return True
        elif sys.platform.startswith("linux"):
            row = (Path("/proc") / str(expected["pid"]) / "stat").read_text().rsplit(")", 1)[1].split()
            return row[19] != expected["start"] or row[0] == "Z"
    except (OSError, ValueError, IndexError):
        pass
    try:
        os.kill(expected["pid"], 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def _descendants(parent, deadline):
    pairs = []
    for line in _ps(["-axo", "pid=,ppid=,uid="], deadline).splitlines():
        parts = line.split()
        if len(parts) == 3 and all(value.isdigit() for value in parts) and int(parts[2]) == os.getuid():
            pairs.append((int(parts[0]), int(parts[1])))
    selected = {parent["pid"]}
    for unused in range(32):
        added = {pid for pid, ppid in pairs if ppid in selected} - selected
        if not added:
            break
        selected.update(added)
        if len(selected) > 64:
            raise ValueError("실행 하위 프로세스가 많아 안전한 종료를 확인할 수 없습니다.")
    return [row for pid in sorted(selected - {parent["pid"]})
            for row in [_process_info(pid, deadline, start_label=False)] if row]


def _capability(supported, method=None, reason=None, **details):
    return {"supported": bool(supported), "method": method if supported else None, "reason": reason, **details}


class SessionControl:
    def __init__(self, home=None, state_dir=None):
        self.home = launch._home(home)
        # Remote workers use the injected instance's relative host state path.
        self.directory = Path(state_dir) if state_dir is not None else instance.remote_path("attachStateDir", self.home) / "control"

    def close_provenance(self, source):
        """Explicit board termination survives reconnects; unknown outcomes stay blocked."""
        identity = native._identity(source)
        result = {"blockedByBoard": False, "status": None}
        if not self.directory.exists():
            return result
        folder = settings.safe_path(self.directory, directory=True, private=True)
        try:
            marker = json.loads(_private_read(folder / ("closed-" + _digest(identity) + ".json"), private=True))
            if marker.get("source") != identity:
                raise ValueError("세션 종료 기록의 대상이 다릅니다.")
            return {"blockedByBoard": True, "status": marker.get("status")}
        except FileNotFoundError:
            pass
        # Preserve intent recorded by the previous receipt-only implementation.
        paths = list(folder.glob("*.json"))
        if len(paths) > 4096:
            raise ValueError("세션 종료 기록을 확인하지 못했습니다.")
        for path in paths:
            if not REQUEST.fullmatch(path.stem):
                continue
            record = json.loads(_private_read(path, private=True)).get("result", {})
            if (record.get("source") == identity and record.get("action") == "terminate"
                    and record.get("applied") is not False):
                return {"blockedByBoard": True, "status": record.get("status")}
        return result

    @contextmanager
    def _lock(self):
        folder = settings.safe_path(self.directory, directory=True, private=True, allow_missing=True)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder = settings.safe_path(folder, directory=True, private=True)
        fd = os.open(str(folder / ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("세션 제어 잠금 파일이 안전하지 않습니다.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("다른 세션 제어 요청을 처리 중입니다. 결과를 확인한 뒤 다시 시도해 주세요.") from None
            yield folder
        finally:
            os.close(fd)

    def _secret(self, folder):
        path = folder / "proof-key"
        try:
            value = _private_read(path, 64, private=True)
        except FileNotFoundError:
            value = secrets.token_bytes(32)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
        if len(value) != 32:
            raise ValueError("세션 제어 서명 파일이 올바르지 않습니다.")
        return value

    def _sign(self, payload, secret):
        data = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
        return data + "." + hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()

    def _verify(self, proof, secret):
        if not isinstance(proof, str) or len(proof) > 4096:
            raise ValueError("세션 제어 확인 정보가 올바르지 않습니다.")
        try:
            data, signature = proof.split(".")
            if not hmac.compare_digest(signature, hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            if (not isinstance(payload, dict) or type(payload.get("expiresAt")) is not int
                    or payload["expiresAt"] < time.time() or payload["expiresAt"] > time.time() + PROOF_TTL + 2):
                raise ValueError()
            return payload
        except (ValueError, TypeError):
            raise ValueError("세션 제어 확인이 만료되었거나 변경되었습니다. 다시 확인해 주세요.") from None

    def _observe(self, source, deadline):
        identity = native._identity(source)
        unavailable = {"source": identity, "phase": "unknown", "target": None,
                       "capabilities": {name: _capability(False, reason=UNAVAILABLE) for name in ("interrupt", "terminate")}}
        try:
            if identity["agent"] == "codex":
                with native._client(identity, self.home, deadline) as client:
                    state = native._codex_state(client, identity)
                try:
                    process = _codex_process(identity, self.home, deadline)
                except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                    process = None
                interrupt = state.get("canInterrupt") is True
                stable = state.get("safeToTransfer") is True
                record = None
                try:
                    record = launch.saved_codex_thread({**source, **identity}, self.home)
                except (OSError, ValueError, sqlite3.Error):
                    pass
                archive = bool(record and not record["archived"] and (interrupt or stable))
                process_terminate = process is not None and (interrupt or stable)
                terminate = process_terminate or archive
                method = "codex-cli-process" if process_terminate else "codex-thread-archive"
                target = {"identity": identity, "turnId": state.get("activeTurnId"), "phase": state["phase"],
                          "process": _process_key(process), "canInterrupt": interrupt, "safe": stable,
                          "record": record, "terminationMethod": method if terminate else None,
                          "archiveRevision": state.get("revision") if archive and not process_terminate else None}
                return {"source": identity, "phase": state["phase"], "target": target, "process": process,
                        "native": state, "capabilities": {
                            "interrupt": _capability(interrupt, "codex-turn-interrupt", None if interrupt else state.get("reason") or "진행 중인 응답이 없습니다."),
                            "terminate": _capability(terminate, method, None if terminate else
                                "원본 실행과 저장된 세션을 정확히 확인하지 못했습니다.",
                                effect="선택한 세션을 종료하고 대화 기록을 보관합니다. 하위 에이전트도 함께 종료·보관을 요청합니다." if archive and not process_terminate else "원본 CLI 실행을 종료합니다.",
                                scope="thread_and_descendants" if archive and not process_terminate else "exact_process")}}
            binary = native._claude_binary(self.home)
            rows = json.loads(native._claude_run(binary, ["agents", "--json", "--all"], deadline))
            if not isinstance(rows, list):
                return unavailable
            matches = [row for row in rows if isinstance(row, dict) and row.get("id") == identity["id"]]
            if not matches and source.get("kind") == "interactive" and source.get("sessionId") == identity["id"]:
                matches = [row for row in rows if isinstance(row, dict) and row.get("kind") == "interactive"
                           and row.get("sessionId") == identity["id"] and row.get("cwd") == source.get("cwd")]
            if len(matches) != 1 or source.get("cwd") is not None and matches[0].get("cwd") != source["cwd"]:
                return unavailable
            row = matches[0]
            if row.get("kind") not in ("interactive", "background"):
                return unavailable
            phase = collector.claude_phase(row.get("state"), row.get("status"))
            background = row["kind"] == "background"
            ended = row.get("state") in ("stopped", "done", "failed")
            process = _claude_process(row, self.home, deadline) if not background else None
            terminate = (background and not ended) or process is not None
            target = {"identity": identity, "kind": row["kind"], "nativeId": row.get("id"),
                      "sessionId": row.get("sessionId"), "startedAt": row.get("startedAt"),
                      "process": _process_key(process), "ended": ended}
            return {"source": identity, "phase": phase, "target": target, "process": process,
                    "native": row, "binary": binary, "capabilities": {
                        "interrupt": _capability(False, reason="이 Claude 실행 방식은 응답만 중단하는 공식 제어를 제공하지 않습니다. 실행 종료 후 기록에서 다시 이어갈 수 있습니다."),
                        "terminate": _capability(terminate, "claude-background-stop" if background else "claude-cli-process",
                            None if terminate else "종료된 세션이거나 원본 프로세스의 신원을 확인하지 못했습니다.")}}
        except (OSError, ValueError, collector.RpcError, TimeoutError, subprocess.TimeoutExpired):
            return unavailable

    def _view(self, observed, folder):
        revision = _digest(observed["target"])
        expires = int(time.time()) + PROOF_TTL
        return {"source": observed["source"], "phase": observed["phase"], "revision": revision,
                "expiresAt": expires, "capabilities": observed["capabilities"],
                "targetProof": self._sign({"source": observed["source"], "revision": revision,
                                           "expiresAt": expires, "nonce": secrets.token_hex(12)}, self._secret(folder))}

    def preview(self, source, timeout=3):
        deadline = time.monotonic() + min(max(float(timeout), 0.05), 10)
        observed = self._observe(source, deadline)
        with self._lock() as folder:
            return self._view(observed, folder)

    def execute(self, source, action, target_proof, request_id, timeout=8):
        if action not in ("interrupt", "terminate") or not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
            raise ValueError("세션 제어 요청이 올바르지 않습니다.")
        identity = native._identity(source)
        deadline = time.monotonic() + min(max(float(timeout), 0.05), 12)
        digest = _digest([identity, action, target_proof])
        with self._lock() as folder:
            receipt_path = folder / (request_id + ".json")
            try:
                previous = json.loads(_private_read(receipt_path, private=True))
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if previous.get("digest") != digest:
                    raise ValueError("같은 요청 식별자를 다른 세션 제어에 사용할 수 없습니다.")
                return {**previous["result"], "replayed": True}
            proof = self._verify(target_proof, self._secret(folder))
            if proof.get("source") != identity:
                raise ValueError("세션 제어 대상이 변경되었습니다.")
            proof_path = folder / ("proof-" + _digest(target_proof) + ".json")
            try:
                used = json.loads(_private_read(proof_path, private=True))
            except FileNotFoundError:
                used = None
            if used is not None:
                if used["result"].get("action") != action:
                    raise ValueError("이미 사용한 확인 정보입니다. 다른 제어를 하려면 상태를 다시 확인해 주세요.")
                return {**used["result"], "replayed": True}
            observed = self._observe(source, deadline)
            result = {"action": action, "requestId": request_id, "source": identity,
                      "confirmed": False, "applied": False, "status": "rejected"}
            if proof.get("revision") != _digest(observed["target"]):
                return {**result, "status": "stale", "reason": "원본 실행이 변경되었습니다. 새 상태를 확인해 주세요.",
                        "preview": self._view(observed, folder)}
            capability = observed["capabilities"][action]
            if not capability["supported"]:
                return {**result, "reason": capability["reason"]}
            pending = {**result, "applied": None, "status": "unconfirmed", "reason": "실행 요청 결과를 확인하지 못했습니다. 자동 재실행하지 않습니다."}
            _save(receipt_path, {"digest": digest, "result": pending})
            _save(proof_path, {"result": pending})
            marker_path = folder / ("closed-" + _digest(identity) + ".json")
            prior_marker = None
            if action == "terminate":
                try:
                    prior_marker = json.loads(_private_read(marker_path, private=True))
                except FileNotFoundError:
                    pass
                _save(marker_path, {"source": identity, "status": "pending", "requestId": request_id,
                                    "requestedAt": int(time.time()), "method": capability["method"]})
            try:
                details = self._apply(source, action, observed, deadline)
                result.update(details)
            except (OSError, ValueError, sqlite3.Error, collector.RpcError, TimeoutError, subprocess.TimeoutExpired):
                result = pending
            _save(receipt_path, {"digest": digest, "result": result})
            _save(proof_path, {"result": result})
            if action == "terminate":
                if result.get("applied") is False:
                    if prior_marker is None:
                        marker_path.unlink(missing_ok=True)
                    else:
                        _save(marker_path, prior_marker)
                else:
                    _save(marker_path, {"source": identity, "status": result["status"], "requestId": request_id,
                                        "requestedAt": int(time.time()), "method": capability["method"]})
            return result

    def _interrupt_codex(self, observed, deadline):
        identity, target = observed["source"], observed["target"]
        with native._client(identity, self.home, deadline) as client:
            state = native._codex_state(client, identity)
            turn = target["turnId"]
            if not state.get("canInterrupt") or state.get("activeTurnId") != turn:
                return {"applied": False, "confirmed": False, "status": "stale", "reason": "응답이 변경되어 중단하지 않았습니다."}
            client.call("turn/interrupt", {"threadId": identity["id"], "turnId": turn})
            # One bounded readback. Timeout/errors terminate this operation; no
            # blind mutation retries, and a new turn is never interrupted.
            state = native._codex_state(client, identity)
            confirmed = (state.get("safeToTransfer") is True
                         and state.get("turnStatuses", {}).get(turn) in ("interrupted", "completed", "failed"))
            return {"applied": True, "confirmed": confirmed, "status": "interrupted" if confirmed else "unconfirmed",
                    "reason": None if confirmed else "중단 요청을 보냈지만 원본 응답의 종료는 아직 확인되지 않았습니다."}

    def _terminate_process(self, observed, deadline):
        expected = observed["process"]
        children = _descendants(expected, deadline)
        current = _process_info(expected["pid"], deadline)
        if not current or any(current.get(key) != expected.get(key) for key in ("pid", "uid", "start", "exe")):
            return {"applied": False, "confirmed": False, "status": "stale", "reason": "원본 프로세스가 변경되어 종료하지 않았습니다."}
        # Never kill a process group: it could contain a user's shell or another
        # session. SIGTERM allows the native CLI to clean up its own resources.
        os.kill(current["pid"], signal.SIGTERM)
        for unused in range(15):
            after = _process_info(current["pid"], deadline)
            if _process_ended(current, after, deadline):
                children_alive = any(not _process_ended(child, after, deadline)
                                     for child in children
                                     for after in [_process_info(child["pid"], deadline, start_label=False)])
                if children_alive:
                    return {"applied": True, "confirmed": False, "status": "unconfirmed",
                            "reason": "원본 CLI는 종료됐지만 하위 실행이 남아 있어 전체 종료는 확인하지 못했습니다."}
                return {"applied": True, "confirmed": True, "status": "terminated", "reason": None}
            time.sleep(min(0.1, native._remaining(deadline)))
        return {"applied": True, "confirmed": False, "status": "unconfirmed",
                "reason": "종료 신호를 보냈지만 원본 프로세스가 아직 남아 있습니다. 강제 종료는 하지 않았습니다."}

    def _apply(self, source, action, observed, deadline):
        method = observed["capabilities"][action]["method"]
        if method == "codex-turn-interrupt":
            return self._interrupt_codex(observed, deadline)
        if method == "codex-thread-archive":
            identity = observed["source"]
            # The API stops and archives the selected thread, and requests the
            # same for its descendants. Never shut down the shared daemon.
            with native._client(identity, self.home, deadline) as client:
                state = native._codex_state(client, identity)
                record = launch.saved_codex_thread({**source, **identity}, self.home)
                if (record != observed["target"]["record"] or record["archived"]
                        or state.get("activeTurnId") != observed["target"].get("turnId")
                        or state.get("revision") != observed["target"].get("archiveRevision")
                        or not (state.get("canInterrupt") or state.get("safeToTransfer"))):
                    return {"applied": False, "confirmed": False, "status": "stale", "reason": "원본 세션이 변경되어 종료하지 않았습니다."}
                client.call("thread/archive", {"threadId": identity["id"]})
                cursor, loaded = None, None
                for unused in range(3):
                    reply = client.call("thread/loaded/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
                    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
                        break
                    if identity["id"] in reply["data"]:
                        loaded = True
                        break
                    cursor = reply.get("nextCursor")
                    if not cursor:
                        loaded = False
                        break
                after = launch.saved_codex_thread({**source, **identity}, self.home)
                confirmed = loaded is False and after["archived"] is True
                return {"applied": True, "confirmed": confirmed, "status": "terminated" if confirmed else "unconfirmed",
                        "archived": after["archived"], "scope": "thread_and_descendants", "scopeReadback": "selected-session",
                        "reason": None if confirmed else "종료·보관 요청을 보냈지만 실행 종료와 보관 상태를 모두 확인하지 못했습니다."}
        if method == "claude-background-stop":
            row, binary = observed["native"], observed["binary"]
            native._claude_run(binary, ["stop", row["id"]], deadline)
            rows = json.loads(native._claude_run(binary, ["agents", "--json", "--all"], deadline))
            if not isinstance(rows, list):
                raise ValueError("원본 종료 상태를 확인하지 못했습니다.")
            matches = [item for item in rows if isinstance(item, dict) and item.get("id") == row["id"]]
            confirmed = (len(matches) == 1 and matches[0].get("state") in ("stopped", "done", "failed")
                         and matches[0].get("sessionId") == row.get("sessionId")
                         and matches[0].get("startedAt") == row.get("startedAt"))
            return {"applied": True, "confirmed": confirmed, "status": "terminated" if confirmed else "unconfirmed",
                    "reason": None if confirmed else "종료 요청을 보냈지만 원본 실행의 종료를 확인하지 못했습니다."}
        if method == "codex-cli-process" and observed["target"].get("canInterrupt"):
            interrupted = self._interrupt_codex(observed, deadline)
            if not interrupted["confirmed"]:
                return {**interrupted, "status": "unconfirmed"}
        # Re-resolve ownership after an interrupt: the same terminal may have
        # resumed another session while the original turn was stopping.
        fresh = self._observe(source, deadline)
        if (not fresh["capabilities"]["terminate"]["supported"]
                or _process_key(fresh.get("process")) != _process_key(observed["process"])
                or method == "codex-cli-process" and not fresh.get("native", {}).get("safeToTransfer")):
            return {"applied": False, "confirmed": False, "status": "stale", "reason": "원본 실행이 변경되어 프로세스를 종료하지 않았습니다."}
        return self._terminate_process(fresh, deadline)
