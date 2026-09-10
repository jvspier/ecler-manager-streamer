#!/usr/bin/env python3
"""Find a device's IP by ARP, when you know its MAC but not its subnet.

The last resort in a specific situation: a receiver that displays its channel
perfectly but never initiates any traffic. Such a unit is invisible to
``tools/scan.py`` (which needs a reachable IP) and to ``tools/sniffmac.py``
(which needs the device to say something unprompted -- and its only unprompted
traffic is IGMP, which snooping switches absorb before it reaches us).

ARP gets around both problems: a device answers an ARP request for its own
address even if it never speaks otherwise, and ARP is link-local, so this works
regardless of what subnet the address belongs to or what addresses this machine
holds.

    python3 tools/arpscan.py --interface eth1 --range 192.168.1.0/24 \\
        --mac 00:1a:96:aa:bb:01

Run as root, on a machine with a leg on the device's VLAN.

**Keep ranges to a /24 at a time.** Each probe is a broadcast frame, and a wide
fast sweep is an ARP flood -- on 2026-09-09 exactly that knocked every receiver
in this building off its multicast stream for 35 seconds. The rate here is
capped and paced for that reason.
"""

from __future__ import annotations

import argparse
import ipaddress
import socket
import struct
import sys
import threading
import time

ETH_P_ALL = 0x0003
ETH_P_ARP = 0x0806
ARP_REQUEST = 1
ARP_REPLY = 2
BROADCAST = b"\xff" * 6

#: Frames per second. Gentle on purpose: see the module docstring.
DEFAULT_RATE = 120.0
#: Refuse a sweep wider than this without --force.
MAX_ADDRESSES = 4096


def normalise_mac(text: str) -> str:
    digits = "".join(c for c in text.lower() if c in "0123456789abcdef")
    return ":".join(digits[i:i + 2] for i in range(0, len(digits), 2))


def mac_bytes(text: str) -> bytes:
    return bytes.fromhex("".join(c for c in text if c in "0123456789abcdefABCDEF"))


def build_request(src_mac: bytes, src_ip: str, target_ip: str) -> bytes:
    ethernet = BROADCAST + src_mac + struct.pack("!H", ETH_P_ARP)
    arp = (struct.pack("!HHBBH", 1, 0x0800, 6, 4, ARP_REQUEST)
           + src_mac + socket.inet_aton(src_ip)
           + b"\x00" * 6 + socket.inet_aton(target_ip))
    return ethernet + arp


def parse_reply(frame: bytes) -> tuple[str, str] | None:
    """Return (mac, ip) from an ARP reply, else None."""
    if len(frame) < 42:
        return None
    if struct.unpack("!H", frame[12:14])[0] != ETH_P_ARP:
        return None
    opcode = struct.unpack("!H", frame[20:22])[0]
    if opcode != ARP_REPLY:
        return None
    sender_mac = ":".join(f"{b:02x}" for b in frame[22:28])
    sender_ip = socket.inet_ntoa(frame[28:32])
    return (sender_mac, sender_ip)


def interface_mac(name: str) -> bytes:
    """Read an interface's hardware address without needing extra libraries."""
    with open(f"/sys/class/net/{name}/address", encoding="ascii") as handle:
        return mac_bytes(handle.read().strip())


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--range", action="append", required=True, dest="ranges",
                        help="CIDR or a.b.c.d-e to probe (repeatable)")
    parser.add_argument("--mac", action="append", default=[],
                        help="stop as soon as this MAC answers (repeatable)")
    parser.add_argument("--source-ip", default="0.0.0.0",
                        help="sender address in the probes; 0.0.0.0 (default) "
                             "is an ARP probe, which most stacks answer and "
                             "which claims no address of its own")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE,
                        help=f"probes per second (default {DEFAULT_RATE:g})")
    parser.add_argument("--settle", type=float, default=3.0,
                        help="seconds to keep listening after the last probe")
    parser.add_argument("--force", action="store_true",
                        help=f"allow more than {MAX_ADDRESSES} addresses")
    args = parser.parse_args(argv)

    targets: list[str] = []
    for spec in args.ranges:
        spec = spec.strip()
        if "/" in spec:
            targets += [str(h) for h in
                        ipaddress.ip_network(spec, strict=False).hosts()]
        elif "-" in spec:
            head, _, tail = spec.rpartition(".")
            first, _, last = tail.partition("-")
            targets += [f"{head}.{n}" for n in range(int(first), int(last) + 1)]
        else:
            targets.append(str(ipaddress.ip_address(spec)))

    if len(targets) > MAX_ADDRESSES and not args.force:
        print(f"✗ {len(targets)} addresses is too wide. Each probe is a "
              "broadcast frame, and\n"
              "  a fast wide sweep is an ARP flood -- that is what disturbed "
              "the receivers\n  on 2026-09-09. Probe a /24 at a time, or pass "
              "--force.", file=sys.stderr)
        return 2

    wanted = {normalise_mac(m) for m in args.mac}

    try:
        src_mac = interface_mac(args.interface)
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                             socket.htons(ETH_P_ALL))
        sock.bind((args.interface, 0))
    except PermissionError:
        print("✗ needs root (raw socket access).", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"✗ cannot use {args.interface}: {exc}", file=sys.stderr)
        return 1

    found: dict[str, str] = {}
    stop = threading.Event()

    def listen() -> None:
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                frame = sock.recv(65535)
            except (TimeoutError, OSError):
                continue
            reply = parse_reply(frame)
            if not reply:
                continue
            mac, ip = reply
            if mac in found:
                continue
            found[mac] = ip
            marker = "  <-- looking for this" if mac in wanted else ""
            print(f"  {mac}  {ip:<15}{marker}")
            if wanted and wanted <= set(found):
                stop.set()

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()

    print(f"→ probing {len(targets)} address(es) on {args.interface} at "
          f"{args.rate:g}/s (~{len(targets) / args.rate:.0f}s)")
    if wanted:
        print(f"  stopping early if {', '.join(sorted(wanted))} answers")
    print()

    interval = 1.0 / args.rate if args.rate > 0 else 0
    try:
        for target in targets:
            if stop.is_set():
                break
            try:
                sock.send(build_request(src_mac, args.source_ip, target))
            except OSError as exc:
                print(f"✗ send failed: {exc}", file=sys.stderr)
                break
            if interval:
                time.sleep(interval)
        if not stop.is_set():
            time.sleep(args.settle)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        stop.set()
        listener.join(timeout=2)
        sock.close()

    print()
    if not found:
        print("No ARP replies. Either nothing in that range exists on this "
              "VLAN, or the\ndevice ignores probes from 0.0.0.0 -- retry with "
              "--source-ip set to an\naddress of this machine on that subnet.")
        return 1

    print(f"{len(found)} device(s) answered:")
    for mac in sorted(found):
        marker = "   <-- the one you were looking for" if mac in wanted else ""
        print(f"  {mac}  ->  {found[mac]}{marker}")
    missing = wanted - set(found)
    if missing:
        print(f"\nStill not found: {', '.join(sorted(missing))}")
        print("Try another range; the address could be in any private subnet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
