"""What each "새 세션" terminal ended up running on this host, read from the process table.

A 새 세션 terminal runs this host's launcher (the host settings' `launcher`). Its launcher
process starts when the terminal opens, so the board's creation time names it; the first
agent CLI below it is the program: Claude's agent view (`claude agents`, with its --cwd),
Claude, or Codex. Only `ps` is read; nothing is signalled. A terminal whose launcher cannot
be named exactly (none, or two within the window) reports no program rather than a guess.
"""
import os
from pathlib import Path
import re
import subprocess
import time

# The launcher's process starts a little after the board created the terminal (SSH setup).
BEFORE, AFTER = 3, 15
MAX_TERMINALS = 32
PS = "/bin/ps"
LSTART = "%a %b %d %H:%M:%S %Y"
# Claude's own helpers under the agent view, never a program a person runs.
CLAUDE_HELPERS = {"daemon", "bg-pty-host", "bg-spare", "--chrome-native-host", "mcp", "doctor"}


def table(runner=None):
    """[(pid, ppid, started_epoch, argv_text)] from `ps`, in the C locale."""
    command = [PS, "-axww", "-o", "pid=,ppid=,lstart=,command="]
    if runner is None:
        done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
        if done.returncode != 0:
            raise RuntimeError("프로세스 목록을 읽지 못했습니다.")
        text = done.stdout.decode("utf-8", "replace")
    else:
        text = runner(command)
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4})\s+(.*)$", line)
        if not match:
            continue
        try:
            started = time.mktime(time.strptime(re.sub(r"\s+", " ", match[3]), LSTART))
        except ValueError:
            continue
        rows.append((int(match[1]), int(match[2]), int(started), match[4].strip()))
    return rows


def program_of(command):
    """{"program", "cwd"} for an agent CLI's command line, else None."""
    words = command.split()
    if not words:
        return None
    name = Path(words[0]).name
    rest = words[1:]
    if name == "node" and rest and Path(rest[0]).name in ("codex", "codex.js"):
        name, rest = "codex", rest[1:]
    if name == "claude":
        if rest[:1] == ["agents"]:
            cwd = None
            for index, word in enumerate(rest):
                if word == "--cwd" and index + 1 < len(rest):
                    cwd = rest[index + 1]
                elif word.startswith("--cwd="):
                    cwd = word.split("=", 1)[1]
            return {"program": "claude-agents", "cwd": cwd if cwd and cwd.startswith("/") else None}
        if rest[:1] and rest[0] in CLAUDE_HELPERS:
            return None
        return {"program": "claude", "cwd": None}
    if name == "codex":
        if rest[:2] == ["app-server", "daemon"] or rest[:1] == ["app-server"]:
            return None
        return {"program": "codex", "cwd": None}
    return None


def describe(terminals, launcher, rows):
    """{terminal id: {"program", "cwd", "pid"} or {"program": None}} for the given terminals."""
    expected = " ".join(launcher)
    children = {}
    for pid, ppid, _, _ in rows:
        children.setdefault(ppid, []).append(pid)
    by_pid = {pid: (ppid, started, command) for pid, ppid, started, command in rows}
    launchers = [(pid, started) for pid, _, started, command in rows if command == expected]
    result = {}
    for terminal in terminals[:MAX_TERMINALS]:
        identifier, created = terminal.get("id"), terminal.get("createdAt")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier) or type(created) is not int:
            continue
        matches = [pid for pid, started in launchers if created - BEFORE <= started <= created + AFTER]
        if len(matches) != 1:
            result[identifier] = {"program": None}
            continue
        found, queue, seen = None, [matches[0]], set()
        while queue and found is None:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if pid != matches[0]:
                found = program_of(by_pid[pid][2])
                if found is not None:
                    found = {**found, "pid": pid}
                    break
            queue.extend(sorted(children.get(pid, [])))
        result[identifier] = found or {"program": None}
    return result


def launcher_programs(args):
    from launch import new_session
    try:
        launcher = new_session()["argv"]
    except ValueError:
        return {"terminals": {}}
    terminals = args.get("terminals") if isinstance(args, dict) else None
    if not isinstance(terminals, list):
        raise ValueError("터미널 목록 형식이 올바르지 않습니다.")
    if os.name == "nt":
        return {"terminals": {}}
    return {"terminals": describe(terminals, launcher, table())}
