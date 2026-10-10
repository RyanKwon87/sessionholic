import json
from pathlib import Path
import random
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events  # noqa: E402

REF = {"host": "remote", "agent": "codex", "home": ".codex", "id": "a1b2c3d4-0000"}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


class FakeBoard:
    def __init__(self):
        self.version, self.listeners = 0, []

    def add_listener(self, callback):
        self.listeners.append(callback)

    def remove_listener(self, callback):
        self.listeners.remove(callback)

    def change(self):
        self.version += 1
        for callback in list(self.listeners):
            callback(self.version)


class FakeChat:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls, self.threads, self.watching = [], set(), []
        self.result = {"phase": "idle", "messages": []}
        self.error = None

    def state(self, data, watching=False):
        with self.lock:
            self.calls.append(json.dumps(data["source"], sort_keys=True))
            self.watching.append(watching)
            self.threads.add(threading.get_ident())
            if self.error:
                raise self.error
            return json.loads(json.dumps(self.result))


class Reader:
    """Runs one stream's pump in a thread and collects what it writes."""
    def __init__(self, hub, stream, fail_after=None):
        self.hub, self.stream, self.fail_after = hub, stream, fail_after
        self.chunks, self.gone, self.lock = [], False, threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def write(self, data):
        with self.lock:
            if self.fail_after is not None and len(self.chunks) >= self.fail_after:
                raise BrokenPipeError("fixture")
            self.chunks.append(data)

    def run(self):
        try:
            self.hub.pump(self.stream, self.write, lambda: self.gone)
        finally:
            self.hub.close_stream(self.stream)

    def text(self):
        with self.lock:
            return b"".join(self.chunks).decode()

    def events(self, name):
        return [json.loads(block.split("data: ", 1)[1]) for block in self.text().split("\n\n")
                if block.startswith("event: " + name + "\n")]


