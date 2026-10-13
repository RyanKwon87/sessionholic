"""Server-sent change feed: snapshot versions, watched conversation digests and the
board terminal list version.

Standard library only and Python 3.9 compatible. Streams never hold the board's
collection locks: they wait on their own small mailbox and write outside every lock.
A conversation is read by one shared watcher, however many streams follow it,
through the existing conversation state path and its short cache. The terminal list
is announced when the terminal manager reports a change (create, close, attach,
detach) and, only while someone follows the stream, when its periodic read finds
that the set of live terminals changed (a tmux session that ended on its own).
"""
import hashlib
import json
import sys
import threading
import time

import instance

BRAND = instance.current()["brand"]

HEARTBEAT = 15.0
MAX_STREAMS = 8
# Conversation watchers alive at once, including those waiting out their grace period
# and those leaving to make room: a watcher counts until its thread has left, so its
# reads never overlap a newer watcher's.
MAX_WATCHERS = 8
# How long a new conversation waits for an idle watcher to leave and free its place.
ROOM_WAIT = 2.0
GRACE = 10.0
RETRY_MS = 3000
# Seconds between conversation reads. Working, a changed digest, or a successful send or
# menu answer from the board: the fast interval. Otherwise each unchanged read backs off
# one step and stays at the last. After an error: at least the error interval, and never
# shorter than the current idle step.
FAST_INTERVAL = 2.0
IDLE_INTERVALS = (5.0, 10.0, 20.0, 30.0)
ERROR_INTERVAL = 10.0
# How often an idle stream checks whether its client went away.
PEER_CHECK = 1.0
# Snapshot announcements of a resident collector's changes to one stream are at least
# this far apart: it can change the board every second, and each announcement makes the
# page read the whole snapshot again. Changes in between are one announcement of the
# newest version; any other change (Board.urgent_version) goes out at once.
SNAPSHOT_GAP = 3.0
# Seconds between terminal list reads while at least one stream is open (at most four
# board terminals, so a read is a few tmux calls); after a failed read, the longer one.
TERMINAL_INTERVAL = 5.0
TERMINAL_ERROR_INTERVAL = 10.0
CHAT_KEYS = ("host", "agent", "home", "id")
EXPECTED = (ValueError, RuntimeError, OSError, KeyError)


