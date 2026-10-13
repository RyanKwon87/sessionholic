"""Persistent tmux terminals with bounded, in-memory PTY output.

Only trusted server code supplies commands. Browser clients use opaque IDs. Closing
an HTTP connection or this process never kills the command owned by tmux.
"""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path, PureWindowsPath
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

try:
    import instance
except ModuleNotFoundError as missing:
    # `python3 -I terminal.py --launch` inside tmux runs only _launch(), which needs no names.
    if missing.name != "instance":
        raise
    instance = None

ID = re.compile(r"^[0-9a-f]{32}$")
SOCKET = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
BUFFER_LIMIT = 1024 * 1024
INPUT_LIMIT = 32 * 1024
MAX_TERMINALS = 16
# A board connection to an existing session left unused this long may be released
# for a new terminal; the session itself keeps running.
RELEASE_IDLE = 60
# Unused this long, a Claude connection is closed even with room to spare; sending to the
# session (or 질문 불러오기) attaches it again.
CLOSE_IDLE = 600
# Closed rows kept, the oldest forgotten first. The user's own closes are kept longer:
# they stop an automatic resume of that session. Any other close is history only.
CLOSED_KEEP = 40
USER_CLOSED_KEEP = 200
# A closed terminal's last output stays readable this long; then its buffer is freed,
# and only rows closed at least this long are forgotten.
CLOSED_OUTPUT_SECONDS = 1800
# Ids of forgotten rows kept for their input receipts to be pruned (take_forgotten).
FORGOTTEN_LIMIT = 1000
HISTORY_LIMIT = 20000
SCROLL_LIMIT = 200
WHEEL_LIMIT = 40
KEY_NAMES = frozenset(("Up", "Down", "Enter"))
KEY_LIMIT = 40
# One display-message call; scroll_position is empty outside copy-mode, so the
# fields are split on an explicit separator rather than on whitespace.
PANE_FORMAT = ("#{alternate_on}|#{mouse_any_flag}|#{mouse_sgr_flag}|#{pane_width}|#{pane_height}"
               "|#{pane_in_mode}|#{scroll_position}|#{history_size}")
PANE_FIELDS = ("alternate", "mouse", "sgr", "width", "height", "inMode", "position", "history")
def socket_dir():
    """Where tmux keeps this user's server sockets (`-L name`). A socket file stays after its
    server ends (tmux 3.7): whoever ends a server it started removes the file too."""
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / ("tmux-%d" % os.getuid())


