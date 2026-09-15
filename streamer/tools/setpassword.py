#!/usr/bin/env python3
"""Create or update the service account used to sign in to the dashboard.

    sudo python3 tools/setpassword.py --user tvadmin

It prompts for a password (never echoed, never taken from the command line
where it would land in shell history), then writes an env file holding the
scrypt hash of it plus a session secret.  The password itself is not stored.

    sudo python3 tools/setpassword.py --user tvadmin \\
        --env-file /etc/eclerstreamer/eclerstreamer.env --owner eclerstreamer

Restart the service afterwards for it to take effect:

    systemctl restart eclerstreamer
"""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclerstreamer import auth  # noqa: E402

DEFAULT_ENV_FILE = Path("/etc/eclerstreamer/eclerstreamer.env")
MIN_PASSWORD_LENGTH = 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True, help="service account name")
    parser.add_argument("--env-file", default=None,
                        help=f"where to write (default {DEFAULT_ENV_FILE})")
    parser.add_argument("--owner", default=None,
                        help="chown the file to this user (e.g. eclerstreamer)")
    parser.add_argument("--session-hours", type=float, default=None,
                        help="how long a login lasts (default 12)")
    parser.add_argument("--new-session-secret", action="store_true",
                        help="roll the session secret, logging everyone out")
    args = parser.parse_args(argv)

    if not auth.valid_user(args.user):
        print(f"✗ {args.user!r} is not a usable account name.\n"
              "  Use letters, digits and . _ - @ + (starting with a letter or\n"
              "  digit), up to 64 characters. Dashes are fine: 'tvadmin' works.\n"
              "  Excluded are characters an env file would mangle: spaces,\n"
              "  '#', quotes and newlines.", file=sys.stderr)
        return 2

    env_path = Path(args.env_file) if args.env_file else DEFAULT_ENV_FILE
    existing = {}
    if env_path.is_file():
        try:
            existing = auth.parse_env_file(env_path)
        except OSError as exc:
            print(f"✗ cannot read {env_path}: {exc}", file=sys.stderr)
            return 1

    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"✗ use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 2
    if password != getpass.getpass("Repeat: "):
        print("✗ passwords do not match.", file=sys.stderr)
        return 2

    secret = existing.get("ECLER_SESSION_SECRET", "")
    if args.new_session_secret or not secret:
        secret = secrets.token_urlsafe(32)
    hours = args.session_hours
    if hours is None:
        hours = float(existing.get("ECLER_SESSION_HOURS",
                                   auth.DEFAULT_SESSION_HOURS))

    lines = [
        "# Ecler VEO Manager credentials. Written by tools/setpassword.py.",
        "# The password itself is not stored here, only an scrypt hash of it.",
        "",
        f"ECLER_AUTH_USER={args.user}",
        f"ECLER_AUTH_PASSWORD_HASH={auth.hash_password(password)}",
        f"ECLER_SESSION_SECRET={secret}",
        f"ECLER_SESSION_HOURS={hours:g}",
        "",
    ]

    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a private temp file so the secret is never briefly world
        # readable, then move it into place atomically.
        fd, tmp_name = tempfile.mkstemp(dir=env_path.parent, prefix=".env-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.chmod(tmp_name, 0o640)
        if args.owner:
            entry = pwd.getpwnam(args.owner)
            os.chown(tmp_name, entry.pw_uid, entry.pw_gid)
        os.replace(tmp_name, env_path)
    except KeyError:
        print(f"✗ no such user: {args.owner}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"✗ cannot write {env_path}: {exc}", file=sys.stderr)
        return 1

    print(f"\n✓ wrote {env_path} (mode 640"
          f"{', owner ' + args.owner if args.owner else ''})")
    print(f"  account: {args.user}")
    print(f"  login lasts: {hours:g}h")
    if args.new_session_secret:
        print("  session secret rolled: existing logins are now invalid")
    print("\nApply it:  systemctl restart eclerstreamer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
