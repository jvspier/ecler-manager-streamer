#!/usr/bin/env python3
"""Restart channels that have been running longer than configured.

A timer runs this daily; it restarts **at most one channel per run**, the one
running longest. That staggers them without any scheduling logic: three
channels on a monthly interval come up for restart on three different days,
and only one wall screen is ever briefly dark.

Why restart at all: memory creeps in all three processes -- browser, Xvfb and
encoder -- and with the host's backup in snapshot mode nothing else ever
restarts them. Measured on this fleet: roughly 750 MB a week across three
channels, against 8 GB.

Why the whole channel rather than just the browser: Xvfb and ffmpeg account
for about a third of that growth between them, so restarting only Chromium
would leave most of it behind. The browser profile persists, so a full
restart comes back logged in.

    python3 maintenance.py            # restart one channel if any is due
    python3 maintenance.py --dry-run  # say what it would do
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from eclerstreamer import config as config_mod, control  # noqa: E402

log = logging.getLogger("maintenance")


def running_since(channel: int) -> float | None:
    """Seconds the unit has been up, or None if it is not running."""
    unit = control.unit_for(channel)
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState",
             "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10, check=False).stdout
    except subprocess.SubprocessError as exc:
        log.warning("cannot read %s: %s", unit, exc)
        return None

    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if values.get("ActiveState") != "active":
        return None
    stamp = values.get("ActiveEnterTimestamp", "").strip()
    if not stamp:
        return None
    # systemd prints e.g. "Sun 2026-09-20 00:00:24 BST". The weekday and the
    # zone abbreviation are not parseable portably, so take the middle.
    parts = stamp.split()
    if len(parts) < 3:
        return None
    try:
        started = datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, time.time() - started.timestamp())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        cfg = config_mod.load(args.config or config_mod.DEFAULT_PATH)
    except (OSError, ValueError) as exc:
        log.error("%s", exc)
        return 2

    days = cfg.restart_interval_days
    if days <= 0:
        log.info("automatic restarts are off")
        return 0

    limit = days * 86400
    due = []
    for dash in cfg.dashboards:
        if not dash.enabled:
            continue
        uptime = running_since(dash.channel)
        if uptime is None:
            continue
        if uptime >= limit:
            due.append((uptime, dash.channel))

    if not due:
        log.info("nothing due (interval %d days)", days)
        return 0

    # One per run, longest-running first: that is the whole staggering
    # mechanism, and it means a single screen is dark at a time.
    uptime, channel = max(due)
    log.info("channel %s has been up %.1f days (limit %d); %s",
             channel, uptime / 86400, days,
             "would restart" if args.dry_run else "restarting")
    if args.dry_run:
        return 0

    ok, message = control.act(channel, "restart")
    if not ok:
        log.error("channel %s: %s", channel, message)
        return 1
    log.info("channel %s restarted", channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
