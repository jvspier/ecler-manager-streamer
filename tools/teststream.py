#!/usr/bin/env python3
"""Stream to a VEO multicast group and see whether a receiver locks onto it.

This answers the one question the "retire the ChromeBoxes" idea depends on:
will a VEO-XRI1C decode a stream that ffmpeg produced, rather than one a
VEO-XTI1C encoded?

Run it from something on the TV VLAN.  The manager container already has a leg
there, so the quickest path is:

    apt-get install -y ffmpeg          # in the container
    python3 tools/teststream.py --channel 5

Then set one TV to channel 5 from the dashboard and look at it.  One click on
that card's starred channel puts it back.

**Addresses.** ``--channel N`` looks the address up in the config, or derives
it from the pattern confirmed on this network, ``239.255.42.(42+N)`` -- channel
1 is `.43`, channel 2 is `.44`.  It refuses a channel that a real transmitter
serves, so a test cannot collide with a live dashboard.  ``--group`` takes an
address directly if you would rather be explicit.

**If the receiver stays black**, work through the variants in order; each tests
a different guess about what the decoder wants:

    --variant ts     raw MPEG-TS over UDP   (default; what the manual implies)
    --variant rtp    RTP over UDP           (the manual mentions both)
    --audio          add a silent audio track (some decoders need one present)
    --profile baseline   simpler H.264 than the default main

Nothing here touches a transmitter or the manager.  It only sends packets to a
multicast group.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 7 * 188: MPEG-TS packets are 188 bytes, and seven of them fit one Ethernet
# frame.  ffmpeg's default UDP payload of 1472 is not a multiple of 188, which
# is exactly the kind of thing a cheap decoder refuses to parse.
TS_PKT_SIZE = 1316


#: Confirmed on two switches by cross-checking against known receivers:
#: channel 1 -> 239.255.42.43, channel 2 -> 239.255.42.44.
GROUP_BASE = "239.255.42."
GROUP_OFFSET = 42


def group_for_channel(channel: int, config_path: str | None) -> str | None:
    """The multicast address for a Group ID.

    Prefers an address recorded in the config -- read off a transmitter's own
    web page -- and otherwise derives it from the observed pattern.  Refuses a
    channel a transmitter is already using, since streaming to it would collide
    with a live dashboard.
    """
    from eclermanager import veo

    if not veo.GROUP_ID_MIN <= channel <= veo.GROUP_ID_MAX:
        print(f"✗ channel {channel} is outside "
              f"{veo.GROUP_ID_MIN}..{veo.GROUP_ID_MAX}", file=sys.stderr)
        return None

    in_use: dict[int, str] = {}
    recorded: dict[int, str] = {}
    try:
        from eclermanager import config as config_mod
        cfg = config_mod.load(config_path) if config_path else config_mod.load(
            "/etc/eclermanager/config.json")
        for entry in cfg.channels:
            if entry.multicast_group:
                recorded[entry.group_id] = entry.multicast_group
            if entry.transmitter_ip:
                in_use[entry.group_id] = entry.name
    except Exception:
        pass                    # no config to hand; fall back to the pattern

    if channel in in_use:
        print(f"✗ channel {channel} is {in_use[channel]!r}, served by a real "
              "transmitter.\n  Streaming to it would collide with a live "
              "dashboard. Pick an unused channel\n  (5 and above are free "
              "here).", file=sys.stderr)
        return None

    address = recorded.get(channel) or f"{GROUP_BASE}{GROUP_OFFSET + channel}"
    source = "from the config" if channel in recorded else "derived"
    print(f"→ channel {channel} is {address} ({source})", file=sys.stderr)
    return address


def addresses_of(interface: str) -> list[str]:
    """Every IPv4 address on an interface, in the order the kernel lists them."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", interface],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            found.append(parts[parts.index("inet") + 1].split("/")[0])
    return found


def address_of(interface: str) -> str | None:
    """An address to send from, complaining if the choice is ambiguous.

    An interface can hold several addresses -- a manager container may keep a
    second one for reaching devices on another subnet -- and which is "first"
    depends on the order they were added, so it changes across reboots. Picking
    silently would make the source address vary without anyone noticing.
    """
    found = addresses_of(interface)
    if not found:
        return None
    if len(found) > 1:
        print(f"! {interface} has {len(found)} addresses: {', '.join(found)}",
              file=sys.stderr)
        print(f"  Sending from {found[0]}. If that is the wrong subnet, name\n"
              f"  the right one:  --local-addr <address>\n", file=sys.stderr)
    return found[0]


