#!/usr/bin/env python3
"""Session board server: poll each host's collector and serve a token-protected page.

Binds to 127.0.0.1 only. Remote access goes through Tailscale Serve, never a public funnel.
Standard library only and Python 3.9 compatible so it can later run on POSIX host as is.
"""
from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import html
import shutil
import hmac
import ipaddress
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import parse_qs, urlparse, unquote
import instance
import settings
from goals import GoalError, Goals
from work_service import WorkService
from session_labels import SessionLabels, reference as label_reference

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
COLLECTOR = HERE / "collector.py"
WINDOWS_COLLECTOR = HERE / "windows_collector.py"
WINDOWS_READ_ONLY = "이 Windows 작업은 아직 지원하지 않습니다. 기존 세션의 대화·터미널·종료 기능을 사용해 주세요."
# Installation names (instance.json); without one these are the public names.
NAMES = instance.current()
BRAND = NAMES["brand"]
CONFIG_DIR = instance.path("configDir")
DEFAULT_HOSTS = instance.path("defaultHosts")
STATE_DIR = instance.path("stateDir")
TOKEN_FILE = instance.path("tokenFile")
COOKIE = NAMES["names"]["cookie"]
SSH_FALLBACK_NAME = NAMES["names"]["sshFallback"]
LOG_PREFIX = BRAND["logPrefix"]
THREAD_PREFIX = BRAND["threadPrefix"]
ONCE_TTL = 120
INTERNAL_ERROR = "서버 내부 오류가 발생했습니다. 다시 시도해 주세요."
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
PYTHON_PATH = re.compile(r"^/[A-Za-z0-9_./-]{1,120}$")
SESSION_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
HOME_KEY = re.compile(r"^\.codex(-[A-Za-z0-9_-]{1,40})?$|^\.codex-router/[0-9a-f]{16}$")
STATIC = re.compile(r"^(?:vendor/)?[a-z0-9_-]{1,40}\.(html|js|css|svg|png|webmanifest)$")
REMOTE_PATH = "/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
SOCKET_PATH_LIMIT = 104
CONTROL_PREFIX = re.compile(r"^/[A-Za-z0-9_./+@-]+$")
SSH_FALLBACK_ROOT = Path("/tmp")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".webmanifest": "application/manifest+json"}
# Static and JSON responses are gzip-compressed for clients that accept it, from this size.
GZIP_MIN = 1024
COMPRESSIBLE = frozenset((".html", ".js", ".css", ".svg", ".webmanifest", ".json"))
IMMUTABLE = "public, max-age=31536000, immutable"
# Always revalidated: the page names every other asset's version, and the worker URL is fixed.
REVALIDATED = ("index.html", "sw.js")
ASSET_META = re.compile(r'(<meta\b[^>]*?\bname="sessionholic-assets"[^>]*?\bcontent=")([^"]*)(")')
# The installation's browser names for app.js and theme-init.js (storage prefix, history key, titles).
INSTANCE_META = re.compile(r'(<meta\b[^>]*?\bname="sessionholic-instance"[^>]*?\bcontent=")([^"]*)(")')
# Page text named by the instance: (brand key, element pattern with the text in group 2, attribute?).
# A value equal to the public default leaves the page as is; a changed value whose element is
# missing stops rendering rather than serving public names.
_WORDMARK = '<div class="wordmark"><span class="brand-icon" aria-hidden="true">↗</span> '
PAGE_TEXT = (
    ("shortName", re.compile(r'(<meta name="apple-mobile-web-app-title" content=")([^"]*)(")'), True),
    ("title", re.compile(r"(<title>)([^<]*)(</title>)"), False),
    ("shortName", re.compile("(" + re.escape(_WORDMARK) + ")([^<]*)(</div>)"), False),
    ("loginHeadline", re.compile("(" + re.escape(_WORDMARK) + r"[^<]*</div>\s*<h1>)([^<]*)(</h1>)"), False),
    ("loginLead", re.compile("(" + re.escape(_WORDMARK) + r"[^<]*</div>\s*<h1>[^<]*</h1>\s*<p>)([^<]*)(</p>)"), False),
    ("shortName", re.compile(r'(<span class="home-label">)([^<]*)(</span>)'), False),
)
TOKEN_HELP = re.compile(r"(<summary>접속 토큰 찾기</summary>\s*<p>)(.*?)(</p>)", re.S)
# Public product metadata, served only while brand.publicMeta is true.
PUBLIC_META = (re.compile(r'\n[ \t]*<meta name="application-name" content="[^"]*" />'),
               re.compile(r'\n[ \t]*<meta name="description" content="[^"]*" />'))
MANIFEST_TEXT = (("id", "web", "manifestId"), ("name", "brand", "manifestName"),
                 ("short_name", "brand", "shortName"), ("description", "brand", "description"))
# The service worker's cache is `<cachePrefix>shell-<sha8 of the asset sources>`, so any
# asset change installs a new worker that drops the older caches with the same prefix.
SW_CACHE = re.compile(r'^(const CACHE = )("[^"\n]*")(;)$', re.M)
SW_PREFIX = re.compile(r'(\.filter\(\(key\) => key\.startsWith\()("[^"\n]*")(\) && key !== CACHE\))')
SW_HEADER = re.compile(r"^(// )(Sessionholic)( caches only public static assets\.)", re.M)
SW_PRIVATE_SUBJECT = "A private PWA"
RENDERED = ("manifest.webmanifest", "sw.js")
ASSET_REF = re.compile(r'\b(src|href)="(/[^"?#]*)"')
# A stalled event-stream reader is dropped after this many seconds of a blocked write.
STREAM_WRITE_TIMEOUT = 30
# Board identity of resident watchers: in their shell command and their lock file name.
BOARD_ID = re.compile(r"^[0-9a-f]{8,32}$")
# Reads `<byte length>\n<program>` from stdin and runs it; the rest of stdin stays for pings.
WATCH_LOADER = ('import sys;b=sys.stdin.buffer;n=int(b.readline());'
                'exec(compile(b.read(n),"<collector>","exec"),{"__name__":"__main__"})')
# A watcher line (one full snapshot) longer than this ends the stream.
WATCH_LINE_LIMIT = 16 * 1024 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                               "connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def load_hosts(path):
    hosts = []
    path = settings.safe_path(path, allow_missing=True)
    if not Path(path).exists() and Path(path) == DEFAULT_HOSTS:
        return [{"name": "local", "label": "이 기기", "local": True, "python": "/usr/bin/python3", "ssh": None}]
    if Path(path).is_symlink() or Path(path).stat().st_size > 65536:
        raise ValueError("기기 설정 파일이 안전하지 않거나 너무 큽니다.")
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("기기 설정은 1~16개 항목의 JSON 목록이어야 합니다.")
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {"name", "label", "local", "ssh", "python", "platform"}
                or type(row.get("local", False)) is not bool
                or not isinstance(row.get("name"), str)
                or not isinstance(row.get("python") or "/usr/bin/python3", str)
                or not isinstance(row.get("label", row["name"]), str)
                or len(row.get("label", row["name"]).encode()) > 256):
            raise ValueError("기기 설정 형식이 올바르지 않습니다.")
        platform = row.get("platform", "posix")
        if platform not in ("posix", "windows") or platform == "windows" and row.get("local"):
            raise ValueError("기기의 운영체제 설정을 확인해 주세요.")
        name, python = row.get("name", ""), row.get("python") or ("python.exe" if platform == "windows" else "/usr/bin/python3")
        if not NAME.fullmatch(name):
            raise ValueError("기기 이름이 올바르지 않습니다.")
        if not row.get("local") and (not isinstance(row.get("ssh"), str) or not NAME.fullmatch(row["ssh"])):
            raise ValueError("SSH 별칭이 올바르지 않습니다.")
        valid_python = PYTHON_PATH.fullmatch(python) if platform != "windows" else (
            python == "python.exe" or re.fullmatch(r"[A-Za-z]:[\\/][A-Za-z0-9_ .\\/-]{1,220}[\\/]python\.exe", python, re.I)
            and ".." not in re.split(r"[\\/]", python))
        if not valid_python:
            raise ValueError("Python 실행 경로가 올바르지 않습니다.")
        hosts.append({"name": name, "label": row.get("label") or name, "local": bool(row.get("local")),
                      "ssh": row.get("ssh"), "python": python})
        if platform == "windows":
            hosts[-1]["platform"] = "windows"
    if len({h["name"] for h in hosts}) != len(hosts):
        raise ValueError("기기 이름이 중복됩니다.")
    if sum(bool(h["local"]) for h in hosts) > 1:
        raise ValueError("로컬 기기는 한 항목만 등록할 수 있습니다.")
    return hosts


def _control_prefix(directory):
    prefix = str(directory) + "/cm-"
    if not CONTROL_PREFIX.fullmatch(prefix) or len(os.fsencode(prefix)) + 40 + 17 >= SOCKET_PATH_LIMIT:
        return None
    return prefix


def ssh_control_dir(state_dir):
    """`<state_dir>/ssh`, or a private `/tmp/<instance name>-<uid>` when that is too long.

    ssh expands %C to a 40 character hash and first binds a master socket at
    `<path>.<16 random characters>`, so that longest form must fit the macOS
    104 byte socket path (including NUL). Paths ssh would re-tokenize are refused.
    A typical home such as /Users/<name>/.local/state/sessionholic is too long
    for names over five characters, hence the short per-user fallback.
    """
    if not state_dir:
        return None
    preferred = Path(state_dir).expanduser().absolute() / "ssh"
    if _control_prefix(preferred):
        return preferred
    fallback = SSH_FALLBACK_ROOT / ("%s-%d" % (SSH_FALLBACK_NAME, os.getuid()))
    return fallback if _control_prefix(fallback) else None


def ssh_control_path(state_dir):
    """Pure path rule for shared SSH connections: `<socket dir>/cm-%C`, or None."""
    directory = ssh_control_dir(state_dir)
    return _control_prefix(directory) + "%C" if directory else None


def prepare_ssh_control_dir(state_dir):
    """Create the private socket folder; a failure only disables connection sharing."""
    directory = ssh_control_dir(state_dir)
    if directory is None:
        return
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            os.chmod(directory, 0o700)
    except OSError:
        pass


def ssh_control_options(state_dir):
    """`ssh -o` options reusing one connection per host for 60 idle seconds, or [].

    Without a state folder, a prepared 0700 socket folder owned by this user, or a
    path within the socket limit, ssh runs exactly as before without sharing.
    """
    path = ssh_control_path(state_dir)
    if path is None:
        return []
    try:
        info = os.lstat(Path(path).parent)
    except OSError:
        return []
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return []
    return ["-o", "ControlMaster=auto", "-o", "ControlPersist=60", "-o", "ControlPath=" + path]


def ssh_command(host, remote, state_dir=None):
    return (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5"]
            + ssh_control_options(state_dir) + ["--", host["ssh"], remote])


