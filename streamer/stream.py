#!/usr/bin/env python3
"""Run one dashboard stream in the foreground.

systemd calls this, one instance per channel:

    ExecStart=/usr/bin/python3 /opt/eclerstreamer/stream.py --channel %i

It reads the dashboard's settings from the streamer config, then hands over to
tools/teststream.py, which renders the page and encodes it. Running in the
foreground is the point: systemd supervises the real work rather than a
launcher that exits immediately and leaves orphans behind.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from eclerstreamer import config as config_mod  # noqa: E402

log = logging.getLogger("stream")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--config", default=None,
                        help=f"default: {config_mod.DEFAULT_PATH}")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the command instead of running it")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        cfg = config_mod.load(args.config or config_mod.DEFAULT_PATH)
    except (OSError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    dash = cfg.dashboard(args.channel)
    if dash is None:
        log.error("channel %s is not in %s", args.channel, cfg.path)
        return 2
    if not dash.url:
        log.error("channel %s has no URL set", args.channel)
        return 2
    # Disabled means the unit should not have been started. Failing loudly is
    # better than exiting cleanly, which systemd would read as "job done".
    if not dash.enabled:
        log.error("channel %s is disabled in the config", args.channel)
        return 2

    teststream = HERE / "tools" / "teststream.py"
    command = [
        sys.executable, str(teststream),
        "--group", config_mod.multicast_for(dash.channel),
        "--url", dash.url,
        "--display", dash.display_name,
        "--size", dash.size,
        "--capture-fps", str(dash.capture_fps),
        "--fps", str(dash.fps),
        "--bitrate", dash.bitrate,
        "--qmin", str(cfg.qmin),
    ]
    if cfg.no_bframes:
        command.append("--no-bframes")
    if cfg.local_addr:
        command += ["--local-addr", cfg.local_addr]
    elif cfg.interface:
        command += ["--interface", cfg.interface]

    if args.dry_run:
        print(" ".join(command))
        return 0

    log.info("channel %s -> %s on %s", dash.channel,
             config_mod.multicast_for(dash.channel), dash.display_name)
    # exec, not spawn: teststream.py becomes this process, so systemd's
    # SIGTERM reaches the thing that knows how to tear the browser down.
    os.execv(command[0], command)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
