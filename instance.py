"""Installation names: brand, browser keys, state paths and per-host folder names.

One board installation is one instance. Its names are fixed at import, from the first of:

1. names injected by the board: the collector's stdin program sets ``INJECTED``, and the
   transfer helper archive carries an ``instance.json`` member. A remote host uses the
   board's names and never reads its own instance file or environment;
2. the file named by the ``SESSIONHOLIC_INSTANCE`` environment variable;
3. ``instance.json`` next to this module (the installation folder);
4. the built-in public defaults below.

An unsafe or malformed file stops the import with ValueError (fail closed) rather than
falling back to public names. Every key is optional; omitted keys keep the public
default. ``brand.loginTokenHelp`` is text, not HTML: the server escapes it, a line break
starts a new source line and a `backtick` span becomes inline code.
Protocol names (tmux sessions ``sb-<id>``, the ``sessionholic-assets`` meta, the
attachment source tag and file names inside the state folder) are not instance names.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import types

ENVIRONMENT = "SESSIONHOLIC_INSTANCE"
FILENAME = "instance.json"
SIZE_LIMIT = 64 * 1024

DEFAULTS = {
    "version": 1,
    "brand": {
        "title": "세션홀릭 - 에이전트 세션 매니저",
        "shortName": "세션홀릭",
        "manifestName": "세션홀릭 - 에이전트 세션 매니저",
        "description": "Claude Code·Codex의 세션과 대화를 확인하고 이어가는 에이전트 세션 매니저",
        # False: the public product name is not served (no application-name or
        # description meta, a neutral service worker header).
        "publicMeta": True,
        "loginHeadline": "여러 세션을 한곳에서.",
        "loginLead": "Claude Code·Codex의 작업과 대화를 확인하고 이어가세요.",
        "loginTokenHelp": "서버 관리자에게 접속 토큰을 확인하세요. 직접 설치했다면\n설치 안내의 접속 설정을 확인하세요.",
        "logPrefix": "세션홀릭",
        "serverVersion": "Sessionholic/1",
        "threadPrefix": "sessionholic",
        "codexClientName": "sessionholic",
    },
    "web": {
        "storagePrefix": "sessionholic:",
        "historyKey": "sessionholic",
        "cachePrefix": "sessionholic-",
        "manifestId": "/sessionholic",
    },
    "paths": {
        "configDir": "~/.config/sessionholic",
        # tokenFile and defaultHosts default to <configDir>/token and <configDir>/hosts.json.
        "stateDir": "~/.local/state/sessionholic",
        "hostConfig": ".config/sessionholic/config.json",
    },
    "names": {
        "cookie": "sessionholic_token",
        "tmuxSocket": "sessionholic",
        # /tmp/<name>-<uid> (SSH connection sharing) and /tmp/<name>-watch-<uid> (watch locks).
        "sshFallback": "sessionholic",
    },
    "remote": {
        "runtimeDir": ".local/state/sessionholic-transfer-runtime",
        "transfersDir": ".local/state/sessionholic-transfers",
        "workspacesDir": ".local/share/sessionholic/workspaces",
        "chatLedgerDir": ".sessionholic",
        "attachmentsDir": ".sessionholic-attachments",
        "attachStateDir": ".local/state/sessionholic",
    },
}

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")
_PATTERNS = {
    "serverVersion": re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z"),
    "threadPrefix": _NAME,
    "codexClientName": _NAME,
    "storagePrefix": re.compile(r"[A-Za-z0-9_.:-]{1,32}\Z"),
    "historyKey": re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z"),
    "cachePrefix": _NAME,
    "manifestId": re.compile(r"/[A-Za-z0-9._~/-]{0,63}\Z"),
    "cookie": re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z"),
    "tmuxSocket": re.compile(r"[A-Za-z0-9_-]{1,60}\Z"),
    "sshFallback": re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z"),
}
_RELATIVE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")


def _plain(value, limit):
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _help(value):
    """A few non-empty text lines; each `code` span closes on its own line."""
    if not isinstance(value, str) or not 0 < len(value.encode("utf-8")) <= 1024:
        return False
    lines = value.split("\n")
    return len(lines) <= 6 and all(_plain(line, 512) and line.count("`") % 2 == 0 for line in lines)


def _absolute(value):
    if not _plain(value, 1024) or not (value.startswith("~/") or value.startswith("/")):
        return False
    parts = value[2:].split("/") if value.startswith("~/") else value[1:].split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _relative(value):
    return (isinstance(value, str) and len(value) <= 256 and bool(_RELATIVE.fullmatch(value))
            and all(part not in (".", "..") for part in value.split("/")))


def _component(value):
    return _relative(value) and "/" not in value and len(value) <= 64


def _pattern(key):
    return lambda value: isinstance(value, str) and bool(_PATTERNS[key].fullmatch(value))


_SCHEMA = {
    "brand": {
        "title": lambda v: _plain(v, 256), "shortName": lambda v: _plain(v, 256),
        "manifestName": lambda v: _plain(v, 256), "description": lambda v: _plain(v, 512),
        "publicMeta": lambda v: type(v) is bool,
        "loginHeadline": lambda v: _plain(v, 256), "loginLead": lambda v: _plain(v, 512),
        "loginTokenHelp": _help, "logPrefix": lambda v: _plain(v, 64),
        "serverVersion": _pattern("serverVersion"), "threadPrefix": _pattern("threadPrefix"),
        "codexClientName": _pattern("codexClientName"),
    },
    "web": {key: _pattern(key) for key in ("storagePrefix", "historyKey", "cachePrefix", "manifestId")},
    "paths": {"configDir": _absolute, "tokenFile": _absolute, "defaultHosts": _absolute,
              "stateDir": _absolute, "hostConfig": _relative},
    "names": {key: _pattern(key) for key in ("cookie", "tmuxSocket", "sshFallback")},
    "remote": {"runtimeDir": _relative, "transfersDir": _relative, "workspacesDir": _relative,
               "chatLedgerDir": _relative, "attachmentsDir": _component, "attachStateDir": _relative},
}
# The part of an instance a remote host needs (remote_blob()).
REMOTE_KEYS = {"brand": ("codexClientName", "threadPrefix"), "names": ("sshFallback",),
               "paths": ("hostConfig",), "remote": tuple(_SCHEMA["remote"])}


def _error(detail="형식"):
    raise ValueError("세션홀릭 인스턴스 설정(instance.json)의 %s이 올바르지 않습니다." % detail)


def safe_path(path, *, directory=False, private=False, allow_missing=False, home=None):
    """Reject user-controlled links; allow only macOS's fixed /tmp and /var aliases."""
    path = Path(path).expanduser().absolute()
    home = Path(home).absolute() if home is not None else Path.home().absolute()
    aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
    try:
        for node in reversed((path,) + tuple(path.parents)):
            if node.is_symlink():
                if node not in aliases or node.resolve() != aliases[node]:
                    raise ValueError("사용자 설정과 상태 경로는 심볼릭 링크를 사용할 수 없습니다.")
            if node != path and node.exists() and not node.is_dir():
                raise ValueError("사용자 설정 또는 상태 경로의 상위 폴더가 올바르지 않습니다.")
            if node.exists() and (node == path or home == node or home in node.parents):
                if node.stat().st_uid != os.getuid():
                    raise ValueError("사용자 설정과 상태 경로의 소유자가 올바르지 않습니다.")
        if not path.exists():
            if allow_missing:
                return path
            raise ValueError("사용자 설정 또는 상태 파일을 찾지 못했습니다.")
        info = path.stat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("사용자 설정 또는 상태 경로 형식이 올바르지 않습니다.")
        if private and info.st_mode & 0o077:
            raise ValueError("사용자 설정 또는 상태 파일의 비공개 권한을 확인해 주세요.")
    except OSError:
        raise ValueError("사용자 설정 또는 상태 경로를 안전하게 확인하지 못했습니다.") from None
    return path


