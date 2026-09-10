#!/usr/bin/env python3
"""Find live VEO devices on a subnet you own, and write a config skeleton.

Run this from a machine that can reach the VEO VLAN.  It only touches the
addresses you name, opens one TCP connection each to port 9999, and issues
read-only ``get_*`` commands.  It changes nothing on any device.

    # what is alive on the TV subnet?
    python3 tools/scan.py 10.0.2.0/24

    # same, but write a starting config.json, naming the transmitters you know
    python3 tools/scan.py 10.0.2.0/24 \
        --transmitter 10.0.2.101 --transmitter 10.0.2.102 \
        --write-config config.json

Why not ping: these units may not answer ICMP, and a device answering on port
9999 is the thing you actually care about.  A receiver powered off (or sitting
in a box without PoE) will not answer -- which is exactly how you tell the
in-service units from the spares without opening a cupboard.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclermanager import discovery, veo  # noqa: E402

# Re-exported so this module stays the single entry point for the CLI while the
# logic lives in the package, shared with the dashboard's own discovery.
parse_targets = discovery.parse_targets
port_open = discovery.port_open
is_veo = discovery.is_veo
classify = discovery.classify
read_with_retry = discovery.read_with_retry


def is_veo(status: veo.DeviceStatus) -> bool:
    """Whether this really is a VEO, rather than something else on port 9999.

    Matters because the TV VLAN also carries the televisions themselves, and
    anything may happen to listen on that port.  A VEO answers the protocol: it
    reports a Group ID, or a firmware/name/MAC through the same command set.
    A device that accepts the connection and says nothing recognisable is not
    one, and should not be counted as a missing receiver.
    """
    return any((
        status.group_id is not None,
        status.fw_version,
        status.mac_address,
        "VEO" in (status.device_name or "").upper(),
    ))


def classify(status: veo.DeviceStatus, transmitters: set[str]) -> str:
    """Transmitter, receiver, or not a VEO at all.

    An explicit --transmitter wins.  Otherwise use the model the device reports
    as its own name: VEO-XTI1C is the encoder, VEO-XRI1C the decoder.  Units
    whose name has been overwritten with something local report neither, and
    stay a guess.
    """
    if not is_veo(status):
        return "not a VEO?"
    if status.host in transmitters:
        return "transmitter"
    name = (status.device_name or "").upper()
    if "XTI" in name:
        return "transmitter"
    if "XRI" in name:
        return "receiver"
    return "receiver?"


def _fit(text: str, width: int) -> str:
    """Truncate so a long name cannot run into the next column."""
    return text if len(text) <= width else text[: width - 1] + "~"


def load_names(path: Path) -> dict[str, str]:
    """Read an inventory of ``<ip> <name>`` lines into {ip: name}.

    Accepts the shape people already keep this in -- whitespace- or
    comma-separated, blank lines and ``#`` comments ignored -- so a table pasted
    straight out of pfSense or a spreadsheet works without reformatting.
    """
    names: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").replace("\t", " ").split()
        if len(parts) < 2:
            continue
        try:
            address = str(ipaddress.ip_address(parts[0]))
        except ValueError:
            continue                      # not an inventory line; skip quietly
        names[address] = " ".join(parts[1:]).strip()
    return names


def slugify(label: str) -> str:
    """A short id safe for URLs and config keys."""
    import re
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "-", label).strip("-").lower()[:64]


def _fit(text: str, width: int) -> str:
    """Truncate so a long name cannot run into the next column."""
    return text if len(text) <= width else text[: width - 1] + "~"


def load_names(path: Path) -> dict[str, str]:
    """Read an inventory of ``<ip> <name>`` lines into {ip: name}.

    Accepts the shape people already keep this in -- whitespace- or
    comma-separated, blank lines and ``#`` comments ignored -- so a table pasted
    straight out of pfSense or a spreadsheet works without reformatting.
    """
    names: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").replace("\t", " ").split()
        if len(parts) < 2:
            continue
        try:
            address = str(ipaddress.ip_address(parts[0]))
        except ValueError:
            continue                      # not an inventory line; skip quietly
        names[address] = " ".join(parts[1:]).strip()
    return names


def slugify(label: str) -> str:
    """A short id safe for URLs and config keys."""
    import re
    return re.sub(r"[^A-Za-z0-9_.@:-]+", "-", label).strip("-").lower()[:64]


def workers_hint(concurrency: int, total: int) -> int:
    return max(1, min(concurrency, total))


def read_with_retry(host: str, *, port: int, timeout: float,
                    retries: int) -> veo.DeviceStatus:
    """Read one device, retrying a connected-but-mute unit.

    A unit that accepts the connection but returns nothing is usually still
    busy with a previous session, so a short pause and one more try is often
    all it needs.
    """
    status = veo.read_status(host, port=port, timeout=timeout, with_details=True)
    attempt = 0
    while attempt < retries and status.online and status.group_id is None:
        attempt += 1
        time.sleep(1.5)
        status = veo.read_status(host, port=port, timeout=timeout,
                                 with_details=True)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("targets", nargs="+",
                        help="CIDR (10.0.2.0/24), range (10.0.2.1-50), or addresses")
    parser.add_argument("--port", type=int, default=veo.DEFAULT_PORT)
    parser.add_argument("--connect-timeout", type=float, default=1.0,
                        help="TCP connect timeout for the sweep (default 1.0s)")
    parser.add_argument("--timeout", type=float, default=4.0,
                        help="timeout for reading device details (default 4.0s)")
    parser.add_argument("--concurrency", type=int, default=12,
                        help="parallel sessions (default 12; these are small "
                             "devices, one session each)")
    parser.add_argument("--prescan-over", type=int, default=512, metavar="N",
                        help="above this many addresses, sweep for open ports "
                             "first instead of opening a session per address "
                             "(default 512)")
    parser.add_argument("--prescan-concurrency", type=int, default=256,
                        help="concurrency for that port sweep (default 256)")
    parser.add_argument("--prescan-pause", type=float, default=3.0,
                        help="seconds to wait after the sweep before opening "
                             "real sessions (default 3.0)")
    parser.add_argument("--retries", type=int, default=1,
                        help="retries for a device that connects but says "
                             "nothing (default 1)")
    parser.add_argument("--transmitter", action="append", default=[], metavar="IP",
                        help="an IP you know is a transmitter (repeatable)")
    parser.add_argument("--config", metavar="FILE", default=None,
                        help="an existing config.json; devices that answer but "
                             "are not in it are flagged as NEW")
    parser.add_argument("--names", metavar="FILE", default=None,
                        help="inventory file of '<ip> <name>' lines, used to name "
                             "devices instead of guessing from the address")
    parser.add_argument("--write-config", metavar="PATH", default=None,
                        help="write a starting config to PATH (refuses to overwrite)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        hosts = parse_targets(args.targets)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    transmitters = set(args.transmitter)
    known: dict[str, str] = {}
    if args.config:
        try:
            import json as _json
            raw = _json.loads(Path(args.config).read_text(encoding="utf-8"))
            for entry in raw.get("receivers", []):
                known[str(entry.get("ip", "")).strip()] = str(
                    entry.get("name") or entry.get("id") or "?")
            for entry in raw.get("channels", []):
                if entry.get("transmitter_ip"):
                    known[str(entry["transmitter_ip"]).strip()] = (
                        f"tx ch{entry.get('group_id')}")
        except (OSError, ValueError) as exc:
            print(f"✗ cannot read {args.config}: {exc}", file=sys.stderr)
            return 2
        print(f"→ {len(known)} device(s) already in {args.config}",
              file=sys.stderr)
    try:
        names = load_names(Path(args.names)) if args.names else {}
    except OSError as exc:
        print(f"✗ cannot read {args.names}: {exc}", file=sys.stderr)
        return 2
    if names:
        print(f"→ loaded {len(names)} name(s) from {args.names}", file=sys.stderr)
    print(f"→ querying {len(hosts)} address(es) on port {args.port} …",
          file=sys.stderr)

    statuses = discovery.sweep(
        hosts,
        port=args.port,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        concurrency=args.concurrency,
        retries=args.retries,
        prescan_over=args.prescan_over,
        prescan_concurrency=args.prescan_concurrency,
        prescan_pause=args.prescan_pause,
        on_progress=lambda message: print(f"→ {message}", file=sys.stderr),
    )
    print(f"→ {len(statuses)} device(s) answering", file=sys.stderr)

    if not statuses:
        print("\nNothing answered on port 9999.\n"
              "  - Can this machine route to these addresses?\n"
              "  - Is the range right?\n"
              "  - Powered-off receivers (no PoE) will not answer, which is normal.",
              file=sys.stderr)
        return 1

    found = [
        {
            "ip": status.host,
            "role": classify(status, transmitters),
            "group_id": status.group_id,
            "video_lock": status.video_lock,
            "name": names.get(status.host) or known.get(status.host),
            "known": status.host in known if known else None,
            "mac": status.mac_address,
            "device_name": status.device_name,
            "fw_version": status.fw_version,
        }
        for status in statuses
    ]

    if args.json:
        print(json.dumps(found, indent=2))
    else:
        header_new = "  " if known else ""
        print(f"\n{header_new}{'IP':<16}{'role':<12}{'chan':<6}{'signal':<8}"
              f"{'name':<20}{'MAC':<19}firmware")
        print("-" * (84 + len(header_new) + 19))
        for entry in found:
            channel = "?" if entry["group_id"] is None else str(entry["group_id"])
            signal = {True: "yes", False: "NO", None: "?"}[entry["video_lock"]]
            label = _fit(entry["name"] or entry["device_name"] or "-", 19)
            flag = ""
            if known:
                flag = "  " if entry["known"] else "* "
            print(f"{flag}{entry['ip']:<16}{entry['role']:<12}{channel:<6}"
                  f"{signal:<8}{label:<20}{(entry['mac'] or '-'):<19}"
                  f"{entry['fw_version'] or '-'}")
        print(f"\n{len(found)} device(s) answering on port {args.port}.")

        receivers_by_channel: dict[int, list[str]] = {}
        for entry in found:
            if entry["role"] == "transmitter" or entry["group_id"] is None:
                continue
            receivers_by_channel.setdefault(entry["group_id"], []).append(entry["ip"])
        if receivers_by_channel:
            print("\nreceivers per channel:")
            transmitter_for = {
                e["group_id"]: e["ip"] for e in found
                if e["role"] == "transmitter" and e["group_id"] is not None
            }
            for channel in sorted(receivers_by_channel):
                ips = receivers_by_channel[channel]
                source = transmitter_for.get(channel)
                print(f"  channel {channel}: {len(ips):>2} TV(s)"
                      f"{'  from ' + source if source else '  (no transmitter seen!)'}")
                print(f"     {', '.join(ips)}")

        others = [e for e in found if e["role"] == "not a VEO?"]
        if others:
            print(f"\n{len(others)} device(s) answered on port {args.port} but "
                  "are not VEOs\n  (the TV VLAN carries the televisions too); "
                  "not counted as receivers:")
            print("    " + ", ".join(e["ip"] for e in others))

        if known:
            new = [e for e in found if not e["known"] and e["role"] != "not a VEO?"]
            # Only addresses that were actually probed.  Reporting a device
            # outside the scanned range as "did not answer" is misleading:
            # it was never asked.
            probed = set(hosts)
            missing = sorted(
                (set(known) & probed) - {e["ip"] for e in found},
                key=discovery.sort_key)
            unprobed = sorted(set(known) - probed, key=discovery.sort_key)
            print()
            if new:
                print(f"* {len(new)} device(s) answering but NOT in "
                      f"{args.config}:")
                for entry in new:
                    channel = "?" if entry["group_id"] is None else entry["group_id"]
                    print(f"    {entry['ip']:<16} channel {channel}"
                          f"   {entry['device_name'] or ''}")
            else:
                print(f"No unknown devices: everything answering is already in "
                      f"{args.config}.")
            if missing:
                print(f"\n{len(missing)} device(s) in the config did not answer "
                      "(spare, powered off, or moved):")
                print("    " + ", ".join(missing))
            if unprobed:
                print(f"\n{len(unprobed)} device(s) in the config are outside "
                      "the range scanned, so\n  they were not asked:")
                print("    " + ", ".join(unprobed))

        ouis = sorted({e["mac"][:8].upper() for e in found
                       if e["mac"] and len(e["mac"]) >= 8
                       and e["role"] != "not a VEO?"})
        if ouis:
            print(f"\nMAC prefixes seen: {', '.join(ouis)}")
            print("  Search your firewall's ARP table for these to find any VEO"
                  " device\n  anywhere on the network, including outside the "
                  "range scanned here.")

        reported = [(e["ip"], e["device_name"]) for e in found
                    if e["device_name"] and e["device_name"] != e["name"]]
        if reported:
            print("\nnames the devices report for themselves:")
            for ip, device_name in reported:
                print(f"  {ip:<16}{device_name}")
        unparsed = [e["ip"] for e in found
                    if e["group_id"] is None and e["role"] != "not a VEO?"]
        if unparsed:
            print(f"\n! Could not read a channel from: {', '.join(unparsed)}\n"
                  f"  Run: python3 tools/probe.py {unparsed[0]}   to see the raw replies.")
        if not transmitters:
            print("\nTip: pass --transmitter <ip> for each encoder you know about, and\n"
                  "     --write-config config.json, to get a ready-made starting config.")

    if args.write_config:
        sys.stdout.flush()      # keep the table ahead of anything on stderr
        return write_config(Path(args.write_config), found, transmitters)
    return 0


def write_config(path: Path, found: list[dict], transmitters: set[str]) -> int:
    """Emit a config skeleton from what the sweep saw."""
    if path.exists():
        print(f"\n✗ {path} already exists; not overwriting. "
              "Delete it or choose another path.", file=sys.stderr)
        return 1

    channels = []
    for entry in found:
        if entry["ip"] in transmitters and entry["group_id"] is not None:
            channels.append({
                "group_id": entry["group_id"],
                "name": entry["name"] or entry["device_name"]
                        or f"Channel {entry['group_id']}",
                "transmitter_ip": entry["ip"],
                "note": "rename me: what dashboard does this show?",
            })
    channels.sort(key=lambda c: c["group_id"])

    receivers = []
    for entry in found:
        if entry["ip"] in transmitters:
            continue
        last_octet = entry["ip"].rsplit(".", 1)[-1]
        label = entry["name"] or entry["device_name"] or f"TV {last_octet}"
        receivers.append({
            "id": slugify(label) or f"tv{last_octet}",
            "name": label,
            "ip": entry["ip"],
            "location": "",
            "expected_group_id": entry["group_id"],
        })

    payload = {
        "_comment": "Generated by tools/scan.py. Rename channels, set locations, "
                    "and check expected_group_id -- it was copied from whatever "
                    "each receiver happened to be showing during the scan.",
        "poll_interval_seconds": 30,
        "telnet_port": veo.DEFAULT_PORT,
        "timeout_seconds": 4.0,
        "auto_repair": False,
        "auto_nudge_on_signal_loss": False,
        "auto_repair_after_polls": 2,
        "channels": channels,
        "receivers": receivers,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ wrote {path}: {len(channels)} channel(s), {len(receivers)} receiver(s)")
    if not channels:
        print("  No channels: pass --transmitter <ip> so the encoders are recognised.")
    print("  Review it before starting the dashboard -- especially "
          "expected_group_id on any TV that was already wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
