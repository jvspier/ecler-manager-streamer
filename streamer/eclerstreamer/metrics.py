"""CPU, memory and GPU load of the streamer host, for the bars on its page.

Read-only and unprivileged: /proc and /sys only, as the service account.

CPU and memory come from /proc/stat and /proc/meminfo. The GPU is the
interesting one. intel_gpu_top needs root, which this service deliberately
does not have. But the i915 driver publishes, per open DRM file, how long each
engine has been busy (``drm-engine-video: <ns>`` in /proc/<pid>/fdinfo), and a
process may read that for processes of its own account -- the stream units run
as the same user as this service. Two readings a few seconds apart give each
engine's busy share, overall and per channel, without any privilege.

Rates need two samples, so a background thread takes one every few seconds and
keeps the latest result; a page load just reads it.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

SAMPLE_SECONDS = 5.0
#: Which engines matter here: "video" encodes, "render" converts colours.
GPU_ENGINES = ("render", "video", "video-enhance", "copy")

_GROUP_RE = re.compile(r"udp://(239\.\d+\.\d+\.\d+):")


def read_cpu(proc: Path) -> tuple[int, int, int] | None:
    """(busy, steal, total) jiffies since boot, from the first line of stat."""
    try:
        fields = (proc / "stat").read_text().split("\n", 1)[0].split()
    except OSError:
        return None
    if not fields or fields[0] != "cpu":
        return None
    values = [int(v) for v in fields[1:]]
    # user nice system idle iowait irq softirq steal guest guest_nice;
    # guest time is already counted in user, so stop at steal.
    values = (values + [0] * 8)[:8]
    idle = values[3] + values[4]
    total = sum(values)
    return total - idle, values[7], total


def read_memory(proc: Path) -> dict | None:
    """Total and used bytes. "Used" means not available to new programs."""
    found = {}
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                found[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if len(found) < 2:
        return None
    return {"total": found["MemTotal"],
            "used": found["MemTotal"] - found["MemAvailable"]}


def read_gpu_clients(proc: Path, uid: int) -> dict[str, dict]:
    """Per DRM client of our own processes: its pid, group and engine ns.

    Keyed by drm-client-id, so a file descriptor duplicated within a process
    is counted once.
    """
    clients: dict[str, dict] = {}
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return clients
    for pid in pids:
        base = proc / pid
        try:
            if base.stat().st_uid != uid:
                continue
            if (base / "comm").read_text().strip() != "ffmpeg":
                continue
            cmdline = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
            fdinfos = list((base / "fdinfo").iterdir())
        except OSError:
            continue                        # gone, or not ours to read
        match = _GROUP_RE.search(cmdline)
        group = match.group(1) if match else None
        for fdinfo in fdinfos:
            try:
                text = fdinfo.read_text()
            except OSError:
                continue
            if "drm-engine-" not in text:
                continue
            fields = {}
            for line in text.splitlines():
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
            client = fields.get("drm-client-id")
            if client is None or client in clients:
                continue
            engines = {}
            for engine in GPU_ENGINES:
                raw = fields.get(f"drm-engine-{engine}", "")
                try:
                    engines[engine] = int(raw.split()[0])
                except (ValueError, IndexError):
                    pass
            clients[client] = {"pid": int(pid), "group": group, "ns": engines}
    return clients


def read_gpu_clock(sys_root: Path) -> dict | None:
    """Current and maximum clock in MHz of the first GPU that reports one."""
    for card in sorted((sys_root / "class" / "drm").glob("card*")):
        try:
            current = int((card / "gt_act_freq_mhz").read_text())
            maximum = int((card / "gt_RP0_freq_mhz").read_text())
        except (OSError, ValueError):
            continue
        return {"mhz": current, "max_mhz": maximum}
    return None


class Sampler:
    """Keeps the latest load figures, refreshed by a background thread."""

    def __init__(self, proc: Path = Path("/proc"), sys_root: Path = Path("/sys"),
                 uid: int | None = None, interval: float = SAMPLE_SECONDS) -> None:
        self.proc, self.sys_root = proc, sys_root
        self.uid = os.getuid() if uid is None else uid
        self.interval = interval
        self._lock = threading.Lock()
        self._previous: dict | None = None
        self._latest: dict = {}
        self._thread: threading.Thread | None = None

    # --- sampling ----------------------------------------------------------
    def _read(self) -> dict:
        return {"at": time.monotonic(), "cpu": read_cpu(self.proc),
                "gpu": read_gpu_clients(self.proc, self.uid)}

    def sample(self) -> dict:
        """Take a reading and work out rates against the previous one."""
        now = self._read()
        result: dict = {"memory": read_memory(self.proc),
                        "gpu_clock": read_gpu_clock(self.sys_root),
                        "cpu": None, "gpu": None, "updated": time.time()}
        previous = self._previous
        if previous is not None:
            result["cpu"] = _cpu_rates(previous["cpu"], now["cpu"])
            result["gpu"] = _gpu_rates(previous["gpu"], now["gpu"],
                                       now["at"] - previous["at"])
        self._previous = now
        with self._lock:
            self._latest = result
        return result

    def latest(self) -> dict:
        """The most recent figures; starts the sampler on first use."""
        self._ensure_running()
        with self._lock:
            return dict(self._latest)

    def _ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="metrics",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:               # a bad reading must not stop it
                pass
            time.sleep(self.interval)


def _cpu_rates(before, after) -> dict | None:
    if not before or not after:
        return None
    busy = after[0] - before[0]
    steal = after[1] - before[1]
    total = after[2] - before[2]
    if total <= 0:
        return None
    return {"busy": round(100.0 * busy / total, 1),
            "steal": round(100.0 * steal / total, 1),
            "cores": os.cpu_count() or 1}


def _gpu_rates(before: dict, after: dict, seconds: float) -> dict | None:
    """Engine busy %, overall and per multicast group (i.e. per channel).

    A client that appeared since the last reading started from zero inside
    the interval, so its whole counter counts. One that disappeared is
    simply gone from both totals.
    """
    if not after:
        return {"engines": {}, "groups": {}}     # nothing is using the GPU
    if seconds <= 0 or not before:
        # Nothing to compare with: every counter would look new, and its
        # whole lifetime would land in this one interval as a fake spike.
        return None
    window = seconds * 1e9
    engines: dict[str, float] = {}
    groups: dict[str, dict[str, float]] = {}
    for client, now in after.items():
        then = before.get(client, {"ns": {}})["ns"]
        for engine, ns in now["ns"].items():
            share = 100.0 * max(0, ns - then.get(engine, 0)) / window
            engines[engine] = engines.get(engine, 0.0) + share
            if now["group"]:
                per = groups.setdefault(now["group"], {})
                per[engine] = per.get(engine, 0.0) + share
    clamp = lambda v: round(min(100.0, v), 1)    # noqa: E731
    return {"engines": {e: clamp(v) for e, v in engines.items()},
            "groups": {g: {e: clamp(v) for e, v in per.items()}
                       for g, per in groups.items()}}
