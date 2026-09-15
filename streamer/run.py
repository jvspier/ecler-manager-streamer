#!/usr/bin/env python3
"""Start the streamer's web service.

    python3 run.py --config /etc/eclerstreamer/config.json --port 8478

It manages the per-channel systemd units; it does not stream anything itself.
Stopping it leaves every stream running.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eclerstreamer import __version__, auth as auth_mod, config as config_mod  # noqa: E402
from eclerstreamer.server import make_server  # noqa: E402

log = logging.getLogger("eclerstreamer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(config_mod.DEFAULT_PATH))
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8478)
    parser.add_argument("--env-file", default=None,
                        help="file holding the login credentials")
    parser.add_argument("--no-auth", action="store_true",
                        help="run without a login. Only on a trusted network.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config_path = Path(args.config)
    # Fail here rather than on the first request: a service that starts and
    # then 500s on every page is harder to diagnose than one that refuses.
    try:
        cfg = config_mod.load(config_path)
    except (OSError, ValueError) as exc:
        log.error("%s", exc)
        return 2
    log.info("%d dashboard(s) configured in %s", len(cfg.dashboards), config_path)

    auth = auth_mod.Auth() if args.no_auth else auth_mod.load(args.env_file)
    if not auth.enabled:
        log.warning("no login configured: anyone who can reach this port can "
                    "start and stop streams")

    server = make_server(config_path, args.host, args.port, auth=auth)
    log.info("listening on http://%s:%d/", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
