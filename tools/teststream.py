#!/usr/bin/env python3
"""Stream to a VEO multicast group and see whether a receiver locks onto it.

This answers the one question the "retire the ChromeBoxes" idea depends on:
will a VEO-XRI1C decode a stream that ffmpeg produced, rather than one a
VEO-XTI1C encoded?

Run it from something on the TV VLAN.  The manager container already has a leg
there, so the quickest path is:

    apt-get install -y ffmpeg          # in the container

    # a test pattern, to prove a receiver locks onto a software stream at all
    python3 tools/teststream.py --channel 5

    # or a real page: rendered on a virtual display, then captured
    python3 tools/teststream.py --channel 5 --url https://example.com/dashboard

Then set one TV to channel 5 and look at it.  One click on that card's starred
channel puts it back.

``--url`` handles the whole pipeline: it calls tools/pagesource.sh to put the
page on a virtual display, captures that, and takes the browser down again on
``--stop``.  Use ``--from-display :99`` instead when you have already started a
page yourself and want to reuse it.

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


def display_is_live(display: str) -> bool:
    """Is there an X server on this display that ffmpeg could read?

    Cheap, and worth it: without this the failure is ffmpeg's
    "Cannot open display :99, error 1" followed by "Error opening input
    files: Input/output error", buried under a screenful of x264 statistics
    from the previous run. Which does not tell you to restart the page.

    The check is the unix socket the server listens on, not xdpyinfo: this
    tool runs on minimal hosts where x11-utils may not be installed, and a
    liveness check that quietly passes when its helper is missing is worse
    than no check at all. xdpyinfo confirms it when present, because a
    socket can outlive a server that was killed rather than asked to stop.
    """
    number = display.lstrip(":").split(".")[0]
    if not number.isdigit():
        return True                       # a remote or odd display; not ours
    if not Path(f"/tmp/.X11-unix/X{number}").exists():
        return False
    try:
        probe = subprocess.run(["xdpyinfo", "-display", display],
                               capture_output=True, timeout=5)
    except FileNotFoundError:
        return True                       # socket is there; take it on trust
    except subprocess.SubprocessError:
        return False
    return probe.returncode == 0


def start_page(url: str, display: str, size: str) -> bool:
    """Render a page on a virtual display, via tools/pagesource.sh."""
    script = Path(__file__).resolve().parent / "pagesource.sh"
    if not script.exists():
        print(f"✗ {script} is missing", file=sys.stderr)
        return False
    print(f"→ rendering {url} on {display}", file=sys.stderr)
    result = subprocess.run(
        ["bash", str(script), "--url", url, "--display", display,
         "--size", size])
    if result.returncode != 0:
        print("✗ the page could not be rendered, so there is nothing to "
              "stream.", file=sys.stderr)
        return False
    return True


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

        display_file = pidfile.with_suffix(".display")
        if display_file.exists():
            display = display_file.read_text().strip()
            script = Path(__file__).resolve().parent / "pagesource.sh"
            if script.exists():
                print(f"also stopping the page on {display}")
                subprocess.run(["bash", str(script), "--display", display,
                                "--stop"])
            display_file.unlink(missing_ok=True)
        return 0

    print(f"pid {pid}: {'running' if alive else 'GONE'}")
    if log.exists():
        print(f"\nlast of {log}:")
        print("\n".join(f"  {line}" for line in
                         log.read_text().splitlines()[-12:]))
    return 0 if alive else 1


def parse_bitrate(value: str) -> int:
    """"10M" -> 10000000. ffmpeg takes the suffix form, arithmetic does not."""
    text = value.strip().lower().rstrip("bps").strip()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    return int(float(text) * multiplier)


def build_command(args: argparse.Namespace) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", args.loglevel]

    if args.capture_display:
        # NOT -re here. x11grab is a live input that already paces itself at
        # -framerate; -re asks ffmpeg to throttle reading to the input's own
        # rate as well, and the two pacers fight over frame timing. ffmpeg's
        # own documentation says not to use -re with a live input. The symptom
        # is janky animation on the television while the encoder reports a
        # healthy speed=1.0x, because the frames arrive uneven rather than
        # late. A test pattern hides this -- testsrc's counter still ticks --
        # which is why it only showed up on a real page.
        #
        # Capture a rendered page.  build_command is only reached once the
        # display is known to have one on it.
        # -draw_mouse 0 keeps the X pointer out of the frame. Xvfb's
        # -nocursor does not: it suppresses only the default root cursor,
        # while the browser sets a cursor on its own window that still draws.
        cmd += ["-f", "x11grab", "-draw_mouse", "0",
                "-framerate", str(args.capture_fps),
                "-video_size", args.size, "-i", args.capture_display]
    else:
        # A test pattern with a moving element, so a frozen picture is
        # distinguishable from a working one at a glance. lavfi generates as
        # fast as it can, so this input does want -re to pace it.
        cmd += ["-re", "-f", "lavfi", "-i",
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

    if args.qmin:
        # A quality floor, and the reason is a conformance limit rather than
        # taste. A 6 Mbit constant-rate budget is far more than flat graphics
        # need, so x264 drops to near-lossless -- q=2.0 measured on a live
        # dashboard -- and on a photographic slide that produces a keyframe of
        # one to two megabytes. H.264 level 4.0, which this stream declares,
        # caps the size of a single coded frame well below that. A hardware
        # decoder that sizes its buffers from the declared level truncates the
        # frame, and the picture tears in a band that persists until the next
        # keyframe, which is oversized in the same way. 98.5% of macroblocks
        # are skip on a static slide, so nothing repairs it in between.
        #
        # Measured symptom: two of six slides tore consistently, both
        # photographic; the flat ones never did. The stream itself was proven
        # valid -- a 110MB capture off the wire decoded with zero errors
        # offline -- so this is the decoder's limit, not corruption.
        #
        # 18 is visually lossless for text and graphics at 1080p and shrinks
        # a photographic keyframe several-fold. --qmin 0 disables the floor.
        cmd += ["-qmin", str(args.qmin)]

    if args.no_bframes:
        # B-frames make the decoder hold and reorder frames. A hardware IP
        # decoder of this class is built for what the matching hardware
        # transmitter sends -- low latency, no reordering -- and x264's
        # defaults are the opposite: measured 12698 B-frames in a ten minute
        # run, 95% of them in runs of three or more. Worth trying whenever
        # the picture is unstable and the network is not losing packets.
        cmd += ["-bf", "0"]

    if not args.vbr:
        # Constant rate, and this matters more than it looks.
        #
        # A dashboard is a nearly static picture. Left to itself the encoder
        # spends almost nothing on it -- measured 1.76 Mbit/s against a 10M
        # cap, with q dropping to 0 -- and then has to burst when a slide
        # crossfades. A hardware IP decoder with a small input buffer handles
        # that badly, which is what put artifacts on the television.
        #
        # It also explains why the test pattern always looked perfect:
        # testsrc changes every pixel of every frame, so it sits at the cap
        # and the transport stream is constant-rate by accident.
        #
        # nal-hrd=cbr makes x264 pad the elementary stream to the target, and
        # -muxrate pads the transport stream with null packets, so the
        # receiver sees a steady arrival rate whatever the picture is doing.
        # force-cfr keeps the frame timing constant too, which the HRD model
        # requires.
        video_bps = parse_bitrate(args.bitrate)
        cmd += ["-minrate", args.bitrate,
                "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        # ~15% over the video rate covers TS packetisation and the tables.
        # Too low and ffmpeg refuses with "muxrate is too low".
        args.muxrate = str(int(video_bps * 1.15))
    else:
        args.muxrate = "0"

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
    # SO_SNDBUF. Linux defaults to a couple of hundred KB, and a constant-rate
    # transport stream is written in bursts, so an overflow drops packets in
    # the kernel with nothing logged anywhere. Cheap insurance rather than a
    # measured fix -- at ~11 Mbit/s the default is probably adequate.
    sndbuf = "&buffer_size=8388608"

    # Pace the bytes onto the wire, which -muxrate does NOT do.
    #
    # -muxrate makes the transport stream constant-rate in its timestamps and
    # null padding. It says nothing about when the bytes are handed to the
    # socket: ffmpeg encodes a frame every 33ms and then writes that whole
    # frame's packets back to back, so a 200KB frame leaves as a ~1.6ms burst
    # at line rate. A receiver with a small input buffer drops the tail of a
    # burst it cannot absorb, and the picture tears from that point down with
    # everything below it lost -- photographed on a wall screen.
    #
    # It fits every other observation: only large frames fail, the switch
    # never loses anything at a 7 Mbit/s average, and a capture read back on
    # the sending host decodes perfectly because a loopback socket has no
    # such bottleneck. The hardware transmitter this replaces paces its
    # output evenly, being built for the job.
    #
    # ffmpeg's udp protocol has bitrate/burst_bits for precisely this. Pace a
    # little above the mux rate so pacing smooths bursts without ever
    # becoming the constraint itself.
    if args.no_pacing:
        pacing = ""
    else:
        nominal = (int(args.muxrate) if args.muxrate != "0"
                   else parse_bitrate(args.bitrate) * 2)
        pacing = (f"&bitrate={int(nominal * 1.10)}"
                  f"&burst_bits={TS_PKT_SIZE * 8 * 2}")
    if args.variant == "rtp":
        cmd += ["-f", "rtp_mpegts",
                f"rtp://{target}?ttl={args.ttl}&pkt_size={TS_PKT_SIZE}"
                f"{sndbuf}{pacing}{local}"]
    else:
        cmd += ["-f", "mpegts", "-muxrate", args.muxrate,
                f"udp://{target}?ttl={args.ttl}&pkt_size={TS_PKT_SIZE}"
                f"&overrun_nonfatal=1{sndbuf}{pacing}{local}"]
    return cmd


def build_parser() -> argparse.ArgumentParser:
    """Split out of main() so tests can build a real argument namespace.

    A test that hand-rolls an argparse.Namespace drifts from the parser the
    moment an option is added, and then tests the wrong thing.
    """
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
    parser.add_argument("--qmin", type=int, default=18, metavar="N",
                        help="quality floor (default 18). Stops the encoder "
                             "spending a constant-rate budget on near-"
                             "lossless keyframes that exceed what the "
                             "declared H.264 level allows, which makes a "
                             "hardware decoder tear on photographic slides. "
                             "0 disables it")
    parser.add_argument("--no-pacing", action="store_true",
                        help="do not pace the bytes onto the wire. ffmpeg "
                             "otherwise writes a whole frame's packets back "
                             "to back at line rate, and a receiver with a "
                             "small input buffer drops the tail of a large "
                             "frame's burst")
    parser.add_argument("--no-bframes", action="store_true",
                        help="encode without B-frames. Removes decoder-side "
                             "frame reordering, which is what the hardware "
                             "transmitters avoid; try this if the picture is "
                             "unstable and the network is clean")
    parser.add_argument("--vbr", action="store_true",
                        help="let the bitrate vary instead of padding to a "
                             "constant rate. Smaller on the wire, but a "
                             "hardware decoder can show artifacts when a "
                             "mostly-static page suddenly bursts")
    parser.add_argument("--size", default="1920x1080")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", metavar="ADDRESS", default=None,
                        help="stream this web page: it is rendered on a "
                             "virtual display with tools/pagesource.sh, then "
                             "captured. Without this, a test pattern is sent")
    source.add_argument("--from-display", metavar=":N", default=None,
                        help="capture a display that already has a page on it, "
                             "e.g. one you started with pagesource.sh yourself")
    parser.add_argument("--display", default=":99",
                        help="display to render on with --url (default :99)")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pidfile = Path(f"/tmp/teststream-{args.port}.pid")

    if args.status or args.stop:
        return manage(pidfile, stop=args.stop)

    if args.channel is None and not args.group:
        parser.error("one of --channel or --group is required to start a stream")

    # One flag settles where the pixels come from, so build_command does not
    # have to know how the display got its page.
    args.capture_display = args.from_display
    if args.from_display and not args.dry_run:
        if not display_is_live(args.from_display):
            print(f"✗ nothing is running on {args.from_display}.\n"
                  f"  Put a page on it first:\n"
                  f"    bash tools/pagesource.sh --url <ADDRESS> "
                  f"--display {args.from_display}\n"
                  f"  or let this tool do both with --url <ADDRESS>.",
                  file=sys.stderr)
            return 2
    started_page = False
    if args.url:
        if not args.dry_run:
            if not start_page(args.url, args.display, args.size):
                return 1
            started_page = True
        args.capture_display = args.display

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
        if started_page:
            # Record it so --stop takes the browser and display down too,
            # rather than leaving them running invisibly.
            pidfile.with_suffix(".display").write_text(args.display)
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