def socket_live(path):
    """Whether a tmux server answers on the socket file `path` (a refused connection: none)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


DEFAULT_STATE = instance.path("stateDir") / "terminals" if instance is not None else None
# The board's own tmux server (`tmux -L <name>`); each saved row records the socket it was
# created on, so a renamed socket never turns live sessions into closed records.
DEFAULT_SOCKET = instance.current()["names"]["tmuxSocket"] if instance is not None else None
LOG_PREFIX = instance.current()["brand"]["logPrefix"] if instance is not None else None


class InputNotSent(RuntimeError):
    """tmux refused the paste before any byte reached the CLI."""


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
                or not isinstance(item.get("cwd"), str)
                or not (item["cwd"].startswith("/") or PureWindowsPath(item["cwd"]).is_absolute())
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
        # When this process saw it close (monotonic); a row loaded closed closed long ago.
        self.closed_at = None if self.alive else float("-inf")
        self.epoch = secrets.token_hex(16)
        self.scrolled = False
        # Set when a new attach client starts: the first input then also leaves any
        # copy-mode, whose in-memory flag a gateway restart would have lost.
        self.attached_fresh = False
        # Last read, input or resize from a browser (monotonic). A row loaded after a
        # restart counts from now, so a connection in use is not released before its
        # browser reads again.
        self.touched = time.monotonic()

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
    def __init__(self, state_dir=None, socket_name=None, tmux=None,
                 max_terminals=MAX_TERMINALS, buffer_limit=BUFFER_LIMIT):
        socket_name = DEFAULT_SOCKET if socket_name is None else socket_name
        if not isinstance(socket_name, str) or not SOCKET.fullmatch(socket_name):
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
        self.reservations = set()
        self._closed = False
        self._warned = set()
        # Unrecorded rows looked for on this user's other tmux servers (_elsewhere), once each.
        self._looked = set()
        self.socket_dir = socket_dir()
        # One list-sessions answer for the _exists calls of a listing (_listing), as
        # (thread ident, names): only the listing thread uses it.
        self._live = None
        # Rows _trim_closed forgot whose input receipts may remain (take_forgotten).
        self._forgotten = []
        # Told (without arguments) when a terminal is created, closed, attached or
        # detached. They may run under self.lock: a listener never calls back in here.
        self._listeners = []
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

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        self._listeners.remove(callback)

    def _changed(self):
        """The terminal list may have changed; a listener's failure never fails the change."""
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                pass

    def _command(self, *args):
        if not self.tmux:
            raise RuntimeError("tmux가 설치되어 있지 않습니다.")
        return [self.tmux, "-L", self.socket_name, "-f", "/dev/null"] + list(args)

    def _run(self, *args, **kwargs):
        if "input" not in kwargs:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            return subprocess.run(self._command(*args),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=10, **kwargs)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("터미널 실행기에 연결하지 못했습니다.") from None

    def _foreign(self, terminal):
        """True for a row created on another tmux socket: this board cannot see or end it."""
        socket = terminal.record.get("socket")
        return socket is not None and socket != self.socket_name

    @staticmethod
    def _unrecorded(terminal):
        """A row saved before sockets were recorded: its socket is not known yet. It is
        looked up on this board's socket and joins it only once found there."""
        return "socket" not in terminal.record

    def _elsewhere(self, terminal):
        """Whether an unrecorded row's tmux session runs on another live tmux server of this
        user (an earlier socket name): True, False, or None when that cannot be told."""
        name = terminal.record.get("name")
        if not self.tmux or not isinstance(name, str) or not name:
            return None
        try:
            paths = [path for path in self.socket_dir.iterdir() if path.name != self.socket_name]
        except FileNotFoundError:
            return False  # No socket folder: no tmux server of this user runs.
        except OSError:
            return None
        for path in paths:
            try:
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or not SOCKET.fullmatch(path.name):
                    continue
                if not socket_live(path):
                    continue
                found = subprocess.run([self.tmux, "-L", path.name, "has-session", "-t", "=" + name],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5)
            except FileNotFoundError:
                continue  # Its socket went meanwhile.
            except (OSError, subprocess.SubprocessError):
                return None
            if found.returncode == 0:
                return True
            said = found.stderr.decode("utf-8", "replace")
            if "can't find session" not in said and "no server running" not in said:
                return None  # Not an answer about the session (another tmux version, an error).
        return False

    def _unrecorded_message(self):
        return ("이 터미널은 소켓 기록 없이 저장됐고 현재 tmux 소켓(%s)에서 찾지 못했습니다. "
                "이전 소켓 이름에서 열린 터미널일 수 있어 종료로 기록하지 않았습니다." % self.socket_name)

    def _adopt(self, terminal):
        """Record this socket on an unrecorded row found on it; the caller holds self.lock."""
        if not self._unrecorded(terminal):
            return
        terminal.record["socket"] = self.socket_name
        try:
            self._save()
        except (ValueError, RuntimeError, OSError):
            terminal.record.pop("socket", None)
            raise

    def _exists(self, terminal):
        if terminal.record.get("closed", False) or self._foreign(terminal):
            return False
        live = self._live
        if live is not None and live[0] == threading.get_ident():
            return terminal.record["name"] in live[1]
        return self._run("has-session", "-t", "=" + terminal.record["name"]).returncode == 0

    def _live_names(self):
        """The session names on this board's tmux socket, in one call. None when the answer
        is unclear: each terminal is then asked on its own (has-session), as before."""
        try:
            done = subprocess.run(self._command("list-sessions", "-F", "#{session_name}"),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=10)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return None
        if done.returncode == 0:
            return set(done.stdout.decode("utf-8", "replace").split())
        message = done.stderr.decode("utf-8", "replace")
        # No server on the socket (or no socket yet) is no sessions.
        if "no server running" in message or "No such file or directory" in message:
            return set()
        return None

    @contextmanager
    def _listing(self):
        """Answer the _exists calls inside from one list-sessions call instead of one tmux
        process per terminal, all while self.lock is held; the caller holds self.lock."""
        if self._live is not None or not any(not t.record.get("closed", False) and not self._foreign(t)
                                             for t in self.terminals.values()):
            yield
            return
        names = self._live_names()
        self._live = None if names is None else (threading.get_ident(), names)
        try:
            yield
        finally:
            self._live = None

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
                    or ("closed" in row and not isinstance(row["closed"], bool))
                    or ("closedReason" in row and row["closedReason"] not in ("user", "connection_ended", "released"))
                    or ("closedAt" in row and type(row["closedAt"]) is not int)
                    or ("socket" in row and (not isinstance(row["socket"], str)
                                             or not SOCKET.fullmatch(row["socket"])))):
                raise RuntimeError("터미널 상태 파일 형식이 올바르지 않습니다.")
            try:
                row["metadata"] = _metadata(row["metadata"])
            except ValueError:
                raise RuntimeError("터미널 상태 파일 형식이 올바르지 않습니다.") from None
            # A row without "socket" was saved before sockets were recorded. It is not
            # given this socket here: it may belong to the socket of an earlier name.
            self.terminals[row["id"]] = _Terminal(row, self.buffer_limit)

    def _save(self):
        _private_json(self.state_path, [t.record for t in self.terminals.values()])

    @staticmethod
    def _public(terminal):
        return {**copy.deepcopy(terminal.record["metadata"]), "id": terminal.record["id"], "key": terminal.record["key"],
                "metadata": copy.deepcopy(terminal.record["metadata"]), "alive": terminal.alive,
                "closed": terminal.record.get("closed", False), "closedReason": terminal.record.get("closedReason"),
                "closedAt": terminal.record.get("closedAt")}

    def _get(self, terminal_id):
        if self._closed:
            raise RuntimeError("터미널 연결 관리자가 종료되었습니다.")
        if not isinstance(terminal_id, str) or not ID.fullmatch(terminal_id):
            raise ValueError("터미널 ID가 올바르지 않습니다.")
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise KeyError("터미널을 찾을 수 없습니다.")
        return terminal

    def _use(self, terminal_id):
        """A terminal a browser reads, types into or resizes; marks it recently used."""
        terminal = self._get(terminal_id)
        terminal.touched = time.monotonic()
        return terminal

    @contextmanager
    def reserve(self, key):
        """Reserve capacity before a launch can submit its first model input."""
        with self.lock:
            if self._closed:
                raise RuntimeError("터미널 연결 관리자가 종료되었습니다.")
            if key in self.reservations or not self._make_room(len(self.reservations), key):
                live = sum(self._exists(t) for t in self.terminals.values())
                raise RuntimeError("열린 터미널 %d개가 가득 찼습니다. 세션 목록의 '터미널'에서 쓰지 않는 "
                                   "터미널을 닫은 뒤 다시 실행해 주세요." % live)
            self.reservations.add(key)
        try:
            yield
        finally:
            with self.lock:
                self.reservations.discard(key)

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
                    if self._foreign(terminal):
                        raise RuntimeError(self._foreign_message(terminal))
                    terminal.alive = self._exists(terminal)
                    if not terminal.alive:
                        if self._unrecorded(terminal) and not terminal.record.get("closed", False):
                            raise RuntimeError(self._unrecorded_message())
                        raise RuntimeError("이 터미널의 CLI가 종료되었습니다. 새 작업으로 시작하세요.")
                    self._adopt(terminal)
                    self._attach(terminal)
                    terminal.touched = time.monotonic()
                    return self._public(terminal)
            if not self._make_room(len(self.reservations - {key}), key):
                raise RuntimeError("열 수 있는 터미널 수를 초과했습니다.")
            terminal_id = secrets.token_hex(16)
            record = {"id": terminal_id, "name": "sb-" + terminal_id, "key": key,
                      "metadata": dict(metadata), "socket": self.socket_name}
            terminal = _Terminal(record, self.buffer_limit)
            terminal.touched = time.monotonic()
            launch_path = self.state_dir / (terminal_id + ".launch.json")
            launch_env = dict(env)
            launch_env.setdefault("TERM", "xterm-256color")
            _private_json(launch_path, {"argv": list(argv), "cwd": str(cwd), "env": launch_env})
            try:
                # The option applies to panes created after it, so it precedes new-session
                # in one command list on this board-only tmux socket.
                done = self._run("set-option", "-g", "history-limit", str(HISTORY_LIMIT), ";",
                                 "new-session", "-d", "-s", record["name"], "-c", str(cwd),
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
            # A copy-mode left behind by an earlier gateway would capture the keys.
            self._cancel_mode(terminal)
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
            terminal.attached_fresh = True
            threading.Thread(target=self._reader, args=(terminal, process, master, generation), daemon=True).start()
        self._changed()

    def _configure(self, terminal):
        # Unlike has-session/attach-session, option targets interpret '=' as part
        # of the name. These complete names are generated here, never by clients.
        # No prefix key: Ctrl+B (and every key after it) belongs to the CLI, never to this
        # board's own tmux client, which Ctrl+B d would otherwise detach.
        name = terminal.record["name"]
        done = self._run("set-option", "-t", name, "status", "off", ";",
                         "set-option", "-t", name, "prefix", "None", ";",
                         "set-option", "-t", name, "prefix2", "None")
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
            terminal = self._use(terminal_id)
            self._attach(terminal)
        return terminal.read(after, wait, epoch)

    def write(self, terminal_id, text):
        if not isinstance(text, str):
            raise ValueError("입력 형식이 올바르지 않습니다.")
        data = text.encode("utf-8")
        if len(data) > INPUT_LIMIT:
            raise ValueError("입력은 32KiB 이하여야 합니다.")
        with self.lock:
            terminal = self._use(terminal_id)
            self._attach(terminal)
        self._leave_scroll(terminal)
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

    def _cancel_mode(self, terminal):
        # Outside a mode tmux answers "not in a mode" and sends nothing to the CLI.
        try:
            self._run("send-keys", "-t", terminal.record["name"], "-X", "cancel")
        except RuntimeError:
            pass

    def _leave_scroll(self, terminal):
        # Keys typed while tmux shows history would drive copy-mode instead of the CLI.
        if terminal.scrolled or terminal.attached_fresh:
            terminal.scrolled = terminal.attached_fresh = False
            self._cancel_mode(terminal)

    def _pane(self, terminal):
        """Live pane flags; _run discards stdout, so this is its own bounded call."""
        try:
            done = subprocess.run(self._command("display-message", "-p", "-t", terminal.record["name"], PANE_FORMAT),
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("터미널 상태를 확인하지 못했습니다.") from None
        values = done.stdout.decode("ascii", "replace").strip().split("|")
        if done.returncode != 0 or len(values) != len(PANE_FIELDS):
            raise RuntimeError("터미널 상태를 확인하지 못했습니다.")
        try:
            return {key: int(value or 0) for key, value in zip(PANE_FIELDS, values)}
        except ValueError:
            raise RuntimeError("터미널 상태를 확인하지 못했습니다.") from None

    def scroll(self, terminal_id, lines):
        """Scroll the view: wheel events for a full-screen mouse app, tmux history otherwise.

        A remote TUI on the alternate screen leaves tmux no history to show, so it
        receives the wheel events a local terminal would send. History scrolling
        itself never reaches the CLI.
        """
        if isinstance(lines, bool) or not isinstance(lines, int) or not 0 < abs(lines) <= SCROLL_LIMIT:
            raise ValueError("스크롤 범위가 올바르지 않습니다.")
        with self.lock:
            terminal = self._use(terminal_id)
        if terminal.record.get("closed", False) or not terminal.alive:
            raise RuntimeError("터미널 연결이 종료되었습니다.")
        pane = self._pane(terminal)
        # A held run's client draws on the main screen (no alternate one), so its mouse modes
        # alone say a full-screen app wants the wheel.
        if pane["mouse"] and (pane["alternate"] or terminal.record["metadata"].get("holdName")):
            return self._wheel(terminal, pane, lines)
        name = terminal.record["name"]
        if lines < 0:
            if not pane["history"]:
                # Nothing above the screen: an empty copy-mode would only trap input.
                return {"ok": True, "mode": "history", "position": 0}
            # -e leaves history automatically once the view returns to the bottom.
            done = self._run("copy-mode", "-e", "-t", name, ";",
                             "send-keys", "-t", name, "-X", "-N", str(-lines), "scroll-up")
            if done.returncode != 0:
                raise RuntimeError("터미널 기록을 스크롤하지 못했습니다.")
            terminal.scrolled = True
        elif pane["inMode"]:
            # Outside history there is nothing below the live screen to scroll to.
            self._run("send-keys", "-t", name, "-X", "-N", str(lines), "scroll-down")
        try:
            after = self._pane(terminal)
        except RuntimeError:
            return {"ok": True, "mode": "history", "position": None}
        terminal.scrolled = bool(after["inMode"])
        return {"ok": True, "mode": "history", "position": after["position"] if after["inMode"] else 0}

    def _wheel(self, terminal, pane, lines):
        # One wheel notch per three lines at the pane centre, in the app's mouse encoding.
        count = min(max(math.ceil(abs(lines) / 3), 1), WHEEL_LIMIT)
        button = 64 if lines < 0 else 65
        x, y = max(pane["width"] // 2, 1), max(pane["height"] // 2, 1)
        if pane["sgr"]:
            event = "\x1b[<%d;%d;%dM" % (button, x, y)
        else:
            event = "\x1b[M" + chr(32 + button) + chr(32 + min(x, 223)) + chr(32 + min(y, 223))
        with terminal.io_lock:
            if pane["inMode"]:
                self._cancel_mode(terminal)
            terminal.scrolled = False
            done = self._run("send-keys", "-t", terminal.record["name"], "-l", event * count)
        if done.returncode != 0:
            raise RuntimeError("터미널 화면을 스크롤하지 못했습니다.")
        return {"ok": True, "mode": "app", "position": None}

    def capture(self, terminal_id):
        """Visible screen text of the live pane (no history, no escape sequences)."""
        with self.lock:
            terminal = self._use(terminal_id)
        if terminal.record.get("closed", False) or not terminal.alive:
            raise RuntimeError("터미널 연결이 종료되었습니다.")
        try:
            done = subprocess.run(self._command("capture-pane", "-p", "-t", terminal.record["name"]),
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("터미널 화면을 확인하지 못했습니다.") from None
        if done.returncode != 0:
            raise RuntimeError("터미널 화면을 확인하지 못했습니다.")
        return done.stdout.decode("utf-8", "replace")

    def send_keys(self, terminal_id, keys):
        """Press named navigation keys (a menu answer) in one tmux call; never text."""
        if (not isinstance(keys, (list, tuple)) or not keys or len(keys) > KEY_LIMIT
                or any(not isinstance(key, str) or key not in KEY_NAMES for key in keys)):
            raise ValueError("보낼 수 없는 키 입력입니다.")
        with self.lock:
            terminal = self._use(terminal_id)
        if terminal.record.get("closed", False) or not terminal.alive:
            raise RuntimeError("터미널 연결이 종료되었습니다.")
        self._leave_scroll(terminal)
        with terminal.io_lock:
            done = self._run("send-keys", "-t", terminal.record["name"], *keys)
            if done.returncode != 0:
                raise InputNotSent("터미널에 키를 보내지 못했습니다.")
        return {"ok": True}

    def scroll_end(self, terminal_id):
        with self.lock:
            terminal = self._use(terminal_id)
        self._leave_scroll(terminal)
        return {"ok": True}

    def paste(self, terminal_id, text, submit=True):
        """Paste text as one bracketed paste (when the CLI asked for it), then Enter."""
        if not isinstance(text, str) or not text or "\0" in text:
            raise ValueError("입력 형식이 올바르지 않습니다.")
        data = text.encode("utf-8")
        if len(data) > INPUT_LIMIT:
            raise ValueError("입력은 32KiB 이하여야 합니다.")
        with self.lock:
            terminal = self._use(terminal_id)
            self._attach(terminal)
        if terminal.record.get("closed", False) or not terminal.alive:
            raise InputNotSent("터미널 연결이 종료되었습니다.")
        self._leave_scroll(terminal)
        name, buffer = terminal.record["name"], "sh-" + secrets.token_hex(8)
        with terminal.io_lock:
            done = self._run("load-buffer", "-b", buffer, "-", ";",
                             "paste-buffer", "-p", "-d", "-b", buffer, "-t", name, input=data)
            if done.returncode != 0:
                # load-buffer may have succeeded; never leave the message text in tmux.
                self._run("delete-buffer", "-b", buffer)
                raise InputNotSent("터미널에 붙여넣지 못했습니다.")
            if submit:
                # Let the TUI finish its paste handling before the separate Enter key.
                time.sleep(0.15)
                done = self._run("send-keys", "-t", name, "Enter")
                if done.returncode != 0:
                    raise RuntimeError("붙여넣은 뒤 Enter를 보내지 못했습니다.")
        return {"ok": True}

    def resize(self, terminal_id, cols, rows):
        if (isinstance(cols, bool) or isinstance(rows, bool) or not isinstance(cols, int)
                or not isinstance(rows, int) or not 2 <= cols <= 500 or not 2 <= rows <= 300):
            raise ValueError("터미널 크기가 올바르지 않습니다.")
        with self.lock:
            terminal = self._use(terminal_id)
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

    def link_native(self, terminal_id, native, confidence="exact"):
        """Record, once, the native session a terminal turned out to run (a launcher's new
        session). A terminal that already names one keeps it."""
        with self.lock:
            if confidence not in ("exact", "estimated"):
                raise ValueError("세션 연결 근거를 확인해 주세요.")
            terminal = self._get(terminal_id)
            if terminal.record["metadata"].get("nativeSource"):
                return False
            metadata = _metadata({**terminal.record["metadata"], "nativeSource": native, "linkConfidence": confidence})
            previous = terminal.record["metadata"]
            terminal.record["metadata"] = metadata
            try:
                self._save()
            except BaseException:
                terminal.record["metadata"] = previous
                raise
        self._changed()
        return True

    def list(self):
        with self.lock, self._listing():
            result = []
            changed, adopted = [], []
            released = self._trim_closed()
            now = time.monotonic()
            for terminal in self.terminals.values():
                terminal.alive = self._exists(terminal)
                # Only Claude connections, which the board opens itself to send or answer;
                # a Codex connection the user opened closes only when room is needed.
                if (terminal.alive and self._releasable(terminal) and terminal.record["metadata"].get("agent") == "claude"
                        and now - terminal.touched >= CLOSE_IDLE):
                    try:
                        self._end(terminal, "released")
                        released = True
                    except (RuntimeError, ValueError, OSError):
                        pass
                    terminal.alive = self._exists(terminal)
                if self._foreign(terminal):
                    # Another socket's session is not visible here, not ended: no tombstone.
                    self._warn_foreign(terminal)
                elif self._unrecorded(terminal):
                    # Found on this socket, the row joins it. Not found, it may still run on
                    # the socket of an earlier name: neither a socket nor a tombstone, unless
                    # no other live tmux server of this user has it either (looked once per
                    # start): then it ended, and is closed here like any ended terminal.
                    if terminal.alive:
                        terminal.record["socket"] = self.socket_name
                        adopted.append(terminal)
                    elif terminal.record["id"] not in self._looked:
                        found = self._elsewhere(terminal)
                        # Not told (None): looked for again on the next listing.
                        if found is not None:
                            self._looked.add(terminal.record["id"])
                        if found is False:
                            terminal.record["socket"] = self.socket_name
                            terminal.record["closed"] = True
                            terminal.record["closedReason"] = "connection_ended"
                            terminal.record["closedAt"] = int(time.time())
                            terminal.closed_at = now
                            changed.append(terminal)
                            adopted.append(terminal)
                elif not terminal.alive and not terminal.record.get("closed", False):
                    # A generated tmux session that ended is never restarted by
                    # this ID. Persist that fact before input receipts are pruned.
                    terminal.record["closed"] = True
                    terminal.record["closedReason"] = "connection_ended"
                    terminal.record["closedAt"] = int(time.time())
                    terminal.closed_at = now
                    changed.append(terminal)
                result.append(self._public(terminal))
            if changed or adopted:
                try:
                    self._save()
                except (ValueError, RuntimeError, OSError):
                    for terminal in changed:
                        terminal.record.pop("closed", None)
                        terminal.record.pop("closedReason", None)
                        terminal.record.pop("closedAt", None)
                    for terminal in adopted:
                        terminal.record.pop("socket", None)
                        self._looked.discard(terminal.record["id"])
                    raise
            if released:
                self._changed()
            return result

    def _foreign_message(self, terminal):
        return ("이 터미널은 다른 tmux 소켓(%s)에서 열려 이 보드(%s)에서 다룰 수 없습니다. "
                "인스턴스의 tmux 소켓 이름을 확인해 주세요." % (terminal.record["socket"], self.socket_name))

    def _warn_foreign(self, terminal):
        if terminal.record["id"] in self._warned:
            return
        self._warned.add(terminal.record["id"])
        sys.stderr.write("%s: 터미널 %s의 기록 tmux 소켓(%s)이 현재 소켓(%s)과 달라 종료로 기록하지 않았습니다.\n"
                         % (LOG_PREFIX or "terminal", terminal.record["id"][:8], terminal.record["socket"],
                            self.socket_name))

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
        self._changed()
        return {"ok": True}

    def set_program(self, terminal_id, info):
        """Record what a 새 세션 terminal runs (read from its host); never its identity or command."""
        program = info.get("program") if isinstance(info, dict) else None
        if program not in (None, "claude-agents", "claude", "codex"):
            raise ValueError("실행 프로그램 정보가 올바르지 않습니다.")
        cwd = info.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") and len(cwd) <= 4096 and "\0" not in cwd else None
        with self.lock:
            terminal = self._get(terminal_id)
            if terminal.record.get("closed", False) or terminal.record["metadata"].get("mode") != "new-session":
                return False
            previous = terminal.record["metadata"]
            terminal.record["metadata"] = _metadata({**previous, "program": program, "programCwd": cwd,
                                                     "programAt": int(time.time())})
            try:
                self._save()
            except BaseException:
                terminal.record["metadata"] = previous
                raise
        if (previous.get("program"), previous.get("programCwd")) != (program, cwd):
            self._changed()
        return True

    def close_terminal(self, terminal_id):
        """Stop one generated tmux session; retain its key as a closed record."""
        with self.lock:
            self._end(self._get(terminal_id), "user")
        self._changed()
        return {"ok": True}

    def _end(self, terminal, reason):
        """Kill one generated tmux session and save its tombstone; the caller holds self.lock."""
        with terminal.io_lock:
            if not terminal.record.get("closed", False):
                if self._foreign(terminal):
                    raise RuntimeError(self._foreign_message(terminal))
                unrecorded = self._unrecorded(terminal)
                if self._exists(terminal):
                    done = self._run("kill-session", "-t", "=" + terminal.record["name"])
                    if done.returncode != 0 and self._exists(terminal):
                        raise RuntimeError("터미널을 닫지 못했습니다.")
                elif unrecorded:
                    # Possibly alive on an earlier socket: like a foreign row, never tombstoned.
                    raise RuntimeError(self._unrecorded_message())
                terminal.record["closed"] = True
                terminal.record["closedReason"] = reason
                terminal.record["closedAt"] = int(time.time())
                terminal.closed_at = time.monotonic()
                if unrecorded:
                    terminal.record["socket"] = self.socket_name
                try:
                    self._save()
                except (ValueError, RuntimeError, OSError):
                    terminal.record.pop("closed", None)
                    terminal.record.pop("closedReason", None)
                    terminal.record.pop("closedAt", None)
                    if unrecorded:
                        terminal.record.pop("socket", None)
                    raise
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

    def _make_room(self, reserved, keep):
        """Whether one more terminal fits, releasing idle connections to existing sessions
        for it; the caller holds self.lock."""
        with self._listing():
            live = sum(self._exists(t) for t in self.terminals.values())
        # Releasing asks tmux itself: a session may have ended since the count.
        while live + reserved >= self.max_terminals:
            if not self._release_idle(keep):
                return False
            live -= 1
        return True

    def _trim_closed(self):
        """Free the output of rows closed CLOSED_OUTPUT_SECONDS ago and forget the oldest of
        them past CLOSED_KEEP (USER_CLOSED_KEEP for the user's own closes); the caller holds
        self.lock. Whether a row was forgotten (the list changed)."""
        now = time.monotonic()
        settled = []
        for terminal in self.terminals.values():
            if (not terminal.record.get("closed", False) or terminal.closed_at is None
                    or now - terminal.closed_at < CLOSED_OUTPUT_SECONDS):
                continue
            if terminal.output:
                with terminal.condition:
                    terminal.output = bytearray()
            settled.append(terminal)
        # Oldest close first; a row saved before close times were kept counts as oldest.
        settled.sort(key=lambda t: t.record.get("closedAt", 0))
        user = [t for t in settled if t.record.get("closedReason") == "user"]
        other = [t for t in settled if t.record.get("closedReason") != "user"]
        forget = user[:max(0, len(user) - USER_CLOSED_KEEP)] + other[:max(0, len(other) - CLOSED_KEEP)]
        if not forget:
            return False
        before = dict(self.terminals)
        for terminal in forget:
            del self.terminals[terminal.record["id"]]
        try:
            self._save()
        except (ValueError, RuntimeError, OSError):
            # Not saved: kept as they were until a later listing saves.
            self.terminals = before
            return False
        self._forgotten = (self._forgotten + [t.record["id"] for t in forget])[-FORGOTTEN_LIMIT:]
        return True

    def take_forgotten(self):
        """Closed rows (as {"id", "closed"}) forgotten since the last call, for their input
        receipts to be pruned; give them back (restore_forgotten) when that fails."""
        with self.lock:
            taken, self._forgotten = self._forgotten, []
        return [{"id": terminal_id, "closed": True} for terminal_id in taken]

    def restore_forgotten(self, rows):
        with self.lock:
            self._forgotten = ([row["id"] for row in rows] + self._forgotten)[-FORGOTTEN_LIMIT:]

    def _releasable(self, terminal):
        """A board connection to an existing session ("attach"), not one being launched."""
        metadata = terminal.record["metadata"]
        return (metadata.get("mode") == "attach" and metadata.get("launchMode") in (None, "attach")
                and terminal.record["key"] not in self.reservations and not terminal.record.get("closed", False)
                and not self._unrecorded(terminal) and not self._foreign(terminal))

    def _release_idle(self, keep):
        """Close the least recently used board connection to an existing session.

        Only an "attach" connection is chosen: closing it stops this board's client,
        not the session (as "연결 닫기" does), and its "released" tombstone does not
        count as the user ending the session. A terminal that runs its own CLI, one in
        use within RELEASE_IDLE seconds, or one being launched is never released.
        """
        now = time.monotonic()
        idle = [t for t in self.terminals.values()
                if self._releasable(t) and t.record["key"] != keep
                and now - t.touched >= RELEASE_IDLE and self._exists(t)]
        if not idle:
            return False
        self._end(min(idle, key=lambda t: t.touched), "released")
        self._changed()
        return True

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


HOLD_RETRY = (2, 5, 10, 20, 30)
HOLD_BACK = 8  # attached again this long (past SSH's 6 s connect wait): the board takes sends again
HOLD_SETTLED = 60  # a connection that lasted this long starts the waits over
HOLD_FIRST_GRACE = 15  # an SSH that fails this soon after opening never ran the step


# Modes a run turned on through the dropped connection (mouse reports, bracketed paste) are
# off while the board terminal waits, so a wheel or a paste does not print escape codes.
HOLD_RESET = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?25h"


def _hold_waiting(path, waiting):
    """The mark the board reads while this terminal reconnects (a Claude send waits for it)."""
    if not path:
        return
    path = Path(path)
    try:
        if waiting:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.touch(mode=0o600)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _hold_quiet(quiet, saved=None):
    """No echo while waiting (keys typed then are dropped anyway); the saved modes back after."""
    try:
        if quiet:
            saved = termios.tcgetattr(0)
            modes = list(saved)
            modes[3] &= ~termios.ECHO
            termios.tcsetattr(0, termios.TCSANOW, modes)
            return saved
        if saved is not None:
            termios.tcsetattr(0, termios.TCSANOW, saved)
    except (termios.error, OSError, ValueError):
        pass
    return None


def _hold_ended(path):
    """Its user stopped this terminal reconnecting (Ctrl+C): the board's sweep ends the run on
    that host once the host is back, as for a terminal closed from the board."""
    if not path:
        return
    try:
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(path).with_name(Path(path).name + ".ended").touch(mode=0o600)
    except OSError:
        pass


def _hold_loop(spec, run=subprocess.call, sleep=time.sleep, clock=time.monotonic, out=None, timer=threading.Timer):
    """A board terminal whose run another host keeps in its own tmux: reconnect after a
    dropped connection (SSH exit 255) by attaching again, never starting anything. Any other
    exit (the run ended, Ctrl+C, a refusal) ends the terminal with that code."""
    out = out or sys.stdout
    wait_path = spec.get("wait")
    argv, attempt, opened = spec["first"], 0, clock()
    try:
        while True:
            started, back = clock(), None
            if argv is not spec["first"]:
                # Still marked while connecting and a moment after: a send then could be
                # split or dropped. Attached that long, the board takes sends again.
                back = timer(HOLD_BACK, _hold_waiting, (wait_path, False))
                back.daemon = True
                back.start()
            try:
                code = run(argv)
            except KeyboardInterrupt:
                # Ctrl+C while attaching again (the terminal is not raw yet): as while waiting.
                if back is not None:
                    back.cancel()
                    _hold_ended(wait_path)
                return 130
            if back is not None:
                back.cancel()
            if code != 255:
                return code if code >= 0 else 1
            if argv is spec["first"] and clock() - opened < HOLD_FIRST_GRACE:
                return 255  # The connection never came up: report it, no loop.
            if clock() - started >= HOLD_SETTLED:
                attempt = 0
            wait = HOLD_RETRY[min(attempt, len(HOLD_RETRY) - 1)]
            attempt += 1
            _hold_waiting(wait_path, True)
            try:
                termios.tcflush(0, termios.TCIFLUSH)
            except (termios.error, OSError, ValueError):
                pass
            out.write(HOLD_RESET + "\r\n[하던 일] 기기 연결이 끊겼습니다. 실행은 그 기기에서 계속되고, "
                      "%d초 뒤 다시 연결합니다… (Ctrl+C를 누르면 다시 연결하지 않고, 기기가 연결되면 그 기기의 "
                      "실행도 끝냅니다)\r\n" % wait)
            out.flush()
            saved = _hold_quiet(True)
            try:
                sleep(wait)
            except KeyboardInterrupt:
                _hold_ended(wait_path)
                return 130
            finally:
                _hold_quiet(False, saved)
            try:
                # Keys typed while away never reach the run.
                termios.tcflush(0, termios.TCIFLUSH)
            except (termios.error, OSError, ValueError):
                pass
            argv = spec["again"]
    finally:
        _hold_waiting(wait_path, False)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--hold-loop":
        try:
            spec = json.loads(sys.argv[2])
            if not (isinstance(spec, dict) and all(isinstance(spec.get(k), list) and spec[k]
                                                   and all(isinstance(a, str) for a in spec[k]) for k in ("first", "again"))
                    and isinstance(spec.get("wait", ""), str)):
                raise ValueError("hold loop")
            # A stopped (Ctrl+Z) loop would leave the terminal frozen; SSH keeps its own keys.
            signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        except ValueError:
            print("터미널 명령을 시작하지 못했습니다.", file=sys.stderr)
            sys.exit(1)
        try:
            sys.exit(_hold_loop(spec))
        except KeyboardInterrupt:
            sys.exit(130)
    if len(sys.argv) == 3 and sys.argv[1] == "--launch":
        try:
            _launch(sys.argv[2])
        except Exception:
            # Never print the envelope, environment, argv or a traceback into the PTY.
            print("터미널 명령을 시작하지 못했습니다.", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit("This module is started by the session board.")
