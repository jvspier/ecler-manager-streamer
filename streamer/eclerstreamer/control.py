"""Start, stop and inspect the per-channel systemd units.

systemd owns the streams; this service is only a remote control. That is the
point of the split: the web UI can crash, hang or be stopped for an upgrade
and not one frame stops, because nothing streaming is a child of it.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

UNIT_TEMPLATE = "dashboard-stream@{channel}.service"

# Only ever these, and only ever on our own template. The service account's
# sudo rule is written to match, so a bug here cannot reach another unit.
ALLOWED = ("start", "stop", "restart")

_SHOW_FIELDS = (
    "ActiveState", "SubState", "Result", "MainPID",
    "ExecMainStartTimestamp", "NRestarts",
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
                "since": "", "restarts": 0, "pid": 0}

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
                "since": "", "restarts": 0, "pid": 0}

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
    }


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
