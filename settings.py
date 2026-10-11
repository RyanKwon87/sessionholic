"""Explicit non-secret host-local settings; no legacy configuration imports.

Version 1 holds `profiles`, `projects` and `environments`. Version 2 accepts the same
fields plus optional ones (docs/setup.md). Every omitted version 2 field keeps the
version 1 behaviour, so a file without them reads exactly as before.
"""
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
            "GIT_SSH_COMMAND", "GIT_SSH", "SSH_AUTH_SOCK", "CMUX_CODEX_HOOKS_DISABLED"}
AGENTS = ("codex", "claude")
# A rule names a home by its leading characters, matched at a name boundary (see _prefix_match).
HOME_PREFIX = re.compile(r"\.codex(?:-[A-Za-z0-9_-]{0,60}/?)?\Z")
TMUX_OPTION = re.compile(r"@[A-Za-z0-9_-]{1,40}\Z")
ACCOUNT = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
HELPER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
# Optional sections; each may appear only in version 2.
SECTIONS = ("profileRules", "boundary", "legacy", "codexHomes", "agentRouter", "tmux",
            "transfer", "attachments", "usage", "launcher")
V1 = {"environment": {"label", "variables"}, "profile": {"agent", "home", "label", "environment"},
      "project": {"id", "label", "root", "environment"}}
V2 = {"environment": V1["environment"] | {"boundaryGroup", "scope", "description"},
      "profile": V1["profile"] | {"project", "placeholder", "title"},
      "project": V1["project"] | {"match"}}


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


def _location(value, home):
    """An absolute or `~/` path as written (not resolved): a file another tool owns."""
    if not _text(value, 4096):
        _error()
    path = home / value[2:] if value.startswith("~/") else Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _error()
    return str(path)


