"""One machine's hardware readings, as one JSON line on standard output.

The board runs it on itself, or sends it over SSH to another machine's Python (as stdin, the
way the collector travels), and keeps nothing there. Standard library only, Python 3.9
compatible, read-only: it starts no service and needs no privilege.

CPU use is not measured here: the cumulative busy and total ticks are reported and the board
compares two readings (no second sample or sleep on the machine).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

SCHEMA = 1
TIMEOUT = 5


def run(argv):
    """A system command's output, or "" when it is missing, fails or takes too long."""
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=TIMEOUT,
                              env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""


def parse_vm_stat(text):
    """Used bytes as Activity Monitor counts them (active + wired + compressed), or None."""
    page = re.search(r"page size of (\d+) bytes", text)
    if not page:
        return None
    pages = {}
    for name, value in re.findall(r'^"?([A-Za-z ]+?)"?:\s+(\d+)\.?$', text, re.M):
        pages[name.strip().lower()] = int(value)
    used = sum(pages.get(name, 0) for name in ("pages active", "pages wired down", "pages occupied by compressor"))
    return used * int(page.group(1)) if used else None


def parse_pmset(text):
    """{percent, charging, power} from `pmset -g batt`, or None without a battery."""
    found = re.search(r"(\d{1,3})%;\s*([^;]+);", text)
    if not found:
        return None
    state = found.group(2).strip().lower()
    # "charging", "finishing charge", "charged", "discharging" or "AC attached; not charging".
    return {"percent": max(0, min(100, int(found.group(1)))), "charging": state in ("charging", "finishing charge"),
            "power": "ac" if "AC Power" in text else "battery"}


def parse_boottime(text, now):
    found = re.search(r"sec = (\d+)", text)
    return max(0, int(now - int(found.group(1)))) if found else None


def mac_ticks():
    """Cumulative {busy, total} CPU ticks from the kernel (host_statistics), or None."""
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.mach_host_self.restype = ctypes.c_uint
        info = (ctypes.c_uint * 4)()  # user, system, idle, nice
        count = ctypes.c_uint(4)
        if libc.host_statistics(libc.mach_host_self(), 3, ctypes.byref(info), ctypes.byref(count)) != 0:
            return None
    except (OSError, AttributeError, ValueError):
        return None
    user, system, idle, nice = list(info)
    return {"busy": user + system + nice, "total": user + system + idle + nice}


def disk(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": usage.total, "free": usage.free}


def macos(command=run, ticks=mac_ticks, now=time.time):
    memory = command(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip()
    used = parse_vm_stat(command(["/usr/bin/vm_stat"]))
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load = None
    data = "/System/Volumes/Data"
    return {"schema": SCHEMA, "system": "macos",
            "cpu": {"model": command(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]).strip()[:120] or None,
                    "cores": os.cpu_count(), "load": load, "ticks": ticks()},
            "memory": {"total": int(memory), "used": used} if memory.isdigit() and used is not None else None,
            "disk": disk(data if os.path.isdir(data) else "/"),
            "battery": parse_pmset(command(["/usr/bin/pmset", "-g", "batt"])),
            "uptimeSeconds": parse_boottime(command(["/usr/sbin/sysctl", "-n", "kern.boottime"]), now())}


def windows():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Memory(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    status = Memory()
    status.dwLength = ctypes.sizeof(Memory)
    memory = ({"total": status.ullTotalPhys, "used": status.ullTotalPhys - status.ullAvailPhys}
              if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None)
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    ticks = None
    if kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        value = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime  # noqa: E731
        total = value(kernel) + value(user)  # kernel time includes idle time
        ticks = {"busy": total - value(idle), "total": total}
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    power = Power()
    battery = None
    if kernel32.GetSystemPowerStatus(ctypes.byref(power)) and power.BatteryFlag not in (128, 255) \
            and power.BatteryLifePercent <= 100:
        battery = {"percent": int(power.BatteryLifePercent), "charging": bool(power.BatteryFlag & 8),
                   "power": {1: "ac", 0: "battery"}.get(power.ACLineStatus)}  # 255: unknown
    model = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            model = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()[:120] or None
    except OSError:
        pass
    return {"schema": SCHEMA, "system": "windows",
            "cpu": {"model": model, "cores": os.cpu_count(), "load": None, "ticks": ticks},
            "memory": memory, "disk": disk(os.environ.get("SystemDrive", "C:") + "\\"), "battery": battery,
            "uptimeSeconds": int(kernel32.GetTickCount64() // 1000)}


def main():
    try:
        result = windows() if os.name == "nt" else macos() if sys.platform == "darwin" else {"error": "unsupported"}
    except Exception as exc:  # one line of JSON either way; the board shows its own message
        result = {"error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
