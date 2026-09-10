#!/usr/bin/env python3
"""Passively learn which IP address a MAC is using on the local VLAN.

The complement to ``tools/scan.py``: that finds devices *by* IP address, this
finds the IP address *of* a device you only know by MAC.

Why it is needed: multicast delivery is layer 2. A receiver whose IP is wrong
for this subnet -- the factory default 192.168.1.12, a leftover from another
site, a duplicate -- still receives and displays its channel perfectly, because
the switch forwards the group to its port regardless of what IP it holds. An IP
scan of 10.0.x.x can never see such a unit, but it is plainly there in the
switch's MAC table.

Run it as root on a machine with a leg on the VLAN (the manager container has
one):

    python3 tools/sniffmac.py --interface eth1 --oui 00:1a:96

It listens; it sends nothing, so it cannot disturb the video. Whatever the
device says out loud -- an ARP request or reply, a DHCP discover, any IP packet
-- reveals its address.

**To make a silent device talk**, bounce its switch port. On boot it will ARP or
try DHCP within seconds:

    interface eth 1/1/33
    disable
    enable

Watch this tool at the same time. A device that sends a DHCP discover has no
static address; one that ARPs reveals the address it thinks it has.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

ETH_P_ALL = 0x0003
ETH_HEADER = 14
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
ETHERTYPE_VLAN = 0x8100


def format_mac(raw: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in raw)


def normalise_mac(text: str) -> str:
    """Accept 001a.96aa.bb04, 00:1a:96:aa:bb:04, 001A96AABB04 alike."""
    digits = "".join(c for c in text.lower() if c in "0123456789abcdef")
    return ":".join(digits[i:i + 2] for i in range(0, len(digits), 2))


def parse(frame: bytes) -> tuple[str, str, str] | None:
    """Return (mac, ip, how) for a frame that reveals a sender's address."""
    if len(frame) < ETH_HEADER:
        return None
    source = format_mac(frame[6:12])
    ethertype = struct.unpack("!H", frame[12:14])[0]
    offset = ETH_HEADER

    if ethertype == ETHERTYPE_VLAN:          # 802.1Q, if the leg is tagged
        if len(frame) < offset + 4:
            return None
        ethertype = struct.unpack("!H", frame[offset + 2:offset + 4])[0]
        offset += 4

    if ethertype == ETHERTYPE_ARP:
        if len(frame) < offset + 28:
            return None
        sender_ip = socket.inet_ntoa(frame[offset + 14:offset + 18])
        opcode = struct.unpack("!H", frame[offset + 6:offset + 8])[0]
        if sender_ip == "0.0.0.0":
            # A duplicate-address probe: it has not claimed an address yet.
            return (source, "0.0.0.0", "ARP probe")
        return (source, sender_ip, "ARP request" if opcode == 1 else "ARP reply")

    if ethertype == ETHERTYPE_IPV4:
        if len(frame) < offset + 20:
            return None
        source_ip = socket.inet_ntoa(frame[offset + 12:offset + 16])
        protocol = frame[offset + 9]
        how = {1: "ICMP", 2: "IGMP join", 6: "TCP", 17: "UDP"}.get(
            protocol, f"IP proto {protocol}")
        if source_ip == "0.0.0.0" and protocol == 17:
            return (source, "0.0.0.0", "DHCP discover (no static IP)")
        return (source, source_ip, how)
    return None


def main(argv: list[str] | None = None) -> int:
    # Redirected to a file, print() is block-buffered, so nothing reaches disk
    # until the process exits -- and if it is killed first, the log is empty
    # even though it ran.  Line buffering makes partial output survive.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interface", required=True,
                        help="interface on the VLAN, e.g. eth1")
    parser.add_argument("--mac", action="append", default=[],
                        help="only report this MAC (repeatable); any format")
    parser.add_argument("--oui", action="append", default=[],
                        help="only report MACs starting with this prefix, "
                             "e.g. 00:1a:96 (repeatable)")
    parser.add_argument("--seconds", type=float, default=180.0,
                        help="how long to listen (default 180)")
    parser.add_argument("--all", action="store_true",
                        help="report every MAC seen, not just new pairings")
    args = parser.parse_args(argv)

    wanted = {normalise_mac(m) for m in args.mac}
    prefixes = tuple(normalise_mac(o) for o in args.oui)

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                             socket.htons(ETH_P_ALL))
        sock.bind((args.interface, 0))
    except PermissionError:
        print("✗ needs root (raw socket access).\n"
              "  In a Proxmox container: pct exec <ctid> -- python3 ...",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"✗ cannot listen on {args.interface}: {exc}", file=sys.stderr)
        return 1

    scope = []
    if wanted:
        scope.append(f"{len(wanted)} MAC(s)")
    if prefixes:
        scope.append("prefix " + ", ".join(prefixes))
    print(f"→ listening on {args.interface} for {args.seconds:g}s"
          f"{' · ' + ' · '.join(scope) if scope else ' · everything'}")
    print("  Sending nothing. To make a silent device talk, bounce its switch")
    print("  port: interface eth 1/1/NN / disable / enable")
    print()

    seen: dict[str, dict[str, str]] = {}
    deadline = time.monotonic() + args.seconds
    sock.settimeout(1.0)

    try:
        while time.monotonic() < deadline:
            try:
                frame = sock.recv(65535)
            except TimeoutError:
                continue
            found = parse(frame)
            if not found:
                continue
            mac, ip, how = found
            if wanted and mac not in wanted:
                continue
            if prefixes and not mac.startswith(prefixes):
                continue
            addresses = seen.setdefault(mac, {})
            if ip in addresses and not args.all:
                continue
            addresses[ip] = how
            stamp = time.strftime("%H:%M:%S")
            print(f"  {stamp}  {mac}  {ip:<15}  {how}")
    except KeyboardInterrupt:
        print("\nstopped.")

    print()
    if not seen:
        print("Nothing heard. Either the device is silent -- bounce its switch")
        print("port to make it speak -- or this interface is not on its VLAN.")
        return 1

    print(f"{len(seen)} MAC(s) heard:")
    for mac in sorted(seen):
        addresses = ", ".join(sorted(seen[mac]))
        print(f"  {mac}  ->  {addresses}")
    off_subnet = {mac: addrs for mac, addrs in seen.items()
                  if any(not a.startswith("10.0.") and a != "0.0.0.0"
                         for a in addrs)}
    if off_subnet:
        print("\n! These are not on 10.0.x.x, so an IP scan of that range")
        print("  cannot see them -- yet multicast still reaches them, because")
        print("  it is delivered at layer 2:")
        for mac, addrs in off_subnet.items():
            print(f"    {mac}  ->  {', '.join(sorted(addrs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