def collector_command(host, args, state_dir=None):
    """Local hosts run the file; SSH hosts read it from stdin so nothing is left on the remote."""
    if host["local"]:
        return [sys.executable, "-I", str(COLLECTOR)] + list(args)
    if host.get("platform") == "windows":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        script = "$ErrorActionPreference='Stop'; $env:PYTHONIOENCODING='utf-8'; & " + " ".join(
            quote(value) for value in [host.get("python") or "python.exe", "-I", "-"] + list(args)) + "; exit $LASTEXITCODE"
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return ssh_command(host, "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded, state_dir)
    remote = (f"PATH={REMOTE_PATH} {shlex.quote(host['python'])} -I - "
              + " ".join(shlex.quote(a) for a in args))
    return ssh_command(host, remote, state_dir)


def remote_prelude():
    """Modules for a remote Python's stdin, from this reviewed bundle rather than a remote
    Python path: `instance` carrying the board's names (the remote host never reads its own
    instance file), then `settings` and the optional `agent_router` adapter. Sources are
    ASCII literals: Python 3.9 rejects a UTF-8 character split across its 8KiB read of a
    long stdin line."""
    names = (HERE / "instance.py").read_text(encoding="utf-8")
    module = (HERE / "settings.py").read_text(encoding="utf-8")
    router = (HERE / "agent_router.py").read_text(encoding="utf-8")
    return ("import types,sys\nm=types.ModuleType('instance')\nm.INJECTED=" + ascii(instance.remote_blob())
            + "\nexec(" + ascii(names) + ",m.__dict__)\nsys.modules['instance']=m\n"
            "m=types.ModuleType('settings')\nexec(" + ascii(module) + ",m.__dict__)\nsys.modules['settings']=m\n"
            "m=types.ModuleType('agent_router')\nexec(" + ascii(router) + ",m.__dict__)\nsys.modules['agent_router']=m\n")


def remote_program():
    """The collector as one self-contained program for a remote Python's stdin."""
    return (b"from __future__ import annotations\n" + remote_prelude().encode()
            + COLLECTOR.read_bytes().replace(b"from __future__ import annotations\n", b"", 1))


def watch_command(host, board_id, state_dir=None):
    """`collector.py watch` for one host. A remote watcher reads its program from stdin as
    `<byte length>\\n<program>` and keeps reading stdin for pings, so nothing is left on disk."""
    if host.get("platform") == "windows":
        raise ValueError("Windows 기기는 제한된 주기의 목록 수집을 사용합니다.")
    if not isinstance(board_id, str) or not BOARD_ID.fullmatch(board_id):
        raise ValueError("보드 식별자가 올바르지 않습니다.")
    args = ["watch", "--board", board_id]
    if host["local"]:
        return [sys.executable, "-I", str(COLLECTOR)] + args
    remote = (f"PATH={REMOTE_PATH} {shlex.quote(host['python'])} -I -c {shlex.quote(WATCH_LOADER)} "
              + " ".join(args))
    return ssh_command(host, remote, state_dir)


def start_watch(host, board_id, state_dir=None):
    """(process, bytes to write first) of one host's watcher; stderr is discarded.

    A remote watcher's program is read before ssh starts: a file that cannot be read
    leaves no ssh process behind."""
    command = watch_command(host, board_id, state_dir)
    preface = None
    if not host["local"]:
        program = remote_program()
        preface = str(len(program)).encode() + b"\n" + program
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return process, preface


def run_collector(host, args, timeout, state_dir=None):
    stdin = None
    if not host["local"]:
        # Remote stdin remains a single self-contained program.
        stdin = WINDOWS_COLLECTOR.read_bytes() if host.get("platform") == "windows" else remote_program()
    try:
        done = subprocess.run(collector_command(host, args, state_dir), input=stdin, capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("응답 시간 초과") from None
    except OSError:
        raise RuntimeError("수집기를 실행하지 못했습니다") from None
    if done.returncode != 0:
        raise RuntimeError("SSH 연결 실패" if done.returncode == 255 else f"수집기 오류 (exit {done.returncode})")
    try:
        result = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError("수집기 응답 형식 오류") from None
    if not isinstance(result, dict):
        raise RuntimeError("수집기 응답 형식 오류")
    if args and args[0] == "snapshot" and result.get("error"):
        raise RuntimeError("세션 목록을 읽지 못했습니다. 마지막 기록은 유지됩니다.")
    return result


def scrub_payload(value):
    from launch import scrub_text
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_text(item) if key in ("text", "title", "snippet", "account", "error") and isinstance(item, str)
                else scrub_payload(item) for key, item in value.items()}
    return value


def log_failure(context, exc):
    """Record where an unexpected error happened without echoing payload text."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    sys.stderr.write(f"{LOG_PREFIX}: {context} 중 예상하지 못한 오류 ({type(exc).__name__})\n{frames}")


def accepts_gzip(header):
    """True when Accept-Encoding lists gzip with a non-zero quality."""
    for item in (header or "").split(","):
        name, _, params = item.partition(";")
        if name.strip().lower() != "gzip":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


def etag_matches(header, etag):
    for item in (header or "").split(","):
        item = item.strip()
        if item.startswith("W/"):
            item = item[2:]
        if item in ("*", etag):
            return True
    return False


def _fill(text, pattern, value, what):
    """Replace group 2 of the one match of `pattern`; anything else is an error."""
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise ValueError("화면 파일에서 %s 위치를 찾지 못했습니다. 설치 파일을 확인해 주세요." % what)
    return text[:found[0].start(2)] + value + text[found[0].end(2):]


def _help_html(text, current):
    """Escaped help lines in place of `current`; `code` spans become <code>, and a
    multi-line paragraph keeps its source indentation."""
    def line(value):
        return "".join(html.escape(part, quote=False) if index % 2 == 0
                       else "<code>%s</code>" % html.escape(part, quote=False)
                       for index, part in enumerate(value.split("`")))
    lines = [line(value) for value in text.split("\n")]
    if "\n" not in current:
        return " ".join(lines)
    indent = re.match(r"[ \t]*", current.split("\n", 1)[1]).group(0)
    return "\n" + "".join(indent + value + "\n" for value in lines) + current[current.rfind("\n") + 1:]


def page_names(names=None):
    """What app.js and theme-init.js read from the page's instance meta."""
    names = names if names is not None else NAMES
    return {"title": names["brand"]["title"], "shortName": names["brand"]["shortName"],
            "storagePrefix": names["web"]["storagePrefix"], "historyKey": names["web"]["historyKey"],
            "attachmentsDir": names["remote"]["attachmentsDir"]}


def render_page(text, names=None):
    """index.html with this installation's names (before asset versions are filled in)."""
    names = names if names is not None else NAMES
    brand, public = names["brand"], instance.defaults()["brand"]
    for key, pattern, attribute in PAGE_TEXT:
        if brand[key] != public[key]:
            text = _fill(text, pattern, html.escape(brand[key], quote=attribute), key)
    if brand["loginTokenHelp"] != public["loginTokenHelp"]:
        found = list(TOKEN_HELP.finditer(text))
        current = found[0].group(2) if len(found) == 1 else ""
        text = _fill(text, TOKEN_HELP, _help_html(brand["loginTokenHelp"], current), "loginTokenHelp")
    if not brand["publicMeta"]:
        for pattern in PUBLIC_META:
            text, count = pattern.subn("", text)
            if count != 1:
                raise ValueError("화면 파일에서 공개 메타데이터 위치를 찾지 못했습니다. 설치 파일을 확인해 주세요.")
    filled = html.escape(json.dumps(page_names(names), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                         quote=True)
    if INSTANCE_META.search(text):
        text = _fill(text, INSTANCE_META, filled, "sessionholic-instance")
    elif page_names(names) != page_names(instance.defaults()):
        raise ValueError("화면 파일에 인스턴스 이름 메타가 없습니다. 설치 파일을 확인해 주세요.")
    return text


def render_manifest(text, names=None):
    """manifest.webmanifest with this installation's id, names and description."""
    names = names if names is not None else NAMES
    public = instance.defaults()
    for field, section, key in MANIFEST_TEXT:
        if names[section][key] != public[section][key]:
            pattern = re.compile(r'^([ \t]*"%s":[ \t]*)("(?:[^"\\\n]|\\.)*")' % field, re.M)
            text = _fill(text, pattern, json.dumps(names[section][key], ensure_ascii=False), "manifest " + field)
    return text


def shell_cache_name(sources, names=None):
    """`<cachePrefix>shell-<sha8>` over the asset sources (path -> sha8 of the file)."""
    names = names if names is not None else NAMES
    digest = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return names["web"]["cachePrefix"] + "shell-" + digest[:8]


def render_service_worker(text, cache, names=None):
    """sw.js with its cache name and the prefix of the older caches it removes."""
    names = names if names is not None else NAMES
    public = instance.defaults()
    if SW_CACHE.search(text):
        text = _fill(text, SW_CACHE, json.dumps(cache), "CACHE")
    elif names["web"]["cachePrefix"] != public["web"]["cachePrefix"]:
        raise ValueError("화면 파일에서 CACHE 위치를 찾지 못했습니다. 설치 파일을 확인해 주세요.")
    if names["web"]["cachePrefix"] != public["web"]["cachePrefix"]:
        text = _fill(text, SW_PREFIX, json.dumps(names["web"]["cachePrefix"]), "cachePrefix")
    if not names["brand"]["publicMeta"]:
        text = _fill(text, SW_HEADER, SW_PRIVATE_SUBJECT, "service worker header")
    return text


class StaticAssets:
    """Public web files with their hashes and gzip bodies, cached by (name, mtime_ns, size).

    Only public assets are cached. index.html, manifest.webmanifest and sw.js are rendered
    with this installation's names (and the page with the current asset versions), so their
    hashes are taken over the rendered bytes.
    """
    def __init__(self, root=WEB, names=None):
        self.root = Path(root)
        self.names = names if names is not None else NAMES
        self.lock = threading.Lock()
        self.files, self.rendered, self.page = {}, {}, None

    @staticmethod
    def build(name, body):
        digest = hashlib.sha256(body).hexdigest()
        packed = None
        if Path(name).suffix in COMPRESSIBLE and len(body) >= GZIP_MIN:
            packed = gzip.compress(body, compresslevel=9, mtime=0)
            if len(packed) >= len(body):
                packed = None
        return {"body": body, "gzip": packed, "sha8": digest[:8], "etag": digest[:16]}

    def source(self, name):
        """One file exactly as installed."""
        if not STATIC.fullmatch(name):
            return None
        path = self.root / name
        try:
            info = path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        key = (info.st_mtime_ns, info.st_size)
        with self.lock:
            cached = self.files.get(name)
        if cached and cached[0] == key:
            return cached[1]
        try:
            body = path.read_bytes()
        except OSError:
            return None
        entry = self.build(name, body)
        with self.lock:
            self.files[name] = (key, entry)
        return entry

    def file(self, name):
        """One file as served: the manifest and the service worker carry this installation's names."""
        entry = self.source(name)
        if entry is None or name not in RENDERED:
            return entry
        cache = shell_cache_name(self.sources(), self.names) if name == "sw.js" else None
        key = (entry["etag"], cache)
        with self.lock:
            cached = self.rendered.get(name)
        if cached and cached[0] == key:
            return cached[1]
        text = entry["body"].decode("utf-8")
        body = (render_service_worker(text, cache, self.names) if name == "sw.js"
                else render_manifest(text, self.names))
        rendered = self.build(name, body.encode("utf-8"))
        with self.lock:
            self.rendered[name] = (key, rendered)
        return rendered

    def names_served(self):
        names = []
        for folder, prefix in ((self.root, ""), (self.root / "vendor", "vendor/")):
            try:
                names.extend(prefix + item for item in sorted(os.listdir(folder)) if STATIC.fullmatch(prefix + item))
            except OSError:
                continue
        return names

    def sources(self):
        """{"/app.js": "<sha8>", ...} of the installed files, before any rendering."""
        result = {}
        for name in self.names_served():
            entry = self.source(name)
            if entry is not None:
                result["/" + name] = entry["sha8"]
        return result

    def manifest(self):
        """{"/app.js": "<sha8>", ...} for every servable file under web/, as served."""
        result = {}
        for name in self.names_served():
            entry = self.file(name)
            if entry is not None:
                result["/" + name] = entry["sha8"]
        return result

    def index(self):
        template = self.source("index.html")
        if template is None:
            return None
        manifest = self.manifest()
        key = (template["etag"], tuple(sorted(manifest.items())))
        with self.lock:
            if self.page is not None and self.page[0] == key:
                return self.page[1]
        text = render_page(template["body"].decode("utf-8"), self.names)
        filled = html.escape(json.dumps(manifest, sort_keys=True, separators=(",", ":")), quote=True)
        text = ASSET_META.sub(lambda match: match.group(1) + filled + match.group(3), text, count=1)

        def versioned(match):
            path = match.group(2)
            if path in manifest and path[1:] not in REVALIDATED:
                return '%s="%s?v=%s"' % (match.group(1), path, manifest[path])
            return match.group(0)
        entry = self.build("index.html", ASSET_REF.sub(versioned, text).encode("utf-8"))
        with self.lock:
            self.page = (key, entry)
        return entry

    def check(self):
        """Render the page, manifest and worker once; a missing or unfillable file is an error."""
        for name in RENDERED:
            if self.file(name) is None:
                raise ValueError("화면 파일 %s을 찾지 못했습니다. 설치 파일을 확인해 주세요." % name)
        if self.index() is None:
            raise ValueError("화면 파일 index.html을 찾지 못했습니다. 설치 파일을 확인해 주세요.")
        # The public names leave sw.js as it is, but its cache name always follows the
        # assets: a worker without its one CACHE line would keep serving an old shell.
        if len(SW_CACHE.findall(self.source("sw.js")["body"].decode("utf-8"))) != 1:
            raise ValueError("화면 파일 sw.js에서 CACHE 위치를 찾지 못했습니다. 설치 파일을 확인해 주세요.")


class HostStream:
    """One host's resident `collector.py watch` process.

    Its own thread starts the process, reads its JSON lines and hands snapshots to the
    board; the board's live manager pings it and ends it. It stays registered on the
    board until its process is gone, so a host never has two watchers at once.
    """
    def __init__(self, board, host):
        self.board, self.host, self.name = board, host, host["name"]
        self.process = None
        self.write_lock = threading.Lock()
        self.began = time.monotonic()
        self.last_line = self.began
        self.next_ping = self.began + board.live_ping
        self.delivered = False
        self.stopping = False
        self.thread = threading.Thread(target=self.run, daemon=True, name=THREAD_PREFIX + "-live-" + self.name)

    def send(self, line, wait=False):
        """Write one control line; a busy or closed pipe skips it."""
        if not (self.write_lock.acquire(timeout=1.0) if wait else self.write_lock.acquire(blocking=False)):
            return False
        try:
            process = self.process
            if process is None or process.stdin is None:
                return False
            process.stdin.write(line)
            process.stdin.flush()
            return True
        except (OSError, ValueError):
            return False
        finally:
            self.write_lock.release()

    def run(self):
        reason = "eof"
        try:
            process, preface = self.board.streamer(self.host)
            with self.write_lock:
                self.process = process
            if self.stopping:
                return
            if preface:
                with self.write_lock:
                    process.stdin.write(preface)
                    process.stdin.flush()
            while True:
                line = process.stdout.readline(WATCH_LINE_LIMIT)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    reason = "overflow"
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                self.last_line = time.monotonic()
                if message.get("type") == "snapshot" and isinstance(message.get("data"), dict):
                    self.board.apply_live(self, message["data"])
                elif message.get("type") == "busy":
                    # The same board's previous watcher still holds that host.
                    reason = "busy"
                    break
        except Exception as exc:
            reason = "error"
            if not isinstance(exc, (OSError, ValueError)):
                log_failure("상주 수집기 연결", exc)
        finally:
            self.reap()
            self.board.stream_ended(self, reason)

    def reap(self):
        """stdin EOF lets the watcher say bye; then terminate, then kill. Never left running."""
        process = self.process
        if process is None:
            return
        with self.write_lock:
            self.close_input(process)
        for action in (None, "terminate", "kill"):
            try:
                if action:
                    getattr(process, action)()
                process.wait(timeout=self.board.live_exit_wait)
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break
        try:
            if process.stdout is not None:
                process.stdout.close()
        except (OSError, ValueError):
            pass

    def end(self, force=False):
        """Make a silent watcher's output end; its thread then reaps it as a failure."""
        process = self.process
        if process is not None:
            try:
                process.kill() if force else process.terminate()
            except OSError:
                pass

    @staticmethod
    def close_input(process):
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass

    def stop(self):
        """An intended stop: not a failure, no fallback collection."""
        self.stopping = True
        process = self.process
        if process is not None:
            if self.write_lock.acquire(blocking=False):
                try:
                    self.close_input(process)
                finally:
                    self.write_lock.release()
            else:
                # Still sending its program: closing that pipe would wait for the write.
                self.end()
        for force in (None, False, True):
            if force is not None:
                self.end(force)
            if self.thread.ident is not None:
                self.thread.join(self.board.live_exit_wait)
            if not self.thread.is_alive():
                return


class Board:
    """Visible-client driven collection.

    Hosts are collected in parallel, one collection per host at a time. A failed
    host is paused and retried automatically after an increasing backoff, or at
    once on an explicit refresh.

    While an event stream is open, each host also runs one resident watcher that
    prints a full snapshot whenever something changes; those snapshots take the same
    `changed()` path. The watchers stop a grace period after the last stream closes.
    """
    RETRY_BACKOFF = (120, 300, 900)
    # Resident watcher restarts, apart from the one-shot collection backoff above.
    STREAM_BACKOFF = (5, 30, 120, 600)
    # Three failed watchers in a row, each within STREAM_HEALTHY of its start: pause it this long.
    STREAM_TRIP = 900
    STREAM_HEALTHY = 60.0

    def __init__(self, hosts, interval, runner=None, state_dir=None, streamer=None, live=True):
        self.hosts, self.interval = hosts, max(interval, 60)
        self.clock = time.time
        self.lock = threading.RLock()
        # Lock order: a host lock, then save_lock, then self.lock, then a change
        # listener's own lock; never the reverse.
        self.host_locks = {h["name"]: threading.Lock() for h in hosts}
        self.save_lock = threading.Lock()
        self.state = {h["name"]: {"name": h["name"], "label": h["label"], "ok": False,
                     "error": "아직 수집하지 않았어요", "data": None, "fetchedAt": None,
                     "durationMs": None, "paused": False, "refreshing": False, "live": False} for h in hosts}
        self.reads, self.attempted, self.backoff = {}, {}, {}
        self.state_dir = Path(state_dir) if state_dir else None
        self.runner = runner or (lambda host, args, timeout: run_collector(host, args, timeout, self.state_dir))
        # Resident watchers run on a real board, or with a streamer a caller hands in;
        # a board with its own runner (a fixture) never starts host processes by itself.
        if streamer is None and runner is None and live:
            streamer = lambda host: start_watch(host, self.live_board_id(), self.state_dir)  # noqa: E731
        self.streamer = streamer
        self.streams, self.stream_backoff, self.live_at, self.live_saved = {}, {}, {}, {}
        self.viewers, self.viewers_left = 0, None
        self.live_manager, self.live_closed, self.board_id = None, False, None
        self.live_wake = threading.Event()
        # Seconds; instance values so a test can shorten them.
        self.live_grace, self.live_ping, self.live_silence = 60.0, 10.0, 30.0
        self.live_tick, self.live_exit_wait = 0.5, 2.0
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            prepare_ssh_control_dir(self.state_dir)
            try:
                cached = json.loads((self.state_dir / "snapshot.json").read_text())
                for row in cached.get("hosts", []):
                    if row["name"] in self.state and row.get("data"):
                        self.state[row["name"]].update(data=row["data"], fetchedAt=row.get("fetchedAt"),
                            error="저장된 목록 · 연결 확인 전")
            except (OSError, ValueError, KeyError, TypeError):
                pass
            try:
                saved_reads = json.loads((self.state_dir / "conversations.json").read_text())
                for entry in saved_reads[-128:]:
                    self.reads[tuple(entry["key"])] = (entry["at"], entry["data"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # `version` rises within this process only when the published host state changes.
        self.version, self.listeners = 0, []
        self.signature = self.state_signature()

    def state_signature(self):
        with self.lock:
            return hashlib.sha256(json.dumps(self.state, sort_keys=True, ensure_ascii=False,
                                             default=str).encode()).digest()

    def add_listener(self, callback):
        with self.lock:
            self.listeners.append(callback)

    def remove_listener(self, callback):
        with self.lock:
            self.listeners.remove(callback)

    def changed(self, force=False):
        """Announce host changes, or a committed board-only display change."""
        with self.lock:
            signature = self.state_signature()
            if signature == self.signature and not force:
                return
            self.signature = signature
            self.version += 1
            for callback in list(self.listeners):
                try:
                    callback(self.version)
                except Exception as exc:
                    log_failure("변경 알림", exc)

    def start(self):
        # Kept for old callers; no background polling without a visible client.
        return None

    def poll(self, host, full=False):
        """One-shot collection; `full` also asks Codex to scan and repair its rollouts."""
        name = host["name"]
        # Only one collection per host; other hosts are collected concurrently.
        with self.host_locks[name]:
            started = self.clock()
            update = {"ok": False, "paused": True, "error": "수집 결과를 처리하지 못했습니다."}
            try:
                with self.lock:
                    self.state[name]["refreshing"] = True
                    self.attempted[name] = started
                    self.changed()
                data = scrub_payload(self.runner(host, ["snapshot", "--full"] if full else ["snapshot"], 45))
                update = {"ok": True, "paused": False, "error": None, "data": data,
                          "fetchedAt": int(self.clock())}
            except RuntimeError as exc:
                update = {"ok": False, "paused": True, "error": str(exc)}
            except Exception as exc:
                log_failure("기기 수집", exc)
            finally:
                # Every outcome clears `refreshing`, so manual retry is never blocked.
                finished = self.clock()
                update.update(durationMs=int((finished - started) * 1000), refreshing=False)
                with self.lock:
                    if self.live_at.get(name, started) > started:
                        # A resident watcher delivered newer data while this one ran.
                        update = {"ok": True, "durationMs": update["durationMs"], "refreshing": False}
                        self.state[name].update(durationMs=update["durationMs"], refreshing=False)
                    else:
                        self.state[name].update(update)
                        if update["ok"]:
                            self.backoff.pop(name, None)
                        else:
                            failures = self.backoff.get(name, (0, 0))[0] + 1
                            delay = self.RETRY_BACKOFF[min(failures, len(self.RETRY_BACKOFF)) - 1]
                            self.backoff[name] = (failures, finished + delay)
                    self.changed()
            self.persist("snapshot.json", self.snapshot)
            return update["ok"]

    def persist(self, filename, build):
        """State files are only a cache; a failed write never changes a collection result."""
        if not self.state_dir:
            return
        with self.save_lock:
            try:
                private_json(self.state_dir / filename, build())
            except Exception as exc:
                sys.stderr.write(f"{LOG_PREFIX}: 상태 파일 {filename} 저장 실패 ({type(exc).__name__})\n")

    def request_refresh(self, host_name=None, force=False):
        if host_name is not None and not isinstance(host_name, str):
            raise ValueError("기기 이름 형식이 올바르지 않습니다.")
        if host_name is not None and host_name not in self.state:
            raise ValueError("등록되지 않은 기기입니다.")
        queued, asked = [], []
        with self.lock:
            now = self.clock()
            for host in self.hosts:
                name = host["name"]
                row = self.state[name]
                if host_name and name != host_name:
                    continue
                stream = self.streams.get(name)
                if row["live"] and stream is not None and not stream.stopping:
                    # A resident watcher keeps this host current; a manual refresh asks it to read now.
                    if force:
                        asked.append(stream)
                    continue
                delay = 5 if force else self.interval
                if row["refreshing"] or now - self.attempted.get(name, 0) < delay:
                    continue
                # A paused host retries by itself only after its backoff; explicit refresh skips it.
                if row["paused"] and not force and now < self.backoff.get(name, (0, 0))[1]:
                    continue
                row["refreshing"] = True
                queued.append(host)
            self.changed()
        asked = [stream for stream in asked if stream.send(b"refresh\n", wait=True)]
        self.start_polls(queued, full=force)
        return bool(queued or asked)

    def start_polls(self, queued, full=False):
        for host in queued:
            try:
                threading.Thread(target=self.poll, args=(host, full), daemon=True,
                                 name=THREAD_PREFIX + "-collect-" + host["name"]).start()
            except RuntimeError:
                with self.lock:
                    self.state[host["name"]].update(refreshing=False, error="수집을 시작하지 못했습니다.")
                    self.changed()

    # ---------- resident watchers ----------
    def live_board_id(self):
        """This board's watcher identity, kept in the state folder so a restarted board
        meets its own previous watchers' locks instead of running beside them."""
        with self.lock:
            if self.board_id:
                return self.board_id
        value = None
        if self.state_dir:
            path = self.state_dir / "board-id"
            for _ in range(2):
                try:
                    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                        text = stream.read(64).strip()
                    value = text if BOARD_ID.fullmatch(text) else None
                    break
                except FileNotFoundError:
                    try:
                        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                            stream.write(secrets.token_hex(8) + "\n")
                    except FileExistsError:
                        continue
                    except OSError:
                        break
                except (OSError, UnicodeError):
                    break
        with self.lock:
            self.board_id = self.board_id or value or secrets.token_hex(8)
            return self.board_id

    def viewer_joined(self):
        """An event stream opened: watchers start (once per host) while anyone watches."""
        start = None
        with self.lock:
            self.viewers += 1
            self.viewers_left = None
            if self.streamer is None or self.live_closed:
                return
            if self.live_manager is None:
                start = self.live_manager = threading.Thread(target=self.manage_live, daemon=True,
                                                             name=THREAD_PREFIX + "-live")
        if start is not None:
            try:
                start.start()
            except RuntimeError:
                with self.lock:
                    self.live_manager = None
        self.live_wake.set()

    def viewer_left(self):
        with self.lock:
            self.viewers = max(0, self.viewers - 1)
            if not self.viewers:
                self.viewers_left = time.monotonic()
        self.live_wake.set()

    def live_wanted(self, now):
        """Caller holds self.lock."""
        return not self.live_closed and (self.viewers > 0 or (
            self.viewers_left is not None and now - self.viewers_left < self.live_grace))

    def manage_live(self):
        """Start, ping, stop and replace watchers; process work happens outside self.lock."""
        try:
            while True:
                starts, stops, pings, silent = [], [], [], []
                with self.lock:
                    now = time.monotonic()
                    wanted = self.live_wanted(now)
                    for host in self.hosts:
                        name = host["name"]
                        stream = self.streams.get(name)
                        if stream is not None:
                            if not wanted:
                                if not stream.stopping:
                                    stream.stopping = True
                                    stops.append(stream)
                            elif now - stream.last_line >= self.live_silence:
                                silent.append((stream, now - stream.last_line >= self.live_silence + 5))
                            elif now >= stream.next_ping:
                                stream.next_ping = now + self.live_ping
                                pings.append(stream)
                        elif (wanted and host.get("platform") != "windows" and not self.state[name]["paused"]
                              and now >= self.stream_backoff.get(name, (0, 0.0))[1]):
                            # A paused host waits for a successful one-shot collection first.
                            self.streams[name] = HostStream(self, host)
                            starts.append(self.streams[name])
                    if not wanted and not self.streams:
                        self.live_manager = None
                        return
                for stream in stops:
                    stream.stop()
                for stream in starts:
                    try:
                        stream.thread.start()
                    except RuntimeError:
                        with self.lock:
                            if self.streams.get(stream.name) is stream:
                                del self.streams[stream.name]
                for stream, force in silent:
                    stream.end(force)
                for stream in pings:
                    stream.send(b"ping\n")
                self.live_wake.wait(self.live_tick)
                self.live_wake.clear()
        except BaseException:
            with self.lock:
                if self.live_manager is threading.current_thread():
                    self.live_manager = None
            raise

    def apply_live(self, stream, data):
        """A watcher's snapshot through the same state path as a one-shot collection.

        The host lock is not taken: a one-shot collection may hold it for many seconds.
        """
        data = scrub_payload(data)
        save = False
        with self.lock:
            if self.streams.get(stream.name) is not stream or stream.stopping or self.live_closed:
                return False
            stream.delivered = True
            row = self.state[stream.name]
            now = self.clock()
            self.live_at[stream.name] = now
            same = (isinstance(row["data"], dict) and {k: v for k, v in row["data"].items() if k != "collectedAt"}
                    == {k: v for k, v in data.items() if k != "collectedAt"})
            if not (same and row["ok"] and row["live"] and not row["paused"] and row["error"] is None):
                row.update(data=data, ok=True, paused=False, error=None, fetchedAt=int(now), live=True)
                self.backoff.pop(stream.name, None)
                self.changed()
            moment = time.monotonic()
            if moment - self.live_saved.get(stream.name, moment - 30) >= 30:
                self.live_saved[stream.name] = moment
                save = True
        if save:
            self.persist("snapshot.json", self.snapshot)
        return True

    def stream_ended(self, stream, reason):
        """A watcher's process is gone. An unintended end backs off its restart and
        falls back to one one-shot collection while someone is watching."""
        fallback = None
        with self.lock:
            if self.streams.get(stream.name) is not stream:
                return
            del self.streams[stream.name]
            row = self.state[stream.name]
            if row["live"]:
                row["live"] = False
                self.changed()
            now = time.monotonic()
            if not stream.stopping and not self.live_closed:
                failures = self.stream_backoff.get(stream.name, (0, 0.0))[0]
                failures = 1 if stream.delivered and now - stream.began >= self.STREAM_HEALTHY else failures + 1
                delay = self.STREAM_BACKOFF[min(failures, len(self.STREAM_BACKOFF)) - 1]
                if failures >= 3:
                    delay = max(delay, self.STREAM_TRIP)
                self.stream_backoff[stream.name] = (failures, now + delay)
                if self.live_wanted(now) and not row["refreshing"]:
                    paused_until = self.backoff.get(stream.name, (0, 0))[1]
                    if not row["paused"] or self.clock() >= paused_until:
                        row["refreshing"] = True
                        self.changed()
                        fallback = stream.host
        self.live_wake.set()
        if stream.delivered:
            self.persist("snapshot.json", self.snapshot)
        if fallback is not None:
            self.start_polls([fallback])

    def close_live(self):
        """Server shutdown: stop every watcher and its manager."""
        with self.lock:
            self.live_closed = True
            streams = list(self.streams.values())
            manager = self.live_manager
            for stream in streams:
                stream.stopping = True
        self.live_wake.set()
        for stream in streams:
            stream.stop()
        if manager is not None and manager is not threading.current_thread() and manager.ident is not None:
            manager.join(5)

    def snapshot(self):
        with self.lock:
            return {"serverTime": int(time.time()), "interval": self.interval, "version": self.version,
                    "hosts": copy.deepcopy(list(self.state.values()))}

    def source(self, ref):
        if not isinstance(ref, dict):
            raise ValueError("작업을 선택해 주세요.")
        host = self.state.get(ref.get("host"))
        if not host:
            raise ValueError("등록되지 않은 기기입니다.")
        agent = ref.get("agent")
        if agent not in ("claude", "codex"):
            raise ValueError("지원하지 않는 에이전트입니다.")
        with self.lock:
            for row in (host.get("data") or {}).get(agent, []):
                identifier = row.get("id")
                if not identifier and agent == "claude" and row.get("kind") == "interactive" and SESSION_ID.fullmatch(row.get("sessionId") or ""):
                    identifier = row["sessionId"]
                if identifier and identifier == ref.get("id") and (agent != "codex" or row.get("home") == ref.get("home")):
                    return {**copy.deepcopy(row), "id": identifier, "host": host["name"], "hostLabel": host["label"],
                            "online": host["ok"], "fetchedAt": host["fetchedAt"]}
        raise ValueError("목록에서 작업을 다시 선택해 주세요.")

    def read(self, host_name, agent, session_id, home, fresh=False):
        host = next((h for h in self.hosts if h["name"] == host_name), None)
        if host is None or agent not in ("claude", "codex") or not SESSION_ID.fullmatch(session_id or ""):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        if agent == "codex" and not HOME_KEY.fullmatch(home or ""):
            raise ValueError("Codex home 형식이 올바르지 않습니다.")
        key = (host_name, agent, session_id, home)
        with self.lock:
            cached = self.reads.get(key)
            paused = self.state[host_name]["paused"]
            if cached and not fresh and (paused or time.time() - cached[0] < 60):
                return {**cached[1], "fetchedAt": int(cached[0]), "stale": not self.state[host_name]["ok"]}
            if paused and not fresh:
                raise RuntimeError("기기 연결을 다시 확인한 뒤 대화를 읽어 주세요.")
        args = ["read", agent, session_id] + ([home] if agent == "codex" else [])
        result = scrub_payload(self.runner(host, args, 30))
        result["messages"] = result.get("messages", [])[-80:]
        fetched = time.time()
        with self.lock:
            if len(self.reads) >= 128 and key not in self.reads:
                self.reads.pop(next(iter(self.reads)))
            if not result.get("error"):
                self.reads[key] = (fetched, result)
        if not result.get("error"):
            def saved_reads():
                with self.lock:
                    return [{"key": list(k), "at": v[0], "data": v[1]} for k, v in self.reads.items()]
            self.persist("conversations.json", saved_reads)
        return {**result, "fetchedAt": int(fetched), "stale": False}


def private_json(path, value):
    path = Path(path)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("상태 경로는 심볼릭 링크를 사용할 수 없습니다.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise ValueError("상태 경로 소유자를 확인해 주세요.")
    path.parent.chmod(0o700)
    temp = path.with_name(path.name + "." + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class LaunchFailure(RuntimeError):
    def __init__(self, recovery):
        super().__init__("대상 실행 준비 뒤 터미널 연결을 완료하지 못했습니다. 대상 작업이 이미 시작됐을 수 있습니다.")
        self.recovery = recovery


class Workflow:
    """Only server-discovered identities become native terminal launch commands."""
    def __init__(self, board, terminals, state_dir=STATE_DIR):
        self.board, self.terminals, self.state_dir = board, terminals, Path(state_dir)
        self.goals = Goals(state_dir)
        self.plans, self.requests = {}, {}
        self.lock = threading.RLock()
        from transfer import Transfers
        self.transfers = Transfers(board.hosts, state_dir)
        # What 새 세션 terminals run, read from their host at most once a minute.
        self.program_lock, self.program_busy, self.program_asked = threading.Lock(), set(), {}

    PROGRAM_REFRESH = 60   # seconds between launcher reads on one host
    PROGRAM_STALE = 300    # a named program is read again after this long (the launcher may switch)

    def describe_launchers(self):
        """Name what each 새 세션 terminal runs (`launcher_programs` on its host), in the background:
        a list read never waits on SSH. A change is saved on the terminal and announced."""
        if getattr(self.terminals, "set_program", None) is None:
            return
        try:
            terminals = self.terminals.list()
        except (RuntimeError, OSError, ValueError):
            return
        now = time.time()
        live, due = {}, {}
        for terminal in terminals:
            if (terminal.get("mode") != "new-session" or not terminal.get("alive") or terminal.get("closed")
                    or type(terminal.get("createdAt")) is not int):
                continue
            # Every live one is sent, so a launcher is weighed against all of them; only those due are saved.
            live.setdefault(terminal.get("host"), []).append({"id": terminal["id"], "createdAt": terminal["createdAt"]})
            seen = terminal.get("programAt")
            if type(seen) is int and now - seen < (self.PROGRAM_STALE if terminal.get("program") else self.PROGRAM_REFRESH):
                continue
            due.setdefault(terminal.get("host"), set()).add(terminal["id"])
        for name, wanted in due.items():
            rows = live[name]
            host = next((h for h in self.board.hosts if h["name"] == name), None)
            if host is None or host.get("platform") == "windows" or not self.board.state.get(name, {}).get("ok"):
                continue
            with self.program_lock:
                if name in self.program_busy or now - self.program_asked.get(name, 0) < self.PROGRAM_REFRESH:
                    continue
                self.program_busy.add(name)
                self.program_asked[name] = now
            try:
                threading.Thread(target=self._describe_host, args=(host, rows, wanted), daemon=True,
                                 name=THREAD_PREFIX + "-launcher-" + name).start()
            except RuntimeError:
                with self.program_lock:
                    self.program_busy.discard(name)

    def _describe_host(self, host, rows, wanted=None):
        try:
            result = self.transfers.rpc(host, "launcher_programs", {"terminals": rows}, timeout=15)
            asked = {row["id"] for row in rows} if wanted is None else set(wanted)
            for identifier, info in (result.get("terminals") or {}).items():
                if identifier in asked and isinstance(info, dict):
                    try:
                        self.terminals.set_program(identifier, info)
                    except (RuntimeError, OSError, ValueError, KeyError):
                        continue
        except (RuntimeError, OSError, ValueError, KeyError, AttributeError):
            pass
        finally:
            with self.program_lock:
                self.program_busy.discard(host["name"])

    def profiles(self):
        from launch import discover_profiles
        return discover_profiles()

    def capabilities(self):
        from launch import profile_metadata
        profiles = self.profiles()
        hosts = []
        for host in self.board.hosts:
            status = self.board.state[host["name"]]
            accounts = profiles if host["local"] else []
            if not host["local"] and status["ok"] and host.get("platform") != "windows":
                try:
                    accounts = self.transfers.profiles(host)
                except (RuntimeError, OSError, ValueError):
                    # Existing same-account attach stays available if transfer setup is unavailable.
                    accounts = []
            if not host["local"]:
                for agent in ("claude", "codex"):
                    for row in (status.get("data") or {}).get(agent, []):
                        key = "claude:default" if agent == "claude" else "codex:" + row["home"]
                        if not any(p["id"] == key for p in accounts):
                            home = row.get("home", ".claude")
                            try:
                                presentation = profile_metadata(agent, home)
                            except ValueError:
                                presentation = {"label": row.get("account") or "현재 계정 설정"}
                            accounts.append({**presentation, "id": key, "agent": agent,
                                             "home": home, "available": True})
            if host.get("platform") == "windows":
                accounts = [{**p, "available": p.get("agent") == "codex", "launchEnabled": False,
                             "reason": None if p.get("agent") == "codex" else
                             "Claude 대화형 세션은 실행한 터미널에서 입력할 수 있습니다."} for p in accounts]
            hosts.append({"name": host["name"], "label": host["label"], "online": status["ok"],
                "newSession": self.new_session_status(host, status["ok"]),
                "agents": [{"id": agent, "label": "Claude Code" if agent == "claude" else "Codex",
                    "accounts": [{k: p.get(k) for k in ("id", "label", "available", "reason", "home",
                        "scope", "scopeLabel", "scopeDescription", "accountKey", "accountLabel",
                        "identityVerified", "identityReason", "environment")} for p in accounts if p["agent"] == agent]}
                    for agent in ("claude", "codex")]})
        return {"hosts": hosts, "terminal": {"available": bool(shutil.which("tmux", path=collector_path())),
                "reason": "터미널 연결에는 tmux가 필요합니다."}}

    def new_session_status(self, host, online=True):
        """Whether this host's launcher can open "새 세션", read from that host's own settings."""
        if host.get("platform") == "windows":
            return {"available": False, "label": None, "reason": WINDOWS_READ_ONLY}
        if host["local"]:
            from launch import new_session_status
            return new_session_status()
        if not online:
            return {"available": False, "label": None, "reason": "기기에 연결할 수 없습니다."}
        try:
            return self.transfers.launcher(host)
        except (RuntimeError, OSError, ValueError):
            return {"available": False, "label": None, "reason": "이 기기의 새 세션 설정을 확인하지 못했습니다."}

    def start_app_server(self, payload):
        """Start one Codex account's app-server on its host, then read that host again.

        Its sessions were listed from the state DB; reading and sending need the daemon.
        The host starts it with that account's own launch variables (`codex app-server
        daemon start`, nothing when it already runs) and never sends anything to a model."""
        if not isinstance(payload, dict):
            raise ValueError("앱 서버 요청 형식이 올바르지 않습니다.")
        host = next((h for h in self.board.hosts if h["name"] == payload.get("host")), None)
        key = payload.get("home")
        if host is None or not isinstance(key, str) or not HOME_KEY.fullmatch(key):
            raise ValueError("기기와 Codex 계정을 확인해 주세요.")
        if host.get("platform") == "windows":
            raise ValueError(WINDOWS_READ_ONLY)
        if host["local"]:
            from launch import start_app_server
            result = start_app_server(key)
        else:
            if not self.board.state[host["name"]]["ok"]:
                raise RuntimeError("기기에 연결할 수 없어 앱 서버를 켜지 않았어요.")
            from transfer import WorkerError
            try:
                # A first start installs the daemon package on that host.
                result = self.transfers.rpc(host, "start_app_server", {"home": key}, timeout=150)
            except WorkerError as exc:
                raise ValueError(str(exc)) from None
            if not isinstance(result, dict) or type(result.get("started")) is not bool:
                raise RuntimeError("기기의 앱 서버 시작 결과를 확인하지 못했습니다.")
        self.board.request_refresh(host["name"], force=True)
        return {"ok": True, "started": result["started"]}

    def new_session(self, payload):
        """Open a host's own launcher in a new board terminal. The launcher, not the board,
        chooses the project, agent and account, so nothing else is decided here."""
        if not isinstance(payload, dict):
            raise ValueError("새 세션 요청 형식이 올바르지 않습니다.")
        host_name, request_id = payload.get("host"), payload.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ValueError("실행 요청 식별자가 필요합니다.")
        host = next((h for h in self.board.hosts if h["name"] == host_name), None)
        if host is None:
            raise ValueError("새 세션을 열 기기를 선택해 주세요.")
        if host.get("platform") == "windows":
            raise ValueError(WINDOWS_READ_ONLY)
        with self.lock:
            if request_id in self.requests:
                previous, result = self.requests[request_id]
                if previous != ("new-session", host["name"]):
                    raise ValueError("이미 사용한 요청입니다.")
                return result
            if not host["local"] and not self.board.state[host["name"]]["ok"]:
                raise RuntimeError("기기에 연결할 수 없어 새 세션을 열지 않았어요.")
            key = "new-session:" + request_id
            with self.terminals.reserve(key):
                if host["local"]:
                    from launch import new_session
                    spec = new_session()
                    label = spec["label"]
                else:
                    # Asked fresh: the host's settings decide, and its helper path is then known.
                    status = self.transfers.launcher(host, refresh=True)
                    if not status["available"]:
                        raise ValueError(status["reason"] or "이 기기에는 새 세션 실행 명령이 설정되지 않았습니다.")
                    import collector
                    label = status["label"] or "새 세션"
                    spec = {"argv": self.transfers.command(host, ["-I", self.transfers.runtime_paths[host["name"]],
                                                                 "new-session"], tty=True),
                            "cwd": str(Path.home()), "env": collector.tool_env()}
                terminal = self.terminals.create(key, spec["argv"], spec["cwd"], spec["env"],
                                                 {"title": label, "host": host["name"], "mode": "new-session",
                                                  "createdAt": int(time.time())})
            result = {"terminal": terminal, "mode": "new-session"}
            self.requests[request_id] = (("new-session", host["name"]), result)
            if len(self.requests) > 256:
                self.requests.pop(next(iter(self.requests)))
            return result

    LINK_WINDOW = 1800  # a launcher's first session counts within 30 minutes of its terminal

    def link_new_sessions(self):
        """A "새 세션" terminal names no session when it opens; its launcher starts one later.
        The earliest session that appears on its host after it opened (30 seconds of clock
        difference allowed), and no other terminal names, is recorded as its own, once: its
        card then shows "터미널 열림" and continuing it opens this terminal. Automated runs
        (Codex `exec`, Claude background agents) are never taken."""
        record = getattr(self.terminals, "link_native", None)
        if record is None:
            return
        try:
            terminals = self.terminals.list()
        except (RuntimeError, OSError, ValueError):
            return
        pending = sorted((t for t in terminals if t.get("mode") == "new-session" and t.get("alive")
                          and not t.get("closed") and not t.get("nativeSource")
                          and type(t.get("createdAt")) is int), key=lambda t: t["createdAt"])
        if not pending:
            return
        taken = {json.dumps(self.identity(t["nativeSource"]), sort_keys=True) for t in terminals
                 if isinstance(t.get("nativeSource"), dict)}
        for terminal in pending:
            host = terminal.get("host")
            data = (self.board.state.get(host) or {}).get("data") or {}
            opened = terminal["createdAt"]
            candidates = []
            for agent in ("codex", "claude"):
                for row in data.get(agent) or []:
                    native = self._native_of(host, agent, row)
                    started = row.get("startedAt")
                    if (native is None or type(started) is not int
                            or not opened - 30 <= started <= opened + self.LINK_WINDOW
                            or json.dumps(self.identity(native), sort_keys=True) in taken):
                        continue
                    candidates.append((started, native))
            if not candidates:
                continue
            native = min(candidates, key=lambda item: item[0])[1]
            try:
                if record(terminal["id"], native, confidence="estimated"):
                    taken.add(json.dumps(self.identity(native), sort_keys=True))
            except (KeyError, ValueError, RuntimeError, OSError):
                continue

    @staticmethod
    def _native_of(host, agent, row):
        """A listed row as a terminal's native session, or None for rows that never count."""
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None
        if agent == "codex":
            if row.get("source") not in (None, "cli", "vscode") or not HOME_KEY.match(row.get("home") or ""):
                return None
            try:
                identifier = str(uuid.UUID(str(row.get("id"))))
            except ValueError:
                return None
            return {"host": host, "agent": "codex", "id": identifier, "home": row["home"], "cwd": cwd}
        identifier = row.get("id")
        if (row.get("kind") == "background" or not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,127}", identifier)):
            return None
        native = {"host": host, "agent": "claude", "id": identifier, "home": None, "cwd": cwd}
        if row.get("kind") == "interactive":
            native["kind"] = "interactive"
        if isinstance(row.get("sessionId"), str) and re.fullmatch(r"[0-9A-Za-z-]{8,64}", row["sessionId"]):
            native["sessionId"] = row["sessionId"]
        return native

    @staticmethod
    def identity(row):
        return {k: row.get(k) for k in ("host", "agent", "id", "home")}

    def bot_request(self, source):
        service = getattr(self, "bot_requests", None)
        if service is None:
            return None
        for request in service.snapshot().get("requests", []):
            native = request.get("nativeRef")
            if isinstance(native, dict) and self.identity({**native, "host": request.get("host")}) == self.identity(source):
                return request
        return None

    def ensure_direct_session(self, source):
        owns = getattr(getattr(self, "bot_requests", None), "owns", None)
        if self.bot_request(source) or (owns is not None and owns(source)):
            raise ValueError("봇을 통해 받은 요청입니다. 요청자의 권한과 사용 범위를 유지하도록 원래 봇에서 이어가 주세요.")

    def ensure_direct_terminal(self, terminal_id):
        for terminal in self.terminals.list():
            if terminal.get("id") != terminal_id or terminal.get("linkConfidence") == "estimated":
                continue
            native = terminal.get("nativeSource") or (terminal.get("metadata") or {}).get("nativeSource")
            if isinstance(native, dict):
                self.ensure_direct_session(native)
            return

    def resume_preview(self, source):
        host = next(h for h in self.board.hosts if h["name"] == source["host"])
        if not self.board.state[host["name"]]["ok"]:
            return {"allowed": False, "blockedByBoard": False,
                    "reason": "기기가 다시 연결되면 같은 세션을 열 수 있습니다."}
        # A tmux disconnect is recorded separately from an explicit close. Old
        # ambiguous tombstones are not proof that the user ended the session.
        for terminal in self.terminals.list():
            native = terminal.get("nativeSource") or (terminal.get("metadata") or {}).get("nativeSource")
            if (terminal.get("closedReason") == "user" and terminal.get("linkConfidence") != "estimated"
                    and isinstance(native, dict) and self.identity(native) == self.identity(source)):
                return {"allowed": False, "blockedByBoard": True,
                        "reason": "웹보드에서 직접 닫은 세션입니다. 자동으로 다시 시작하지 않습니다."}
        selector = {key: source.get(key) for key in ("host", "agent", "id", "home", "cwd", "kind", "sessionId", "phase")}
        result = self.transfers.rpc(host, "resume_preview", {"source": selector}, timeout=12)
        if not isinstance(result, dict) or type(result.get("allowed")) is not bool:
            raise RuntimeError("원본 세션의 재개 가능 여부를 확인하지 못했습니다.")
        return result

    def plan(self, payload):
        source = self.board.source(payload.get("source"))
        self.ensure_direct_session(source)
        target = payload.get("target")
        if not isinstance(target, dict) or target.get("agent") not in ("claude", "codex"):
            raise ValueError("이어갈 에이전트와 계정을 선택해 주세요.")
        target = {key: target.get(key) for key in ("host", "agent", "account")}
        if any(not isinstance(value, str) for value in target.values()):
            raise ValueError("기기·에이전트·계정을 선택해 주세요.")
        host = next((h for h in self.board.hosts if h["name"] == target.get("host")), None)
        if host is None:
            raise ValueError("등록되지 않은 기기입니다.")
        current = "claude:default" if source["agent"] == "claude" else "codex:" + source["home"]
        same = source["host"] == target["host"] and source["agent"] == target["agent"] and current == target.get("account")
        cross_host = source["host"] != target["host"]
        source_host = next(h for h in self.board.hosts if h["name"] == source["host"])
        windows = source_host.get("platform") == "windows"
        if (windows or host.get("platform") == "windows") and not same:
            raise ValueError(WINDOWS_READ_ONLY)
        resume = payload.get("resume") is True
        if resume and (not same or source["agent"] != "codex"):
            raise ValueError("같은 기기·계정의 Codex 세션만 다시 열 수 있습니다.")
        resume = resume or (same and source["agent"] == "codex" and source.get("phase") in ("closed", "notLoaded"))
        allowed, reason, profile = True, None, None
        warnings, transfer_preview = [], None
        if cross_host:
            try:
                choices = self.profiles() if host["local"] else self.transfers.profiles(host, refresh=True)
                profile = next((p for p in choices if p["id"] == target["account"] and p["agent"] == target["agent"]), None)
                if not profile or not profile.get("available"):
                    allowed, reason = False, (profile or {}).get("reason") or "대상 기기의 기존 계정을 확인해 주세요."
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc)
            if not self.board.state[host["name"]]["ok"]:
                allowed, reason = False, "대상 기기의 연결을 확인한 뒤 다시 시도해 주세요."
        elif host["local"]:
            profile = next((p for p in self.profiles() if p["id"] == target.get("account") and p["agent"] == target["agent"]), None)
            if not profile or not profile.get("available"):
                allowed, reason = False, (profile or {}).get("reason") or "이 계정의 저장된 로그인을 확인해 주세요."
        elif same:
            profile = {"id": current, "agent": source["agent"], "home": source.get("home", ".claude"), "label": source.get("account") or "현재 계정", "available": True}
        elif not self.board.state[host["name"]]["ok"]:
            allowed, reason = False, "원격 기기의 연결을 확인한 뒤 다시 시도해 주세요."
        else:
            # Another agent or account on the same remote host: that host's helper prepares it.
            try:
                choices = self.transfers.profiles(host, refresh=True)
                profile = next((p for p in choices if p["id"] == target["account"] and p["agent"] == target["agent"]), None)
                if not profile or not profile.get("available"):
                    allowed, reason = False, (profile or {}).get("reason") or "원격 기기의 대상 계정을 확인해 주세요."
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc) or "원격 기기의 실행 연결을 확인하지 못했습니다."
        if not source["online"]:
            allowed, reason = False, "원본 기기의 연결을 확인한 뒤 다시 시도해 주세요."
        if not same and not cross_host and source.get("phase") in ("working", "needs_input"):
            allowed, reason = False, "진행 중인 실행이나 승인 요청을 원래 터미널에서 마무리한 뒤 전환해 주세요."
        if same and source["agent"] == "claude" and source.get("kind") != "background":
            allowed, reason = False, "이 Claude 세션은 기존 터미널에서 실행 중입니다. 백그라운드 세션만 직접 연결할 수 있어요."
        if allowed and resume:
            preview = self.resume_preview(source)
            allowed, reason = preview["allowed"], preview.get("reason")
        elif allowed and windows:
            try:
                self.transfers.rpc(host, "terminal_plan", {"source": source}, timeout=12)
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc)
        config = settings.load()
        if source_host["local"]:
            from launch import source_environment as resolve_environment
            source_environment = resolve_environment(source)
        else:
            # Another host's path is not resolved here: its names give a quick estimate.
            source_environment = settings.quick_environment(source, config=config)
        if not same and not cross_host and profile and settings.crosses_boundary(
                target["agent"], source_environment, profile.get("environment", "default"), config):
            allowed, reason = False, "원본과 대상의 실행 환경 경계가 다릅니다."
        if not shutil.which("tmux", path=collector_path()):
            allowed, reason = False, "이 기기에 tmux를 설치한 뒤 터미널을 열 수 있어요."
        mode = "transfer" if cross_host else ("resume" if resume else "attach" if same else "handoff")
        summary = ("코드·미커밋 변경·최근 대화를 대상 기기로 넘겨 이어갑니다." if cross_host else
                   "같은 기기·계정에서 저장된 대화를 다시 엽니다. 메시지는 보내지 않습니다." if resume else
                   "기존 작업의 터미널에 연결합니다." if same else "작업 맥락을 자동으로 넘겨 새 터미널을 준비합니다.")
        route_key = hashlib.sha256(json.dumps([self.identity(source), target], sort_keys=True).encode()).hexdigest()
        existing = next((item for item in self.terminals.list() if item.get("alive") and item.get("key", "").split(":")[0] == route_key), None)
        if allowed and existing:
            mode, summary = "reconnect", "이미 열어 둔 터미널에 다시 연결합니다."
            if not same:
                warnings.append("이전 인계 뒤 원본에 추가된 대화는 다시 전달하지 않습니다. 새 요청은 연결한 터미널에서 입력해 주세요.")
        elif cross_host and allowed:
            try:
                transfer_preview = self.transfers.preview(source, target, source_host, host)
                source_environment = transfer_preview.get("workspace", {}).get("sourceEnvironment")
                if not isinstance(source_environment, str) or settings.crosses_boundary(
                        target["agent"], source_environment, profile.get("environment", "default"), config):
                    allowed, reason = False, "원본과 대상의 실행 환경 경계가 다릅니다."
                warnings.extend(transfer_preview.get("workspace", {}).get("warnings", []))
                warnings.append("진행 중인 응답을 중단한 뒤 이전합니다. 승인 대기와 프로세스 자체는 새 기기로 이동하지 않습니다.")
            except (ValueError, RuntimeError, OSError) as exc:
                allowed, reason = False, str(exc)
        elif not same:
            warnings.append("최근 대화와 파일 변경 상태를 넘겨 새 실행을 준비합니다. 기존 프로세스·승인 상태는 이어지지 않습니다.")
        plan = {"id": secrets.token_hex(16), "mode": mode, "allowed": allowed,
                "reason": reason, "summary": summary,
                "warnings": warnings, "source": self.identity(source), "target": target,
                "expiresAt": int(time.time()) + 180}
        if transfer_preview is not None:
            plan["transfer"] = transfer_preview
        with self.lock:
            self.plans = {k: v for k, v in self.plans.items() if v[0]["expiresAt"] > time.time()}
            if len(self.plans) >= 64:
                self.plans.pop(next(iter(self.plans)))
            self.plans[plan["id"]] = (plan, source, profile)
        return plan

    def _link_handoff_goals(self, result):
        """Native launch results identify both ends; time-based new-session links do not."""
        terminal = result.get("terminal") or {}
        mode = terminal.get("launchMode") or terminal.get("mode")
        if (mode not in ("handoff", "transfer") or not terminal.get("sourceKey")
                or not terminal.get("nativeSource") or terminal.get("linkConfidence") == "estimated"):
            return result
        try:
            source = json.loads(terminal["sourceKey"])
            count = self.goals.link_sessions(source, terminal["nativeSource"], actor_label=BRAND["shortName"])
            if getattr(self, "work", None) is not None:
                self.work.link_sessions(source, terminal["nativeSource"])
            if count:
                result["goalLinksUpdated"] = count
            result.pop("goalLinkWarning", None)
        except (GoalError, ValueError, TypeError):
            # The native launch may already be running: never report that it failed or
            # encourage another launch just because optional goal metadata could not save.
            result["goalLinkWarning"] = "세션은 준비됐지만 작업 연결을 저장하지 못했습니다. 작업 상세에서 세션을 연결해 주세요."
        return result

    def launch(self, plan_id, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
            raise ValueError("실행 요청 식별자가 필요합니다.")
        with self.lock:
            if request_id in self.requests:
                previous_plan, result = self.requests[request_id]
                if previous_plan != plan_id:
                    raise ValueError("이미 사용한 요청입니다.")
                if isinstance(result, LaunchFailure):
                    raise result
                return self._link_handoff_goals(result)
            saved = self.plans.get(plan_id)
            if not saved or saved[0]["expiresAt"] <= time.time():
                raise ValueError("선택이 만료됐어요. 실행 설정을 다시 확인해 주세요.")
            plan, source, profile = saved
            if not plan["allowed"]:
                raise ValueError(plan["reason"])
            key_base = hashlib.sha256(json.dumps([plan["source"], plan["target"]], sort_keys=True).encode()).hexdigest()
            previous = [item for item in self.terminals.list() if item.get("key", "").split(":")[0] == key_base]
            live = next((item for item in reversed(previous) if item.get("alive")), None)
            if live:
                result = {"terminal": live, "mode": plan["mode"], "reused": True}
                self.requests[request_id] = (plan_id, result)
                return self._link_handoff_goals(result)
            if plan["mode"] == "reconnect":
                raise RuntimeError("열어 둔 터미널이 종료됐어요. 실행 설정을 다시 확인해 주세요.")
            # A new explicit launch after a completed CLI is separate from reconnect.
            key = key_base + (":" + plan_id if previous else "")
            with self.terminals.reserve(key):
                # Re-validate cached identity, and refresh original status at the transition boundary.
                host = next(h for h in self.board.hosts if h["name"] == source["host"])
                if not self.board.poll(host):
                    raise RuntimeError("원본 기기에 연결할 수 없어 실행하지 않았어요.")
                fresh = self.board.source(plan["source"])
                if plan["mode"] == "handoff" and fresh.get("phase") in ("working", "needs_input"):
                    raise ValueError("원래 작업이 실행 중이거나 승인을 기다리고 있어요. 마무리한 뒤 다시 전환해 주세요.")
                source = fresh
                self.ensure_direct_session(source)
                if plan["mode"] == "resume":
                    preview = self.resume_preview(source)
                    if not preview["allowed"]:
                        raise ValueError(preview.get("reason") or "이 세션은 다시 열 수 없습니다.")
                    source = {**source, "resume": True}
                if plan["mode"] == "transfer":
                    target_host = next(h for h in self.board.hosts if h["name"] == plan["target"]["host"])
                    def read_for_transfer():
                        result = self.board.read(source["host"], source["agent"], source.get("sessionId") or source["id"], source.get("home"), fresh=True)
                        if result.get("error"):
                            raise RuntimeError("중단 후 최근 대화를 읽지 못해 이전을 중단했습니다.")
                        return result.get("messages") or []
                    spec = self.transfers.execute(source, plan["target"], host, target_host, profile, request_id, read_for_transfer)
                elif host["local"]:
                    from launch import build_launch
                    messages = []
                    if plan["mode"] == "handoff":
                        result = self.board.read(source["host"], source["agent"], source.get("sessionId") or source["id"], source.get("home"), fresh=True)
                        if result.get("error"):
                            raise RuntimeError("원본 대화를 읽지 못해 인계를 중단했어요.")
                        messages = result.get("messages") or []
                    spec = build_launch(source, {**profile, "host": source["host"]}, messages, self.state_dir)
                    from managed_launch import bind
                    spec = bind(spec, profile, source["host"], receipt_path=self.state_dir / "native-launches" / (key_base + ".json"))
                elif plan["mode"] == "handoff":
                    result = self.board.read(source["host"], source["agent"], source.get("sessionId") or source["id"], source.get("home"), fresh=True)
                    if result.get("error"):
                        raise RuntimeError("원본 대화를 읽지 못해 인계를 중단했어요.")
                    spec = self.remote_handoff(host, source, plan["target"], profile, request_id, key_base,
                                               result.get("messages") or [])
                else:
                    spec = self.remote_attach(host, source)
                metadata = {"title": source.get("title") or "작업", "host": plan["target"]["host"], "agent": profile["agent"],
                            "account": profile["label"], "sourceKey": json.dumps(plan["source"], sort_keys=True), "mode": plan["mode"]}
                if spec.get("connectionMode"):
                    metadata.update(mode=spec["connectionMode"], launchMode=plan["mode"])
                native_source = spec.get("nativeSource")
                if plan["mode"] in ("attach", "resume"):
                    native_source = {**self.identity(source), "cwd": source.get("cwd"), "kind": source.get("kind")}
                if native_source:
                    metadata["nativeSource"] = native_source
                if spec.get("transferId"):
                    metadata.update(transferId=spec["transferId"], destinationCwd=spec["destinationCwd"], sourceHost=source["host"])
                try:
                    terminal = self.terminals.create(key, spec["argv"], spec["cwd"], spec["env"], metadata)
                    if spec.get("nativeReceiptPath"):
                        from managed_launch import mark_attached
                        mark_attached(spec, terminal["id"])
                    if spec.get("transferId"):
                        self.transfers.mark_started(spec["transferId"], terminal["id"])
                except (RuntimeError, ValueError, OSError) as exc:
                    # Preparation can start a native turn before the TUI connects.
                    # Retain an explicit recovery result instead of rerunning it.
                    recovery = {"targetStarted": None, "host": plan["target"]["host"],
                                "destinationCwd": spec.get("destinationCwd") or spec["cwd"]}
                    if native_source:
                        recovery["nativeSource"] = native_source
                    try:
                        opened = next((t for t in self.terminals.list() if t.get("key") == key), None)
                    except (RuntimeError, OSError):
                        opened = None
                    if opened:
                        recovery["terminal"] = opened
                    failure = LaunchFailure(recovery)
                    self.requests[request_id] = (plan_id, failure)
                    raise failure from exc
                result = {"terminal": terminal, "mode": plan["mode"]}
                self.requests[request_id] = (plan_id, result)
                if len(self.requests) > 256:
                    self.requests.pop(next(iter(self.requests)))
                return self._link_handoff_goals(result)

    def remote_handoff(self, host, source, target, profile, request_id, route_key, messages):
        """Another agent or account on the same remote host, prepared and run by that host.

        The host's helper decides the boundary again with its own rules, writes the handoff
        document, binds the first input (`stage_handoff`) and reports the new native identity.
        The terminal then runs its `handoff <token>` step, which execs the prepared launch once.
        """
        from transfer import PRE_BIND_STAGES, UNKNOWN_HANDOFF, WorkerError
        from transfer_worker import handoff_token, SELECTOR
        token = handoff_token(request_id)
        # A durable request record: one board request never stages twice, even after a restart.
        marker = self.state_dir / "handoffs" / (token + ".json")
        if os.path.lexists(marker):
            raise ValueError("이미 처리한 인계 요청입니다. 열린 터미널이나 원격 기기의 작업 목록을 확인해 주세요.")
        record = {"token": token, "host": host["name"], "status": "staging", "startedAt": int(time.time())}
        private_json(marker, record)
        selector = {key: source.get(key) for key in SELECTOR}
        try:
            prepared = self.transfers.rpc(host, "stage_handoff", {
                "source": selector, "target": target, "messages": messages,
                "requestId": request_id, "routeKey": route_key}, timeout=120)
            if (not isinstance(prepared, dict) or prepared.get("token") != token
                    or not isinstance(prepared.get("cwd"), str) or not prepared["cwd"].startswith("/")):
                raise RuntimeError("원격 인계 준비 응답이 올바르지 않습니다.")
        except WorkerError as exc:
            record.update(status="failed", error=str(exc)[:1000], updatedAt=int(time.time()))
            private_json(marker, record)
            # Refused before any native session was bound: nothing started there.
            if exc.stage in PRE_BIND_STAGES or exc.stage == "prepared":
                raise ValueError(str(exc)) from None
            raise RuntimeError(UNKNOWN_HANDOFF) from None
        except (OSError, RuntimeError, ValueError):
            record.update(status="unknown", updatedAt=int(time.time()))
            private_json(marker, record)
            raise RuntimeError(UNKNOWN_HANDOFF) from None
        native = prepared.get("nativeSource")
        keys = ("host", "agent", "id", "home", "cwd", "kind", "sessionId")
        if (not isinstance(native, dict) or native.get("host") != host["name"]
                or native.get("agent") != profile["agent"]):
            native = None
        else:
            native = {key: native[key] for key in keys if key in native}
        record.update(status="prepared", updatedAt=int(time.time()))
        private_json(marker, record)
        import collector
        return {"argv": self.transfers.command(host, ["-I", self.transfers.runtime_paths[host["name"]],
                "handoff", token], tty=True),
                "cwd": str(Path.home()), "env": collector.tool_env(), "destinationCwd": prepared["cwd"],
                "nativeSource": native,
                "connectionMode": "attach" if prepared.get("connectionMode") == "attach" else None}

    def remote_attach(self, host, source):
        # The owning helper resolves binaries and explicit environment settings.
        # No configuration values or credentials cross back into an SSH command.
        if host.get("platform") == "windows":
            spec = self.transfers.rpc(host, "terminal_plan", {"source": source}, timeout=12)
            import collector
            return {"argv": self.transfers.windows_terminal(host, spec), "cwd": str(Path.home()),
                    "env": collector.tool_env(), "connectionMode": spec.get("connectionMode") or "attach",
                    "nativeSource": {**self.identity(source), "cwd": source.get("cwd"), "kind": source.get("kind")}}
        profiles = self.transfers.profiles(host, refresh=True)
        identity = "claude:default" if source["agent"] == "claude" else "codex:" + source["home"]
        profile = next((p for p in profiles if p.get("id") == identity and p.get("available")), None)
        if profile is None:
            raise ValueError("원격 기기의 원본 실행 프로필을 확인하지 못했습니다.")
        selector = {key: source.get(key) for key in ("host", "agent", "id", "home", "cwd", "kind", "sessionId", "phase", "resume")}
        import collector
        return {"argv": self.transfers.command(host, ["-I", self.transfers.runtime_paths[host["name"]],
                "attach", json.dumps(selector, ensure_ascii=False)], tty=True),
                "cwd": str(Path.home()), "env": collector.tool_env()}


def collector_path():
    return os.pathsep.join(["/opt/homebrew/bin", str(Path.home() / ".local/bin"), "/usr/local/bin", os.environ.get("PATH", "")])


def load_token(path=TOKEN_FILE):
    path = settings.safe_path(path, private=True, allow_missing=True)
    settings.safe_path(path.parent, directory=True, private=True, allow_missing=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as incoming:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("접속 토큰의 소유자와 비공개 권한을 확인해 주세요.")
        value = incoming.read(513).strip()
    if not value or len(value) > 512 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("접속 토큰 파일 형식이 올바르지 않습니다.")
    return value


def load_tailscale_user(path):
    """Read one private, owner-controlled Serve login; never log its contents."""
    path = settings.safe_path(path, private=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Tailscale 사용자 파일은 심볼릭 링크를 사용할 수 없습니다.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
                    or not stat.S_ISREG(metadata.st_mode)):
                raise ValueError("Tailscale 사용자 파일은 현재 사용자 소유의 비공개 일반 파일이어야 합니다.")
            raw = stream.read(4097)
            value = raw.strip()
    except (OSError, UnicodeError):
        raise ValueError("Tailscale 사용자 파일을 읽지 못했습니다.") from None
    if not value or len(raw) > 4096 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Tailscale 사용자 파일에는 허용할 로그인 하나를 적어 주세요.")
    return value


def host_allowed(value):
    """Reject DNS-rebinding style Host headers; only loopback and tailnet names are served."""
    value = value or ""
    host = value.split("]")[0] + "]" if value.startswith("[") else value.rsplit(":", 1)[0]
    return host in ("127.0.0.1", "localhost", "[::1]") or host.endswith(".ts.net")


class Handler(BaseHTTPRequestHandler):
    server_version = BRAND["serverVersion"]
    responded = False

    # ---------- plumbing ----------
    def send_response(self, code, message=None):
        self.responded = True
        super().send_response(code, message)

    def guarded(self, route):
        """An unexpected error becomes one generic JSON 500 without details.

        It never claims a dispatch state: a chat send may already have started.
        """
        self.responded = False
        try:
            route()
        except Exception as exc:
            log_failure(f"{self.command} {urlparse(self.path).path} 처리", exc)
            if self.responded:
                self.close_connection = True
                return
            try:
                self.send_json(500, {"error": INTERNAL_ERROR})
            except OSError:
                self.close_connection = True

    def log_message(self, fmt, *args):
        # Log the path only; query strings may carry a one-time login code.
        sys.stderr.write("%s %s %s\n" % (self.log_date_time_string(), self.command, urlparse(self.path).path))

    def accepts_gzip(self):
        return accepts_gzip(self.headers.get("Accept-Encoding") if self.headers else None)

    def send_body(self, code, body, content_type, extra=None, compress=False):
        headers = {**SECURITY_HEADERS, **(extra or {})}
        if compress:
            headers["Vary"] = "Accept-Encoding"
            if len(body) >= GZIP_MIN and self.accepts_gzip():
                body = gzip.compress(body, compresslevel=6, mtime=0)
                headers["Content-Encoding"] = "gzip"
        self.send_response(code)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, data, extra=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_body(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})},
                       compress=True)

    def send_not_modified(self, extra):
        self.send_response(304)
        for key, value in {**SECURITY_HEADERS, **extra}.items():
            self.send_header(key, value)
        self.end_headers()

    def tailscale_authorized(self):
        """Trust identity headers only from the opted-in loopback HTTPS Serve route.

        The local Serve process and other processes of this OS user are trusted.
        Serve replaces client-supplied Tailscale identity headers before proxying.
        """
        expected = self.server.tailscale_user
        if expected is None:
            return False
        for name in ("Host", "X-Forwarded-Proto", "Tailscale-User-Login"):
            if len(self.headers.get_all(name, [])) != 1:
                return False
        try:
            local = ipaddress.ip_address(self.client_address[0]).is_loopback
            hostname = urlparse("//" + self.headers["Host"]).hostname or ""
        except ValueError:
            return False
        return (local and hostname.endswith(".ts.net")
                and self.headers["X-Forwarded-Proto"] == "https"
                and hmac.compare_digest(self.headers["Tailscale-User-Login"].encode(), expected.encode()))

    def authorized(self):
        if self.tailscale_authorized():
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return False
        name = self.server.cookie_name
        given = jar[name].value if name in jar else ""
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return hmac.compare_digest(header[7:].encode(), self.server.token.encode())
        with self.server.auth_lock:
            return self.server.sessions.get(given, 0) > time.time()

    def session_cookie(self, clear=False):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        value = "" if clear else secrets.token_urlsafe(32)
        if not clear:
            with self.server.auth_lock:
                now = time.time()
                self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
                if len(self.server.sessions) >= 64:
                    self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[value] = now + 86400
        return f"{self.server.cookie_name}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={0 if clear else 86400}{secure}"

    def origin_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site")
        parsed = urlparse(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")

    DISCARD_LIMIT = 1024 * 1024

    def discard_body(self, length):
        """Read a refused body that is not huge before answering, then close: a socket closed
        with the body unread resets the connection, and the client sees that instead of the
        answer. A larger one, or one that stalls, only closes."""
        self.close_connection = True
        if not 0 < length <= self.DISCARD_LIMIT:
            return
        previous = self.connection.gettimeout()
        try:
            self.connection.settimeout(5)
            while length > 0:
                chunk = self.rfile.read(min(length, 65536))
                if not chunk:
                    break
                length -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                self.connection.settimeout(previous)
            except OSError:
                pass

    def json_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("JSON 요청만 사용할 수 있습니다.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("요청 크기가 올바르지 않습니다.") from None
        if not 0 < length <= 65536:
            self.discard_body(length)
            raise ValueError("요청이 비었거나 너무 큽니다.")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ValueError("요청 형식이 올바르지 않습니다.") from None
        if not isinstance(data, dict):
            raise ValueError("객체 형식의 요청이 필요합니다.")
        return data

    # ---------- routes ----------
    def observe_work(self, source, conversation):
        if not isinstance(source, dict) or not all(source.get(k) for k in ("host", "agent", "id")):
            return
        try:
            self.server.work.observe(source, conversation)
        except (GoalError, OSError, ValueError):
            # Optional organization cannot turn a successful conversation read into a failure.
            self.server.work.health = {"state": "limited", "message": "작업 요약의 최신 내용을 저장하지 못했습니다."}

    def session_control(self, path, data):
        ref = data.get("source")
        source = self.server.board.source(ref)
        source["agent"] = ref.get("agent")
        source["home"] = source.get("home") or (".codex" if source["agent"] == "codex" else ".claude")
        if path == "/api/session/archive":
            archived = data.get("archived", True)
            if type(archived) is not bool:
                raise ValueError("보관 여부를 확인해 주세요.")
            return self.server.work.archive_session(source, archived)
        self.server.workflow.ensure_direct_session(source)
        if not source.get("online"):
            raise RuntimeError("기기에 연결할 수 없습니다. 마지막 실행 상태를 유지합니다.")
        host = next(h for h in self.server.board.hosts if h["name"] == source["host"])
        selector = {key: source.get(key) for key in ("agent", "id", "home", "cwd", "kind", "sessionId", "phase")}
        args = {"source": selector}
        action = path == "/api/session/action"
        if action:
            if (data.get("action") not in ("interrupt", "terminate")
                    or not isinstance(data.get("requestId"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", data["requestId"])
                    or not isinstance(data.get("targetProof"), str) or len(data["targetProof"]) > 4096
                    or not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-f0-9]{64}", data["targetProof"])):
                raise ValueError("실행 대상과 요청 정보를 다시 확인해 주세요.")
            args.update({key: data[key] for key in ("action", "requestId", "targetProof")})
        try:
            result = self.server.workflow.transfers.rpc(host, "control_execute" if action else "control_preview",
                                                       args, timeout=12 if action else 8)
        except (RuntimeError, OSError):
            raise RuntimeError("세션 제어 결과를 확인하지 못했습니다. 현재 실행 상태를 확인해 주세요. 요청은 자동으로 다시 보내지 않습니다.") from None
        if not isinstance(result, dict) or (action and type(result.get("confirmed")) is not bool):
            raise RuntimeError("세션 제어 결과를 확인하지 못했습니다. 요청은 자동으로 다시 보내지 않습니다.")
        if action and result.get("confirmed"):
            self.server.chat._forget_state(source)
            self.server.board.request_refresh(host["name"], force=False)
        return result

    def session_label(self, data):
        if set(data) != {'source', 'title'}:
            raise ValueError('세션과 보드 표시 제목을 확인해 주세요.')
        if isinstance(data['source'], dict) and data['source'].get('agent') == 'codex' and not data['source'].get('home'):
            raise ValueError('Codex 세션의 계정 홈을 확인해 주세요.')
        ref = label_reference(data['source'])
        board = self.server.board
        with board.lock:
            board.source(ref)  # Validate the registered host and listed native selector.
            host = board.state[ref['host']]
            matches = []
            for row in (host.get('data') or {}).get(ref['agent'], []):
                candidate = {**copy.deepcopy(row), 'host': ref['host'], 'agent': ref['agent']}
                try:
                    if label_reference(candidate) == ref:
                        matches.append(candidate)
                except GoalError:
                    continue
            if len(matches) != 1:
                raise ValueError('세션의 계정 홈 또는 중복 식별자를 확인해 주세요. 목록에서 다시 선택해 주세요.')
            source = {**matches[0], **ref, 'hostLabel': host['label'], 'online': host['ok'], 'fetchedAt': host['fetchedAt']}
            if ref['agent'] == 'claude' and (data['source'].get('sessionId') or None) != (source.get('sessionId') or None):
                raise ValueError('Claude 세션의 대화 식별자가 바뀌었습니다. 목록에서 다시 선택해 주세요.')
        self.server.workflow.ensure_direct_session(source)
        result = self.server.session_labels.set(source, data['title'])
        board.changed(force=True)
        return result

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.guarded(self.route_get)

    def do_POST(self):
        self.guarded(self.route_post)

    def route_get(self):
        if not host_allowed(self.headers.get("Host")):
            return self.send_body(421, b"misdirected", "text/plain; charset=utf-8")
        url = urlparse(self.path)
        if url.path == "/login":
            return self.login_once(parse_qs(url.query).get("once", [""])[0])
        if url.path.startswith("/api/"):
            if not self.authorized():
                return self.send_json(401, {"error": "로그인이 필요합니다."})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/events":
                return self.stream_events(query)
            try:
                if url.path == "/api/goals":
                    return self.send_json(200, self.server.goals.snapshot())
                if url.path == "/api/work":
                    return self.send_json(200, self.server.work.snapshot())
                if url.path == "/api/bot-requests":
                    return self.send_json(200, scrub_payload(self.server.bot_requests.snapshot()))
                if url.path == "/api/push":
                    return self.send_json(200, self.server.push.status(query.get("id")))
                if url.path == "/api/push/pending":
                    # Only this board's service worker reads (and so clears) a device's queue:
                    # a cross-site request cannot add this header without a preflight.
                    if self.headers.get("X-Board-Push") != "1":
                        return self.send_json(403, {"error": "이 보드의 알림에서만 읽을 수 있습니다."})
                    if self.command != "GET":
                        # HEAD runs this route too; it would clear the queue without a body to show.
                        return self.send_json(405, {"error": "알림 내용은 GET으로만 읽을 수 있습니다."})
                    return self.send_json(200, self.server.push.pending_for(query.get("id")))
                goal_match = re.fullmatch(r"/api/goals/([a-f0-9]{32})", url.path)
                if goal_match:
                    return self.send_json(200, self.server.goals.get(goal_match[1]))
                if url.path == "/api/snapshot":
                    if query.get("refresh") == "1":
                        self.server.board.request_refresh()
                    return self.send_json(200, self.snapshot())
                if url.path == "/api/read":
                    result = self.server.board.read(query.get("host"), query.get("agent"), query.get("id"), query.get("home"))
                    self.observe_work(query, result)
                    return self.send_json(200, result)
                if url.path == "/api/usage" and self.server.usage is not None:
                    return self.send_json(200, self.server.usage.snapshot(refresh=True))
                if url.path == "/api/capabilities":
                    result = self.server.workflow.capabilities()
                    return self.send_json(200, {**result, "csrfToken": self.server.csrf,
                                               "authMode": "tailscale" if self.tailscale_authorized() else "token"})
                if url.path == "/api/terminals":
                    # Read before the list: a change in between is announced with a newer
                    # version, so the client reads again rather than missing it.
                    self.server.workflow.link_new_sessions()
                    self.server.workflow.describe_launchers()
                    version = self.server.events.terminals_version
                    records = self.server.terminals.list()
                    result = {"terminals": records, "version": version}
                    try:
                        self.server.terminal_inputs.prune_all_closed(records)
                    except (ValueError, RuntimeError, OSError):
                        result["inputCleanupPending"] = True
                    return self.send_json(200, result)
                match = re.fullmatch(r"/api/terminal/([a-f0-9]{32})/events", url.path)
                if match:
                    after = int(query.get("after", "0"))
                    wait = min(max(float(query.get("wait", "20")), 0), 20)
                    if after < 0:
                        raise ValueError("출력 위치가 올바르지 않습니다.")
                    result = self.server.terminals.read(match[1], after, wait, epoch=query.get("epoch"))
                    return self.send_json(200, result)
            except GoalError as exc:
                return self.send_json(exc.status, exc.payload)
            except (ValueError, KeyError) as exc:
                return self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "터미널을 찾지 못했어요."})
            except RuntimeError as exc:
                return self.send_json(409, {"error": str(exc)})
            except (OSError, ImportError):
                return self.send_json(503, {"error": "실행 연결을 준비하지 못했어요. 설정을 확인해 주세요."})
            return self.send_json(404, {"error": "없는 경로입니다."})
        return self.static(url.path, url.query)

    def snapshot(self):
        """The board's snapshot with this server process's bootId."""
        # A "새 세션" terminal's session may have appeared in this snapshot.
        self.server.workflow.link_new_sessions()
        snapshot = self.server.session_labels.decorate(self.server.board.snapshot())
        return {**snapshot, "bootId": self.server.boot_id}

    def stream_events(self, query):
        """Server-sent events; the same cookie login as other reads, no CSRF for a GET."""
        from events import chat_ref
        ref = chat_ref(query)
        if ref is not None:
            # Same sources as /api/chat/state. An offline host may still be followed:
            # its watcher backs off until the host is reachable again. The watcher follows
            # the server's identity of the listed source, never the query's spelling.
            try:
                ref = self.server.chat.identity(self.server.chat.resolve(ref))
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            except (RuntimeError, OSError):
                return self.events_unavailable()
        hub = self.server.events
        try:
            stream = hub.open(ref)
        except RuntimeError:
            # The conversation watcher could not be started.
            return self.events_unavailable()
        if stream is None:
            if hub.closed:
                return self.events_unavailable()
            # Only this answer means "every stream or watcher place is taken".
            return self.send_json(503, {"error": "실시간 연결이 너무 많습니다. 잠시 뒤 다시 연결합니다.",
                                        "code": "events_busy"}, {"Retry-After": "10"})
        watching = False
        try:
            self.send_response(200)
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            self.connection.settimeout(STREAM_WRITE_TIMEOUT)
            # Counted outside the hub lock: resident watchers run while anyone watches.
            self.server.board.viewer_joined()
            watching = True
            hub.pump(stream, self.stream_write, self.peer_closed)
        finally:
            hub.close_stream(stream)
            if watching:
                self.server.board.viewer_left()

    def events_unavailable(self):
        return self.send_json(503, {"error": "실시간 연결을 준비하지 못했습니다. 잠시 뒤 다시 연결합니다.",
                                    "code": "events_unavailable"}, {"Retry-After": "10"})

    def follow_closely(self, data):
        """After a successful send or menu answer, its conversation watcher reads quickly again."""
        try:
            # The watcher follows the server's identity of the source, as in stream_events.
            self.server.events.boost(self.server.chat.identity(self.server.chat.resolve(data.get("source"))))
        except (ValueError, RuntimeError, OSError):
            # No longer listed: there is no watcher to speed up.
            pass
        except Exception as exc:
            # The board input already succeeded; a watcher hint never changes that reply.
            log_failure("대화 감시 간격 조정", exc)

    def stream_write(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def peer_closed(self):
        """An event-stream client sends nothing after its request, so readable means gone."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def route_post(self):
        from chat import PromptRejected, SendRejected
        path = urlparse(self.path).path
        not_started = {"dispatchState": "not_started"} if path == "/api/chat/send" else {}
        if not host_allowed(self.headers.get("Host")):
            if not_started:
                return self.send_json(421, {"error": "접속 주소를 확인해 주세요.", **not_started})
            return self.send_body(421, b"misdirected", "text/plain; charset=utf-8")
        if not self.origin_allowed():
            return self.send_json(403, {"error": "이 보드에서 요청해 주세요.", **not_started})
        if path == "/api/chat/upload":
            if not self.authorized():
                return self.send_json(401, {"error": "로그인이 필요합니다."})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), self.server.csrf.encode()):
                return self.send_json(403, {"error": "화면을 새로 열고 다시 시도해 주세요."})
            try:
                from chat import MAX_FILE
                length = int(self.headers.get("Content-Length") or 0)
                if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_FILE:
                    raise ValueError("첨부 파일은 20MiB까지 보낼 수 있습니다.")
                if self.headers.get_content_type() != "application/octet-stream":
                    raise ValueError("파일 전송 형식이 올바르지 않습니다.")
                raw_source, raw_name = self.headers.get("X-Chat-Source", ""), self.headers.get("X-File-Name", "")
                if len(raw_source) > 4096 or len(raw_name) > 2048:
                    raise ValueError("파일 정보가 너무 깁니다.")
                source = json.loads(unquote(raw_source))
                # Validate the destination before reading an upload body.
                self.server.chat.source(source)
                self.connection.settimeout(30)
                if not self.server.upload_gate.acquire(blocking=False):
                    raise RuntimeError("다른 파일을 전송 중입니다.")
                try:
                    content = self.rfile.read(length)
                    if len(content) != length:
                        raise ValueError("파일을 끝까지 받지 못했습니다.")
                    return self.send_json(200, self.server.chat.upload(source, unquote(raw_name), content))
                finally:
                    self.server.upload_gate.release()
            except (ValueError, KeyError):
                return self.send_json(400, {"error": "파일 또는 대상 정보가 올바르지 않습니다. 20MiB 이하 파일을 다시 선택해 주세요."})
            except (RuntimeError, OSError):
                return self.send_json(409, {"error": "파일 전송을 확인하지 못했습니다. 기기 연결을 확인해 주세요."})
        try:
            data = self.json_body()
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc), **not_started})
        if path == "/login":
            token = str(data.get("token") or "")
            if not token or not hmac.compare_digest(token.encode(), self.server.token.encode()):
                time.sleep(0.5)
                return self.send_json(403, {"error": "토큰이 맞지 않습니다."})
            return self.send_json(200, {"ok": True, "csrfToken": self.server.csrf}, {"Set-Cookie": self.session_cookie()})
        if not self.authorized():
            return self.send_json(401, {"error": "로그인이 필요합니다.", **not_started})
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), self.server.csrf.encode()):
            return self.send_json(403, {"error": "보안 연결을 갱신한 뒤 다시 시도해 주세요.",
                                        "code": "csrf_expired", **not_started})
        try:
            if path == "/api/bot-requests/refresh":
                return self.send_json(200, scrub_payload(self.server.bot_requests.snapshot(force=True)))
            if path == "/api/push/subscribe":
                return self.send_json(200, self.server.push.subscribe(data))
            if path == "/api/push/unsubscribe":
                return self.send_json(200, self.server.push.unsubscribe(data))
            if path == "/api/push/test":
                return self.send_json(200, self.server.push.test(data))
            work_match = re.fullmatch(r"/api/work/([a-f0-9]{32})", path)
            if work_match:
                return self.send_json(200, self.server.work.update(work_match[1], data))
            if path in ("/api/session/preview", "/api/session/action", "/api/session/archive"):
                return self.send_json(200, self.session_control(path, data))
            if path == "/api/session/label":
                return self.send_json(200, self.session_label(data))
            if path == "/api/goals":
                return self.send_json(201, self.server.goals.save(data))
            goal_match = re.fullmatch(r"/api/goals/([a-f0-9]{32})", path)
            if goal_match:
                return self.send_json(200, self.server.goals.save(data, goal_match[1]))
            if path == "/logout":
                jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
                if self.server.cookie_name in jar:
                    with self.server.auth_lock:
                        self.server.sessions.pop(jar[self.server.cookie_name].value, None)
                return self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie(clear=True)})
            if path == "/api/refresh":
                host = data.get("host")
                if host is not None and not isinstance(host, str):
                    raise ValueError("기기 이름 형식이 올바르지 않습니다.")
                self.server.board.request_refresh(host, force=True)
                return self.send_json(200, self.snapshot())
            if path == "/api/usage/refresh" and self.server.usage is not None:
                return self.send_json(200, self.server.usage.snapshot(force=True))
            if path == "/api/chat/state":
                result = self.server.chat.state(data)
                self.observe_work(result.get("route") or data.get("source"), result)
                return self.send_json(200, result)
            if path == "/api/chat/send":
                result = self.server.chat.send(data)
                self.follow_closely(data)
                return self.send_json(200, result)
            if path == "/api/chat/prompt":
                result = self.server.chat.prompt(data)
                if data.get("action") == "answer":
                    self.follow_closely(data)
                return self.send_json(200, result)
            if path == "/api/plan":
                return self.send_json(200, self.server.workflow.plan(data))
            if path == "/api/launch":
                return self.send_json(200, self.server.workflow.launch(data.get("planId"), data.get("requestId")))
            if path == "/api/new-session":
                return self.send_json(200, self.server.workflow.new_session(data))
            if path == "/api/app-server":
                return self.send_json(200, self.server.workflow.start_app_server(data))
            match = re.fullmatch(r"/api/terminal/([a-f0-9]{32})/(input|resize|scroll|scroll-end|detach|close)", path)
            if match:
                tid, action = match.groups()
                if action in ("input", "close"):
                    self.server.workflow.ensure_direct_terminal(tid)
                if action == "input":
                    request_id = data.get("requestId")
                    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
                        raise ValueError("입력 요청 식별자가 필요합니다.")
                    text = data.get("text")
                    if not isinstance(text, str) or len(text.encode()) > 32768:
                        raise ValueError("입력이 비었거나 너무 깁니다.")
                    self.server.terminal_inputs.submit(tid, request_id, text, self.server.terminals.write)
                elif action == "resize":
                    self.server.terminals.resize(tid, data.get("cols"), data.get("rows"))
                elif action == "scroll":
                    # mode "app" (wheel events to a full-screen TUI) or "history" (tmux copy-mode).
                    return self.send_json(200, self.server.terminals.scroll(tid, data.get("lines")))
                elif action == "scroll-end":
                    self.server.terminals.scroll_end(tid)
                elif action == "close":
                    self.server.terminals.close_terminal(tid)
                    try:
                        self.server.terminal_inputs.prune_closed(tid, self.server.terminals.list())
                    except (ValueError, RuntimeError, OSError):
                        # Closing the CLI succeeded even if optional receipt cleanup failed.
                        return self.send_json(200, {"ok": True, "inputCleanupPending": True})
                else:
                    self.server.terminals.detach(tid)
                return self.send_json(200, {"ok": True})
        except GoalError as exc:
            return self.send_json(exc.status, exc.payload)
        except SendRejected as exc:
            # A refusal with a code (e.g. "terminal_required") lets the composer act on it at once.
            payload = {"error": str(exc), "dispatchState": "not_started"}
            if exc.code:
                payload["code"] = exc.code
            return self.send_json(exc.status, payload)
        except PromptRejected as exc:
            # No key reached the terminal; "prompt_changed" means read the screen again.
            return self.send_json(exc.status, {"error": str(exc), "code": exc.code})
        except LaunchFailure as exc:
            return self.send_json(409, {"error": str(exc), "recovery": exc.recovery})
        except (ValueError, KeyError) as exc:
            return self.send_json(400, {"error": str(exc) if isinstance(exc, ValueError) else "터미널을 찾지 못했어요."})
        except RuntimeError as exc:
            return self.send_json(409, {"error": str(exc)})
        except (OSError, ImportError):
            return self.send_json(503, {"error": "실행 연결을 준비하지 못했어요. 원래 작업은 유지됩니다."})
        return self.send_json(404, {"error": "없는 경로입니다."})

    def login_once(self, code):
        """One-time code from `--open`; the long-lived token itself never appears in a URL."""
        with self.server.once_lock:
            expected, expires = self.server.once
            valid = (bool(code) and bool(expected) and time.time() < expires
                     and hmac.compare_digest(code.encode(), expected.encode()))
            if valid:
                self.server.once = ("", 0)
        if not valid:
            return self.send_body(403, "로그인 링크가 만료됐습니다. 토큰으로 로그인하세요.".encode(), "text/plain; charset=utf-8")
        self.send_response(303)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Set-Cookie", self.session_cookie())
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def static(self, path, query=""):
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        assets = self.server.assets
        entry = assets.index() if name == "index.html" else assets.file(name)
        if entry is None:
            return self.send_body(404, b"not found", "text/plain; charset=utf-8")
        # Only a URL naming the current content hash may be kept without revalidation.
        immutable = name not in REVALIDATED and parse_qs(query).get("v", [""])[0] == entry["sha8"]
        packed = entry["gzip"] if self.accepts_gzip() else None
        etag = '"%s%s"' % (entry["etag"], "-gz" if packed else "")
        extra = {"Cache-Control": IMMUTABLE if immutable else "no-cache", "ETag": etag, "Vary": "Accept-Encoding"}
        if name == "sw.js":
            extra["Service-Worker-Allowed"] = "/"
        if etag_matches(self.headers.get("If-None-Match"), etag):
            return self.send_not_modified(extra)
        if packed:
            extra["Content-Encoding"] = "gzip"
        self.send_body(200, packed or entry["body"], TYPES[Path(name).suffix], extra)


class BoardHTTPServer(ThreadingHTTPServer):
    # A page asks for its styles, scripts and first reads at once, and Tailscale Serve opens an
    # upstream connection for each: socketserver's backlog of 5 reset the rest (a 502, and a
    # page without its stylesheet that scrolls as one long sheet).
    request_queue_size = 128

    def server_close(self):
        work = getattr(self, "work", None)
        if work is not None:
            work.close()
        push = getattr(self, "push", None)
        if push is not None:
            push.close()
        # Open event streams and conversation watchers end instead of outliving the server.
        events = getattr(self, "events", None)
        if events is not None:
            events.close()
        board = getattr(self, "board", None)
        if board is not None and hasattr(board, "close_live"):
            board.close_live()
        manager = getattr(self, "terminals", None)
        if manager is not None and hasattr(manager, "close"):
            manager.close()
        super().server_close()


def make_server(bind, port, board, token, terminals=None, workflow=None, state_dir=STATE_DIR, max_terminals=None,
                tailscale_user=None, usage=None, automatic_work=False, push=True):
    """`usage` (usage.UsageMeter) serves /api/usage; without it the routes stay 404."""
    if bind not in ("127.0.0.1", "::1"):
        raise ValueError("서버는 loopback에서만 실행할 수 있습니다.")
    if tailscale_user is not None and (not isinstance(tailscale_user, str) or not tailscale_user
            or len(tailscale_user) > 4096 or any(char.isspace() or ord(char) < 32 for char in tailscale_user)):
        raise ValueError("허용할 Tailscale 로그인 하나가 필요합니다.")
    from terminal import MAX_TERMINALS, TerminalManager
    server = BoardHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.board, server.token, server.tailscale_user = board, token, tailscale_user
    server.usage = usage
    server.goals = Goals(state_dir)
    server.session_labels = SessionLabels(state_dir)
    server.cookie_name = COOKIE + "_" + str(server.server_address[1])
    server.once, server.once_lock = ("", 0), threading.Lock()
    server.sessions, server.auth_lock = {}, threading.Lock()
    server.csrf = secrets.token_urlsafe(32)
    # Names this server process (and so its CSRF token): a client that sees another
    # bootId reads /api/capabilities again instead of sending a token from before a restart.
    server.boot_id = secrets.token_urlsafe(12)
    server.upload_gate = threading.BoundedSemaphore(2)
    server.assets = StaticAssets(WEB)
    try:
        server.terminals = terminals if terminals is not None else TerminalManager(state_dir=Path(state_dir) / "terminals", max_terminals=max_terminals or MAX_TERMINALS)
        from terminal_input import TerminalInputs
        server.terminal_inputs = TerminalInputs(state_dir)
    except BaseException:
        server.server_close()
        raise
    server.workflow = workflow if workflow is not None else Workflow(board, server.terminals, state_dir)
    server.workflow.goals = server.goals
    server.work = WorkService(board, server.goals, state_dir)
    server.workflow.work = server.work
    from bot_requests import BotRequests
    server.bot_requests = BotRequests(board.hosts, CONFIG_DIR / "bot-sources.json", state_dir)
    server.workflow.bot_requests = server.bot_requests
    from chat import Chat
    server.chat = Chat(board, server.workflow, state_dir)
    from events import EventHub
    # Also announces the terminal list: what the board's terminals report, and while
    # anyone follows the stream, terminals that ended on their own.
    server.events = EventHub(board, server.chat, boot_id=server.boot_id, terminals=server.terminals)
    from push import PushService

    def push_category(host, row):
        """The list's 일반/에이전트/봇 split: only 일반 sessions notify."""
        source = {"host": host.get("name"), "agent": row.get("agent"), "id": row.get("id"), "home": row.get("home")}
        try:
            if server.workflow.bot_request(source):
                return "bot"
        except (RuntimeError, OSError, ValueError):
            return "unknown"
        return "agent" if row.get("initiator") == "agent" else "general"
    server.push = PushService(board, state_dir, classify=push_category, labels=server.session_labels.decorate,
                              log_prefix=LOG_PREFIX, enabled=push)
    if push:
        server.push.start()
    if automatic_work:
        server.work.start()
    return server


def issue_login_link(httpd, ttl=ONCE_TTL):
    """Arm one single-use, short-lived login code; the long-lived token never enters a URL."""
    code = secrets.token_urlsafe(24)
    with httpd.once_lock:
        httpd.once = (code, time.time() + ttl)
    return f"http://127.0.0.1:{httpd.server_address[1]}/login?once={code}"


def main(argv=None):
    from terminal import MAX_TERMINALS
    parser = argparse.ArgumentParser(description=BRAND["title"])
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--hosts", default=str(DEFAULT_HOSTS))
    parser.add_argument("--interval", type=int, default=60, help="보이는 화면의 기기별 최소 수집 간격(초)")
    parser.add_argument("--max-terminals", type=int, choices=range(1, 33), default=MAX_TERMINALS, help="동시에 유지할 보드 터미널 수(1~32)")
    parser.add_argument("--local-only", action="store_true", help="이 기기만 수집·검증")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--no-live", action="store_true",
                        help="상주 수집기를 쓰지 않고 화면 요청 때만 일회성으로 수집")
    # The automatic work tab is retired: its off-screen conversation reads are opt-in.
    parser.add_argument("--auto-work", action="store_true", help="화면 밖 자동 작업 관찰을 켬(기본 꺼짐)")
    parser.add_argument("--no-auto-work", action="store_true", help="화면 밖 자동 작업 관찰을 끔(기본값, 이전 설정 호환)")
    parser.add_argument("--no-push", action="store_true", help="폰 알림을 끔(구독이 있어도 화면 밖 수집과 발송을 하지 않음)")
    parser.add_argument("--tailscale-user-file", type=Path,
                        help="비공개 파일에 적힌 Tailscale 사용자에게 HTTPS Serve 자동 인증 허용")
    parser.add_argument("--open", action="store_true",
                        help=f"{ONCE_TTL // 60}분 동안 한 번만 쓸 수 있는 로그인 링크를 표준 출력에 표시")
    args = parser.parse_args(argv)
    if args.bind != "127.0.0.1":
        parser.error("127.0.0.1만 허용합니다. 원격 접속은 Tailscale Serve를 사용하세요.")
    try:
        config = settings.load()
        for folder in dict.fromkeys((CONFIG_DIR, TOKEN_FILE.parent)):
            settings.safe_path(folder, directory=True, private=True, allow_missing=True)
        args.state_dir = settings.safe_path(args.state_dir, directory=True, private=True, allow_missing=True)
        for filename in ("snapshot.json", "conversations.json"):
            settings.safe_path(args.state_dir / filename, private=True, allow_missing=True)
        tailscale_user = load_tailscale_user(args.tailscale_user_file) if args.tailscale_user_file else None
        hosts = load_hosts(args.hosts)
        token = load_token()
        # The page, manifest and worker carry this installation's names: fail before serving.
        StaticAssets(WEB).check()
    except ValueError as exc:
        parser.error(str(exc))
    if args.local_only:
        hosts = [h for h in hosts if h["local"]]
    board = Board(hosts, max(args.interval, 60), state_dir=args.state_dir, live=not args.no_live)
    meter = None
    if config["usage"] is not None:
        from usage import UsageMeter
        meter = UsageMeter(config["usage"])
    httpd = make_server(args.bind, args.port, board, token, state_dir=args.state_dir,
                        max_terminals=args.max_terminals, tailscale_user=tailscale_user, usage=meter,
                        automatic_work=args.auto_work and not args.no_auto_work, push=not args.no_push)
    collecting = "화면을 열 때와 알림을 받는 기기가 있을 때 수집합니다." if not args.no_push else "화면을 열 때만 수집합니다."
    print(f"{LOG_PREFIX}: http://127.0.0.1:{args.port}/ · {collecting}", file=sys.stderr)
    if args.open:
        print(issue_login_link(httpd), flush=True)
        print(f"표준 출력의 일회용 로그인 링크를 {ONCE_TTL // 60}분 안에 개인 브라우저에서 여세요. 한 번만 사용할 수 있습니다.",
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
