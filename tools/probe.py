#!/usr/bin/env python3
"""Diagnose one VEO device: what ports it has, and exactly what bytes it sends.

    python3 tools/probe.py 10.0.2.1

Read-only.  Use --set-channel to test a write on a device you can see.

When a device answers on port 9999 but tells you nothing, run this against a
unit that *works* and one that does not, and compare.  It reports:

  1. which ports are open (9999 control, 80 web UI, 23 telnet)
  2. the connect banner, waited for generously
  3. every read-only command, shown as a raw repr so an empty reply is
     distinguishable from whitespace
  4. the same query with LF and CR line endings, if CRLF produced nothing
  5. the web UI on port 80, which is an independent way to tell a real VEO
     from something else answering on that port
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclermanager import veo  # noqa: E402

PORTS = ((9999, "control (set_group_id / get_group_id)"),
         (80, "web UI"),
         (23, "telnet"))

READ_COMMANDS = ("list", "get_group_id", "get_video_lock", "get_device_name",
                 "get_fw_version", "get_lan_status", "get_ip_config")


def port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def show(label: str, payload: str) -> None:
    print(f"\n--- {label} " + "-" * max(0, 58 - len(label)))
    if payload == "":
        print("  (nothing at all - device sent zero bytes)")
    elif not payload.strip():
        print(f"  (whitespace only) {payload!r}")
    else:
        for line in payload.strip().splitlines():
            print(f"  {line}")
        print(f"  raw: {payload!r}")


def raw_session(host: str, port: int, timeout: float, line_ending: bytes,
                commands: tuple[str, ...]) -> dict[str, str]:
    """One session, driven at the byte level so line endings can be varied."""
    out: dict[str, str] = {}
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        out["<banner>"] = _read(sock, timeout, quiet=0.4)
        for command in commands:
            try:
                sock.sendall(command.encode("ascii") + line_ending)
            except OSError as exc:
                # Keep this out of `out`: it is our error text, not device
                # output, and parsing it would invent a channel number from
                # the errno.
                print(f"\n--- {command} " + "-" * max(0, 58 - len(command)))
                print(f"  ✗ write failed: {exc} (device closed the session)")
                break
            out[command] = _read(sock, timeout, quiet=0.4)
    return out


def _read(sock: socket.socket, first_byte: float, *, quiet: float) -> str:
    import time
    buf = bytearray()
    deadline = time.monotonic() + first_byte + 3.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(min(quiet if buf else first_byte, remaining))
        try:
            chunk = sock.recv(4096)
        except (TimeoutError, OSError):
            break
        if not chunk:
            break
        buf.extend(chunk)
    return veo.strip_telnet_negotiation(bytes(buf)).decode("utf-8", "replace")


def check_http(host: str, timeout: float) -> None:
    """Fetch the web UI; an Ecler serves a recognisable page here."""
    url = f"http://{host}/"
    request = urllib.request.Request(url)
    token = base64.b64encode(b"admin:admin").decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, body = response.status, response.read(4000)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(4000)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"  port 80 did not serve HTTP: {exc}")
        return
    text = body.decode("utf-8", "replace")
    print(f"  HTTP {status}, {len(body)} bytes")
    snippet = " ".join(text.split())[:300]
    print(f"  {snippet or '(empty body)'}")
    lowered = text.lower()
    for marker in ("veo", "ecler", "group", "iptv", "astparam", "channel"):
        if marker in lowered:
            print(f"  ! contains '{marker}' - looks like a real VEO web UI")
            break

    # Multicast details, if the page shows them.  On a transmitter this is the
    # group its stream goes to -- the fact you need to know whether a software
    # encoder could produce the same stream.
    groups = sorted(set(re.findall(r"\b(2(?:2[4-9]|3[0-9])\.\d{1,3}\.\d{1,3}\.\d{1,3})\b",
                                   text)))
    ports = sorted(set(re.findall(r"(?:port|Port)\D{0,12}(\d{2,5})", text)))
    if groups:
        print(f"  multicast group(s) on the page: {', '.join(groups)}")
    if ports:
        print(f"  port-ish numbers on the page:   {', '.join(ports[:6])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=veo.DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="how long to wait for a reply to start (default 8s)")
    parser.add_argument("--command", action="append", default=[],
                        help="extra command to try (repeatable)")
    parser.add_argument("--set-channel", type=int, default=None, metavar="N",
                        help="also test set_group_id N (changes what the TV shows)")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    print(f"=== {args.host} " + "=" * max(0, 60 - len(args.host)))

    print("\n[1] open ports")
    open_ports = {}
    for port, description in PORTS:
        is_open = port_open(args.host, port, min(args.timeout, 3.0))
        open_ports[port] = is_open
        print(f"  {port:<6} {'OPEN  ' if is_open else 'closed'}  {description}")
    if not open_ports.get(args.port):
        print(f"\n✗ port {args.port} is not accepting connections; nothing more to try.")
        return 1

    commands = READ_COMMANDS + tuple(args.command)

    print(f"\n[2] control session on {args.port}, CRLF line endings, "
          f"waiting up to {args.timeout:g}s for a reply to start")
    try:
        replies = raw_session(args.host, args.port, args.timeout, b"\r\n", commands)
    except OSError as exc:
        print(f"  ✗ session failed: {exc}")
        return 1
    for label, payload in replies.items():
        show(label, payload)

    # The banner does not count: a device can greet you on connect and then
    # close the session on the first CRLF command, which is exactly the case
    # the fallback exists for.
    said_nothing = all(not payload.strip()
                       for label, payload in replies.items()
                       if label != "<banner>")
    if said_nothing:
        print("\n[3] CRLF produced nothing; retrying with other line endings")
        for name, ending in (("LF only", b"\n"), ("CR only", b"\r")):
            print(f"\n  ~~~ {name} ~~~")
            try:
                # The full command list, not just one probe: if CRLF failed,
                # everything the caller asked for still needs running.
                alt = raw_session(args.host, args.port, args.timeout, ending,
                                  commands)
            except OSError as exc:
                print(f"  ✗ {exc}")
                continue
            for label, payload in alt.items():
                show(f"{name}: {label}", payload)
            if any(v.strip() for k, v in alt.items() if k != "<banner>"):
                # This ending works; no need to try the next one, and the
                # summary below should be based on these replies.
                replies = alt
                print(f"\n  -> this device needs {name} line endings")
                break
    else:
        print("\n[3] skipped (device is talking on CRLF)")

    if args.skip_http:
        print("\n[4] skipped (--skip-http)"
              + ("; port 80 is open" if open_ports.get(80) else ""))
    elif open_ports.get(80):
        print("\n[4] web UI on port 80 (admin/admin)")
        check_http(args.host, min(args.timeout, 6.0))
    else:
        print("\n[4] port 80 closed - no web UI to compare against.")
        if said_nothing:
            print("  A device silent on 9999 AND with no web server is unlikely to")
            print("  be a VEO at all; something else may be answering that port.")

    if args.set_channel is not None:
        result = veo.set_group_id(args.host, args.set_channel, port=args.port,
                                  timeout=args.timeout)
        print(f"\n[5] set_group_id {args.set_channel}: "
              f"{'OK' if result.ok else 'FAILED'} - {result.message}")

    print("\n" + "=" * 68)
    group_id = veo.parse_int(replies.get("get_group_id", ""), "get_group_id",
                             veo.GROUP_ID_MIN, veo.GROUP_ID_MAX)
    lock = veo.parse_bool(replies.get("get_video_lock", ""), "get_video_lock")
    print(f"parsed channel    : {group_id if group_id is not None else 'COULD NOT PARSE'}")
    print(f"parsed video lock : {lock if lock is not None else 'COULD NOT PARSE'}")

    if args.json:
        print(json.dumps({"ports": open_ports, "replies": replies}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
