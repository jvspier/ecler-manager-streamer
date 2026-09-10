#!/usr/bin/env python3
"""Compare a switch's MAC-address table against the receivers the manager knows.

Doing this by eye across a dozen switches is slow and easy to get wrong. Paste
the switch output in and this says which MACs are known receivers and which are
not -- the ones not known are receivers the manager has never seen, typically
because they hold an address outside the scanned range (multicast is delivered
at layer 2, so an off-subnet receiver still shows its channel perfectly).

    # on the switch
    show mac-address ethernet 1/1/33
    ...

    # then, with that output in a file
    python3 tools/macdiff.py --url http://10.0.5.20:8477 \\
        --user tvadmin switchdump.txt

Reads the known MACs from a running manager, or from a saved
``/api/state`` JSON with --state. Accepts any MAC notation and ignores
everything in the input that is not a MAC line, so a whole terminal session can
be pasted in unedited.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAC_RE = re.compile(
    r"\b((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}"          # 00:1a:96:aa:bb:04
    r"|(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}"              # 001a.96aa.bb04
    r"|[0-9a-fA-F]{12})\b"                                  # 001a96aabb04
)
PORT_RE = re.compile(r"\b(\d+/\d+/\d+|[Gg]i\d\S*|[Ee]th\S*\d)\b")


def normalise(text: str) -> str:
    digits = "".join(c for c in text.lower() if c in "0123456789abcdef")
    return ":".join(digits[i:i + 2] for i in range(0, len(digits), 2))


def parse_switch_output(text: str) -> list[tuple[str, str]]:
    """Pull (mac, port) pairs out of pasted switch output.

    Handles both a per-port query, where the port appears in the command line
    above the table, and a full table where each row carries its own port.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    current_port = ""
    for line in text.splitlines():
        # "show mac-address eth 1/1/33" -- remember the port being queried
        if "mac-address" in line.lower() or "mac address-table" in line.lower():
            port = PORT_RE.search(line)
            current_port = port.group(1) if port else ""
            continue
        found = MAC_RE.search(line)
        if not found:
            continue
        mac = normalise(found.group(1))
        if len(mac) != 17:
            continue
        rest = line[found.end():]
        port = PORT_RE.search(rest) or PORT_RE.search(line[:found.start()])
        where = port.group(1) if port else current_port
        key = f"{mac}@{where}"
        if key in seen:
            continue
        seen.add(key)
        pairs.append((mac, where))
    return pairs


def fetch_state(url: str, user: str | None, password: str | None) -> dict:
    request = urllib.request.Request(url.rstrip("/") + "/api/state")
    if user:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit("✗ login required: pass --user (and --password)")
        raise SystemExit(f"✗ {url}: HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SystemExit(f"✗ cannot reach {url}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*",
                        help="files of switch output; omit to read stdin")
    parser.add_argument("--url", default="http://127.0.0.1:8477",
                        help="the manager (default http://127.0.0.1:8477)")
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None,
                        help="omit to be prompted when --user is given")
    parser.add_argument("--state", metavar="FILE", default=None,
                        help="read known MACs from a saved /api/state instead")
    args = parser.parse_args(argv)

    text = ""
    if args.files:
        for name in args.files:
            text += Path(name).read_text(encoding="utf-8", errors="replace")
    else:
        if sys.stdin.isatty():
            print("→ paste the switch output, then Ctrl-D", file=sys.stderr)
        text = sys.stdin.read()

    pairs = parse_switch_output(text)
    if not pairs:
        print("✗ no MAC addresses found in the input.", file=sys.stderr)
        return 2

    if args.state:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    else:
        password = args.password
        if args.user and password is None:
            password = getpass.getpass(f"Password for {args.user}: ")
        state = fetch_state(args.url, args.user, password)

    known: dict[str, dict] = {}
    for device in state.get("devices", []):
        mac = device.get("mac_address")
        if mac:
            known[normalise(mac)] = device
    unread = [d for d in state.get("devices", []) if not d.get("mac_address")]

    print(f"\n{len(pairs)} MAC(s) in the input, "
          f"{len(known)} receiver MAC(s) known to the manager\n")
    print(f"{'MAC':<19}{'port':<12}{'status':<11}receiver")
    print("-" * 74)
    unknown: list[tuple[str, str]] = []
    for mac, port in pairs:
        device = known.get(mac)
        if device:
            channel = device.get("group_id")
            detail = (f"{device.get('name')}  {device.get('ip')}"
                      f"  channel {channel if channel is not None else '?'}")
            print(f"{mac:<19}{port:<12}{'known':<11}{detail}")
        else:
            unknown.append((mac, port))
            print(f"{mac:<19}{port:<12}{'UNKNOWN':<11}-")

    if unknown:
        print(f"\n{len(unknown)} MAC(s) not known to the manager:")
        for mac, port in unknown:
            print(f"  {mac}   port {port or '?'}")
        print("\nIf the OUI matches your other receivers, these are Eclers the")
        print("manager has never seen -- most likely holding an address outside")
        print("the scanned range. Multicast is delivered at layer 2, so such a")
        print("unit displays its channel perfectly while being invisible to an")
        print("IP scan. To find its address:")
        print("  tools/sniffmac.py --interface eth1 --mac " + unknown[0][0])
        print("  (then bounce its switch port to make it speak)")
    else:
        print("\nEvery MAC in the input is a receiver the manager knows.")

    if unread:
        print(f"\nNote: {len(unread)} receiver(s) have not reported a MAC yet "
              "(offline, or not\n  polled since the manager started), so a "
              "match may be missed:")
        print("  " + ", ".join(f"{d['name']} ({d['ip']})" for d in unread[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