def warn_if_multihomed() -> None:
    """Say so when the egress interface is ambiguous, rather than failing mutely.

    This is the single most likely reason a test stream never reaches a
    receiver: the packets leave by the wrong leg and nothing reports an error.
    """
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    legs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] != "lo" and "inet" in parts:
            legs.append(f"{parts[1]} ({parts[parts.index('inet') + 1]})")
    if len(legs) > 1:
        print("! this host has more than one interface:", file=sys.stderr)
        for leg in legs:
            print(f"    {leg}", file=sys.stderr)
        print("  Multicast has no route of its own, so it will leave by the\n"
              "  default route -- probably the wrong VLAN, silently. Pass\n"
              "  --interface <iface> to pin it.\n", file=sys.stderr)


def manage(pidfile: Path, *, stop: bool) -> int:
    """Report on, or stop, a stream started with --detach."""
    log = Path(str(pidfile).replace(".pid", ".log"))
    if not pidfile.exists():
        print(f"nothing recorded in {pidfile}")
        if log.exists():
            print(f"\nlast of {log}:")
            print("\n".join(f"  {line}" for line in
                             log.read_text().splitlines()[-8:]))
        return 0 if stop else 1

    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        print(f"✗ {pidfile} is unreadable", file=sys.stderr)
        return 1

    alive = Path(f"/proc/{pid}").exists()
    if stop:
        if alive:
            import signal
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.25)
                if not Path(f"/proc/{pid}").exists():
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                print(f"  forced pid {pid}")
            print(f"stopped pid {pid}")
        else:
            print(f"pid {pid} was already gone")
        pidfile.unlink(missing_ok=True)
        return 0

    print(f"pid {pid}: {'running' if alive else 'GONE'}")
    if log.exists():
        print(f"\nlast of {log}:")
        print("\n".join(f"  {line}" for line in
                         log.read_text().splitlines()[-12:]))
    return 0 if alive else 1


