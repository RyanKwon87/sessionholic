"""Server-sent change feed: snapshot versions and watched conversation digests.

Standard library only and Python 3.9 compatible. Streams never hold the board's
collection locks: they wait on their own small mailbox and write outside every lock.
A conversation is read by one shared watcher, however many streams follow it,
through the existing conversation state path and its short cache.
"""
import hashlib
import json
import sys
import threading
import time

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
CHAT_KEYS = ("host", "agent", "home", "id")
EXPECTED = (ValueError, RuntimeError, OSError, KeyError)


def encode_event(name, data):
    return ("event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False, separators=(",", ":")))).encode()


def state_digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


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
                 room_wait=ROOM_WAIT):
        self.board, self.chat = board, chat
        self.heartbeat, self.max_streams, self.grace = heartbeat, max_streams, grace
        self.max_watchers, self.room_wait = max_watchers, room_wait
        self.fast, self.idle, self.error = fast, tuple(idle), error
        self.peer_check, self.clock = peer_check, clock
        # Lock order: the board lock may be held when snapshot_changed takes this lock;
        # nothing here calls into the board or a conversation while holding it.
        self.lock = threading.Lock()
        # Signalled when a watcher leaves, stops reading or loses its last stream.
        self.room = threading.Condition(self.lock)
        self.streams = set()
        self.watchers = {}
        self.closed = False
        board.add_listener(self.snapshot_changed)

    # ---------- publishing ----------
    def snapshot_changed(self, version):
        with self.lock:
            for stream in self.streams:
                if stream.version is None or version > stream.version:
                    stream.version = version
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
        if start is not None:
            try:
                threading.Thread(target=self._watch, args=(start,), daemon=True,
                                 name="sessionholic-watch").start()
            except RuntimeError:
                self.close_stream(stream)
                with self.lock:
                    self._leave(start)
                raise
        return stream

    def close_stream(self, stream):
        with self.lock:
            self.streams.discard(stream)
            watcher = self.watchers.get(stream.key) if stream.key else None
            if watcher is not None and stream in watcher.streams:
                watcher.streams.discard(stream)
                if not watcher.streams:
                    watcher.idle_since = self.clock()
                    # A watcher sleeping out a long idle interval counts down its grace now.
                    watcher.wake.set()
                    # It may now make room for a conversation waiting for a place.
                    self.room.notify_all()

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
            write(("retry: %d\n\n" % RETRY_MS).encode() + encode_event("hello", {"snapshotVersion": stream.hello}))
            sent_version, sent_digest = stream.hello, stream.sent_digest
            next_ping = self.clock() + self.heartbeat
            while True:
                stream.ready.wait(max(0.0, min(self.peer_check, next_ping - self.clock())))
                with self.lock:
                    stream.ready.clear()
                    version, digest, closed = stream.version, stream.digest, self.closed
                if closed:
                    return
                out = b""
                if version is not None and version > sent_version:
                    out += encode_event("snapshot", {"version": version})
                    sent_version = version
                if digest is not None and digest != sent_digest:
                    out += encode_event("chat", {"digest": digest})
                    sent_digest = digest
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
            self.room.notify_all()
        try:
            self.board.remove_listener(self.snapshot_changed)
        except (AttributeError, ValueError):
            pass

    # ---------- conversation watchers ----------
    def _read(self, watcher):
        """(working, digest) from one state read; digest is None after an error."""
        try:
            # Also carries the receipts clients wait for: one remote read serves them all.
            state = self.chat.state({"source": dict(watcher.ref)}, watching=True)
        except Exception as exc:
            if not isinstance(exc, EXPECTED):
                sys.stderr.write("세션홀릭: 대화 변경 확인 중 예상하지 못한 오류 (%s)\n" % type(exc).__name__)
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
