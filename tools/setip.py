#!/usr/bin/env python3
"""Set a VEO device's static IP address over the control port.

The command format, read off a real unit's own usage message:

    set_static_ip ip [ip(n.n.n.n)] netmask [ip(n.n.n.n)] gateway [ip(n.n.n.n)]

Example -- putting a receiver that reverted to the factory default back where
it belongs:

    python3 tools/setip.py 192.168.1.12 \\
        --ip 10.0.2.32 --netmask 255.255.0.0 --gateway 10.0.0.1

It reads the current configuration first, shows exactly what will change, and
asks before writing. Line endings are handled automatically -- some units
reject the documented CRLF and need LF alone.

**A device usually needs a reboot for a new address to take effect**, and on
this platform a setting can fail to persist: 10.0.2.32 was found to have lost
its static address across a power cycle and reverted to 192.168.1.12. So
``--reboot`` is offered, and afterwards the address should be verified at its
new home rather than assumed:

    python3 tools/scan.py 10.0.2.32
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclermanager import veo  # noqa: E402


def valid(address: str, label: str) -> str:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise SystemExit(f"✗ {label} {address!r} is not an IP address")
    if parsed.version != 4:
        raise SystemExit(f"✗ {label} must be IPv4")
    return str(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="the device's current address")
    parser.add_argument("--ip", required=True, help="the address to give it")
    parser.add_argument("--netmask", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--port", type=int, default=veo.DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--line-ending", choices=["auto", "lf", "crlf"],
                        default="auto",
                        help="force a line ending. 'auto' tries the learned "
                             "one and falls back if the change does not take")
    parser.add_argument("--reboot", action="store_true",
                        help="reboot afterwards so the address takes effect")
    parser.add_argument("--wait", type=float, default=90.0,
                        help="seconds to wait for the new address to answer "
                             "after a reboot (default 90)")
    parser.add_argument("--yes", action="store_true", help="skip the prompt")
    args = parser.parse_args(argv)

    new_ip = valid(args.ip, "--ip")
    netmask = valid(args.netmask, "--netmask")
    gateway = valid(args.gateway, "--gateway")

    try:
        network = ipaddress.ip_network(f"{new_ip}/{netmask}", strict=False)
    except ValueError as exc:
        raise SystemExit(f"✗ {new_ip}/{netmask} is not a valid network: {exc}")
    if ipaddress.ip_address(gateway) not in network:
        print(f"! {gateway} is outside {network}, so the device would have no "
              "usable gateway.", file=sys.stderr)
        if not args.yes:
            raise SystemExit("  Refusing. Pass --yes to do it anyway.")

    print(f"→ reading {args.host}")
    status = veo.read_status(args.host, port=args.port, timeout=args.timeout,
                             with_details=True)
    if not status.online:
        raise SystemExit(f"✗ cannot reach {args.host}: {status.error}")
    learned = "LF" if veo.line_ending_for(args.host) == veo.LF else "CRLF"
    print(f"  name    : {status.device_name or '?'}")
    print(f"  channel : {status.group_id if status.group_id is not None else '?'}")
    print(f"  firmware: {status.fw_version or '?'}")
    print(f"  MAC     : {status.mac_address or '?'}")
    print(f"  DHCP    : {status.dhcp if status.dhcp is not None else '?'}")
    print(f"  protocol: answered over {learned} when read")

    if status.dhcp is True:
        print("\n! DHCP is enabled. A static address set now will be lost the "
              "next time\n  the device boots and finds a DHCP server -- or "
              "reverts if it does not.\n  Turn it off first: set_dhcp 0")

    command = (f"set_static_ip ip {new_ip} netmask {netmask} "
               f"gateway {gateway}")
    if args.line_ending == "lf":
        endings = [veo.LF]
    elif args.line_ending == "crlf":
        endings = [veo.CRLF]
    else:
        learned = veo.line_ending_for(args.host)
        endings = [learned] + [e for e in veo.LINE_ENDINGS if e != learned]

    print(f"\nabout to send:\n  {command}\n")
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted.")
            return 1

    applied = False
    accepted = False
    for attempt, ending in enumerate(endings):
        label = "LF" if ending == veo.LF else "CRLF"
        print(f"\n→ sending over {label}")
        try:
            with veo.VeoSession(args.host, args.port, timeout=args.timeout,
                                line_ending=ending) as session:
                reply = session.command(command)
                print(f"  reply: {reply.strip() or '(nothing)'}")
                if veo.looks_unsupported(reply, command):
                    print(f"  ✗ rejected over {label}")
                    continue
                accepted = True
                back = session.command("get_ip_config")
                print("  get_ip_config now says:\n"
                      + "\n".join("    " + line
                                   for line in back.strip().splitlines()))
                if new_ip in back:
                    print(f"\n✓ applied live: the device reports {new_ip}")
                    applied = True
                    veo.remember_line_ending(args.host, ending)
                    break
                # Not a failure on its own.  On this firmware get_ip_config
                # reports the *running* configuration, so a stored change is
                # invisible until the device restarts.
                print(f"\n  accepted over {label}, but it still reports the "
                      "old address.\n  On this firmware get_ip_config shows "
                      "the running config, so a stored\n  change only appears "
                      "after a reboot.")
                break
        except veo.VeoError as exc:
            print(f"  ✗ {exc}")

    if not accepted:
        print("\n✗ the device did not accept the command over either line "
              "ending.\n  Nothing was changed. Use the web UI instead: "
              f"http://{args.host}/ (admin/admin)", file=sys.stderr)
        return 1

    if not args.reboot:
        print("\nNot rebooted, so a stored change has not taken effect yet.\n"
              "  Re-run with --reboot, or power-cycle the device.")
        return 0

    print("\n→ rebooting")
    try:
        with veo.VeoSession(args.host, args.port, timeout=args.timeout,
                            line_ending=endings[0]) as session:
            session.command("reboot", budget=3.0)
    except veo.VeoError as exc:
        print(f"  ! reboot command may not have landed: {exc}")

    # The only real verification: does it answer at the new address?
    print(f"\n→ waiting for {new_ip} to come up (up to {args.wait:g}s)")
    import time as _time
    deadline = _time.monotonic() + args.wait
    while _time.monotonic() < deadline:
        _time.sleep(5)
        check = veo.read_status(new_ip, port=args.port, timeout=3.0)
        if check.online:
            print(f"\n✓ {new_ip} is answering. Channel "
                  f"{check.group_id if check.group_id is not None else '?'}.")
            print("  The address stuck. The manager will pick it up on its "
                  "next poll.")
            return 0
        print("  not yet …")

    print(f"\n✗ {new_ip} did not come up within {args.wait:g}s.")
    print(f"  Check whether it went back to {args.host}:")
    print(f"    python3 {Path(__file__).resolve().parent}/scan.py {args.host}")
    print("  If it did, this firmware is not storing the setting and the web "
          "UI is the\n  way to do it: http://" + args.host + "/ (admin/admin)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