def _fields(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        _error()
    return value


def _flag(value):
    if type(value) is not bool:
        _error()
    return value


def _seconds(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _error()
    return value


def defaults():
    """Version 2 sections when they are omitted: the version 1 behaviour."""
    return {"profileRules": [], "boundary": {"agents": list(AGENTS), "claude": "inherit-source"},
            "legacy": {}, "codexHomes": {"allowSymlinks": False, "excludeSuspended": False, "requireMarker": False},
            "agentRouter": None, "tmux": {}, "transfer": {"agentConfig": "exclude-private"},
            "attachments": {"gitExclude": True}, "usage": None, "launcher": None}


def config_path(home=None):
    """This host's settings file: the instance's `paths.hostConfig` under its home folder."""
    return instance.path("hostConfig", Path(home) if home is not None else Path.home())


def load(home=None, path=None):
    home = Path(home) if home is not None else Path.home()
    path = safe_path(Path(path) if path is not None else config_path(home),
                     allow_missing=True, home=home)
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "profiles": [], "projects": [],
                "environments": {"default": {"label": "기본 환경", "variables": {}}}, **defaults()}
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 65536:
        _error()
    # The file names programs the board runs (usage.python, helpers, GIT_SSH_COMMAND):
    # nobody but its owner may change it. 0600 is recommended.
    if path.stat().st_mode & 0o022:
        raise ValueError("config.json을 다른 사용자가 수정할 수 있습니다. 권한을 600으로 바꿔 주세요.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        _error()
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2):
        _error()
    version = data["version"]
    keys = V1 if version == 1 else V2
    if set(data) - {"version", "profiles", "projects", "environments"} - (set(SECTIONS) if version == 2 else set()):
        _error()
    environments = data.get("environments", {})
    if not isinstance(environments, dict) or len(environments) > 32:
        _error()
    groups = {"default": {"label": "기본 환경", "variables": {}}}
    for name, value in environments.items():
        if (not isinstance(name, str) or not GROUP.fullmatch(name) or not isinstance(value, dict)
                or set(value) - keys["environment"] or not _text(value.get("label", name))):
            _error()
        variables = value.get("variables", {})
        if (not isinstance(variables, dict) or set(variables) - ENV_KEYS
                or any(not _text(v, 4096) for v in variables.values())):
            _error()
        group = {"label": value.get("label", name), "variables": dict(variables)}
        if "scope" in value:
            if not isinstance(value["scope"], str) or not GROUP.fullmatch(value["scope"]):
                _error()
            group["scope"] = value["scope"]
        if "description" in value:
            if not _text(value["description"], 512):
                _error()
            group["description"] = value["description"]
        if "boundaryGroup" in value:
            if not isinstance(value["boundaryGroup"], str) or not GROUP.fullmatch(value["boundaryGroup"]):
                _error()
            group["boundaryGroup"] = value["boundaryGroup"]
        groups[name] = group
    for group in groups.values():
        # One level: a boundary group is itself a plain group.
        target = group.get("boundaryGroup")
        if target is not None and (target not in groups or groups[target].get("boundaryGroup", target) != target):
            _error()
    profiles, projects = data.get("profiles", []), data.get("projects", [])
    if (not isinstance(profiles, list) or len(profiles) > 64
            or not isinstance(projects, list) or len(projects) > 128):
        _error()
    seen = set()
    for row in profiles:
        if (not isinstance(row, dict) or set(row) - keys["profile"]
                or row.get("agent") not in AGENTS or not isinstance(row.get("home"), str)
                or (row["agent"] == "codex" and not HOME_KEY.fullmatch(row["home"]))
                or (row["agent"] == "claude" and row["home"] != ".claude")
                or not _text(row.get("label", row["home"]))
                or not isinstance(row.get("environment", "default"), str)
                or row.get("environment", "default") not in groups
                or not isinstance(row.get("project", "x"), str) or not GROUP.fullmatch(row.get("project", "x"))
                or type(row.get("placeholder", False)) is not bool
                or ("title" in row and not _text(row["title"]))):
            _error()
        identity = row["agent"], row["home"]
        if identity in seen:
            _error()
        seen.add(identity)
    seen, roots = set(), set()
    for row in projects:
        if (not isinstance(row, dict) or set(row) - keys["project"]
                or not isinstance(row.get("id"), str) or not GROUP.fullmatch(row["id"])
                or not _text(row.get("label")) or not isinstance(row.get("environment", "default"), str)
                or row.get("environment", "default") not in groups or row["id"] in seen
                or row.get("match", "prefix") not in ("prefix", "component")):
            _error()
        if row.get("match") == "component":
            # A folder name matched as a whole path component anywhere in a work folder.
            root = row.get("root")
            if not _text(root, 255) or "/" in root or root in (".", ".."):
                _error()
            key = ("component", root)
        else:
            row["root"] = key = _path(row.get("root"), home)
        if key in roots:
            _error()
        roots.add(key)
        seen.add(row["id"])
    config = {"version": version, "profiles": profiles, "projects": projects, "environments": groups, **defaults()}
    if version == 2:
        _sections(data, config, groups, home)
    return config


def _sections(data, config, groups, home):
    rules = data.get("profileRules", [])
    if not isinstance(rules, list) or len(rules) > 32:
        _error()
    for rule in rules:
        _fields(rule, ("agent", "homePrefix", "environment", "label", "suffixLabel"))
        prefix = rule.get("homePrefix")
        if (rule.get("agent") not in AGENTS or not isinstance(prefix, str)
                or not (HOME_PREFIX.fullmatch(prefix) if rule["agent"] == "codex" else prefix == ".claude")
                or not isinstance(rule.get("environment", "default"), str)
                or rule.get("environment", "default") not in groups
                or ("label" in rule and not _text(rule["label"]))
                or type(rule.get("suffixLabel", False)) is not bool):
            _error()
    config["profileRules"] = rules
    if "boundary" in data:
        boundary = _fields(data["boundary"], ("agents", "claude", "priority"))
        agents = boundary.get("agents", list(AGENTS))
        if (not isinstance(agents, list) or not agents or len(set(agents)) != len(agents)
                or any(agent not in AGENTS for agent in agents)
                or boundary.get("claude", "inherit-source") != "inherit-source"
                or ("priority" in boundary and (not isinstance(boundary["priority"], str)
                                                or boundary["priority"] not in groups))):
            _error()
        config["boundary"] = {"agents": list(agents), "claude": "inherit-source"}
        if "priority" in boundary:
            config["boundary"]["priority"] = boundary["priority"]
    if "legacy" in data:
        legacy = _fields(data["legacy"], ("sourcePersonal",))
        mapping = _fields(legacy.get("sourcePersonal"), ("true", "false"))
        if set(mapping) != {"true", "false"} or any(not isinstance(g, str) or g not in groups for g in mapping.values()):
            _error()
        config["legacy"] = {"sourcePersonal": dict(mapping)}
    if "agentRouter" in data:
        router = _fields(data["agentRouter"], ("projectsFile", "workspacesRoot", "accountPolicy"))
        if not router:
            _error()
        config["agentRouter"] = {key: _location(value, home) for key, value in router.items()}
    if "codexHomes" in data:
        homes = _fields(data["codexHomes"], ("allowSymlinks", "excludeSuspended", "requireMarker"))
        config["codexHomes"] = {key: _flag(homes.get(key, False))
                                for key in ("allowSymlinks", "excludeSuspended", "requireMarker")}
        if config["codexHomes"]["excludeSuspended"] and "accountPolicy" not in (config["agentRouter"] or {}):
            _error()
    if "tmux" in data:
        tmux = _fields(data["tmux"], ("titleOption", "taskOption"))
        if not tmux or any(not isinstance(v, str) or not TMUX_OPTION.fullmatch(v) for v in tmux.values()):
            _error()
        config["tmux"] = dict(tmux)
    if "transfer" in data:
        transfer = _fields(data["transfer"], ("agentConfig",))
        if transfer.get("agentConfig", "exclude-private") not in ("exclude-private", "exclude-all"):
            _error()
        config["transfer"] = {"agentConfig": transfer.get("agentConfig", "exclude-private")}
    if "attachments" in data:
        attachments = _fields(data["attachments"], ("gitExclude",))
        config["attachments"] = {"gitExclude": _flag(attachments.get("gitExclude", True))}
    if "usage" in data:
        config["usage"] = _usage(data["usage"], home)
    if "launcher" in data:
        config["launcher"] = _launcher(data["launcher"], home)


def _launcher(value, home):
    """The command a board terminal runs for "새 세션": this host's own launcher, which then
    chooses the project, agent and account itself. Its program is an absolute or `~/` path."""
    launcher = _fields(value, ("command", "label", "cwd"))
    command = launcher.get("command")
    if (not isinstance(command, list) or not 0 < len(command) <= 16
            or any(not _text(arg, 1024) for arg in command)):
        _error()
    label = launcher.get("label", "새 세션")
    if not _text(label, 64):
        _error()
    result = {"command": [_location(command[0], home)] + list(command[1:]), "label": label}
    if "cwd" in launcher:
        result["cwd"] = _location(launcher["cwd"], home)
    return result


def _usage(value, home):
    usage = _fields(value, ("provider", "resources", "python", "path", "codex", "codexHelper", "codexAccounts",
                            "claude", "claudeHelper", "intervalSeconds", "manualIntervalSeconds"))
    if usage.get("provider") != "helper":
        _error()
    result = {"provider": "helper", "resources": _location(usage.get("resources"), home),
              "python": _location(usage.get("python"), home),
              "intervalSeconds": _seconds(usage.get("intervalSeconds", 300), 60, 86400),
              "manualIntervalSeconds": _seconds(usage.get("manualIntervalSeconds", 20), 5, 3600)}
    path = usage.get("path", "/usr/bin:/bin")
    if not _text(path, 4096) or any(not part.startswith("/") or ".." in part.split("/") for part in path.split(":")):
        _error()
    result["path"] = path
    codex = {"codex", "codexHelper", "codexAccounts"} & set(usage)
    if codex:
        accounts = usage.get("codexAccounts")
        if (codex != {"codex", "codexHelper", "codexAccounts"} or not isinstance(accounts, list)
                or not 0 < len(accounts) <= 8):
            _error()
        for account in accounts:
            _fields(account, ("key", "label"))
            if not isinstance(account.get("key"), str) or not ACCOUNT.fullmatch(account["key"]) or not _text(account.get("label")):
                _error()
        if len({account["key"] for account in accounts}) != len(accounts):
            _error()
        result.update(codex=_location(usage["codex"], home), codexHelper=_helper(usage["codexHelper"]),
                      codexAccounts=[{"key": a["key"], "label": a["label"]} for a in accounts])
    claude = {"claude", "claudeHelper"} & set(usage)
    if claude:
        if claude != {"claude", "claudeHelper"}:
            _error()
        result.update(claude=_location(usage["claude"], home), claudeHelper=_helper(usage["claudeHelper"]))
    if not codex and not claude:
        _error()
    return result


def _helper(value):
    if not isinstance(value, str) or not HELPER.fullmatch(value):
        _error()
    return value


def _prefix_match(prefix, key):
    """`prefix` itself, or `prefix` followed by `-` or `/` (".codex-personal" ≠ ".codex-personalx")."""
    if key == prefix:
        return True
    if not key.startswith(prefix):
        return False
    return prefix[-1] in "-/" or key[len(prefix)] in "-/"


def profile(agent, key, home=None, config=None):
    """The explicit `profiles` row of one home ({} when it is not listed)."""
    config = config if config is not None else load(home)
    return next((p for p in config["profiles"] if p["agent"] == agent and p["home"] == key), {})


def resolve_profile(agent, key, home=None, config=None):
    """Explicit row fields, else the first matching `profileRules` entry, else the default group.

    The result has `environment` and, when known, `label`, `title` and `project`.
    """
    config = config if config is not None else load(home)
    result = {"environment": "default"}
    rule = next((r for r in config["profileRules"]
                 if r["agent"] == agent and isinstance(key, str) and _prefix_match(r["homePrefix"], key)), None)
    if rule is not None:
        result["environment"] = rule.get("environment", "default")
        suffix = key[len(rule["homePrefix"]):].lstrip("-/")
        if rule.get("suffixLabel") and suffix:
            result["label"] = suffix
        elif "label" in rule:
            result["label"] = rule["label"]
    row = profile(agent, key, home, config)
    result.update({name: row[name] for name in ("environment", "label", "title", "project") if name in row})
    return result


def boundary_group(group, config):
    """The group an environment is compared under (`environments.<g>.boundaryGroup`)."""
    return config["environments"].get(group, {}).get("boundaryGroup", group)


def crosses_boundary(target_agent, source_group, target_group, config):
    """True when this host keeps these two environments apart for a target of this agent.

    `boundary.agents` lists the target agents that are checked (all by default). An agent
    outside the list starts with the source's environment variables (`claude: inherit-source`).
    """
    if target_agent not in config["boundary"]["agents"]:
        return False
    return boundary_group(source_group, config) != boundary_group(target_group, config)


def legacy_environment(value, config):
    """An older transfer record's boolean `sourcePersonal`, read through `legacy.sourcePersonal`."""
    mapping = config["legacy"].get("sourcePersonal")
    if type(value) is not bool or not mapping:
        raise ValueError("이전 기록에서 원본 실행 환경를 확인하지 못했습니다.")
    return mapping["true" if value else "false"]


def _project_environment(cwd, config):
    """A project boundary for this path: the longest root, then the first component name."""
    matches = [p for p in config["projects"] if "environment" in p and p.get("match") != "component" and
               (cwd == p["root"] or cwd.startswith(p["root"].rstrip("/") + "/"))]
    if matches:
        return max(matches, key=lambda p: len(p["root"])).get("environment", "default")
    parts = cwd.split("/")
    return next((p["environment"] for p in config["projects"]
                 if "environment" in p and p.get("match") == "component" and p["root"] in parts), None)


def priority_environment(source, home=None, config=None, roots=True):
    """`boundary.priority` when any signal names that group, else None.

    Signals: the source home's profile row or rule, every component project on the path,
    and (with `roots`) every root project containing it. One signal is enough, and it is
    decided before the longest-root order and before an imported workspace's transfer
    record, which is then not read at all. `source["cwd"]` must already be resolved.
    """
    config = config if config is not None else load(home)
    priority = config["boundary"].get("priority")
    if priority is None:
        return None
    cwd = source.get("cwd") if isinstance(source.get("cwd"), str) else ""
    parts = cwd.split("/")
    for p in config["projects"]:
        if p.get("environment") != priority:
            continue
        if p.get("match") == "component":
            if p["root"] in parts:
                return priority
        elif roots and (cwd == p["root"] or cwd.startswith(p["root"].rstrip("/") + "/")):
            return priority
    if resolve_profile(source.get("agent"), source.get("home") or ".claude", home, config)["environment"] == priority:
        return priority
    return None


def source_environment(source, home=None, config=None):
    config = config if config is not None else load(home)
    home = Path(home) if home is not None else Path.home()
    cwd = _path(source.get("cwd"), home)
    found = priority_environment({**source, "cwd": cwd}, home, config)
    if found is not None:
        return found
    found = _project_environment(cwd, config)
    if found is not None:
        return found
    return resolve_profile(source.get("agent"), source.get("home") or ".claude", home, config)["environment"]


def quick_environment(source, home=None, config=None):
    """Another host's source, by names only: component projects on the path as written,
    then the home's profile rules. Root projects name this host's folders and are skipped."""
    config = config if config is not None else load(home)
    cwd = source.get("cwd") if isinstance(source.get("cwd"), str) else ""
    found = priority_environment({**source, "cwd": cwd}, home, config, roots=False)
    if found is not None:
        return found
    parts = cwd.split("/")
    found = next((p["environment"] for p in config["projects"]
                  if "environment" in p and p.get("match") == "component" and p["root"] in parts), None)
    if found is not None:
        return found
    return resolve_profile(source.get("agent"), source.get("home") or ".claude", home, config)["environment"]


def variables(group, home=None):
    config = load(home)
    if not isinstance(group, str) or group not in config["environments"]:
        raise ValueError("이 호스트에 등록되지 않은 실행 환경입니다.")
    home = Path(home) if home is not None else Path.home()
    return {key: str(home / value[2:]) if value.startswith("~/") else value
            for key, value in config["environments"][group]["variables"].items()}