def encode_event(name, data):
    return ("event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False, separators=(",", ":")))).encode()


def state_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def live_terminals(rows):
    """IDs of the live (alive, not closed) terminals in a terminal list."""
    return frozenset(row.get("id") for row in rows
                     if isinstance(row, dict) and row.get("alive") and not row.get("closed"))


def chat_ref(query):
    """The conversation named by `host`, `agent`, `home` and `id`, or None when none is given."""
    if not any(query.get(key) for key in CHAT_KEYS):
        return None
    ref = {key: query.get(key) for key in ("host", "agent", "id")}
    if query.get("home"):
        ref["home"] = query["home"]
    return ref


def watch_key(ref):
    """Key of a server-normalized conversation identity (chat.Chat.identity of the listed
    source): the same string as the conversation state cache key, so one watcher serves
    one conversation however the client spelled it."""
    return json.dumps({key: ref.get(key) for key in CHAT_KEYS}, sort_keys=True)


class Stream:
    """One open event stream: the newest values announced to it, and a wake-up flag."""
    def __init__(self):
        self.key = None
        self.ready = threading.Event()
        self.version = None
        self.digest = None
        self.hello = 0
        self.sent_digest = None
        # Terminal list versions: the one sent with hello and the newest announced since.
        self.hello_terminals = None
        self.terminals = None


class Watcher:
    def __init__(self, key, ref):
        self.key, self.ref = key, dict(ref)
        self.streams = set()
        self.digest = None
        self.idle_since = None
        self.wake = threading.Event()
        # Index of the next idle interval; reset by activity.
        self.idle_step = 0
        self.boosted = False
        # Its thread is reading the conversation now (decided and cleared under the hub lock).
        self.reading = False
        # Asked to leave to make room; following it again before it has left revives it.
        self.retiring = False


class EventHub:
    def __init__(self, board, chat, heartbeat=HEARTBEAT, max_streams=MAX_STREAMS, grace=GRACE,
                 fast=FAST_INTERVAL, idle=IDLE_INTERVALS, error=ERROR_INTERVAL,
                 peer_check=PEER_CHECK, clock=time.monotonic, max_watchers=MAX_WATCHERS,
                 room_wait=ROOM_WAIT, boot_id=None, terminals=None,
                 terminal_interval=TERMINAL_INTERVAL, terminal_error=TERMINAL_ERROR_INTERVAL,
                 snapshot_gap=SNAPSHOT_GAP):
        self.board, self.chat = board, chat
        # The server process's bootId, sent with hello: snapshot versions restart with it.
        self.boot_id = boot_id
        self.heartbeat, self.max_streams, self.grace = heartbeat, max_streams, grace
        self.max_watchers, self.room_wait = max_watchers, room_wait
        self.fast, self.idle, self.error = fast, tuple(idle), error
        self.peer_check, self.clock = peer_check, clock
        self.snapshot_gap = snapshot_gap
        # Lock order: the board lock or the terminal manager's lock may be held when
        # snapshot_changed or terminals_changed takes this lock; nothing here calls into
        # the board, the terminals or a conversation while holding it.
        self.lock = threading.Lock()
        # Signalled when a watcher leaves, stops reading or loses its last stream.
        self.room = threading.Condition(self.lock)
        self.streams = set()
        self.watchers = {}
        self.closed = False
        # The board terminal list (terminal.TerminalManager), when this hub announces it.
        self.terminals = terminals if callable(getattr(terminals, "list", None)) else None
        self.terminal_interval, self.terminal_error = terminal_interval, terminal_error
        # Counts within this server process, like snapshot versions; only ever increases.
        self.terminals_version = 0
        # Live terminal IDs at the terminal watcher's last read (kept while it is away, so
        # a terminal that ended meanwhile is announced by its first read on return).
        self.terminals_seen = None
        self.terminal_watching = False
        self.terminal_idle_since = None
        self.terminal_wake = threading.Event()
        board.add_listener(self.snapshot_changed)
        if self.terminals is not None and callable(getattr(self.terminals, "add_listener", None)):
            self.terminals.add_listener(self.terminals_changed)

    # ---------- publishing ----------
    def snapshot_changed(self, version):
        with self.lock:
            for stream in self.streams:
                if stream.version is None or version > stream.version:
                    stream.version = version
                    stream.ready.set()

    def terminals_changed(self):
        """The terminal manager created, closed, attached or detached a terminal: announce
        a new list version, and let the terminal watcher read the list now so its own
        comparison lands in the same batch instead of announcing again seconds later."""
        with self.lock:
            self._terminals_bump()
            self.terminal_wake.set()

    def _terminals_bump(self):
        """Caller holds self.lock."""
        self.terminals_version += 1
        for stream in self.streams:
            stream.terminals = self.terminals_version
            stream.ready.set()

    def _chat_changed(self, watcher, digest):
        with self.lock:
            if digest == watcher.digest:
                return False
            watcher.digest = digest
            for stream in watcher.streams:
                stream.digest = digest
                stream.ready.set()
            return True

    def boost(self, identity):
        """A send or menu answer from the board succeeded: follow that conversation quickly again.

        `identity` is the server-normalized conversation, as for `open`."""
        if not isinstance(identity, dict):
            return
        with self.lock:
            watcher = self.watchers.get(watch_key(identity))
            if watcher is not None:
                watcher.idle_step = 0
                watcher.boosted = True
                # Cut a long idle wait short; the next read starts the fast interval.
                watcher.wake.set()

    # ---------- streams ----------
    def open(self, ref=None):
        """Register a stream (and its conversation watcher), or None when at capacity or closed.

        `ref` is the server-normalized conversation identity (chat.Chat.identity of the
        listed source), never the raw query, so spellings of one conversation share a watcher.
        Raises RuntimeError when the watcher thread cannot be started."""
        stream = Stream()
        start = None
        key = watch_key(ref) if ref is not None else None
        deadline = time.monotonic() + self.room_wait
        leaving = None
        with self.lock:
            while True:
                if self.closed or len(self.streams) >= self.max_streams:
                    return None
                watcher = self.watchers.get(key) if key is not None else None
                if key is None or watcher is not None or len(self.watchers) < self.max_watchers:
                    break
                if (leaving is None or not leaving.retiring
                        or self.watchers.get(leaving.key) is not leaving):
                    # First pass, or the place it left was taken or it was followed again.
                    leaving = self._retire_idle()
                    if leaving is None:
                        return None
                # A leaving watcher keeps its place until its thread has left, so a read
                # it is still making never overlaps the new watcher's.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.room.wait(remaining)
            self.streams.add(stream)
            # Read under the lock that announces versions: no later change is missed.
            stream.hello_terminals = self.terminals_version
            start_terminals = self._follow_terminals()
            if key is not None:
                stream.key = key
                if watcher is None:
                    watcher = start = self.watchers[key] = Watcher(key, ref)
                elif watcher.retiring or not watcher.streams:
                    # Back within the grace period, or before it left to make room: the same
                    # watcher reads again now; a second one would overlap its reads.
                    watcher.retiring = False
                    watcher.wake.set()
                watcher.streams.add(stream)
                watcher.idle_since = None
                # The client reads the conversation itself; only later changes are announced.
                stream.sent_digest = watcher.digest
        # Registered before the version is read, so no later change can be missed.
        stream.hello = self.board.version
        if start_terminals:
            try:
                threading.Thread(target=self._watch_terminals, daemon=True,
                                 name=BRAND["threadPrefix"] + "-terminals").start()
            except RuntimeError:
                # The stream still serves snapshots and conversations; a later stream retries.
                with self.lock:
                    self.terminal_watching = False
        if start is not None:
            try:
                threading.Thread(target=self._watch, args=(start,), daemon=True,
                                 name=BRAND["threadPrefix"] + "-watch").start()
            except RuntimeError:
                self.close_stream(stream)
                with self.lock:
                    self._leave(start)
                raise
        return stream

    def close_stream(self, stream):
        with self.lock:
            if stream in self.streams:
                self.streams.discard(stream)
                if not self.streams:
                    # The terminal watcher stops reading and counts down its grace.
                    self.terminal_idle_since = self.clock()
            watcher = self.watchers.get(stream.key) if stream.key else None
            if watcher is not None and stream in watcher.streams:
                watcher.streams.discard(stream)
                if not watcher.streams:
                    watcher.idle_since = self.clock()
                    # A watcher sleeping out a long idle interval counts down its grace now.
                    watcher.wake.set()
                    # It may now make room for a conversation waiting for a place.
                    self.room.notify_all()

    def _follow_terminals(self):
        """A stream opened: True when the terminal watcher must be started. Caller holds self.lock."""
        if self.terminals is None or self.closed:
            return False
        was_idle = self.terminal_idle_since is not None
        self.terminal_idle_since = None
        if self.terminal_watching:
            if was_idle:
                # Back within the grace period: read now instead of after the countdown.
                self.terminal_wake.set()
            return False
        self.terminal_watching = True
        return True

    def _retire_idle(self):
        """Ask the watcher without streams idle longest to leave, preferring one that is not
        reading; None when every watcher has a stream. Caller holds self.lock."""
        idle = [item for item in self.watchers.values() if not item.streams and not item.retiring]
        if not idle:
            return None
        quiet = [item for item in idle if not item.reading]
        watcher = min(quiet or idle, key=lambda item: item.idle_since)
        watcher.retiring = True
        watcher.wake.set()
        return watcher

    def _leave(self, watcher):
        """A watcher's thread is leaving (or never started): free its place. Caller holds self.lock."""
        watcher.reading = False
        if self.watchers.get(watcher.key) is watcher:
            del self.watchers[watcher.key]
        self.room.notify_all()

    def pump(self, stream, write, peer_closed):
        """Write hello, then changes and heartbeats until the client leaves or the hub closes."""
        try:
            hello = {"snapshotVersion": stream.hello}
            if self.boot_id is not None:
                hello["bootId"] = self.boot_id
            if self.terminals is not None:
                hello["terminalsVersion"] = stream.hello_terminals
            write(("retry: %d\n\n" % RETRY_MS).encode() + encode_event("hello", hello))
            sent_version, sent_digest = stream.hello, stream.sent_digest
            sent_terminals = stream.hello_terminals
            next_ping = self.clock() + self.heartbeat
            # When the last snapshot announcement went out, and whether a newer version waits.
            announced, held = None, False
            while True:
                wake = min(self.peer_check, next_ping - self.clock())
                if held:
                    wake = min(wake, announced + self.snapshot_gap - self.clock())
                stream.ready.wait(max(0.0, wake))
                with self.lock:
                    stream.ready.clear()
                    version, digest, closed = stream.version, stream.digest, self.closed
                    terminals = stream.terminals
                if closed:
                    return
                out = b""
                held = False
                if version is not None and version > sent_version:
                    now = self.clock()
                    # Only a resident watcher's changes wait for the gap: a collection that
                    # finished, a refresh or a label is announced at once.
                    urgent = getattr(self.board, "urgent_version", 0) or 0
                    if announced is None or now - announced >= self.snapshot_gap or urgent > sent_version:
                        out += encode_event("snapshot", {"version": version})
                        sent_version, announced = version, now
                    else:
                        held = True
                if digest is not None and digest != sent_digest:
                    out += encode_event("chat", {"digest": digest})
                    sent_digest = digest
                if terminals is not None and (sent_terminals is None or terminals > sent_terminals):
                    # Several changes since the last write are one announcement.
                    out += encode_event("terminals", {"version": terminals})
                    sent_terminals = terminals
                now = self.clock()
                if now >= next_ping:
                    out += b": ping\n\n"
                    next_ping = now + self.heartbeat
                if out:
                    write(out)
                elif peer_closed():
                    return
        except OSError:
            # A closed tab, a dropped network or a stalled reader ends only this stream.
            return

    def close(self):
        with self.lock:
            self.closed = True
            for stream in self.streams:
                stream.ready.set()
            for watcher in self.watchers.values():
                watcher.wake.set()
            self.terminal_wake.set()
            self.room.notify_all()
        try:
            self.board.remove_listener(self.snapshot_changed)
        except (AttributeError, ValueError):
            pass
        try:
            self.terminals.remove_listener(self.terminals_changed)
        except (AttributeError, ValueError):
            pass

    # ---------- the terminal watcher ----------
    def _read_terminals(self):
        """One terminal list read; a changed set of live terminals is a new version.
        Returns the seconds until the next read."""
        try:
            # No hub lock here: the list asks tmux about each terminal.
            live = live_terminals(self.terminals.list())
        except Exception as exc:
            if not isinstance(exc, EXPECTED):
                sys.stderr.write("%s: 터미널 목록 확인 중 예상하지 못한 오류 (%s)\n" % (BRAND["logPrefix"], type(exc).__name__))
            return self.terminal_error
        with self.lock:
            if self.terminals_seen is not None and live != self.terminals_seen:
                self._terminals_bump()
            self.terminals_seen = live
        return self.terminal_interval

    def _watch_terminals(self):
        """Reads the terminal list while any stream is open; leaves `grace` seconds after the
        last one closed. Never reads while nobody follows the stream."""
        try:
            while True:
                with self.lock:
                    self.terminal_wake.clear()
                    since = self.terminal_idle_since
                    idle = None if self.streams else self.clock() - (since if since is not None else self.clock())
                    if self.closed or (idle is not None and idle >= self.grace):
                        self.terminal_watching = False
                        return
                    if idle is not None and since is None:
                        self.terminal_idle_since = self.clock()
                if idle is not None:
                    self.terminal_wake.wait(self.grace - idle)
                    continue
                self.terminal_wake.wait(self._read_terminals())
        except BaseException:
            with self.lock:
                self.terminal_watching = False
            raise

    # ---------- conversation watchers ----------
    def _read(self, watcher):
        """(working, digest) from one state read; digest is None after an error."""
        try:
            # Also carries the receipts clients wait for: one remote read serves them all.
            state = self.chat.state({"source": dict(watcher.ref)}, watching=True)
        except Exception as exc:
            if not isinstance(exc, EXPECTED):
                sys.stderr.write("%s: 대화 변경 확인 중 예상하지 못한 오류 (%s)\n" % (BRAND["logPrefix"], type(exc).__name__))
            return False, None
        if not isinstance(state, dict):
            return False, None
        return state.get("phase") == "working", state_digest(state)

    def _next_delay(self, watcher, working, digest, changed):
        with self.lock:
            boosted, watcher.boosted = watcher.boosted, False
            step = min(watcher.idle_step, len(self.idle) - 1)
            if digest is None:
                # Errors keep the idle step: never faster than the idle schedule.
                return max(self.error, self.idle[step])
            if working or changed or boosted:
                watcher.idle_step = 0
                return self.fast
            watcher.idle_step = min(step + 1, len(self.idle) - 1)
            return self.idle[step]

    def _watch(self, watcher):
        try:
            while True:
                with self.lock:
                    watcher.wake.clear()
                    idle = None if watcher.streams else self.clock() - watcher.idle_since
                    if (self.closed or watcher.retiring or self.watchers.get(watcher.key) is not watcher
                            or (idle is not None and idle >= self.grace)):
                        # Left in the same step as the decision: an open() that finds the
                        # watcher still registered may safely revive it.
                        self._leave(watcher)
                        return
                    # Set together with the decision, so a reading watcher is never retired
                    # as if it were sleeping.
                    active = watcher.reading = idle is None
                    if not active:
                        delay = self.grace - idle
                if active:
                    # No hub lock here: the read may take seconds on a remote host.
                    working, digest = self._read(watcher)
                    with self.lock:
                        watcher.reading = False
                        self.room.notify_all()
                    changed = digest is not None and self._chat_changed(watcher, digest)
                    delay = self._next_delay(watcher, working, digest, changed)
                watcher.wake.wait(delay)
        except BaseException:
            # Never keep a place for a thread that is gone.
            with self.lock:
                self._leave(watcher)
            raise
