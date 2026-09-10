#!/usr/bin/env python3
"""A fake VEO device, so the dashboard can be exercised without hardware.

    python3 tools/mock_veo.py --fleet 6      # 127.0.0.2 .. 127.0.0.7 on port 9999

It mimics the port 9999 line protocol: an ``input>`` prompt, ``get_``/``set_``
commands, and — with --chaos — the failure this project exists to fix, where a
receiver silently drops to another channel or loses video lock.
"""

from __future__ import annotations

import argparse
import random
import socketserver
import threading
import time

FW_VERSION = "V1.01.r0-mock"
#: Real units pad the prompt with a NUL and echo the command after it.
PROMPT = b"input>\x00"
BANNER = (b"=" * 30 + b"\r\n" + b"========IPTV RX Server========" + b"\r\n"
          + b"=" * 30 + b"\r\n\x00")


class FakeDevice:
    """Mutable state of one emulated receiver."""

    def __init__(self, name: str, group_id: int, mac: str = "") -> None:
        self.name = name
        self.group_id = group_id
        self.mac = mac or "00:19:F5:00:00:01"
        self.video_lock = True
        self.stuck = False
        self.lock = threading.Lock()

    def chaos(self) -> None:
        """Occasionally wander off channel or drop the stream."""
        with self.lock:
            roll = random.random()
            if roll < 0.10:
                self.group_id = random.randint(0, 4)
                print(f"[chaos] {self.name} drifted to channel {self.group_id}")
            elif roll < 0.30:
                # The observed real failure: the transmitter restarts underneath a
                # receiver that keeps power, and the receiver never re-acquires.
                self.video_lock = False
                self.stuck = True
                print(f"[chaos] {self.name} stuck: lost its stream, right channel")


COMMANDS = (
    "set_group_id, get_group_id, set_dhcp, get_dhcp, set_uart_baudrate, "
    "get_uart_baudrate, set_static_ip, get_static_ip, set_mac_address, "
    "get_mac_address, get_lan_status, get_hdcp, get_video_lock, get_ip_config, "
    "set_session_key, set_device_name, get_device_name, set_video_bitrate, "
    "get_video_bitrate, set_downscale_mode, get_downscale_mode, "
    "set_video_out_mode, get_video_out_mode, set_streaming_mode, "
    "get_streaming_mode, get_fw_version, get_company_id, reboot, list, exit"
)


class Handler(socketserver.StreamRequestHandler):
    timeout = 30
    device: FakeDevice
    reply_style: str
    reply_delay: float = 0.0

    def handle(self) -> None:
        self.wfile.write(BANNER + PROMPT)
        while True:
            try:
                raw = self.rfile.readline()
            except OSError:
                return
            if not raw:
                return
            line = raw.decode("ascii", "replace").strip()
            if not line:
                self.wfile.write(PROMPT)
                continue
            if self.reply_delay:
                # Real units take a moment to answer.  Long enough and a reader
                # that ends messages on a pause will mismatch replies to
                # commands, which is exactly the bug this reproduces.
                time.sleep(self.reply_delay)
            reply = self.dispatch(line)
            if reply is None:
                return
            # Echo the command the way the real firmware does, then answer.
            self.wfile.write(
                line.encode() + b"\r\n" + reply.encode() + b"\r\n" + PROMPT
            )

    def dispatch(self, line: str) -> str | None:
        device = self.device
        parts = line.split()
        command, args = parts[0].lower(), parts[1:]
        if command == "exit":
            return None
        if command == "list":
            return COMMANDS
        if command == "get_group_id":
            with device.lock:
                return self.styled("group_id", device.group_id)
        if command == "set_group_id":
            if not args or not args[0].isdigit() or not 0 <= int(args[0]) <= 63:
                return "invalid parameter"
            requested = int(args[0])
            with device.lock:
                if requested == device.group_id:
                    # Firmware that already believes it is on this channel does
                    # nothing -- so a stuck receiver stays stuck.  This is why the
                    # manager bounces through another channel instead.
                    return "OK"
                device.group_id = requested
                device.video_lock = True
                device.stuck = False
            return "OK"
        if command == "_break":
            # Mock-only: reproduce the observed failure on demand, so the
            # re-acquire path can be tested without waiting for chaos.
            with device.lock:
                device.video_lock = False
                device.stuck = True
            return "OK (mock: stuck on the right channel, no stream)"
        if command == "get_video_lock":
            with device.lock:
                if self.reply_style == "real":
                    return "Lock" if device.video_lock else "Unlock"
                if self.reply_style == "words":
                    return "video lock: " + ("locked" if device.video_lock else "unlocked")
                return self.styled("video_lock", 1 if device.video_lock else 0)
        if command == "get_device_name":
            return self.styled("device_name", device.name)
        if command == "get_mac_address":
            return self.styled("mac_address", device.mac)
        if command == "get_fw_version":
            return self.styled("fw_version", FW_VERSION)
        if command == "get_lan_status":
            return self.styled("lan_status", "100Mbps Full Duplex")
        if command == "get_ip_config":
            return self.styled("ip_config", "static")
        if command == "get_hdcp":
            return self.styled("hdcp", 0)
        if command == "get_company_id":
            return self.styled("company_id", "0x0001")
        if command == "reboot":
            print(f"[mock] {device.name} rebooting")
            return "rebooting"
        return f"unknown command: {command}"

    def styled(self, key: str, value: object) -> str:
        """Vary the reply phrasing the way rebranded firmwares do."""
        if self.reply_style in ("bare", "real"):
            return str(value)      # what the actual VEO-XRI1C does
        if self.reply_style == "words":
            return f"{key} is {value}"
        return f"{key}={value}"


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host: str, port: int, device: FakeDevice, style: str,
          reply_delay: float = 0.0) -> Server:
    handler = type("Bound", (Handler,), {"device": device, "reply_style": style,
                                         "reply_delay": reply_delay})
    server = Server((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True,
                     name=f"mock-{host}").start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fleet", type=int, default=4,
                        help="how many devices to emulate on 127.0.0.2+")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--style", default="real",
                        choices=["real", "mixed", "keyed", "bare", "words"],
                        help="reply phrasing to emulate; 'real' matches a "
                             "VEO-XRI1C on firmware Rx V1.01.r0 (default)")
    parser.add_argument("--reply-delay", type=float, default=0.0, metavar="SECONDS",
                        help="pause before answering, to emulate slow firmware")
    parser.add_argument("--chaos", type=float, default=0.0, metavar="SECONDS",
                        help="every N seconds, randomly break a device")
    args = parser.parse_args()

    styles = ["real", "keyed", "bare", "words"]
    devices = []
    for index in range(args.fleet):
        host = f"127.0.0.{index + 2}"
        device = FakeDevice(name=f"MOCK-RX-{index + 1}",
                            group_id=(index % 4) + 1,
                            mac=f"00:19:F5:00:00:{index + 1:02X}")
        style = args.style if args.style != "mixed" else styles[index % len(styles)]
        serve(host, args.port, device, style, args.reply_delay)
        devices.append(device)
        print(f"mock device {device.name}: {host}:{args.port} "
              f"channel {device.group_id} (style={style})")

    if args.chaos:
        def chaos_loop() -> None:
            while True:
                time.sleep(args.chaos)
                random.choice(devices).chaos()
        threading.Thread(target=chaos_loop, daemon=True, name="chaos").start()
        print(f"chaos enabled: something breaks every ~{args.chaos}s")

    print("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