class EventHubTest(unittest.TestCase):
    def setUp(self):
        self.board, self.chat = FakeBoard(), FakeChat()
        self.hub = events.EventHub(self.board, self.chat, heartbeat=0.1, grace=0.3, fast=0.02,
                                   idle=(0.04, 0.04), error=0.08, peer_check=0.02)
        self.addCleanup(self.hub.close)

    def test_event_format(self):
        self.assertEqual(events.encode_event("hello", {"snapshotVersion": 3}),
                         b'event: hello\ndata: {"snapshotVersion":3}\n\n')
        self.assertEqual(events.encode_event("chat", {"digest": "ab"}), b'event: chat\ndata: {"digest":"ab"}\n\n')
        self.assertIsNone(events.chat_ref({}))
        self.assertEqual(events.chat_ref({"host": "h", "agent": "claude", "id": "x"}),
                         {"host": "h", "agent": "claude", "id": "x"})
        self.assertEqual(events.chat_ref(REF), REF)

    def test_hello_names_the_server_process_when_given(self):
        hub = events.EventHub(self.board, self.chat, heartbeat=0.1, peer_check=0.02, boot_id="boot-fixture")
        self.addCleanup(hub.close)
        self.board.version = 2
        reader = Reader(hub, hub.open())
        wait_for(lambda: reader.events("hello"))
        self.assertEqual(reader.events("hello"), [{"snapshotVersion": 2, "bootId": "boot-fixture"}])

    def test_hello_heartbeat_and_snapshot_versions(self):
        self.board.version = 4
        stream = self.hub.open()
        reader = Reader(self.hub, stream)
        wait_for(lambda: reader.text().count(": ping\n\n") >= 2)
        text = reader.text()
        self.assertTrue(text.startswith('retry: 3000\n\nevent: hello\ndata: {"snapshotVersion":4}\n\n'))
        # A version at or below hello was already covered by the client's first snapshot read.
        self.hub.snapshot_changed(4)
        self.board.change()
        self.board.change()
        wait_for(lambda: reader.events("snapshot") and reader.events("snapshot")[-1] == {"version": 6})
        self.assertTrue(all(item["version"] > 4 for item in reader.events("snapshot")))
        self.hub.close()
        reader.thread.join(2)
        self.assertFalse(reader.thread.is_alive())
        self.assertEqual(self.hub.streams, set())
        self.assertNotIn(self.hub.snapshot_changed, self.board.listeners)

    def test_capacity_and_closed_hub(self):
        self.hub.max_streams = 2
        first, second = self.hub.open(), self.hub.open()
        self.assertIsNone(self.hub.open())
        self.hub.close_stream(first)
        third = self.hub.open()
        self.assertIsNotNone(third)
        for stream in (second, third):
            self.hub.close_stream(stream)
        self.hub.close()
        self.assertIsNone(self.hub.open())

    def test_broken_pipe_and_departed_client_end_only_that_stream(self):
        broken = Reader(self.hub, self.hub.open(), fail_after=1)
        departed = Reader(self.hub, self.hub.open())
        staying = Reader(self.hub, self.hub.open())
        broken.thread.join(2)
        self.assertFalse(broken.thread.is_alive())
        departed.gone = True
        departed.thread.join(2)
        self.assertFalse(departed.thread.is_alive())
        self.assertTrue(staying.thread.is_alive())
        self.assertEqual(self.hub.streams, {staying.stream})

    def test_one_watcher_per_conversation_with_grace_stop(self):
        first = Reader(self.hub, self.hub.open(dict(REF)))
        # The watcher's first digest is announced; an unchanged state is not repeated.
        wait_for(lambda: first.events("chat"))
        second = Reader(self.hub, self.hub.open(dict(REF)))
        wait_for(lambda: len(self.chat.calls) >= 4)
        self.assertEqual(len(self.hub.watchers), 1)
        self.assertEqual(len(self.chat.threads), 1)
        watcher = next(iter(self.hub.watchers.values()))
        self.assertEqual(json.loads(self.chat.calls[0]), REF)
        self.assertEqual(len(first.events("chat")), 1)
        # A later subscriber is not told about the digest that already existed.
        self.assertEqual(second.events("chat"), [])
        self.chat.result = {"phase": "idle", "messages": [{"role": "assistant", "text": "새 답"}]}
        wait_for(lambda: len(first.events("chat")) == 2 and len(second.events("chat")) == 1)
        self.assertNotEqual(first.events("chat")[0], first.events("chat")[1])
        self.assertEqual(second.events("chat")[0], first.events("chat")[1])
        late = Reader(self.hub, self.hub.open(dict(REF)))
        time.sleep(0.15)
        self.assertEqual(late.events("chat"), [])
        for reader in (first, second, late):
            reader.gone = True
            reader.thread.join(2)
        # Within the grace period the same watcher is reused and resumes reading.
        time.sleep(0.1)
        self.assertIs(self.hub.watchers.get(watcher.key), watcher)
        calls = len(self.chat.calls)
        time.sleep(0.1)
        self.assertEqual(len(self.chat.calls), calls, "no reads without subscribers")
        again = Reader(self.hub, self.hub.open(dict(REF)))
        self.assertIs(self.hub.watchers.get(watcher.key), watcher)
        wait_for(lambda: len(self.chat.calls) > calls)
        again.gone = True
        again.thread.join(2)
        # The last stream left: the watcher stops after the grace period.
        wait_for(lambda: not self.hub.watchers, timeout=3)

    def test_watcher_reads_carry_the_receipts_clients_wait_for(self):
        reader = Reader(self.hub, self.hub.open(dict(REF)))
        wait_for(lambda: len(self.chat.calls) >= 2)
        # Every watcher read asks the conversation view to include waiting receipts.
        self.assertTrue(all(self.chat.watching))
        reader.gone = True
        reader.thread.join(2)

    def test_last_stream_leaving_wakes_a_watcher_sleeping_a_long_idle_interval(self):
        hub = events.EventHub(self.board, self.chat, heartbeat=5, grace=0.2, fast=0.02, idle=(30.0,),
                              error=30.0, peer_check=0.02)
        self.addCleanup(hub.close)
        stream = hub.open(dict(REF))
        wait_for(lambda: len(self.chat.calls) == 2)
        # Unchanged now, so the watcher sleeps its 30 s idle step.
        time.sleep(0.05)
        hub.close_stream(stream)
        started = time.monotonic()
        # The grace period counts from the last stream leaving, not from the next idle read.
        wait_for(lambda: not hub.watchers, timeout=2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(self.chat.calls), 2)

    def test_watcher_cap_counts_grace_watchers_and_retires_the_longest_idle(self):
        hub = events.EventHub(self.board, self.chat, heartbeat=5, grace=30.0, fast=0.02, idle=(30.0,),
                              error=30.0, peer_check=0.02, max_watchers=2)
        self.addCleanup(hub.close)
        finished, original = [], hub._watch
        def watch(watcher):
            try:
                original(watcher)
            finally:
                finished.append(watcher)
        hub._watch = watch
        refs = [{**REF, "id": "a1b2c3d4-000%d" % number} for number in range(4)]
        keys = [events.watch_key(ref) for ref in refs]
        streams = []
        for ref in refs[:2]:
            # Opened and closed at once: both watchers wait out their grace period.
            hub.close_stream(hub.open(dict(ref)))
        # A first read may still be running; a watcher that is not reading leaves first.
        wait_for(lambda: not any(item.reading for item in list(hub.watchers.values())))
        retired = hub.watchers[keys[0]]
        streams.append(hub.open(dict(refs[2])))
        self.assertIsNotNone(streams[-1])
        self.assertEqual(set(hub.watchers), {keys[1], keys[2]})
        wait_for(lambda: retired in finished, timeout=2)
        # The same conversation again: a new watcher, which the retired thread never removes.
        streams.append(hub.open(dict(refs[0])))
        self.assertIsNotNone(streams[-1])
        self.assertEqual(set(hub.watchers), {keys[2], keys[0]})
        self.assertIsNot(hub.watchers[keys[0]], retired)
        wait_for(lambda: len(finished) == 2, timeout=2)
        time.sleep(0.05)
        self.assertIn(keys[0], hub.watchers)
        # Every watcher has a subscriber: a new conversation is refused.
        self.assertIsNone(hub.open(dict(refs[3])))
        self.assertEqual(len(hub.streams), 2)
        self.assertEqual(set(hub.watchers), {keys[2], keys[0]})
        # An already-followed conversation and snapshot-only streams still fit.
        streams.append(hub.open(dict(refs[2])))
        streams.append(hub.open())
        self.assertNotIn(None, streams)
        for stream in streams:
            hub.close_stream(stream)

    def test_thread_start_failure_frees_the_stream_and_watcher(self):
        from unittest.mock import patch
        with patch.object(events.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.hub.open(dict(REF))
        self.assertEqual((self.hub.streams, self.hub.watchers), (set(), {}))

    def test_read_reports_working_and_errors(self):
        watcher = events.Watcher("key", REF)
        self.chat.result = {"phase": "working"}
        self.assertEqual(self.hub._read(watcher), (True, events.state_digest({"phase": "working"})))
        self.chat.result = {"phase": "needs_input"}
        self.assertEqual(self.hub._read(watcher)[0], False)
        self.chat.error = RuntimeError("offline")
        self.assertEqual(self.hub._read(watcher), (False, None))
        self.assertEqual((events.FAST_INTERVAL, events.IDLE_INTERVALS, events.ERROR_INTERVAL),
                         (2.0, (5.0, 10.0, 20.0, 30.0), 10.0))
        self.assertEqual((events.HEARTBEAT, events.MAX_STREAMS, events.GRACE), (15.0, 8, 10.0))
        self.assertEqual(events.MAX_WATCHERS, 8)

    def test_failing_reads_keep_streams_open_without_chat_events(self):
        self.chat.error = RuntimeError("기기 연결을 다시 확인한 뒤 메시지를 보내 주세요.")
        reader = Reader(self.hub, self.hub.open(dict(REF)))
        wait_for(lambda: len(self.chat.calls) >= 2)
        self.assertEqual(reader.events("chat"), [])
        self.assertTrue(reader.thread.is_alive())
        self.chat.error = None
        wait_for(lambda: reader.events("chat"))


class GatedChat:
    """Conversation reads that may wait for a gate; counts reads running at once, overall
    and per conversation, and the threads that read each conversation."""
    def __init__(self, gate=None, pause=0.0):
        self.gate, self.pause = gate, pause
        self.lock = threading.Lock()
        self.running, self.threads = {}, {}
        self.peak = self.peak_one = self.reads = 0

    def state(self, data, watching=False):
        key = events.watch_key(data["source"])
        with self.lock:
            self.running[key] = self.running.get(key, 0) + 1
            self.peak = max(self.peak, sum(self.running.values()))
            self.peak_one = max(self.peak_one, self.running[key])
            self.threads.setdefault(key, set()).add(threading.get_ident())
            self.reads += 1
        try:
            if self.gate is not None and not self.gate.wait(5):
                raise RuntimeError("fixture gate was not opened")
            if self.pause:
                time.sleep(self.pause)
            return {"phase": "idle", "messages": []}
        finally:
            with self.lock:
                self.running[key] -= 1

    def reading(self, key=None):
        with self.lock:
            return self.running.get(key, 0) if key else sum(self.running.values())


class WatcherPlaceTest(unittest.TestCase):
    """Switching conversations quickly: a watcher keeps its place until its thread has left,
    so watcher reads never exceed the cap and one conversation is never read twice at once."""
    def setUp(self):
        self.board = FakeBoard()
        self.refs = [{**REF, "id": "a1b2c3d4-%04d" % number} for number in range(12)]
        self.keys = [events.watch_key(ref) for ref in self.refs]

    def hub(self, chat, room_wait):
        hub = events.EventHub(self.board, chat, heartbeat=5, grace=30.0, fast=0.01, idle=(0.01,), error=0.01,
                              peer_check=0.02, max_watchers=8, room_wait=room_wait)
        self.addCleanup(lambda: wait_for(lambda: not hub.watchers, timeout=3))
        self.addCleanup(hub.close)
        return hub

    def test_switching_away_during_reads_never_starts_a_ninth_read(self):
        gate = threading.Event()
        chat = GatedChat(gate)
        hub = self.hub(chat, room_wait=0.2)
        # Runs first: blocked reads end before the hub waits for its watchers to leave.
        self.addCleanup(gate.set)
        for number in range(8):
            stream = hub.open(dict(self.refs[number]))
            wait_for(lambda: chat.reading(self.keys[number]) == 1)
            # Switched away while that conversation's read is still running.
            hub.close_stream(stream)
        # Every place is idle but still reading: the ninth conversation waits, then is refused.
        started = time.monotonic()
        self.assertIsNone(hub.open(dict(self.refs[8])))
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertEqual(len(hub.watchers), 8)
        self.assertEqual(chat.reading(), 8)
        # The longest idle one was asked to leave; following it again revives that same watcher.
        first = hub.watchers[self.keys[0]]
        self.assertTrue(first.retiring)
        again = hub.open(dict(self.refs[0]))
        self.assertIs(hub.watchers[self.keys[0]], first)
        self.assertFalse(first.retiring)
        self.assertEqual(chat.reading(self.keys[0]), 1)
        # Reads finish: idle watchers leave, one at a time, for the next conversations.
        gate.set()
        streams = [hub.open(dict(ref)) for ref in self.refs[8:12]]
        self.assertNotIn(None, streams)
        self.assertEqual(len(hub.watchers), 8)
        self.assertIs(hub.watchers[self.keys[0]], first)
        wait_for(lambda: all(key in chat.threads for key in self.keys[8:12]))
        for stream in [again] + streams:
            hub.close_stream(stream)
        self.assertLessEqual(chat.peak, 8)
        self.assertEqual(chat.peak_one, 1)
        self.assertEqual(len(chat.threads[self.keys[0]]), 1)

    def test_rapid_switching_from_several_tabs_keeps_reads_within_the_cap(self):
        chat = GatedChat(pause=0.003)
        hub = self.hub(chat, room_wait=0.05)
        stop = time.monotonic() + 0.6
        opened, crowded = [], []

        def tab(seed):
            pick = random.Random(seed)
            while time.monotonic() < stop:
                stream = hub.open(dict(self.refs[pick.randrange(len(self.refs))]))
                if stream is None:
                    continue
                opened.append(stream)
                time.sleep(pick.random() * 0.004)
                hub.close_stream(stream)
                with hub.lock:
                    if len(hub.watchers) > 8:
                        crowded.append(len(hub.watchers))

        tabs = [threading.Thread(target=tab, args=(seed,), daemon=True) for seed in range(6)]
        for thread in tabs:
            thread.start()
        for thread in tabs:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in tabs))
        self.assertEqual(crowded, [])
        self.assertGreater(len(opened), 30)
        self.assertGreater(chat.reads, 30)
        self.assertLessEqual(chat.peak, 8)
        self.assertEqual(chat.peak_one, 1)


