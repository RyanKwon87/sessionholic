"""Explicit non-secret host-local settings; no legacy configuration imports."""
import json
from pathlib import Path
import re
import sys

try:
    import instance
except ModuleNotFoundError as missing:
    # `python3 -I collector.py` excludes the script directory; load only the sibling module.
    if missing.name != "instance":
        raise
    import importlib.util
    _spec = importlib.util.spec_from_file_location("instance", Path(__file__).resolve().with_name("instance.py"))
    instance = importlib.util.module_from_spec(_spec)
    sys.modules["instance"] = instance
    try:
        _spec.loader.exec_module(instance)
    except BaseException:
        del sys.modules["instance"]
        raise

# One link and owner rule for settings, state and the instance file itself.
safe_path = instance.safe_path
HOME_KEY = re.compile(r"(?:\.codex(?:-[A-Za-z0-9_-]{1,40})?|\.codex-router/[0-9a-f]{16})\Z")
GROUP = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
ENV_KEYS = {"GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
            "GIT_SSH_COMMAND", "GIT_SSH", "SSH_AUTH_SOCK"}


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


def config_path(home=None):
    """This host's settings file: the instance's `paths.hostConfig` under its home folder."""
    return instance.path("hostConfig", Path(home) if home is not None else Path.home())


def load(home=None, path=None):
    home = Path(home) if home is not None else Path.home()
    path = safe_path(Path(path) if path is not None else config_path(home),
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
