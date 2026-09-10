#!/usr/bin/env python3
"""Set the name a VEO device reports for itself.

Distinct from renaming in the dashboard: that changes the label in
``config.json``, which is what you read day to day. This writes the name into
the device, which is what appears in its own web page, its OSD status screen,
and in ``tools/scan.py`` output under "names the devices report for
themselves". Worth setting after a factory reset, which resets it to the model
name.

    python3 tools/setname.py 10.0.2.11 --name TV_OFFICE_11

Reads the current name, writes, and reads back to confirm -- these units
acknowledge writes they have not applied, so the read-back is the only
evidence. Line endings are handled automatically; some units reject the
documented CRLF.

**No spaces.** ``set_device_name`` stops at the first whitespace, so
"TV RECEIVER 11" stores as "TV". Use underscores. (The storage itself accepts
spaces -- some units here are called things like "Recieve 01" -- so those were
presumably set through the web UI, which submits the whole field.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclermanager import veo  # noqa: E402

#: Longest name observed on a working unit is well inside this; the firmware
#: may silently truncate, which the read-back will reveal.
SANE_LENGTH = 32


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("--name", required=True,
                        help="the name to store on the device")
    parser.add_argument("--port", type=int, default=veo.DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    name = args.name.strip()
    if not name:
        print("✗ --name may not be empty", file=sys.stderr)
        return 2
    if "\n" in name or "\r" in name:
        print("✗ --name may not contain newlines", file=sys.stderr)
        return 2
    if len(name) > SANE_LENGTH:
        print(f"✗ --name is longer than {SANE_LENGTH} characters; the device "
              "will very likely truncate it", file=sys.stderr)
        return 2
    if name.split()[0] != name:
        suggestion = "_".join(name.split())
        print(f"✗ set_device_name stops at the first space, so {name!r} would "
              f"store as {name.split()[0]!r}.\n"
              f"  Use underscores instead:  --name {suggestion}\n"
              "  (The web UI can store spaces; this command cannot.)",
              file=sys.stderr)
        return 2

    print(f"→ reading {args.host}")
    status = veo.read_status(args.host, port=args.port, timeout=args.timeout,
                             with_details=True)
    if not status.online:
        print(f"✗ cannot reach {args.host}: {status.error}", file=sys.stderr)
        return 1
    print(f"  current name: {status.device_name or '(none)'}")
    print(f"  channel     : "
          f"{status.group_id if status.group_id is not None else '?'}")
    if status.device_name == name:
        print(f"\n✓ already called {name!r}; nothing to do")
        return 0

    print(f"\nabout to set the device's own name to: {name!r}")
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted.")
            return 1

    command = f"set_device_name {name}"
    try:
        with veo.VeoSession(args.host, args.port, timeout=args.timeout) as session:
            reply = session.command(command)
            print(f"\nreply: {reply.strip() or '(nothing)'}")
            if veo.looks_unsupported(reply, command):
                print("✗ the device rejected it. Its usage message, if any, is "
                      "above.", file=sys.stderr)
                return 1
            back = session.command("get_device_name")
            stored = veo.parse_text(back, "get_device_name")
    except veo.VeoError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    if stored == name:
        print(f"\n✓ the device now reports {stored!r}")
        return 0
    if stored:
        print(f"\n! the device reports {stored!r}, not {name!r}.")
        if stored == name.split()[0]:
            print("  It kept only the first word: this command stops at "
                  "whitespace.\n"
                  f"  Try:  --name {'_'.join(name.split())}")
        elif name.startswith(stored):
            print(f"  It truncated the name at {len(stored)} characters. "
                  "Use a shorter one.")
        return 1
    print("\n! the device did not report a name back, so this is unconfirmed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
