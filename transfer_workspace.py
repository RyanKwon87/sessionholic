"""Read-only workspace export and verified, atomic import (Python 3.9+).

No source Git configuration, hooks, credentials, or tar links are transported.
The receiver creates a new repository from a local bundle; no network is used.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit, unquote

MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 20000
SCHEMA_VERSION = 1
_DEFAULT_EXCLUDED = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                     ".mypy_cache", ".ruff_cache", ".next", "dist", "build", ".DS_Store"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class WorkspaceTransferError(ValueError):
    """The workspace cannot be safely transported."""


def _fail(message):
    raise WorkspaceTransferError(message)


def _safe_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value
            or "\x00" in value or value.startswith("/")):
        _fail("번들 파일 경로가 안전하지 않습니다.")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail("번들 파일 경로가 안전하지 않습니다.")
    return value


def _sensitive(value):
    for part in PurePosixPath(value).parts:
        name = part.lower()
        if name in (".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"):
            return True
        if name.startswith(".codex") or name == ".claude":
            return True
        if name == ".env" or name.startswith(".env."):
            if name.split(".")[-1] not in ("example", "examples", "sample", "template", "dist"):
                return True
        if (name in ("auth.json", ".credentials.json", "credentials.json", "credentials",
                     "token", "tokens", "id_rsa", "id_ed25519", ".npmrc", ".netrc", ".pypirc",
                     ".git-credentials", ".gitconfig", ".claude.json", "secrets.json", "secret.json")
                or re.fullmatch(r"(?:access[_-]?|refresh[_-]?|api[_-]?)?tokens?(?:\.(?:json|txt|yaml|yml|key))?", name)
                or re.fullmatch(r"(?:api[_-]?key|client[_-]?secret)(?:\.(?:json|txt|yaml|yml))?", name)
                or name.startswith("service-account") and name.endswith(".json")
                or name.endswith((".pem", ".p12", ".pfx", ".key"))):
            return True
    return False


def _git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return env


def _git(root, *args, allow_failure=False, timeout=30):
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-C", str(root)] + list(args)
    try:
        done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_git_env(), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        _fail("Git 작업을 완료하지 못했습니다.")
    if len(done.stdout) > MAX_BYTES:
        _fail("Git 데이터가 전송 크기 한도를 초과했습니다.")
    if done.returncode and not allow_failure:
        # Git stderr can contain private configuration or remote URLs.
        _fail("Git 작업을 완료하지 못했습니다.")
    return done


def _text(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("UTF-8이 아닌 파일 이름은 전송할 수 없습니다.")


def _safe_remote(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        return False
    decoded = unquote(url).lower()
    if any(fragment in decoded for fragment in ("ghp_", "github_pat_", "sk-proj-", "sk-ant-", "token=", "password=")):
        return False
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", url):
        return True
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ("https", "http", "ssh", "git") and bool(parsed.hostname)
                and parsed.password is None and (parsed.username is None or
                     (parsed.scheme == "ssh" and parsed.username == "git"))
                and not parsed.query and not parsed.fragment and bool(parsed.path))
    except ValueError:
        return False


def _git_info(root, head, with_patch):
    tracked = _text(_git(root, "ls-files", "--cached", "-z").stdout).split("\x00")[:-1]
    if any(_sensitive(path) for path in tracked):
        _fail("Git 추적 파일에 인증·비밀 파일이 있어 전송을 거절했습니다.")
    if _git(root, "ls-files", "--unmerged", "-z").stdout:
        _fail("충돌 중인 Git index는 먼저 해결한 뒤 전송해 주세요.")
    stage_data = _git(root, "ls-files", "--stage", "-z").stdout
    stages = stage_data.split(b"\x00")
    if any(row.startswith(b"160000 ") for row in stages):
        _fail("Git submodule은 별도 이전이 필요합니다.")
    if head:
        # Names of every change in HEAD's reachable history, including deleted
        # and renamed credential paths. Never inspect or print their contents.
        history = _text(_git(root, "log", "--format=", "--name-only", "-z", head, "--").stdout)
        if any(_sensitive(path) for path in history.split("\x00") if path):
            _fail("HEAD Git 이력에 인증·비밀 파일이 있어 번들 전송을 거절했습니다.")
    others = _text(_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout).split("\x00")[:-1]
    branch_result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    branch = _text(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    if branch:
        _git(root, "check-ref-format", "refs/heads/" + branch)
    warnings, remotes = [], []
    for name in _text(_git(root, "remote").stdout).splitlines():
        url = _text(_git(root, "remote", "get-url", "--", name).stdout).strip()
        if _NAME.fullmatch(name) and _safe_remote(url):
            remotes.append({"name": name, "url": url})
        else:
            warnings.append("인증 정보가 포함되거나 지원하지 않는 remote URL을 제외했습니다.")
    patch = b""
    if with_patch:
        args = ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv"]
        if head:
            args.append(head)
        args.append("--")
        patch = _git(root, *args).stdout
    return {"head": head, "branch": branch, "remotes": remotes,
            "objectFormat": _text(_git(root, "rev-parse", "--show-object-format").stdout).strip(),
            "indexStateSha256": hashlib.sha256(stage_data).hexdigest()}, tracked, others, warnings, patch


def _root(cwd):
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        _fail("전송할 작업 폴더의 절대 경로가 필요합니다.")
    home = Path.home().absolute().resolve()
    managed_root = home / ".local/share/sessionholic/workspaces"
    _check_managed_parents(path.absolute(), home)
    path = path.resolve()
    if _sensitive(path.name):
        _fail("인증·Git 메타데이터 폴더는 전송할 수 없습니다.")
    blocked = [Path("/"), home, Path("/Users"), Path("/home"), Path("/tmp"), Path("/private/tmp")]
    system = ("/System", "/Library", "/Applications", "/bin", "/sbin", "/usr", "/etc", "/var", "/private/etc", "/private/var")
    temporary_root = Path(tempfile.gettempdir()).resolve()
    is_temporary_child = temporary_root in path.parents
    if path in blocked or path == temporary_root or (not is_temporary_child and
            any(str(path) == p or str(path).startswith(p + "/") for p in system)):
        _fail("홈·시스템 루트는 작업 폴더로 전송할 수 없습니다.")
    managed_workspace = managed_root in path.parents and len(path.relative_to(managed_root).parts) >= 2
    if (not managed_workspace and any(path == home / p or (home / p) in path.parents for p in
           (".ssh", ".aws", ".claude", ".config", ".local", ".gnupg")) or any(
           ancestor.parent == home and ancestor.name.startswith(".codex") for ancestor in [path] + list(path.parents))):
        _fail("인증·설정 폴더는 전송할 수 없습니다.")
    found = _git(path, "rev-parse", "--show-toplevel", allow_failure=True)
    root = Path(_text(found.stdout).strip()).resolve() if found.returncode == 0 else path
    if root in blocked:
        _fail("홈·시스템 루트 저장소는 전송할 수 없습니다.")
    if managed_workspace and (managed_root not in root.parents or len(root.relative_to(managed_root).parts) < 2):
        _fail("관리 작업 폴더의 Git 루트가 허용 범위 밖에 있습니다.")
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        _fail("작업 폴더가 저장소 밖에 있습니다.")
    if _sensitive(relative):
        _fail("인증·Git 메타데이터 폴더는 전송할 수 없습니다.")
    return root, relative, found.returncode == 0


def _check_managed_parents(path, home):
    """Only the exact app-owned workspace subtree may bypass .local exclusion."""
    # macOS /var and /tmp are system aliases. Match both the caller's home
    # spelling and its canonical spelling, without resolving workspace links.
    matched_home = None
    for spelling in (home, Path.home().absolute()):
        managed = spelling / ".local/share/sessionholic/workspaces"
        if path == managed or managed in path.parents:
            matched_home = spelling
            break
    if matched_home is None:
        return
    for ancestor in [path] + list(path.parents):
        if os.path.lexists(ancestor):
            info = ancestor.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()):
                _fail("관리 작업 폴더의 상위 경로가 링크이거나 현재 사용자 소유가 아닙니다.")
        if ancestor == matched_home:
            break


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)


def _file_data(path, expected=None):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or (expected and _fingerprint(before) != expected):
                _fail("전송 준비 중 파일이 변경되었습니다.")
            if before.st_size > MAX_BYTES:
                _fail("파일이 전송 크기 한도를 초과했습니다.")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_BYTES or _fingerprint(before) != _fingerprint(after):
            _fail("전송 준비 중 파일이 변경되었거나 크기 한도를 초과했습니다.")
        return data, _fingerprint(after)
    except OSError:
        _fail("전송할 파일을 안전하게 읽지 못했습니다.")


def _symlink_target(root, path, target):
    if not target or "\x00" in target or "\\" in target or os.path.isabs(target):
        _fail("심볼릭 링크가 안전하지 않습니다.")
    try:
        resolved = (path.parent / target).resolve()
        relative = resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        _fail("작업 폴더 밖으로 나가는 심볼릭 링크는 전송할 수 없습니다.")
    if _sensitive(relative):
        _fail("인증·Git 메타데이터를 가리키는 링크는 전송할 수 없습니다.")
    return target


def _reject_special_files(root):
    # Git deliberately omits untracked FIFOs/devices from ls-files. Inspect
    # their metadata without opening them, pruning known excluded directories.
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if _sensitive(path.relative_to(root).as_posix()) or name in _DEFAULT_EXCLUDED or path.is_symlink():
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            if _sensitive(path.relative_to(root).as_posix()) or name in _DEFAULT_EXCLUDED:
                continue
            count += 1
            if count > MAX_FILES:
                _fail("작업 파일 수가 전송 한도를 초과했습니다.")
            try:
                mode = path.lstat().st_mode
            except OSError:
                _fail("전송 준비 중 파일이 변경되었습니다.")
            if not stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                _fail("FIFO·장치·소켓 파일은 전송할 수 없습니다.")


def _managed_attachments(root):
    """Validate and explicitly carry managed uploads that Git may ignore."""
    from attachments import DIRECTORY, AttachmentError, workspace_files
    roots, names, total = [], set(), 0
    for directory, dirs, unused in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if name == DIRECTORY:
                dirs.remove(name)
                try:
                    files = workspace_files(path.parent)
                except (AttachmentError, OSError):
                    _fail("이전할 첨부 저장 경로 또는 검증값이 안전하지 않습니다.")
                total += sum((path.parent / filename).lstat().st_size for filename in files)
                if total > MAX_BYTES:
                    _fail("첨부 파일 총 크기가 이전 한도를 초과했습니다.")
                parent_relative = path.parent.relative_to(root).as_posix()
                if files:
                    roots.append(parent_relative)
                names.update((path.parent / filename).relative_to(root).as_posix() for filename in files)
            elif _sensitive(relative) or name in _DEFAULT_EXCLUDED or path.is_symlink():
                dirs.remove(name)
        # os.walk reports symlinks-to-files and corrupt non-directory roots as files.
        if DIRECTORY in unused:
            _fail("이전할 첨부 저장 경로가 폴더가 아닙니다.")
    return sorted(roots), names


def _snapshot(cwd, hash_files=False):
    root, cwd_relative, is_git = _root(cwd)
    git_info, patch, warnings, tracked, excluded = None, b"", [], [], []
    if is_git:
        _reject_special_files(root)
        head_done = _git(root, "rev-parse", "--verify", "HEAD", allow_failure=True)
        head = _text(head_done.stdout).strip() if head_done.returncode == 0 else None
        git_info, tracked, others, warnings, patch = _git_info(root, head, hash_files)
        ignored = _text(_git(root, "ls-files", "--others", "--ignored", "--exclude-standard",
                            "--directory", "-z").stdout).split("\x00")[:-1]
        excluded.extend(ignored)
        candidates = set(tracked)
        for name in others:
            if _sensitive(name) or any(p in _DEFAULT_EXCLUDED for p in PurePosixPath(name).parts):
                excluded.append(name)
            else:
                candidates.add(name)
        excluded.append(".git")
    else:
        candidates = set()
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in list(dirs):
                path = Path(directory) / name
                rel = path.relative_to(root).as_posix()
                if _sensitive(rel) or name in _DEFAULT_EXCLUDED:
                    dirs.remove(name)
                    excluded.append(rel)
                elif path.is_symlink():
                    dirs.remove(name)
                    candidates.add(rel)
            for name in files:
                rel = (Path(directory) / name).relative_to(root).as_posix()
                if _sensitive(rel) or name in _DEFAULT_EXCLUDED:
                    excluded.append(rel)
                else:
                    candidates.add(rel)
            if len(candidates) > MAX_FILES:
                _fail("작업 파일 수가 전송 한도를 초과했습니다.")
    attachment_roots, attachment_files = _managed_attachments(root)
    candidates.update(attachment_files)
    # Validated uploads are included despite their Git ignore rule.
    excluded = [name for name in excluded if not any(
        name.rstrip("/") == (PurePosixPath(parent) / ".sessionholic-attachments").as_posix()
        for parent in attachment_roots)]
    if len(candidates) > MAX_FILES:
        _fail("작업 파일 수가 전송 한도를 초과했습니다.")
    entries, fingerprints, deleted, size = [], {}, [], 0
    if is_git and git_info["head"]:
        head_paths = _text(_git(root, "ls-tree", "-r", "--name-only", "-z", git_info["head"]).stdout).split("\x00")[:-1]
        deleted.extend(name for name in head_paths if name not in candidates)
    for name in sorted(candidates):
        _safe_path(name)
        path = root / name
        try:
            if not path.parent.resolve().is_relative_to(root):
                _fail("작업 파일 경로가 폴더 밖으로 나갑니다.")
            info = path.lstat()
        except FileNotFoundError:
            if name in tracked:
                deleted.append(name)
                continue
            _fail("전송 준비 중 파일이 변경되었습니다.")
        except OSError:
            _fail("작업 파일 정보를 읽지 못했습니다.")
        if stat.S_ISLNK(info.st_mode):
            original = os.readlink(path)
            target = _symlink_target(root, path, original)
            data = target.encode("utf-8")
            entry = {"path": name, "kind": "symlink", "target": target, "mode": 0o777,
                     "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            fingerprints[name] = (_fingerprint(info), original)
        elif stat.S_ISREG(info.st_mode):
            entry = {"path": name, "kind": "file", "mode": stat.S_IMODE(info.st_mode) & 0o777,
                     "size": info.st_size}
            fingerprints[name] = (_fingerprint(info), None)
            if hash_files:
                data, signature = _file_data(path, _fingerprint(info))
                entry["sha256"] = hashlib.sha256(data).hexdigest()
        else:
            _fail("FIFO·장치·소켓 파일은 전송할 수 없습니다.")
        size += entry["size"]
        if size > MAX_BYTES:
            _fail("작업 파일 크기가 전송 한도를 초과했습니다.")
        entries.append(entry)
    summary = {"root": str(root), "projectName": root.name, "cwdRelative": cwd_relative,
               "git": is_git, "head": git_info["head"] if git_info else None,
               "branch": git_info["branch"] if git_info else None,
               "fileCount": len(entries), "bytes": size, "excluded": sorted(set(excluded)),
               "warnings": warnings, "attachmentCount": len(attachment_files) // 2}
    # This local revision deliberately uses inode/ctime as well as mtime/size:
    # an inexpensive read-only inspect can detect post-export writes, deletes,
    # replacements, permissions, symlinks, HEAD, and staged index transitions.
    # It is a source freshness token, not a cross-machine file-content hash.
    revision = {"git": git_info, "files": fingerprints, "deleted": deleted,
                "cwdRelative": cwd_relative, "excluded": summary["excluded"],
                "attachmentRoots": attachment_roots}
    summary["fingerprint"] = hashlib.sha256(json.dumps(revision, sort_keys=True,
                                                       ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"summary": summary, "git": git_info, "entries": entries, "deleted": deleted,
            "fingerprints": fingerprints, "patch": patch, "attachmentRoots": attachment_roots}


def inspect_workspace(cwd):
    """Inspect without changing files, index, configuration, or running models."""
    return _snapshot(cwd)["summary"]


def _sha_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_workspace(cwd, output_dir):
    """Export a stable snapshot; refuse any included file/index/HEAD change."""
    snapshot = _snapshot(cwd, True)
    root = Path(snapshot["summary"]["root"])
    output = Path(output_dir).expanduser().absolute()
    if output == root or root in output.parents:
        _fail("번들 출력 폴더는 원본 작업 폴더 밖에 두어 주세요.")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".workspace-export-", dir=output) as temporary:
        staging = Path(temporary)
        payload = staging / "payload"
        payload.mkdir()
        for entry in snapshot["entries"]:
            path = root / entry["path"]
            signature, original_link = snapshot["fingerprints"][entry["path"]]
            if entry["kind"] == "file":
                data, unused = _file_data(path, signature)
            else:
                if _fingerprint(path.lstat()) != signature or os.readlink(path) != original_link:
                    _fail("전송 준비 중 심볼릭 링크가 변경되었습니다.")
                data = entry["target"].encode("utf-8")
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                _fail("전송 준비 중 파일 내용이 변경되었습니다.")
            target = payload / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o600)
        git_manifest = None
        if snapshot["git"]:
            git_manifest = dict(snapshot["git"])
            git_dir = staging / "git"
            git_dir.mkdir()
            patch_path = git_dir / "index.patch"
            patch_path.write_bytes(snapshot["patch"])
            git_manifest["indexPatchSha256"] = _sha_file(patch_path)
            if git_manifest["head"]:
                bundle = git_dir / "history.bundle"
                # A revision hash alone is not a named bundle prerequisite; HEAD
                # includes the named ref while the final snapshot checks its value.
                _git(root, "bundle", "create", str(bundle), "HEAD", timeout=90)
                git_manifest["bundleSha256"] = _sha_file(bundle)
        manifest = {"schemaVersion": SCHEMA_VERSION, "projectName": root.name,
                    "cwdRelative": snapshot["summary"]["cwdRelative"], "git": git_manifest,
                    "entries": snapshot["entries"], "deleted": snapshot["deleted"],
                    "summary": snapshot["summary"], "attachmentRoots": snapshot["attachmentRoots"]}
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        archive = staging / "workspace.tar.gz"
        members = [manifest_path] + sorted(payload.rglob("*")) + sorted((staging / "git").glob("*"))
        total = sum(path.stat().st_size for path in members if path.is_file())
        if total > MAX_BYTES:
            _fail("Git 이력을 포함한 번들 크기가 전송 한도를 초과했습니다.")
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as stream:
            for path in members:
                if path.is_file():
                    stream.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
        if archive.stat().st_size > MAX_BYTES:
            _fail("압축 번들 크기가 전송 한도를 초과했습니다.")
        after = _snapshot(cwd, True)
        if after != snapshot:
            _fail("번들 생성 중 원본 작업 파일 또는 Git 상태가 변경되었습니다. 다시 시도해 주세요.")
        archive.chmod(0o600)
        destination = output / ("workspace-" + secrets.token_hex(16) + ".tar.gz")
        os.rename(archive, destination)
        return {"archivePath": str(destination), "sha256": _sha_file(destination),
                "bytes": destination.stat().st_size, "summary": snapshot["summary"], "attachmentRoots": snapshot["attachmentRoots"]}


def _read_archive(archive, staging):
    seen, count, total = set(), 0, 0
    try:
        with tarfile.open(archive, "r:gz") as stream:
            for member in stream:
                name = _safe_path(member.name)
                if not member.isfile() or member.pax_headers.get("linkpath") or name in seen:
                    _fail("번들 링크·특수 파일·중복 경로는 허용하지 않습니다.")
                count += 1
                total += member.size
                if count > MAX_FILES + 3 or member.size < 0 or total > MAX_BYTES:
                    _fail("번들이 파일 수 또는 해제 크기 한도를 초과했습니다.")
                seen.add(name)
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, open(target, "xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                target.chmod(0o600)
    except (tarfile.TarError, OSError, EOFError):
        _fail("번들을 안전하게 해제하지 못했습니다.")
    return seen


def _validate_manifest(manifest, members):
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SCHEMA_VERSION:
        _fail("지원하지 않는 번들 형식입니다.")
    project = manifest.get("projectName")
    if (not isinstance(project, str) or len(project) > 255 or project in ("", ".", "..")
            or "/" in project or "\\" in project or "\x00" in project or _sensitive(project)):
        _fail("번들 프로젝트 이름이 안전하지 않습니다.")
    if not isinstance(manifest.get("summary"), dict):
        _fail("번들 작업 요약이 올바르지 않습니다.")
    cwd_relative = manifest.get("cwdRelative")
    if cwd_relative != ".":
        _safe_path(cwd_relative)
        if _sensitive(cwd_relative):
            _fail("번들 작업 폴더가 인증·Git 메타데이터 경로입니다.")
    entries, deleted = manifest.get("entries"), manifest.get("deleted")
    if not isinstance(entries, list) or not isinstance(deleted, list) or len(entries) > MAX_FILES:
        _fail("번들 파일 목록이 올바르지 않습니다.")
    paths, expected = set(), {"manifest.json"}
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("번들 파일 정보가 올바르지 않습니다.")
        path = _safe_path(entry.get("path"))
        if path in paths or _sensitive(path):
            _fail("번들에 중복 또는 인증·비밀 파일 경로가 있습니다.")
        paths.add(path)
        expected.add("payload/" + path)
        if (entry.get("kind") not in ("file", "symlink") or type(entry.get("size")) is not int
                or not 0 <= entry["size"] <= MAX_BYTES or type(entry.get("mode")) is not int
                or not 0 <= entry["mode"] <= 0o777 or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", "")))):
            _fail("번들 파일 정보가 올바르지 않습니다.")
        if entry["kind"] == "symlink" and not isinstance(entry.get("target"), str):
            _fail("번들 링크 정보가 올바르지 않습니다.")
    attachment_roots = manifest.get("attachmentRoots", [])
    if not isinstance(attachment_roots, list) or len(attachment_roots) > MAX_FILES or any(not isinstance(parent, str) for parent in attachment_roots) or len(set(attachment_roots)) != len(attachment_roots):
        _fail("번들 첨부 저장 폴더 목록이 올바르지 않습니다.")
    prefixes = []
    for parent in attachment_roots:
        if parent != ".":
            _safe_path(parent)
            if _sensitive(parent):
                _fail("번들 첨부 폴더가 인증 경로입니다.")
        prefixes.append((PurePosixPath(parent) / ".sessionholic-attachments").as_posix() + "/")
    for entry in entries:
        if ".sessionholic-attachments" in PurePosixPath(entry["path"]).parts:
            if (not any(entry["path"].startswith(prefix) for prefix in prefixes)
                    or entry["kind"] != "file" or entry["mode"] != 0o400):
                _fail("번들 첨부 경로 또는 권한이 올바르지 않습니다.")
    if any(any(parent.as_posix() in paths for parent in PurePosixPath(path).parents if parent.as_posix() != ".") for path in paths):
        _fail("파일과 하위 경로가 충돌하는 번들입니다.")
    for path in deleted:
        _safe_path(path)
        if _sensitive(path) or path in paths:
            _fail("번들 삭제 경로가 올바르지 않습니다.")
    git = manifest.get("git")
    if git is not None:
        if not isinstance(git, dict):
            _fail("번들 Git 정보가 올바르지 않습니다.")
        head, branch = git.get("head"), git.get("branch")
        if git.get("objectFormat") not in ("sha1", "sha256"):
            _fail("지원하지 않는 Git object 형식입니다.")
        if head is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", str(head)):
            _fail("번들 Git HEAD가 올바르지 않습니다.")
        if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 1024):
            _fail("번들 Git branch가 올바르지 않습니다.")
        for field in ["indexPatchSha256", "indexStateSha256"] + (["bundleSha256"] if head else []):
            if not re.fullmatch(r"[a-f0-9]{64}", str(git.get(field, ""))):
                _fail("번들 Git 검증값이 올바르지 않습니다.")
        remotes = git.get("remotes", [])
        if not isinstance(remotes, list) or any(not isinstance(remote, dict) or not _NAME.fullmatch(str(remote.get("name", "")))
               or not _safe_remote(remote.get("url")) for remote in remotes):
            _fail("번들 remote URL이 안전하지 않습니다.")
        expected.add("git/index.patch")
        if head:
            expected.add("git/history.bundle")
    if members != expected:
        _fail("번들 파일 목록과 manifest가 일치하지 않습니다.")
    return manifest


def import_workspace(archive_path, destination_root, transfer_id, expected_sha256):
    """Verify then install at a new destination_root/id/project atomically."""
    if not isinstance(transfer_id, str) or not _ID.fullmatch(transfer_id):
        _fail("이전 작업 ID가 올바르지 않습니다.")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        _fail("번들 SHA-256 검증값이 필요합니다.")
    archive = Path(archive_path).expanduser().absolute()
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAX_BYTES:
        _fail("번들 파일이 안전하지 않거나 크기 한도를 초과했습니다.")
    archive_data, unused = _file_data(archive)
    if hashlib.sha256(archive_data).hexdigest() != expected_sha256:
        _fail("번들 SHA-256이 일치하지 않습니다.")
    destination = Path(destination_root).expanduser().absolute()
    _check_managed_parents(destination, Path.home().absolute().resolve())
    if destination.is_symlink():
        _fail("대상 폴더에 심볼릭 링크를 사용할 수 없습니다.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = destination.resolve()
    final = destination / transfer_id
    if os.path.lexists(final):
        _fail("대상 이전 폴더가 이미 있어 덮어쓰지 않았습니다.")
    with tempfile.TemporaryDirectory(prefix=".workspace-import-", dir=destination) as temporary:
        staging = Path(temporary)
        private_archive = staging / "verified.tar.gz"
        private_archive.write_bytes(archive_data)
        extracted = staging / "extracted"
        extracted.mkdir()
        members = _read_archive(private_archive, extracted)
        try:
            manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            _fail("번들 manifest를 읽지 못했습니다.")
        _validate_manifest(manifest, members)
        install = staging / "install"
        root = install / manifest["projectName"]
        root.mkdir(parents=True)
        git = manifest.get("git")
        if git:
            patch = extracted / "git/index.patch"
            if _sha_file(patch) != git["indexPatchSha256"]:
                _fail("Git index patch 검증에 실패했습니다.")
            _git(root, "init", "--quiet", "--template=", "--object-format=" + git["objectFormat"])
            if git.get("branch"):
                _git(root, "check-ref-format", "refs/heads/" + git["branch"])
            if git.get("head"):
                bundle = extracted / "git/history.bundle"
                if _sha_file(bundle) != git["bundleSha256"]:
                    _fail("Git history bundle 검증에 실패했습니다.")
                _git(root, "bundle", "verify", str(bundle))
                _git(root, "fetch", "--quiet", "--no-tags", str(bundle), "HEAD", timeout=90)
                if _text(_git(root, "rev-parse", "FETCH_HEAD").stdout).strip() != git["head"]:
                    _fail("Git bundle HEAD가 manifest와 일치하지 않습니다.")
                # Recheck history safety before checkout materializes any files.
                history = _text(_git(root, "log", "--format=", "--name-only", "-z", git["head"], "--").stdout)
                if any(_sensitive(path) for path in history.split("\x00") if path):
                    _fail("수신 Git 이력에 인증·비밀 파일이 있습니다.")
                if git.get("branch"):
                    _git(root, "checkout", "--quiet", "-b", git["branch"], git["head"])
                else:
                    _git(root, "checkout", "--quiet", "--detach", git["head"])
            elif git.get("branch"):
                _git(root, "symbolic-ref", "HEAD", "refs/heads/" + git["branch"])
            if patch.stat().st_size:
                _git(root, "apply", "--cached", "--binary", "--whitespace=nowarn", str(patch))
            restored_index = _git(root, "ls-files", "--stage", "-z").stdout
            if hashlib.sha256(restored_index).hexdigest() != git["indexStateSha256"]:
                _fail("복원된 Git index가 원본 staging 상태와 일치하지 않습니다.")
            if any(_sensitive(path) for path in _text(_git(root, "ls-files", "-z").stdout).split("\x00") if path):
                _fail("수신 Git index에 인증·비밀 파일이 있습니다.")
            for remote in git.get("remotes", []):
                _git(root, "remote", "add", remote["name"], remote["url"])
        # Validate all payload hashes before replacing working-tree entries.
        for entry in manifest["entries"]:
            payload = extracted / "payload" / entry["path"]
            if payload.stat().st_size != entry["size"] or _sha_file(payload) != entry["sha256"]:
                _fail("작업 파일 SHA-256 검증에 실패했습니다.")
        for path in manifest["deleted"]:
            target = root / path
            if not target.parent.resolve().is_relative_to(root):
                _fail("삭제 경로가 대상 작업 폴더 밖으로 나갑니다.")
            if target.is_dir() and not target.is_symlink():
                _fail("삭제 경로가 폴더와 충돌합니다.")
            target.unlink(missing_ok=True)
        # Checkout may introduce tracked symlink directories; never follow them
        # when writing overlay paths. All entries are recreated from metadata.
        for entry in manifest["entries"]:
            target = root / entry["path"]
            for parent in target.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    _fail("대상 파일의 상위 경로가 심볼릭 링크입니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target):
                if target.is_dir() and not target.is_symlink():
                    _fail("작업 파일과 대상 폴더가 충돌합니다.")
                target.unlink()
            if entry["kind"] == "symlink":
                value = (extracted / "payload" / entry["path"]).read_text(encoding="utf-8")
                if value != entry["target"] or os.path.isabs(value):
                    _fail("번들 심볼릭 링크가 안전하지 않습니다.")
                _symlink_target(root, target, value)
                target.symlink_to(value)
            else:
                shutil.copyfile(extracted / "payload" / entry["path"], target)
                target.chmod(entry["mode"])
        # Resolve the complete symlink graph again after all links exist.
        for entry in manifest["entries"]:
            path = root / entry["path"]
            if entry["kind"] == "symlink":
                _symlink_target(root, path, os.readlink(path))
                digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
            else:
                digest = _sha_file(path)
            if digest != entry["sha256"]:
                _fail("복원된 작업 파일 SHA-256 검증에 실패했습니다.")
        from attachments import DIRECTORY, AttachmentError, workspace_files, resolve
        imported_attachments = []
        for parent in manifest.get("attachmentRoots", []):
            attachment_cwd = root / parent
            managed = attachment_cwd / DIRECTORY
            if (not attachment_cwd.resolve().is_relative_to(root) or attachment_cwd.is_symlink()
                    or managed.is_symlink() or not managed.is_dir()):
                _fail("복원된 첨부 저장 폴더가 올바르지 않습니다.")
            managed.chmod(0o700)
            for child in managed.iterdir():
                if child.is_symlink() or not child.is_dir():
                    _fail("복원된 첨부 저장 경로가 올바르지 않습니다.")
                child.chmod(0o700)
            try:
                actual = {(attachment_cwd / name).relative_to(root).as_posix() for name in workspace_files(attachment_cwd)}
                expected = {entry["path"] for entry in manifest["entries"] if entry["path"].startswith(managed.relative_to(root).as_posix() + "/")}
                if actual != expected:
                    _fail("복원된 첨부 파일 목록이 일치하지 않습니다.")
                for child in managed.iterdir():
                    record = resolve(attachment_cwd, child.name)
                    imported_attachments.append({"id": record["id"], "relativePath": (Path(parent) / record["relativePath"]).as_posix(), "sha256": record["sha256"]})
            except (AttachmentError, OSError):
                _fail("복원된 첨부 파일 검증에 실패했습니다.")
        cwd = root / manifest["cwdRelative"]
        if not cwd.resolve().is_relative_to(root):
            _fail("복원된 작업 폴더가 올바르지 않습니다.")
        cwd.mkdir(parents=True, exist_ok=True)
        if not cwd.is_dir() or not cwd.resolve().is_relative_to(root):
            _fail("복원된 작업 폴더가 올바르지 않습니다.")
        if os.path.lexists(final):
            _fail("대상 이전 폴더가 이미 있어 덮어쓰지 않았습니다.")
        # Reserve the final name without replacing any concurrent import. The
        # marker directory is empty until this single rename swaps in install.
        try:
            final.mkdir(mode=0o700)
        except FileExistsError:
            _fail("대상 이전 폴더가 이미 있어 덮어쓰지 않았습니다.")
        try:
            os.rename(install, final)
        except BaseException:
            final.rmdir()
            raise
        final_root = final / manifest["projectName"]
        summary = dict(manifest.get("summary") or {})
        summary.update({"root": str(final_root), "cwdRelative": manifest["cwdRelative"],
                        "originalRoot": (manifest.get("summary") or {}).get("root"),
                        "originalHead": git.get("head") if git else None,
                        "originalBranch": git.get("branch") if git else None})
        original_root = summary.get("originalRoot")
        mapping = [{**record, "from": str(Path(original_root) / record["relativePath"]) if isinstance(original_root, str) else None,
                    "to": str(final_root / record["relativePath"])} for record in imported_attachments]
        return {"cwd": str(final_root / manifest["cwdRelative"]), "root": str(final_root), "summary": summary,
                "attachmentsPathMap": mapping}
