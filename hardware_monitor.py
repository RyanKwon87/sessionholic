"""The board's machines' hardware, read only while someone looks (설정 `hardware`).

Each machine is read in its own thread (`runner(host)` returns hardware.py's JSON), so a slow
or sleeping one never holds up the rest. A failed read keeps the last good numbers, marked as
earlier ones, and waits longer before the next try. CPU use is the share of busy ticks between
two readings of the same machine.
"""
import copy
import threading
import time

MAX_BACKOFF = 8  # a failing machine is read at most every interval × 8


def _count(value, limit=1 << 62):
    return value if type(value) is int and 0 <= value <= limit else None


def _share(part, whole):
    return round(100 * part / whole) if part is not None and whole else None


def normalize(raw):
    """The fields the page shows, checked: a machine's output is only data."""
    if not isinstance(raw, dict) or raw.get("schema") != 1 or raw.get("system") not in ("macos", "windows"):
        raise ValueError("기기 정보 형식이 올바르지 않습니다.")
    cpu = raw.get("cpu") if isinstance(raw.get("cpu"), dict) else {}
    load = cpu.get("load")
    load = [round(float(v), 2) for v in load] if (isinstance(load, list) and len(load) == 3 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10000 for v in load)) else None
    ticks = cpu.get("ticks") if isinstance(cpu.get("ticks"), dict) else {}
    ticks = {"busy": _count(ticks.get("busy")), "total": _count(ticks.get("total"))}
    model = cpu.get("model")
    memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
    total, used = _count(memory.get("total")), _count(memory.get("used"))
    disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else {}
    disk_total, free = _count(disk.get("total")), _count(disk.get("free"))
    battery = raw.get("battery")
    if isinstance(battery, dict) and type(battery.get("percent")) is int and 0 <= battery["percent"] <= 100:
        battery = {"percent": battery["percent"], "charging": battery.get("charging") is True,
                   "power": "ac" if battery.get("power") == "ac" else "battery"}
    else:
        battery = None
    return {"system": raw["system"],
            "cpu": {"model": model[:120] if isinstance(model, str) and model.strip() else None,
                    "cores": _count(cpu.get("cores"), 4096), "load": load,
                    "ticks": ticks if None not in ticks.values() and ticks["busy"] <= ticks["total"] else None},
            "memory": {"total": total, "used": used, "percent": _share(used, total)} if total and used is not None
            and used <= total else None,
            "disk": {"total": disk_total, "free": free, "percent": _share(disk_total - free, disk_total)}
            if disk_total and free is not None and free <= disk_total else None,
            "battery": battery, "uptimeSeconds": _count(raw.get("uptimeSeconds"))}


class HardwareMonitor:
    def __init__(self, hosts, runner, interval=30, manual_interval=10, clock=time.time, threaded=True):
        self.hosts = [{"name": h["name"], "label": h.get("label") or h["name"]} for h in hosts]
        self.runner, self.interval, self.manual_interval = runner, interval, manual_interval
        self.clock, self.threaded = clock, threaded
        self.by_name = {h["name"]: h for h in hosts}
        self.lock = threading.Lock()
        self.state = {h["name"]: {"data": None, "fetchedAt": None, "attemptedAt": None, "failures": 0,
                                  "refreshing": False, "error": None, "cpuPercent": None} for h in hosts}

    def snapshot(self, refresh=False, force=False):
        now = self.clock()
        due = []
        with self.lock:
            for host in self.hosts:
                row = self.state[host["name"]]
                if row["refreshing"] or not (refresh or force):
                    continue
                spacing = self.manual_interval if force and not row["failures"] else \
                    self.interval * min(2 ** row["failures"], MAX_BACKOFF) if row["failures"] else self.interval
                if row["attemptedAt"] is None or now - row["attemptedAt"] >= spacing:
                    row.update(refreshing=True, attemptedAt=now)
                    due.append(host["name"])
        for name in due:
            if self.threaded:
                threading.Thread(target=self._read, args=(name,), daemon=True, name="hardware-" + name).start()
            else:
                self._read(name)
        return self.public()

    def _read(self, name):
        try:
            data, error = normalize(self.runner(self.by_name[name])), None
        except Exception as exc:  # any failure is that machine's status, never the board's
            data, error = None, exc
        with self.lock:
            row = self.state[name]
            row["refreshing"] = False
            if data is None:
                row["failures"] += 1
                row["error"] = ("연결하지 못했습니다. 잠자기·오프라인이면 깨어난 뒤 다시 읽습니다."
                                if isinstance(error, (RuntimeError, OSError)) else "기기 정보를 읽지 못했습니다.")
                return
            before = (row["data"] or {}).get("cpu", {}).get("ticks")
            after = data["cpu"]["ticks"]
            row["cpuPercent"] = None
            if before and after and after["total"] > before["total"] and after["busy"] >= before["busy"]:
                row["cpuPercent"] = max(0, min(100, _share(after["busy"] - before["busy"], after["total"] - before["total"])))
            row.update(data=data, fetchedAt=int(self.clock()), failures=0, error=None)

    def public(self):
        with self.lock:
            hosts = []
            for host in self.hosts:
                row = self.state[host["name"]]
                data = copy.deepcopy(row["data"])
                status = "pending" if data is None and not row["failures"] else \
                    "offline" if data is None else "stale" if row["failures"] else "ready"
                item = {"name": host["name"], "label": host["label"], "status": status,
                        "refreshing": row["refreshing"], "fetchedAt": row["fetchedAt"], "error": row["error"]}
                if data is not None:
                    cpu = data["cpu"]
                    cpu.pop("ticks", None)
                    cpu["percent"] = row["cpuPercent"]
                    item.update(data)
                hosts.append(item)
            return {"available": True, "refreshing": any(h["refreshing"] for h in hosts), "hosts": hosts}
