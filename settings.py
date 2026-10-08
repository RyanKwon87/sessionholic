"""Explicit non-secret host-local settings; no legacy configuration imports."""
import json
import os
from pathlib import Path
import re
import stat

HOME_KEY = re.compile(r"(?:\.codex(?:-[A-Za-z0-9_-]{1,40})?|\.codex-router/[0-9a-f]{16})\Z")
GROUP = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
ENV_KEYS = {"GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
            "GIT_SSH_COMMAND", "GIT_SSH", "SSH_AUTH_SOCK"}


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


def _error():
    raise ValueError("sessionholic 설정 형식이 올바르지 않습니다. config.json을 확인해 주세요.")


def _text(value, limit=256):
    return isinstance(value, str) and 0 < len(value.encode()) <= limit and not any(ord(c) < 32 for c in value)


def _path(value, home):
    if not _text(value, 4096):
        _error()
    path = home / value[2:] if value.startswith("~/") else Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _error()
    return str(path.resolve())


def load(home=None, path=None):
    home = Path(home) if home is not None else Path.home()
    path = safe_path(Path(path) if path is not None else home / ".config/sessionholic/config.json",
                     allow_missing=True, home=home)
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "profiles": [], "projects": [], "environments": {"default": {"label": "기본 환경", "variables": {}}}}
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 65536:
        _error()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        _error()
    if (not isinstance(data, dict) or set(data) - {"version", "profiles", "projects", "environments"}
            or type(data.get("version")) is not int or data["version"] != 1):
        _error()
    environments = data.get("environments", {})
    if not isinstance(environments, dict) or len(environments) > 32:
        _error()
    groups = {"default": {"label": "기본 환경", "variables": {}}}
    for name, value in environments.items():
        if (not isinstance(name, str) or not GROUP.fullmatch(name) or not isinstance(value, dict)
                or set(value) - {"label", "variables"} or not _text(value.get("label", name))):
            _error()
        variables = value.get("variables", {})
        if (not isinstance(variables, dict) or set(variables) - ENV_KEYS
                or any(not _text(v, 4096) for v in variables.values())):
            _error()
        groups[name] = {"label": value.get("label", name), "variables": dict(variables)}
    profiles, projects = data.get("profiles", []), data.get("projects", [])
    if (not isinstance(profiles, list) or len(profiles) > 64
            or not isinstance(projects, list) or len(projects) > 128):
        _error()
    seen = set()
    for row in profiles:
        if (not isinstance(row, dict) or set(row) - {"agent", "home", "label", "environment"}
                or row.get("agent") not in ("codex", "claude") or not isinstance(row.get("home"), str)
                or (row["agent"] == "codex" and not HOME_KEY.fullmatch(row["home"]))
                or (row["agent"] == "claude" and row["home"] != ".claude")
                or not _text(row.get("label", row["home"]))
                or not isinstance(row.get("environment", "default"), str)
                or row.get("environment", "default") not in groups):
            _error()
        identity = row["agent"], row["home"]
        if identity in seen:
            _error()
        seen.add(identity)
    seen, roots = set(), set()
    for row in projects:
        if (not isinstance(row, dict) or set(row) - {"id", "label", "root", "environment"}
                or not isinstance(row.get("id"), str) or not GROUP.fullmatch(row["id"])
                or not _text(row.get("label")) or not isinstance(row.get("environment", "default"), str)
                or row.get("environment", "default") not in groups or row["id"] in seen):
            _error()
        row["root"] = _path(row.get("root"), home)
        if row["root"] in roots:
            _error()
        roots.add(row["root"])
        seen.add(row["id"])
    return {"version": 1, "profiles": profiles, "projects": projects, "environments": groups}


def profile(agent, key, home=None, config=None):
    config = config if config is not None else load(home)
    return next((p for p in config["profiles"] if p["agent"] == agent and p["home"] == key), {})


def source_environment(source, home=None, config=None):
    config = config if config is not None else load(home)
    home = Path(home) if home is not None else Path.home()
    cwd = _path(source.get("cwd"), home)
    matches = [p for p in config["projects"] if "environment" in p and
               (cwd == p["root"] or cwd.startswith(p["root"].rstrip("/") + "/"))]
    if matches:
        return max(matches, key=lambda p: len(p["root"])).get("environment", "default")
    return profile(source.get("agent"), source.get("home") or ".claude", home, config).get("environment", "default")


def variables(group, home=None):
    config = load(home)
    if not isinstance(group, str) or group not in config["environments"]:
        raise ValueError("이 호스트에 등록되지 않은 실행 환경입니다.")
    home = Path(home) if home is not None else Path.home()
    return {key: str(home / value[2:]) if value.startswith("~/") else value
            for key, value in config["environments"][group]["variables"].items()}