def parse(data):
    """A validated, complete instance (plain dict) from decoded JSON; unknown keys fail."""
    if not isinstance(data, dict) or set(data) - {"version"} - set(_SCHEMA):
        _error()
    if type(data.get("version")) is not int or data["version"] != 1:
        _error("버전")
    result = copy.deepcopy(DEFAULTS)
    for section, rules in _SCHEMA.items():
        values = data.get(section, {})
        if not isinstance(values, dict) or set(values) - set(rules):
            _error(section + " 항목")
        for key, value in values.items():
            if not rules[key](value):
                _error("%s.%s 값" % (section, key))
            result[section][key] = value
    paths = result["paths"]
    paths.setdefault("tokenFile", paths["configDir"] + "/token")
    paths.setdefault("defaultHosts", paths["configDir"] + "/hosts.json")
    return result


def read(path):
    """Parse one instance file: no links, owned by this user, not writable by others, 64KiB."""
    path = safe_path(path)
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                    or info.st_size > SIZE_LIMIT):
                raise ValueError("인스턴스 설정 파일의 소유자·권한·크기를 확인해 주세요.")
            raw = stream.read(SIZE_LIMIT + 1)
    except OSError:
        raise ValueError("인스턴스 설정 파일을 안전하게 읽지 못했습니다.") from None
    if len(raw) > SIZE_LIMIT:
        raise ValueError("인스턴스 설정 파일의 소유자·권한·크기를 확인해 주세요.")
    return parse(_decode(raw))


