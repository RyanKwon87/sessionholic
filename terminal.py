"""Persistent tmux terminals with bounded, in-memory PTY output.

Only trusted server code supplies commands. Browser clients use opaque IDs. Closing
an HTTP connection or this process never kills the command owned by tmux.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

ID = re.compile(r"^[0-9a-f]{32}$")
SOCKET = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
BUFFER_LIMIT = 1024 * 1024
INPUT_LIMIT = 32 * 1024
MAX_TERMINALS = 4
DEFAULT_STATE = Path.home() / ".local/state/sessionholic/terminals"


def _metadata(value):
    if not isinstance(value, dict):
        raise ValueError("터미널 정보가 올바르지 않습니다.")
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("터미널 정보가 올바르지 않습니다.")
        if key != "nativeSource":
            if not isinstance(item, (str, int, bool, type(None))):
                raise ValueError("터미널 정보가 올바르지 않습니다.")
            continue
        required = {"host", "agent", "id", "home", "cwd"}
        if (not isinstance(item, dict) or not required <= item.keys()
                or not item.keys() <= required | {"kind", "sessionId"}
                or any(not isinstance(v, (str, type(None))) for v in item.values())
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", item.get("host") or "")
                or item.get("agent") not in ("codex", "claude")
                or not isinstance(item.get("cwd"), str) or not item["cwd"].startswith("/")
                or "\0" in item["cwd"] or len(item["cwd"].encode()) > 4096
                or item.get("kind") not in (None, "interactive", "background")):
            raise ValueError("Native 세션 정보가 올바르지 않습니다.")
        if item["agent"] == "codex":
            try:
                valid_id = str(uuid.UUID(item["id"])) == item["id"]
            except (ValueError, TypeError, AttributeError):
                valid_id = False
            valid_home = re.fullmatch(r"(?:\.codex(?:-[A-Za-z0-9_-]{1,40})?|\.codex-router/[0-9a-f]{16})", item["home"] or "")
        else:
            valid_id = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}", item["id"] or "")
            valid_home = item["home"] in (None, ".claude")
        if (not valid_id or not valid_home or ("sessionId" in item and
                (item["agent"] != "claude" or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", item["sessionId"] or "")))):
            raise ValueError("Native 세션 정보가 올바르지 않습니다.")
    if len(json.dumps(value, ensure_ascii=False).encode()) > 8192:
        raise ValueError("터미널 정보가 너무 큽니다.")
    return copy.deepcopy(value)


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("터미널 상태 경로가 안전하지 않습니다.")
    path.chmod(0o700)


def _private_json(path, value):
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class _Terminal:
    def __init__(self, record, buffer_limit):
        self.record = record
        self.limit = buffer_limit
        self.output = bytearray()
        self.offset = 0
        self.condition = threading.Condition()
        self.io_lock = threading.Lock()
        self.process = None
        self.fd = None
        self.generation = 0
        self.alive = not record.get("closed", False)
        self.epoch = secrets.token_hex(16)

    def append(self, data):
        with self.condition:
            self.output.extend(data)
            self.offset += len(data)
            if len(self.output) > self.limit:
                del self.output[:-self.limit]
            self.condition.notify_all()

    def read(self, after, wait, epoch=None):
        deadline = time.monotonic() + wait
        with self.condition:
            changed_epoch = epoch is not None and epoch != self.epoch
            while after == self.offset and self.alive and not changed_epoch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            start = self.offset - len(self.output)
            reset = changed_epoch or after < start or after > self.offset
            begin = start if reset else after
            data = bytes(self.output[begin - start:])
            return {"after": self.offset, "data": base64.b64encode(data).decode("ascii"),
                    "reset": reset, "alive": self.alive, "epoch": self.epoch}


class TerminalManager:
    def __init__(self, state_dir=None, socket_name="sessionholic", tmux=None,
                 max_terminals=MAX_TERMINALS, buffer_limit=BUFFER_LIMIT):
        if not SOCKET.fullmatch(socket_name):
            raise ValueError("터미널 소켓 이름이 올바르지 않습니다.")
        self.tmux = tmux or shutil.which("tmux")
        self.socket_name = socket_name
        self.state_dir = Path(state_dir) if state_dir is not None else DEFAULT_STATE
        _private_directory(self.state_dir)
        self.state_path = self.state_dir / "sessions.json"
        self.max_terminals = max_terminals
        self.buffer_limit = buffer_limit
        self.lock = threading.RLock()
        self.terminals = {}
        self._closed = False
        lock_path = self.state_dir / ".lock"
        try:
            self._lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if os.fstat(self._lock_fd).st_uid != os.getuid():
                raise OSError("unsafe lock owner")
            os.fchmod(self._lock_fd, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if hasattr(self, "_lock_fd"):
                os.close(self._lock_fd)
            raise RuntimeError("다른 보드가 같은 터미널 상태 경로를 사용 중이거나 경로가 안전하지 않습니다.") from None
        try:
            self._load()
        except BaseException:
            os.close(self._lock_fd)
            raise

    def _command(self, *args):
        if not self.tmux:
            raise RuntimeError("tmux가 설치되어 있지 않습니다.")
        return [self.tmux, "-L", self.socket_name, "-f", "/dev/null"] + list(args)

    def _run(self, *args, **kwargs):
        try:
            return subprocess.run(self._command(*args), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=10, **kwargs)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("터미널 실행기에 연결하지 못했습니다.") from None

    def _exists(self, terminal):
        if terminal.record.get("closed", False):
            return False
        return self._run("has-session", "-t", "=" + terminal.record["name"]).returncode == 0

    def _load(self):
        if not self.state_path.exists():
            return
        if self.state_path.is_symlink() or self.state_path.stat().st_uid != os.getuid():
            raise ValueError("터미널 상태 파일이 안전하지 않습니다.")
        self.state_path.chmod(0o600)
        try:
            rows = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("터미널 상태 파일을 읽지 못했습니다.") from None
        if not isinstance(rows, list):
            raise RuntimeError("터미널 상태 파일 형식이 올바르지 않습니다.")
        for row in rows:
            if (not isinstance(row, dict) or not ID.fullmatch(str(row.get("id", "")))
                    or row.get("name") != "sb-" + row["id"] or not isinstance(row.get("key"), str)
                    or not isinstance(row.get("metadata"), dict)
                    or ("closed" in row and not isinstance(row["closed"], bool))):
                raise RuntimeError("터미널 상태 파일 형식이 올바르지 않습니다.")
            try:
                row["metadata"] = _metadata(row["metadata"])
            except ValueError:
                raise RuntimeError("터미널 상태 파일 형식이 올바르지 않습니다.") from None
            self.terminals[row["id"]] = _Terminal(row, self.buffer_limit)

    def _save(self):
        _private_json(self.state_path, [t.record for t in self.terminals.values()])

    @staticmethod
    def _public(terminal):
        return {**copy.deepcopy(terminal.record["metadata"]), "id": terminal.record["id"], "key": terminal.record["key"],
                "metadata": copy.deepcopy(terminal.record["metadata"]), "alive": terminal.alive,
                "closed": terminal.record.get("closed", False)}

    def _get(self, terminal_id):
        if self._closed:
            raise RuntimeError("터미널 연결 관리자가 종료되었습니다.")
        if not isinstance(terminal_id, str) or not ID.fullmatch(terminal_id):
            raise ValueError("터미널 ID가 올바르지 않습니다.")
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise KeyError("터미널을 찾을 수 없습니다.")
        return terminal

    def create(self, key, argv, cwd, env, metadata=None):
        """Create once per stable key; a dead saved command is never rerun implicitly."""
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise ValueError("터미널 키가 올바르지 않습니다.")
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                or not os.path.isabs(argv[0])):
            raise ValueError("터미널 명령이 올바르지 않습니다.")
        if not isinstance(cwd, (str, Path)) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("작업 경로가 올바르지 않습니다.")
        if (not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                or not k or "=" in k or "\0" in k or "\0" in v for k, v in env.items())):
            raise ValueError("터미널 환경이 올바르지 않습니다.")
        metadata = _metadata({} if metadata is None else metadata)
        with self.lock:
            if self._closed:
                raise RuntimeError("터미널 연결 관리자가 종료되었습니다.")
            for terminal in self.terminals.values():
                if terminal.record["key"] == key:
                    terminal.alive = self._exists(terminal)
                    if not terminal.alive:
                        raise RuntimeError("이 터미널의 CLI가 종료되었습니다. 새 작업으로 시작하세요.")
                    self._attach(terminal)
                    return self._public(terminal)
            live = sum(self._exists(t) for t in self.terminals.values())
            if live >= self.max_terminals:
                raise RuntimeError("열 수 있는 터미널 수를 초과했습니다.")
            terminal_id = secrets.token_hex(16)
            record = {"id": terminal_id, "name": "sb-" + terminal_id, "key": key,
                      "metadata": dict(metadata)}
            terminal = _Terminal(record, self.buffer_limit)
            launch_path = self.state_dir / (terminal_id + ".launch.json")
            launch_env = dict(env)
            launch_env.setdefault("TERM", "xterm-256color")
            _private_json(launch_path, {"argv": list(argv), "cwd": str(cwd), "env": launch_env})
            try:
                done = self._run("new-session", "-d", "-s", record["name"], "-c", str(cwd),
                                 sys.executable, "-I", str(Path(__file__).resolve()),
                                 "--launch", str(launch_path))
                if done.returncode != 0:
                    raise RuntimeError("터미널을 시작하지 못했습니다.")
                self.terminals[terminal_id] = terminal
                self._save()
                if not self._started(terminal, launch_path):
                    terminal.alive = False
                    launch_path.unlink(missing_ok=True)
                    raise RuntimeError("CLI가 시작 직후 종료되었거나 실행하지 못했습니다.")
                self._attach(terminal)
            except BaseException:
                # A successfully started tmux command remains available after a gateway error.
                if terminal_id not in self.terminals:
                    launch_path.unlink(missing_ok=True)
                raise
            return self._public(terminal)

    def _started(self, terminal, launch_path):
        # The envelope disappears immediately before exec. This checks process
        # startup only; authentication and the model connection remain native CLI UI.
        deadline = time.monotonic() + 3
        while launch_path.exists():
            if not self._exists(terminal) or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        time.sleep(0.075)
        return self._exists(terminal)

    def _attach(self, terminal):
        with terminal.io_lock:
            if terminal.record.get("closed", False):
                terminal.alive = False
                return
            if terminal.process is not None and terminal.process.poll() is None:
                return
            if not self._exists(terminal):
                terminal.alive = False
                return
            self._configure(terminal)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            attach_env = dict(os.environ)
            attach_env.pop("TMUX", None)
            attach_env.pop("TMUX_PANE", None)
            attach_env["TERM"] = "xterm-256color"
            try:
                process = subprocess.Popen(self._command("attach-session", "-t", "=" + terminal.record["name"]),
                                           stdin=slave, stdout=slave, stderr=slave,
                                           env=attach_env, start_new_session=True, close_fds=True)
            except OSError:
                os.close(master)
                raise RuntimeError("터미널 화면을 연결하지 못했습니다.") from None
            finally:
                os.close(slave)
            terminal.process, terminal.fd = process, master
            terminal.generation += 1
            generation = terminal.generation
            terminal.alive = True
            threading.Thread(target=self._reader, args=(terminal, process, master, generation), daemon=True).start()

    def _configure(self, terminal):
        # Unlike has-session/attach-session, option targets interpret '=' as part
        # of the name. These complete names are generated here, never by clients.
        done = self._run("set-option", "-t", terminal.record["name"], "status", "off")
        if done.returncode != 0:
            raise RuntimeError("터미널 화면 설정을 적용하지 못했습니다.")

    def _reader(self, terminal, process, descriptor, generation):
        try:
            while True:
                ready, _, _ = select.select([descriptor], [], [], 0.5)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                terminal.append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            with terminal.io_lock:
                if terminal.generation == generation:
                    terminal.fd = None
                    with terminal.condition:
                        # A detached client is not a stopped CLI.
                        try:
                            terminal.alive = self._exists(terminal)
                        except RuntimeError:
                            terminal.alive = False
                        terminal.condition.notify_all()
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def read(self, terminal_id, after=0, wait=20, epoch=None):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("출력 위치가 올바르지 않습니다.")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 20:
            raise ValueError("대기 시간이 올바르지 않습니다.")
        if epoch is not None and (not isinstance(epoch, str) or not ID.fullmatch(epoch)):
            raise ValueError("출력 식별자가 올바르지 않습니다.")
        with self.lock:
            terminal = self._get(terminal_id)
            self._attach(terminal)
        return terminal.read(after, wait, epoch)

    def write(self, terminal_id, text):
        if not isinstance(text, str):
            raise ValueError("입력 형식이 올바르지 않습니다.")
        data = text.encode("utf-8")
        if len(data) > INPUT_LIMIT:
            raise ValueError("입력은 32KiB 이하여야 합니다.")
        with self.lock:
            terminal = self._get(terminal_id)
            self._attach(terminal)
        with terminal.io_lock:
            if (terminal.record.get("closed", False) or terminal.fd is None
                    or terminal.process.poll() is not None):
                raise RuntimeError("터미널 연결이 종료되었습니다.")
            # A full PTY input queue must not tie up an HTTP worker indefinitely.
            deadline = time.monotonic() + 2
            sent = 0
            while sent < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [terminal.fd], [], remaining)[1]:
                    raise RuntimeError("터미널 입력 대기 시간이 초과되었습니다.")
                try:
                    sent += os.write(terminal.fd, data[sent:sent + 1024])
                except OSError:
                    raise RuntimeError("터미널에 입력하지 못했습니다.") from None
        return {"ok": True}

    def resize(self, terminal_id, cols, rows):
        if (isinstance(cols, bool) or isinstance(rows, bool) or not isinstance(cols, int)
                or not isinstance(rows, int) or not 2 <= cols <= 500 or not 2 <= rows <= 300):
            raise ValueError("터미널 크기가 올바르지 않습니다.")
        with self.lock:
            terminal = self._get(terminal_id)
            self._attach(terminal)
        with terminal.io_lock:
            if terminal.record.get("closed", False) or terminal.fd is None:
                raise RuntimeError("터미널 연결이 종료되었습니다.")
            try:
                fcntl.ioctl(terminal.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                os.killpg(terminal.process.pid, signal.SIGWINCH)
            except OSError:
                raise RuntimeError("터미널 크기를 변경하지 못했습니다.") from None
        return {"ok": True}

    def list(self):
        with self.lock:
            result = []
            for terminal in self.terminals.values():
                terminal.alive = self._exists(terminal)
                result.append(self._public(terminal))
            return result

    def detach(self, terminal_id):
        """Stop only this gateway's tmux client; never send keys or kill-session."""
        with self.lock:
            terminal = self._get(terminal_id)
        with terminal.io_lock:
            process = terminal.process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        return {"ok": True}

    def close_terminal(self, terminal_id):
        """Stop one generated tmux session; retain its key as a closed record."""
        with self.lock:
            terminal = self._get(terminal_id)
            with terminal.io_lock:
                if not terminal.record.get("closed", False):
                    if self._exists(terminal):
                        done = self._run("kill-session", "-t", "=" + terminal.record["name"])
                        if done.returncode != 0 and self._exists(terminal):
                            raise RuntimeError("터미널을 닫지 못했습니다.")
                    terminal.record["closed"] = True
                    self._save()
                with terminal.condition:
                    terminal.alive = False
                    terminal.condition.notify_all()
                # Wait only for our attach client, never for the reader (which
                # needs io_lock). Writes and resizes check the tombstone under
                # this same lock, including requests queued before the close.
                process = terminal.process
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        return {"ok": True}

    def close(self):
        with self.lock:
            if self._closed:
                return
            try:
                for terminal_id in list(self.terminals):
                    self.detach(terminal_id)
            finally:
                self._closed = True
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)


def _launch(path):
    """Private one-use envelope keeps argv and environment out of tmux's argv."""
    path = Path(path)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("unsafe launch envelope")
    data = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    os.chdir(data["cwd"])
    # Preserve only this new tmux session's identity, never the gateway's parent pane.
    for name in ("TMUX", "TMUX_PANE"):
        data["env"].pop(name, None)
        if name in os.environ:
            data["env"][name] = os.environ[name]
    os.execve(data["argv"][0], data["argv"], data["env"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--launch":
        try:
            _launch(sys.argv[2])
        except Exception:
            # Never print the envelope, environment, argv or a traceback into the PTY.
            print("터미널 명령을 시작하지 못했습니다.", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit("This module is started by the session board.")