class WatcherIntervalTest(unittest.TestCase):
    """The production schedule: 2 s when active, idle backoff 5→10→20→30 s, errors ≥ 10 s."""
    def setUp(self):
        self.board, self.chat = FakeBoard(), FakeChat()
        self.hub = events.EventHub(self.board, self.chat)
        self.addCleanup(self.hub.close)
        self.watcher = events.Watcher(events.watch_key(REF), REF)

    def delays(self, *reads):
        return [self.hub._next_delay(self.watcher, *read) for read in reads]

    IDLE = (False, "same", False)
    CHANGED = (False, "new", True)
    WORKING = (True, "same", False)
    ERROR = (False, None, False)

    def test_unchanged_idle_reads_back_off_and_stay_at_thirty_seconds(self):
        self.assertEqual(self.delays(*[self.IDLE] * 7), [5.0, 10.0, 20.0, 30.0, 30.0, 30.0, 30.0])

    def test_working_and_changed_digest_return_to_two_seconds_and_restart_backoff(self):
        self.assertEqual(self.delays(self.IDLE, self.IDLE, self.IDLE, self.CHANGED, self.IDLE, self.IDLE),
                         [5.0, 10.0, 20.0, 2.0, 5.0, 10.0])
        self.assertEqual(self.delays(self.IDLE, self.IDLE, self.IDLE, self.IDLE, self.WORKING, self.WORKING,
                                     self.IDLE), [20.0, 30.0, 30.0, 30.0, 2.0, 2.0, 5.0])

    def test_errors_wait_at_least_ten_seconds_and_never_shorter_than_the_idle_step(self):
        self.assertEqual(self.delays(self.ERROR, self.ERROR), [10.0, 10.0])
        self.assertEqual(self.delays(self.IDLE, self.IDLE, self.ERROR, self.IDLE, self.IDLE, self.ERROR),
                         [5.0, 10.0, 20.0, 20.0, 30.0, 30.0])
        # An error does not reset the backoff either.
        self.assertEqual(self.delays(self.IDLE), [30.0])

    def test_send_or_answer_boost_matches_only_that_conversation(self):
        other = events.Watcher(events.watch_key({**REF, "id": "ffffffff-0000"}), {**REF, "id": "ffffffff-0000"})
        # The server's identity of a listed Claude row: its home is the row's (none).
        claude_ref = {"host": "remote", "agent": "claude", "home": None, "id": "c1c2c3c4"}
        claude = events.Watcher(events.watch_key(claude_ref), claude_ref)
        self.hub.watchers = {w.key: w for w in (self.watcher, other, claude)}
        self.assertEqual(self.delays(self.IDLE, self.IDLE, self.IDLE, self.IDLE), [5.0, 10.0, 20.0, 30.0])
        other.idle_step = claude.idle_step = 3
        # Boosts carry the server-normalized identity; a different Codex home is another conversation.
        self.hub.boost({**REF, "home": ".codex-other"})
        self.assertFalse(self.watcher.wake.is_set())
        self.hub.boost(dict(REF))
        self.assertTrue(self.watcher.wake.is_set())
        self.assertFalse(other.wake.is_set())
        self.assertEqual(self.delays(self.IDLE, self.IDLE), [2.0, 5.0])
        self.assertEqual(other.idle_step, 3)
        self.hub.boost(dict(claude_ref))
        self.assertTrue(claude.wake.is_set())
        self.assertEqual(self.hub._next_delay(claude, False, "same", False), 2.0)
        for value in (None, "remote", ["remote"]):
            self.hub.boost(value)

    def test_boost_wakes_a_watcher_waiting_out_a_long_idle_interval(self):
        hub = events.EventHub(self.board, self.chat, heartbeat=5, fast=0.02, idle=(30.0,), error=30.0,
                              peer_check=0.02)
        self.addCleanup(hub.close)
        stream = hub.open(dict(REF))
        self.addCleanup(hub.close_stream, stream)
        wait_for(lambda: len(self.chat.calls) == 2)
        # Unchanged now, so the watcher sleeps its 30 s idle step.
        time.sleep(0.1)
        self.assertEqual(len(self.chat.calls), 2)
        hub.boost(dict(REF))
        wait_for(lambda: len(self.chat.calls) >= 3, timeout=2)


if __name__ == "__main__":
    unittest.main()
