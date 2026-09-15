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