def build_command(args: argparse.Namespace) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-re"]

    if args.url:
        # Capture a real page.  Needs Xvfb plus a browser already showing it on
        # the given display; see --url in the help text.
        cmd += ["-f", "x11grab", "-framerate", str(args.capture_fps),
                "-video_size", args.size, "-i", args.display]
    else:
        # A test pattern with a moving element, so a frozen picture is
        # distinguishable from a working one at a glance.
        cmd += ["-f", "lavfi", "-i",
                f"testsrc=size={args.size}:rate={args.capture_fps}"]

    if args.audio:
        # Silence, purely to satisfy a decoder that expects an audio track.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    gop = max(1, int(round(args.fps * args.gop_seconds)))
    cmd += [
        "-c:v", "libx264",
        "-profile:v", args.profile,
        "-level", "4.0",
        "-pix_fmt", "yuv420p",          # 8-bit 4:2:0; anything else is a risk
        "-preset", "veryfast",
        "-b:v", args.bitrate,
        "-maxrate", args.bitrate,
        "-bufsize", args.bitrate,
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",           # keep keyframes evenly spaced
        "-r", str(args.fps),
    ]

    if args.audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                "-shortest"]
    else:
        cmd += ["-an"]

    target = f"{args.group}:{args.port}"
    # On a multi-homed host, multicast has no route of its own, so the kernel
    # sends it out the default-route interface.  On a manager container that is
    # the management leg, not the TV VLAN -- the stream then never reaches the
    # receivers.  localaddr pins the egress interface by source address.
    local = f"&localaddr={args.local_addr}" if args.local_addr else ""
    if args.variant == "rtp":
        cmd += ["-f", "rtp_mpegts",
                f"rtp://{target}?ttl={args.ttl}&pkt_size={TS_PKT_SIZE}{local}"]
    else:
        cmd += ["-f", "mpegts", "-muxrate", "0",
                f"udp://{target}?ttl={args.ttl}&pkt_size={TS_PKT_SIZE}"
                f"&overrun_nonfatal=1{local}"]
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--channel", type=int, metavar="N",
                        help="Group ID to stream to; its multicast address is "
                             "looked up in the config, or derived from the "
                             "observed pattern 239.255.42.(42+N)")
    target.add_argument("--group", help="the multicast address directly, "
                                        "e.g. 239.255.42.47")
    parser.add_argument("--config", default=None,
                        help="config to read channel addresses from "
                             "(default: the installed one)")
    parser.add_argument("--port", type=int, default=5004)
    parser.add_argument("--interface", metavar="IFACE", default=None,
                        help="send from this interface, e.g. eth1. Required on "
                             "a host with more than one leg: multicast "
                             "otherwise leaves via the default route, which is "
                             "usually the wrong VLAN")
    parser.add_argument("--local-addr", metavar="IP", default=None,
                        help="send from this source address (the same thing, "
                             "if you would rather name the address)")
    parser.add_argument("--ttl", type=int, default=4,
                        help="multicast TTL (default 4; 1 stays on the local "
                             "segment, which may not be enough)")
    parser.add_argument("--variant", choices=["ts", "rtp"], default="ts",
                        help="container: raw MPEG-TS (default) or RTP")
    parser.add_argument("--profile", choices=["baseline", "main", "high"],
                        default="main")
    parser.add_argument("--audio", action="store_true",
                        help="include a silent audio track")
    parser.add_argument("--fps", type=int, default=30,
                        help="OUTPUT framerate; the receiver lists 24/25/30/50/60")
    parser.add_argument("--capture-fps", type=float, default=5.0,
                        help="capture/source framerate; frames are duplicated "
                             "up to --fps. Pick one that divides evenly")
    parser.add_argument("--gop-seconds", type=float, default=1.5,
                        help="keyframe interval in seconds. This is also the "
                             "channel-switch latency (default 1.5)")
    parser.add_argument("--bitrate", default="6M")
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--url", action="store_true",
                        help="capture an X display instead of a test pattern; "
                             "needs Xvfb plus a browser already on --display")
    parser.add_argument("--display", default=":99")
    parser.add_argument("--loglevel", default="info")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ffmpeg command and stop")
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument("--detach", action="store_true",
                           help="run in the background and return. Useful "
                                "through 'pct exec', which does not forward "
                                "Ctrl-C into the container")
    lifecycle.add_argument("--stop", action="store_true",
                           help="stop a stream started with --detach")
    lifecycle.add_argument("--status", action="store_true",
                           help="report on a stream started with --detach")
    args = parser.parse_args(argv)

    pidfile = Path(f"/tmp/teststream-{args.port}.pid")

    if args.status or args.stop:
        return manage(pidfile, stop=args.stop)

    if args.channel is None and not args.group:
        parser.error("one of --channel or --group is required to start a stream")

    if args.channel is not None:
        args.group = group_for_channel(args.channel, args.config)
        if args.group is None:
            return 2

    if args.interface and not args.local_addr:
        args.local_addr = address_of(args.interface)
        if args.local_addr is None:
            print(f"✗ {args.interface} has no IPv4 address", file=sys.stderr)
            return 2
        print(f"→ sending from {args.interface} ({args.local_addr})",
              file=sys.stderr)
    if not args.local_addr:
        warn_if_multihomed()

    try:
        address = ipaddress.ip_address(args.group)
    except ValueError:
        print(f"✗ {args.group!r} is not an IP address", file=sys.stderr)
        return 2
    if not address.is_multicast:
        print(f"✗ {args.group} is not a multicast address (needs 224-239.x.x.x).\n"
              "  Read a transmitter's group with: tools/probe.py <transmitter-ip>",
              file=sys.stderr)
        return 2

    if args.fps % args.capture_fps != 0:
        print(f"! {args.capture_fps} does not divide evenly into {args.fps}, so "
              "frames will be duplicated unevenly.\n"
              "  That shows up as judder, most visibly during fades. Consider "
              f"{args.fps}/2 or {args.fps}/6.", file=sys.stderr)

    command = build_command(args)

    if args.dry_run:
        print(" ".join(command))
        return 0

    if shutil.which("ffmpeg") is None:
        print("✗ ffmpeg not found. Install it first:\n"
              "    apt-get update && apt-get install -y ffmpeg", file=sys.stderr)
        return 1

    gop = max(1, int(round(args.fps * args.gop_seconds)))
    print(f"→ sending to {args.group}:{args.port}  ({args.variant}, "
          f"{args.profile} profile, {args.bitrate})")
    print(f"  capture {args.capture_fps:g} fps -> output {args.fps} fps, "
          f"keyframe every {gop} frames (~{args.gop_seconds:g}s)")
    print(f"  audio: {'silent track included' if args.audio else 'none'}")
    print()
    print("  Now set one TV to the channel that matches this group, and look at")
    print("  it. Give it a few seconds -- a receiver can only start decoding at")
    print("  a keyframe. Ctrl+C here to stop.")
    print()

    if args.detach:
        # start_new_session detaches from this terminal, so the stream survives
        # the exec session that started it.
        log = Path(f"/tmp/teststream-{args.port}.log")
        with log.open("ab") as handle:
            process = subprocess.Popen(command, stdout=handle, stderr=handle,
                                       stdin=subprocess.DEVNULL,
                                       start_new_session=True)
        time.sleep(3)
        if process.poll() is not None:
            print(f"✗ ffmpeg exited immediately. Its output:", file=sys.stderr)
            print(log.read_text()[-1500:], file=sys.stderr)
            return 1
        pidfile.write_text(str(process.pid))
        print(f"→ running in the background as pid {process.pid}")
        print(f"  log:    {log}")
        print(f"  status: {sys.argv[0]} --port {args.port} --status")
        print(f"  stop:   {sys.argv[0]} --port {args.port} --stop")
        return 0

    print("  (Ctrl-C to stop. Through 'pct exec' that does not arrive -- use\n"
          "   --detach instead, or stop it with: pkill -f teststream.py)\n")
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