def _decode(raw):
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeError):
        _error()


def _archive_blob():
    """The board's names inside the transfer helper archive, when this module runs from it."""
    loader = globals().get("__loader__")
    try:
        import zipimport
    except ImportError:  # pragma: no cover - part of the standard library
        return None
    if not isinstance(loader, zipimport.zipimporter):
        return None
    try:
        return loader.get_data(os.path.join(os.path.dirname(globals()["__file__"]), FILENAME))
    except (OSError, KeyError):
        raise ValueError("이전 도우미에 인스턴스 이름이 없습니다.") from None


def _load():
    injected = globals().get("INJECTED")
    if injected is not None:
        return parse(_decode(injected)), "injected"
    packed = _archive_blob()
    if packed is not None:
        return parse(_decode(packed)), "archive"
    named = os.environ.get(ENVIRONMENT)
    if named:
        return read(named), "environment"
    beside = Path(globals()["__file__"]).resolve().parent / FILENAME
    if os.path.lexists(str(beside)):
        return read(beside), "file"
    return parse({"version": 1}), "default"


def _freeze(value):
    if isinstance(value, dict):
        return types.MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


_DATA, ORIGIN = _load()
_CURRENT = _freeze(_DATA)
_DEFAULT = _freeze(parse({"version": 1}))


def current():
    """This installation's names as a read-only mapping."""
    return _CURRENT


def defaults():
    """The public names (a read-only mapping), for comparisons."""
    return _DEFAULT


def path(name, home=None):
    """`paths.<name>` with `~/` expanded; `hostConfig` is relative to the home folder."""
    value = _DATA["paths"][name]
    home = Path(home) if home is not None else Path.home()
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value) if value.startswith("/") else home / value


def remote_path(name, home):
    """`remote.<name>` under one host's home folder (`attachmentsDir` is per work folder)."""
    return Path(home) / _DATA["remote"][name]


def remote_blob():
    """Canonical JSON with the names a remote host uses; it never reads its own file."""
    blob = {"version": 1}
    for section, keys in REMOTE_KEYS.items():
        blob[section] = {key: _DATA[section][key] for key in keys}
    return json.dumps(blob, sort_keys=True, separators=(",", ":"))
