"""Start, stop and inspect the per-channel systemd units.

systemd owns the streams; this service is only a remote control. That is the
point of the split: the web UI can crash, hang or be stopped for an upgrade
and not one frame stops, because nothing streaming is a child of it.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
import subprocess
import time

log = logging.getLogger(__name__)

UNIT_TEMPLATE = "dashboard-stream@{channel}.service"

# Only ever these, and only ever on our own template. The service account's
# sudo rule is written to match, so a bug here cannot reach another unit.
ALLOWED = ("start", "stop", "restart", "enable", "disable")

_SHOW_FIELDS = (
    "ActiveState", "SubState", "Result", "MainPID",
    "ExecMainStartTimestamp", "NRestarts",
    # Whether it comes back after a reboot, which is a different question
    # from whether it is running now, and the one people forget to ask.
    "UnitFileState",
)


def unit_for(channel: int) -> str:
    return UNIT_TEMPLATE.format(channel=int(channel))


def available() -> bool:
    """False on a machine without systemd, e.g. a laptop running the tests."""
    return shutil.which("systemctl") is not None


def status(channel: int) -> dict:
    """What systemd thinks of one channel's unit."""
    if not available():
        return {"available": False, "active": "unknown", "sub": "",
                "since": "", "restarts": 0, "pid": 0, "at_boot": False}

    unit = unit_for(channel)
    args = ["systemctl", "show", unit]
    for field in _SHOW_FIELDS:
        args += ["-p", field]
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=10, check=False).stdout
    except subprocess.SubprocessError as exc:
        log.warning("systemctl show %s failed: %s", unit, exc)
        return {"available": True, "active": "unknown", "sub": "",
                "since": "", "restarts": 0, "pid": 0, "at_boot": False}

    values = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {
        "available": True,
        "active": values.get("ActiveState", "unknown"),
        "sub": values.get("SubState", ""),
        "result": values.get("Result", ""),
        "since": values.get("ExecMainStartTimestamp", ""),
        "restarts": int(values.get("NRestarts") or 0),
        "pid": int(values.get("MainPID") or 0),
        "at_boot": values.get("UnitFileState", "") == "enabled",
    }


# ffmpeg appends a block of key=value lines every stats period, so the last
# value of each key is the current one. Reading only the tail keeps this cheap
# however long the stream has been up.
PROGRESS_TAIL_BYTES = 4096
_PROGRESS_KEYS = ("fps", "speed", "bitrate", "drop_frames", "dup_frames",
                  "out_time", "frame")


def progress(channel: int, run_dir: str = "/run/eclerstreamer") -> dict:
    """The encoder's own numbers, or {} if it is not writing any.

    This is what turns "the process is alive" into "this stream is keeping
    up". drop_frames matters more than speed: speed reports pace, drops
    report loss.
    """
    path = Path(run_dir) / f"progress-{int(channel)}.txt"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - PROGRESS_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return {}

    found: dict[str, str] = {}
    for line in tail.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in _PROGRESS_KEYS:
            found[key] = value.strip()
    return found


def progress_age(channel: int, run_dir: str = "/run/eclerstreamer",
                 now: float | None = None) -> float | None:
    """Seconds since ffmpeg last wrote its progress, or None if it never did.

    ffmpeg writes every 10s. A process that is alive but no longer writing
    has stopped encoding -- the one failure systemd cannot see.
    """
    path = Path(run_dir) / f"progress-{int(channel)}.txt"
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - modified)


#: "Dropped" on the page means bursts within this much recent stream time.
RECENT_DROPS_SECONDS = 3600
#: An hour of 10s progress blocks is about 90 KB; read a margin more.
RECENT_TAIL_BYTES = 256 * 1024


def _seconds(out_time: str) -> float | None:
    """ffmpeg's out_time, "HHH:MM:SS.ffffff", in seconds."""
    try:
        hours, minutes, seconds = out_time.strip().split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def recent_drops(channel: int, run_dir: str = "/run/eclerstreamer",
                 window: float = RECENT_DROPS_SECONDS) -> int | None:
    """Frames dropped in bursts during the last `window` of stream time.

    A burst is more than one frame dropped between two 10s reports: the
    encoder fell behind. Single frames, roughly one an hour, are drift
    between the capture and output clocks -- harmless, and on a stream that
    runs for weeks they add up to hundreds, which a cumulative counter showed
    as a red warning on a perfectly healthy channel (543 in three weeks, all
    drift). None if there is no progress file.
    """
    path = Path(run_dir) / f"progress-{int(channel)}.txt"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - RECENT_TAIL_BYTES)
            handle.seek(start)
            tail = handle.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    lines = tail.splitlines()
    if start > 0 and lines:
        lines = lines[1:]                   # the first line may be cut short
    blocks: list[tuple[float, int]] = []
    when, dropped = None, None
    for line in lines:
        key, _, value = line.partition("=")
        if key == "out_time":
            when = _seconds(value)
        elif key == "drop_frames":
            try:
                dropped = int(value)
            except ValueError:
                dropped = None
        elif key == "progress":
            if when is not None and dropped is not None:
                blocks.append((when, dropped))
            when, dropped = None, None
    if len(blocks) < 2:
        return 0 if blocks else None
    newest = blocks[-1][0]
    total = 0
    for (_, before), (at, after) in zip(blocks, blocks[1:]):
        if at >= newest - window and after - before > 1:
            total += after - before
    return total


#: Progress older than this means the encoder has stopped writing: three
#: missed 10s reports, so one slow write is not an alarm.
STALL_SECONDS = 30.0
#: Below this the encoder is falling behind real time and frames are lost.
SLOW_SPEED = 0.95


def health(enabled: bool, status: dict, found: dict,
           age: float | None) -> str:
    """One word for "is this channel actually streaming?".

    off      not meant to be streaming
    down     meant to be, but the unit is not running
    stalled  running, but the encoder has stopped reporting progress
    slow     encoding, but slower than real time
    ok       running and keeping up
    """
    if not enabled:
        return "off"
    if status.get("active") != "active":
        return "down"
    if age is None or age > STALL_SECONDS:
        return "stalled"
    try:
        speed = float(str(found.get("speed", "")).rstrip("x"))
    except ValueError:
        speed = None
    if speed is not None and speed < SLOW_SPEED:
        return "slow"
    return "ok"


def act(channel: int, action: str) -> tuple[bool, str]:
    """Run one allowed systemctl verb. Returns (ok, message)."""
    if action not in ALLOWED:
        raise ValueError(f"{action} is not one of {', '.join(ALLOWED)}")
    if not available():
        return False, "systemd is not available on this host"

    unit = unit_for(channel)
    # sudo -n: never prompt. If the rule is missing we want a clear failure
    # here, not a service that hangs waiting for a password nobody can type.
    command = ["sudo", "-n", "systemctl", action, unit]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=45, check=False)
    except subprocess.SubprocessError as exc:
        return False, str(exc)

    if done.returncode == 0:
        return True, f"{action} {unit}"
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return False, detail[-1] if detail else f"systemctl {action} failed"
