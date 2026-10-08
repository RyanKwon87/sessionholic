#!/usr/bin/env python3
"""Sessionholic setup utilities. No agent login or remote command is performed."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LABEL = "io.github.sessionholic"


def paths(home=None):
    home = Path(home) if home is not None else Path.home()
    return home, home / ".config/sessionholic", home / ".local/state/sessionholic"


def private_directory(path, home):
    relative = path.relative_to(home)
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("설정 경로에 심볼릭 링크가 있습니다. 경로를 확인해 주세요.")
        if current.exists() and not current.is_dir():
            raise ValueError("설정 폴더 대신 파일이 있습니다. 경로를 확인해 주세요.")
        current.mkdir(mode=0o700, exist_ok=True)


def write_new(path, data):
    if path.exists() or path.is_symlink():
        raise ValueError("기존 파일을 덮어쓰지 않습니다. 현재 파일을 먼저 확인해 주세요.")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


def initialize(home=None):
    home, config, _ = paths(home)
    private_directory(config, home)
    defaults = {
        "config.json": {"version": 1, "profiles": [], "projects": [], "environments": {}},
        "hosts.json": [{"name": "local", "label": "이 컴퓨터", "local": True}],
    }
    for name in defaults:
        path = config / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("기존 설정 파일의 종류를 확인해 주세요. 파일을 덮어쓰지 않았습니다.")
    created = []
    for name, value in defaults.items():
        path = config / name
        if not path.exists():
            write_new(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
            created.append(name)
    return created


def diagnose(home=None):
    import settings
    import server
    home, config, state = paths(home)
    checks = []

    def check(name, status, message):
        checks.append({"name": name, "status": status, "message": message})

    check("python", "ok" if sys.version_info >= (3, 9) else "error",
          "Python 3.9 이상이 필요합니다.")
    check("platform", "ok" if sys.platform == "darwin" else "warning",
          "첫 공개판의 지원 대상은 macOS입니다. 다른 POSIX 환경은 미검증입니다.")
    for binary in ("tmux", "git", "ssh"):
        found = bool(shutil.which(binary))
        check(binary, "ok" if found else "error",
              binary + (" 실행 파일을 찾았습니다." if found else "를 설치하거나 PATH에 등록해 주세요."))
    agent_found = any(shutil.which(name) for name in ("codex", "claude"))
    check("agent", "ok" if agent_found else "warning",
          "설치·로그인한 Codex 또는 Claude Code가 필요합니다. 이 명령은 로그인하지 않습니다.")
    # Match the startup path checks without creating state, reading credentials,
    # acquiring locks, or changing permissions on an existing installation.
    for name, path, directory, advice in (
        ("config-dir", config, True, "~/.config/sessionholic 폴더의 소유자·권한을 확인하세요(700)."),
        ("token-file", config / "token", False, "접속 토큰 파일의 종류·소유자·권한을 확인하세요(600). 내용은 출력하지 않습니다."),
        ("state-dir", state, True, "~/.local/state/sessionholic 폴더의 소유자·권한을 확인하세요(700)."),
        ("snapshot-file", state / "snapshot.json", False, "snapshot.json 파일의 종류·소유자·권한을 확인하세요(600)."),
        ("conversations-file", state / "conversations.json", False, "conversations.json 파일의 종류·소유자·권한을 확인하세요(600)."),
        ("terminal-dir", state / "terminals", True, "terminals 폴더의 종류·소유자·권한을 확인하세요(700)."),
        ("terminal-input-dir", state / "terminal-inputs", True, "terminal-inputs 폴더의 종류·소유자·권한을 확인하세요(700)."),
        ("terminal-input-file", state / "terminal-inputs/receipts.sqlite3", False, "터미널 입력 기록 파일의 종류·소유자·읽기·쓰기 권한을 확인하세요(600)."),
        ("terminal-input-journal", state / "terminal-inputs/receipts.sqlite3-journal", False, "터미널 입력 기록 journal의 종류·소유자·권한을 확인하세요(600)."),
    ):
        try:
            settings.safe_path(path, directory=directory, private=True,
                               allow_missing=True, home=home)
            if directory:
                # For a new directory, its closest existing parent must allow
                # creation. A read-only config is valid if its token exists.
                existing = path
                while not existing.exists():
                    existing = existing.parent
                access = os.R_OK | os.X_OK
                if (existing != path or name != "config-dir"
                        or not (config / "token").exists()):
                    access |= os.W_OK
                if not os.access(existing, access):
                    raise ValueError("startup directory is not accessible")
            elif path.exists() and not os.access(path, os.R_OK | (os.W_OK if name.startswith("terminal-input-") else 0)):
                raise ValueError("startup file is not readable")
            if name == "token-file" and path.exists() and path.stat().st_size == 0:
                check(name, "error", "접속 토큰 파일이 비어 있습니다. 기존 토큰을 확인·복구하세요. 자동 교체하지 않습니다.")
            else:
                check(name, "ok", "경로·권한 검사 통과. 파일 내용은 읽지 않았습니다."
                      if path.exists() else "첫 실행 시 준비할 경로입니다.")
        except (ValueError, OSError):
            check(name, "error", advice + " 상위 폴더 접근 권한도 필요하며 심볼릭 링크는 사용할 수 없습니다.")
    try:
        settings.load(home=home)
        check("config", "ok", "사용자 설정 형식이 올바릅니다. 설정 값은 출력하지 않습니다.")
    except (ValueError, OSError):
        check("config", "error", "config.json 형식·경로·권한을 확인해 주세요.")
    try:
        path = settings.safe_path(config / "hosts.json", allow_missing=True, home=home)
        if not path.exists():
            check("hosts", "ok", "기기 설정이 없어 이 컴퓨터 한 대로 시작합니다.")
        else:
            hosts = server.load_hosts(path)
            check("hosts", "ok", "기기 설정 형식이 올바릅니다. SSH 접속은 실행하지 않았습니다.")
            if not hosts:
                check("host-count", "error", "기기를 한 대 이상 등록해 주세요.")
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        check("hosts", "error", "hosts.json에 유효한 기기 목록을 넣어 주세요.")
    return checks


def service_file(port=8790, tailscale_user_file=None, home=None):
    import server
    home, config, state = paths(home)
    if sys.platform != "darwin":
        raise ValueError("LaunchAgent 설치 파일은 macOS에서만 만들 수 있습니다.")
    if any(item["status"] == "error" for item in diagnose(home)):
        raise ValueError("doctor의 오류를 해결한 뒤 백그라운드 실행을 설정해 주세요.")
    folder = home / "Library/LaunchAgents"
    private_directory(folder, home)
    private_directory(state, home)
    output = folder / (LABEL + ".plist")
    args = [sys.executable, str(ROOT / "server.py"), "--port", str(port),
            "--hosts", str(config / "hosts.json"), "--state-dir", str(state)]
    if tailscale_user_file:
        path = Path(tailscale_user_file).expanduser().absolute()
        server.load_tailscale_user(path)
        args.extend(["--tailscale-user-file", str(path)])
    search = [str(home / ".local/bin")]
    for tool in ("codex", "claude", "node", "tmux", "git", "ssh"):
        binary = shutil.which(tool)
        if binary and Path(binary).is_absolute():
            search.append(str(Path(binary).parent))
    search.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    search = list(dict.fromkeys(search))
    payload = {"Label": LABEL, "ProgramArguments": args,
               "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True,
               "ThrottleInterval": 15, "Umask": 0o077,
               "EnvironmentVariables": {"PATH": os.pathsep.join(search)},
               "StandardOutPath": str(state / "service.stdout.log"),
               "StandardErrorPath": str(state / "service.stderr.log")}
    write_new(output, plistlib.dumps(payload))
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="세션홀릭 - 에이전트 세션 매니저")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="기존 설정을 보존하며 사용자 설정 파일 생성")
    doctor = commands.add_parser("doctor", help="로컬 설치 조건·설정 검사 (SSH·모델 호출 없음)")
    doctor.add_argument("--json", action="store_true")
    commands.add_parser("serve", help="서버 시작; 뒤에 server.py 옵션을 붙일 수 있음", add_help=False)
    service = commands.add_parser("service-file", help="macOS 백그라운드 실행 파일 생성 (자동 실행 없음)")
    service.add_argument("--port", type=int, default=8790)
    service.add_argument("--tailscale-user-file", type=Path)
    cleanup = commands.add_parser("cleanup-transfer", help="이전 한 건의 남은 임시 압축 파일 정리 (등록 기기에 연결)")
    cleanup.add_argument("transfer_id", metavar="TRANSFER_ID")
    cleanup.add_argument("--hosts", type=Path)
    cleanup.add_argument("--state-dir", type=Path)
    args, remaining = parser.parse_known_args(argv)
    if remaining and args.command != "serve":
        parser.error("알 수 없는 옵션입니다.")
    try:
        if args.command == "init":
            created = initialize()
            print("설정 준비 완료. " + (", ".join(created) + " 생성." if created else "기존 설정을 유지했습니다."))
            print("다음: python3 scripts/sessionholic.py doctor")
        elif args.command == "doctor":
            checks = diagnose()
            if args.json:
                print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
            else:
                for item in checks:
                    print("[" + item["status"] + "] " + item["name"] + ": " + item["message"])
                if not any(item["status"] == "error" for item in checks):
                    print("다음: python3 scripts/sessionholic.py serve")
            return 1 if any(item["status"] == "error" for item in checks) else 0
        elif args.command == "serve":
            import server
            if remaining[:1] == ["--"]:
                remaining = remaining[1:]
            return server.main(remaining)
        elif args.command == "cleanup-transfer":
            import server
            from transfer import Transfers
            transfers = Transfers(server.load_hosts(args.hosts or server.DEFAULT_HOSTS),
                                  args.state_dir or server.STATE_DIR)
            result = transfers.cleanup_pending(args.transfer_id)
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result.get("archiveCleanupPending") else 0
        elif args.command == "service-file":
            if not 1 <= args.port <= 65535:
                raise ValueError("포트는 1~65535 범위여야 합니다.")
            path = service_file(args.port, args.tailscale_user_file)
            print("백그라운드 실행 파일을 만들었습니다. 아직 서비스를 시작하지 않았습니다.")
            print("시작: launchctl bootstrap gui/$(id -u) " + shlex.quote(str(path)))
            print("중지: launchctl bootout gui/$(id -u)/" + LABEL)
        return 0
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("파일을 준비하지 못했습니다. 기존 파일과 경로 권한을 확인해 주세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
