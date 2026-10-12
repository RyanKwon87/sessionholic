"""Session-derived work, with a bounded observer inside the existing board process."""
import hashlib
import json
import re
import threading
import time

from automatic_work import AutomaticWork
from goals import GoalError


def source_ref(host, agent, row):
    sid = row.get("id")
    if not sid and agent == "claude" and row.get("kind") == "interactive" and re.fullmatch(r"[0-9A-Za-z-]{8,64}", row.get("sessionId") or ""):
        sid = row["sessionId"]
    return {"host": host, "agent": agent, "id": sid,
            "home": row.get("home") or (".codex" if agent == "codex" else ".claude")}


def identity(source):
    return (source.get("host"), source.get("agent"), source.get("home") or
            (".codex" if source.get("agent") == "codex" else ".claude"), source.get("id"))


class WorkService:
    """No model calls, native turns, app-server starts, or new external services.

    List changes create cards immediately. One observer reads only changed sessions,
    at most six per minute, and records a failed digest before trying it. A failure
    is not retried until that session changes again. Offline host recovery uses
    the board's existing bounded backoff, even with no browser open.
    """
    def __init__(self, board, goals, state_dir, store=None, clock=None):
        self.board, self.goals = board, goals
        self.store = store if store is not None else AutomaticWork(state_dir)
        self.clock = clock or time.monotonic
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.thread = None
        self.observed = {}
        self.observed_at = {}
        self.version = None
        self.last_read = -1000.0
        self.last_poll = -1000.0
        self.read_interval = 10.0
        self.poll_interval = max(120.0, float(getattr(board, "interval", 120)))
        self.health = {"state": "ready", "message": None}

    def snapshot(self):
        automatic = self.store.sync(self.board.snapshot())
        manual = self.goals.snapshot()
        manual_rows = []
        covered = set()
        for goal in manual["goals"]:
            if not goal.get("archived") and goal.get("status") != "done":
                covered.update(identity(ref) for ref in goal.get("sessions", []))
            activity = {"review": "attention", "in_progress": "active",
                        "waiting": "waiting", "done": "result"}.get(goal["status"], "recent")
            manual_rows.append({**goal, "automatic": False, "activity": activity})
        rows = [goal for goal in automatic["goals"]
                if goal.get("automation", {}).get("segment") or not goal.get("sessions")
                or not all(identity(ref) in covered for ref in goal["sessions"])]
        return {"revision": automatic["revision"] + manual["revision"],
                "goals": rows + manual_rows, "automation": dict(self.health)}

    def update(self, work_id, payload):
        auto = self.store.snapshot()
        if any(goal["id"] == work_id for goal in auto["goals"]):
            return self.store.update(work_id, payload)
        return self.goals.save(payload, work_id)

    def archive_session(self, source, archived=True):
        """Board-only organization: it never stops a native execution."""
        self.store.sync(self.board.snapshot())
        matches = [goal for goal in self.store.snapshot()["goals"]
                   if any(identity(ref) == identity(source) for ref in goal.get("sessions", []))]
        changed = 0
        for goal in matches:
            self.store.update(goal["id"], {"revision": goal["revision"], "archived": archived,
                              "reason": "세션 작업 보관" if archived else "세션 작업 복원"})
            changed += 1
        return {"archived": archived, "changed": changed, "executionChanged": False}

    def observe(self, source, conversation):
        if not conversation.get("error") and not conversation.get("stale"):
            self.store.observe(source, conversation)
            self.health = {"state": "ready", "message": None}
        else:
            self.health = {"state": "limited", "message": "일부 작업은 마지막 확인 내용을 표시합니다."}

    def link_sessions(self, source, target):
        return self.store.link_sessions(source, target)

    def changed(self, *_):
        self.wake.set()

    def start(self):
        if self.thread is not None:
            return
        self.board.add_listener(self.changed)
        self.thread = threading.Thread(target=self.run, name="work-observer", daemon=True)
        self.thread.start()

    def candidates(self, snapshot):
        pending = []
        for host in snapshot.get("hosts", []):
            if not host.get("ok") or host.get("paused") or host.get("refreshing"):
                continue
            for agent in ("codex", "claude"):
                for row in (host.get("data") or {}).get(agent, []):
                    source = source_ref(host["name"], agent, row)
                    if not source.get("id") or row.get("appServer") == "off" and not row.get("canRead"):
                        continue
                    # fetchedAt, elapsed timers and host heartbeat never trigger a reread.
                    content = {key: row.get(key) for key in
                               ("name", "title", "preview", "snippet", "phase", "state", "status",
                                "updatedAt", "lastMessageAt", "lastActiveAt", "waitingFor", "parentSessionId")}
                    digest = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
                    if self.observed.get(identity(source)) == digest:
                        continue
                    phase = row.get("phase") or row.get("state") or row.get("status")
                    priority = 0 if phase == "needs_input" else 1 if phase == "working" else 2
                    updated = row.get("updatedAt") or row.get("lastActiveAt") or 0
                    updated = updated if isinstance(updated, (int, float)) else 0
                    pending.append((self.observed_at.get(identity(source), -1), priority, -updated, source, digest))
        return sorted(pending, key=lambda item: item[:3])

    def step(self):
        snapshot = self.board.snapshot()
        if snapshot.get("version") != self.version:
            self.store.sync(snapshot)
            self.version = snapshot.get("version")
        now = self.clock()
        if now - self.last_poll >= self.poll_interval:
            self.last_poll = now
            for host in snapshot.get("hosts", []):
                if not host.get("refreshing"):
                    self.board.request_refresh(host["name"], force=False)
        if now - self.last_read < self.read_interval:
            return
        pending = self.candidates(snapshot)
        if not pending:
            return
        _, _, _, source, digest = pending[0]
        self.last_read = now
        if len(self.observed) >= 4096 and identity(source) not in self.observed:
            removed = next(iter(self.observed))
            self.observed.pop(removed)
            self.observed_at.pop(removed, None)
        self.observed[identity(source)] = digest
        self.observed_at[identity(source)] = now
        data = self.board.read(source["host"], source["agent"], source["id"], source["home"], fresh=True)
        self.observe(source, data)

    def run(self):
        while not self.stopped.is_set():
            try:
                self.step()
            except (GoalError, OSError, RuntimeError, ValueError, KeyError, TypeError):
                # No private conversation, path, request or exception text in logs/UI.
                self.health = {"state": "limited", "message": "일부 작업의 최신 내용을 확인하지 못했습니다. 마지막 기록은 유지됩니다."}
            self.wake.wait(2)
            self.wake.clear()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread is not None:
            self.board.remove_listener(self.changed)
            self.thread.join(2)
        self.store.close()
