#!/usr/bin/env python3
"""Start the Ecler VEO management dashboard.

    python3 run.py                      # http://127.0.0.1:8477
    python3 run.py --host 0.0.0.0       # reachable from other machines
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from eclermanager import __version__
from eclermanager import auth as auth_module
from eclermanager.config import ConfigError, load
from eclermanager.poller import Poller
from eclermanager.server import make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1, this machine only)")
    parser.add_argument("--port", type=int, default=8477, help="bind port (default: 8477)")
    parser.add_argument("--event-log", default=None,
                        help="append events as JSON lines to this file")
    parser.add_argument("--env-file", default=None,
                        help="file holding ECLER_AUTH_* credentials "
                             "(default: /etc/eclermanager/eclermanager.env "
                             "or ./.env)")
    parser.add_argument("--no-auth", action="store_true",
                        help="ignore any configured login (local testing)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    auth = auth_module.Auth() if args.no_auth else auth_module.load(args.env_file)
    logger = logging.getLogger("eclermanager")
    for warning in auth.warnings:
        logger.warning("auth: %s", warning)

    event_log = Path(args.event_log) if args.event_log else None
    poller = Poller(config, event_log_path=event_log)

    try:
        server = make_server(poller, args.host, args.port, auth)
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    logger.info(
        "watching %d receiver(s), %d channel(s), polling every %gs",
        len(config.active_receivers), len(config.channels), config.poll_interval_seconds,
    )
    exposed = args.host not in ("127.0.0.1", "localhost", "::1")
    if auth.enabled:
        logger.info("login required as %r", auth.user)
    elif exposed:
        logger.warning(
            "NO LOGIN and bound to %s: anyone who can reach this port can "
            "switch your TVs. Set one up with tools/setpassword.py", args.host
        )
    else:
        logger.info("no login configured (listening on %s only)", args.host)
    print(f"\n  Dashboard: http://{args.host}:{args.port}/\n")

    poller.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.shutdown()
        server.server_close()
        poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
