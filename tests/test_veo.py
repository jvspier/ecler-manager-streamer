"""Unit tests for reply parsing and config validation (stdlib unittest)."""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclermanager import auth  # noqa: E402
from eclermanager import config as config_mod  # noqa: E402
from eclermanager import veo  # noqa: E402


class TestTelnetStripping(unittest.TestCase):
    def test_removes_negotiation(self):
        data = b"\xff\xfb\x01\xff\xfd\x03group_id=5\r\n"
        self.assertEqual(veo.strip_telnet_negotiation(data), b"group_id=5\r\n")

    def test_keeps_escaped_ff(self):
        self.assertEqual(veo.strip_telnet_negotiation(b"a\xff\xffb"), b"a\xffb")

    def test_skips_subnegotiation(self):
        data = b"\xff\xfa\x18\x00xterm\xff\xf0ok"
        self.assertEqual(veo.strip_telnet_negotiation(data), b"ok")

    def test_plain_data_untouched(self):
        self.assertEqual(veo.strip_telnet_negotiation(b"hello"), b"hello")


class TestParseInt(unittest.TestCase):
    """Rebranded firmwares phrase replies differently; all must parse."""

    def test_phrasings(self):
        cases = {
            "group_id=5": 5,
            "group_id = 05": 5,
            "group_id is 12": 12,
            "group_id: 3": 3,
            "5": 5,
            "\r\n05\r\ninput> ": 5,
            "get_group_id\r\ngroup_id=41\r\ninput> ": 41,
            "Group ID 63": 63,
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(
                    veo.parse_int(reply, "get_group_id", 0, 63), expected
                )

    def test_out_of_range_is_ignored(self):
        self.assertIsNone(veo.parse_int("group_id=900", "get_group_id", 0, 63))

    def test_unsupported_command(self):
        for reply in ("unknown command: get_group_id", "Usage: get_group_id",
                      "invalid parameter"):
            with self.subTest(reply=reply):
                self.assertIsNone(veo.parse_int(reply, "get_group_id", 0, 63))

    def test_empty(self):
        self.assertIsNone(veo.parse_int("", "get_group_id", 0, 63))
        self.assertIsNone(veo.parse_int("input> ", "get_group_id", 0, 63))

    def test_echo_only_is_not_a_value(self):
        self.assertIsNone(veo.parse_int("get_group_id\r\ninput> ", "get_group_id", 0, 63))


class TestRealDeviceWireFormat(unittest.TestCase):
    """Bytes captured from a real VEO-XRI1C (firmware Rx V1.01.r0).

    The units prefix the echoed command with a NUL-padded prompt
    ("input>\\x00get_group_id") and open the session with a banner of "=" rules,
    so replies do not arrive as a bare value on its own line.
    """

    BANNER = ("=" * 30 + "\r\n" + "========IPTV RX Server========" + "\r\n"
              + "=" * 30 + "\r\n\x00")

    def test_group_id(self):
        raw = "input>\x00get_group_id\r\n1\r\n"
        self.assertEqual(veo.parse_int(raw, "get_group_id", 0, 63), 1)

    def test_group_id_two_digits(self):
        raw = "input>\x00get_group_id\r\n12\r\n"
        self.assertEqual(veo.parse_int(raw, "get_group_id", 0, 63), 12)

    def test_banner_bundled_with_the_reply(self):
        """A slow greeting read can leave the banner in front of the answer."""
        raw = self.BANNER + "input>\x00get_group_id\r\n7\r\n"
        self.assertEqual(veo.parse_int(raw, "get_group_id", 0, 63), 7)

    def test_video_lock_word(self):
        raw = "input>\x00get_video_lock\r\nLock\r\n"
        self.assertIs(veo.parse_bool(raw, "get_video_lock"), True)

    def test_device_name(self):
        raw = "input>\x00get_device_name\r\nRecieve 01\r\n"
        self.assertEqual(veo.parse_text(raw, "get_device_name"), "Recieve 01")

    def test_firmware_version(self):
        raw = "input>\x00get_fw_version\r\nRx Firmware     : V1.01.r0\r\n"
        self.assertEqual(veo.parse_text(raw, "get_fw_version"), "V1.01.r0")

    def test_lan_status(self):
        raw = "input>\x00get_lan_status\r\nLink_Up\r\n"
        self.assertEqual(veo.parse_text(raw, "get_lan_status"), "Link_Up")

    def test_nul_padding_is_stripped(self):
        self.assertNotIn("\x00", veo.strip_telnet_negotiation(
            b"input>\x00get_group_id\r\n1\r\n").decode())

    def test_banner_alone_yields_no_value(self):
        """The banner must never be mistaken for an answer."""
        self.assertIsNone(veo.parse_int(self.BANNER, "get_group_id", 0, 63))


class TestParseBool(unittest.TestCase):
    def test_true_phrasings(self):
        for reply in ("video_lock=1", "video lock is locked", "lock: yes",
                      "video_lock on", "1"):
            with self.subTest(reply=reply):
                self.assertIs(veo.parse_bool(reply, "get_video_lock"), True)

    def test_false_phrasings(self):
        for reply in ("video_lock=0", "video lock is unlocked", "lock: no",
                      "video_lock off", "0", "no signal"):
            with self.subTest(reply=reply):
                self.assertIs(veo.parse_bool(reply, "get_video_lock"), False)

    def test_unknown(self):
        self.assertIsNone(veo.parse_bool("", "get_video_lock"))
        self.assertIsNone(veo.parse_bool("unknown command", "get_video_lock"))

    def test_enabled_and_disabled(self):
        """get_dhcp phrasings; "disabled" must not read as "enabled"."""
        self.assertIs(veo.parse_bool("DHCP: enabled", "get_dhcp"), True)
        self.assertIs(veo.parse_bool("dhcp is enable", "get_dhcp"), True)
        self.assertIs(veo.parse_bool("disabled", "get_dhcp"), False)
        self.assertIs(veo.parse_bool("DHCP: disabled", "get_dhcp"), False)


class TestParseText(unittest.TestCase):
    def test_strips_key(self):
        self.assertEqual(
            veo.parse_text("device_name=TV Canteen", "get_device_name"), "TV Canteen"
        )
        self.assertEqual(
            veo.parse_text('device_name is "TV 11"', "get_device_name"), "TV 11"
        )

    def test_bare_value(self):
        self.assertEqual(veo.parse_text("v1.0.3", "get_fw_version"), "v1.0.3")


class TestParseTextValuesWithColons(unittest.TestCase):
    """A value containing colons must survive key-stripping.

    Regression: the key-prefix regex treated the first octet of a bare MAC
    address as a key name, turning 00:19:F5:0A:0B:0C into 19:F5:0A:0B:0C.
    """

    def test_bare_mac_untouched(self):
        self.assertEqual(
            veo.parse_text("00:19:F5:0A:0B:0C", "get_mac_address"),
            "00:19:F5:0A:0B:0C")

    def test_keyed_mac(self):
        for raw in ("mac_address=00:19:F5:0A:0B:0C",
                    "MAC Address : 00:19:F5:0A:0B:0C",
                    "mac is 00:19:F5:0A:0B:0C"):
            with self.subTest(raw=raw):
                self.assertEqual(veo.parse_text(raw, "get_mac_address"),
                                 "00:19:F5:0A:0B:0C")

    def test_bare_ip_untouched(self):
        self.assertEqual(veo.parse_text("10.0.2.1", "get_ip_config"),
                         "10.0.2.1")

    def test_keys_are_still_stripped(self):
        self.assertEqual(
            veo.parse_text("Rx Firmware     : V1.01.r0", "get_fw_version"),
            "V1.01.r0")
        self.assertEqual(veo.parse_text("device_name=Recieve 01",
                                        "get_device_name"), "Recieve 01")


class TestVeoDetection(unittest.TestCase):
    """The TV VLAN carries televisions too, so port 9999 alone proves nothing."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import scan
        self.scan = scan

    def test_real_veo_recognised(self):
        status = veo.DeviceStatus(host="10.0.0.1", online=True, group_id=2)
        self.assertTrue(self.scan.is_veo(status))
        self.assertEqual(self.scan.classify(status, set()), "receiver?")

    def test_recognised_from_firmware_alone(self):
        status = veo.DeviceStatus(host="10.0.0.1", online=True,
                                  fw_version="V1.01.r0")
        self.assertTrue(self.scan.is_veo(status))

    def test_silent_device_is_not_a_veo(self):
        status = veo.DeviceStatus(host="10.0.0.1", online=True)
        self.assertFalse(self.scan.is_veo(status))
        self.assertEqual(self.scan.classify(status, set()), "not a VEO?")

    def test_model_name_decides_role(self):
        rx = veo.DeviceStatus(host="10.0.0.1", online=True, group_id=1,
                              device_name="VEO-XRI1C")
        tx = veo.DeviceStatus(host="10.0.0.2", online=True, group_id=1,
                              device_name="VEO-XTI1C")
        self.assertEqual(self.scan.classify(rx, set()), "receiver")
        self.assertEqual(self.scan.classify(tx, set()), "transmitter")

    def test_explicit_transmitter_flag_wins(self):
        status = veo.DeviceStatus(host="10.0.0.9", online=True, group_id=1)
        self.assertEqual(self.scan.classify(status, {"10.0.0.9"}), "transmitter")


class TestSetGroupIdValidation(unittest.TestCase):
    def test_rejects_out_of_range(self):
        for bad in (-1, 64, 999):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    veo.set_group_id("192.0.2.1", bad)


class TestBounceValidation(unittest.TestCase):
    def test_rejects_same_channel(self):
        """A bounce through the same channel is a no-op, so refuse it."""
        with self.assertRaisesRegex(ValueError, "must differ"):
            veo.bounce("192.0.2.1", target_group_id=2, via_group_id=2)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            veo.bounce("192.0.2.1", target_group_id=2, via_group_id=64)
        with self.assertRaises(ValueError):
            veo.bounce("192.0.2.1", target_group_id=-1, via_group_id=2)

    def test_unreachable_reports_cleanly(self):
        result = veo.bounce("192.0.2.123", target_group_id=1, via_group_id=63,
                            timeout=0.4)
        self.assertFalse(result.ok)
        self.assertTrue(result.message)


class TestBounceVia(unittest.TestCase):
    def _cfg(self, payload):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return config_mod.load(Path(tmp.name))

    def test_picks_highest_unused_channel(self):
        cfg = self._cfg({
            "channels": [{"group_id": 1, "name": "A"}, {"group_id": 63, "name": "B"}],
            "receivers": [{"ip": "10.0.0.1"}],
        })
        self.assertEqual(cfg.bounce_via(1), 62)

    def test_honours_configured_value(self):
        cfg = self._cfg({
            "bounce_via_group_id": 40,
            "channels": [{"group_id": 1, "name": "A"}],
            "receivers": [{"ip": "10.0.0.1"}],
        })
        self.assertEqual(cfg.bounce_via(1), 40)

    def test_never_returns_the_target(self):
        cfg = self._cfg({
            "bounce_via_group_id": 40,
            "receivers": [{"ip": "10.0.0.1"}],
        })
        self.assertNotEqual(cfg.bounce_via(40), 40)

    def test_rejects_out_of_range_config(self):
        with self.assertRaisesRegex(config_mod.ConfigError, "out of range"):
            self._cfg({"bounce_via_group_id": 99, "receivers": [{"ip": "10.0.0.1"}]})


class TestReadStatusOffline(unittest.TestCase):
    def test_unreachable_never_raises(self):
        # 192.0.2.0/24 is the reserved TEST-NET-1 block: nothing answers.
        status = veo.read_status("192.0.2.123", timeout=0.4)
        self.assertFalse(status.online)
        self.assertIsNotNone(status.error)
        self.assertIsNone(status.group_id)


class TestScanTargets(unittest.TestCase):
    """Target expansion for tools/scan.py."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import scan
        self.parse = scan.parse_targets

    def test_cidr_excludes_network_and_broadcast(self):
        hosts = self.parse(["10.0.2.0/29"])
        self.assertEqual(hosts, [f"10.0.2.{n}" for n in range(1, 7)])

    def test_dash_range(self):
        self.assertEqual(self.parse(["10.0.2.10-13"]),
                         ["10.0.2.10", "10.0.2.11", "10.0.2.12", "10.0.2.13"])

    def test_single_addresses_and_dedup(self):
        self.assertEqual(self.parse(["10.0.0.1", "10.0.0.1", "10.0.0.2"]),
                         ["10.0.0.1", "10.0.0.2"])

    def test_mixed_specs_keep_order(self):
        self.assertEqual(self.parse(["10.0.0.5", "10.0.0.1-2"]),
                         ["10.0.0.5", "10.0.0.1", "10.0.0.2"])

    def test_rejects_nonsense(self):
        for bad in ["not-an-ip", "10.0.0.1-x", "10.0.0.999"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.parse([bad])


class TestInventoryNames(unittest.TestCase):
    """tools/scan.py --names must read a table as people actually keep it."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import scan
        self.scan = scan

    def _names(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        tmp.write(text); tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return self.scan.load_names(Path(tmp.name))

    def test_tabs_spaces_commas_and_comments(self):
        names = self._names(
            "# a comment\n"
            "\n"
            "10.0.1.1\ttv_transmit_01\n"
            "10.0.2.1     tv_receiver_01\n"
            "10.0.2.2,tv_receiver_02\n"
            "10.0.2.3 tv_receiver_03   # trailing comment\n"
        )
        self.assertEqual(names, {
            "10.0.1.1": "tv_transmit_01",
            "10.0.2.1": "tv_receiver_01",
            "10.0.2.2": "tv_receiver_02",
            "10.0.2.3": "tv_receiver_03",
        })

    def test_skips_lines_that_are_not_inventory(self):
        names = self._names("just some prose\n10.0.0.1\n10.0.0.2 ok\n")
        self.assertEqual(names, {"10.0.0.2": "ok"})

    def test_multi_word_names_kept(self):
        self.assertEqual(self._names("10.0.0.1 TV 11 Canteen\n"),
                         {"10.0.0.1": "TV 11 Canteen"})

    def test_slugify(self):
        self.assertEqual(self.scan.slugify("tv_receiver_02"), "tv_receiver_02")
        self.assertEqual(self.scan.slugify("TV 11 (Canteen)"), "tv-11-canteen")
        self.assertEqual(self.scan.slugify("!!!"), "")


class TestSlowDevice(unittest.TestCase):
    """A device slow to *begin* answering must not read as unsupported.

    Regression test for a real failure: the inter-byte quiet gap (0.35s) was
    also being used as the wait for the first byte, so any unit that took
    longer than that to start replying returned an empty string -- which looks
    identical to an unsupported command.
    """

    DELAY = 1.2

    def setUp(self):
        import socket as _socket
        import threading as _threading

        self.server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self.server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self._stop = _threading.Event()

        def serve():
            while not self._stop.is_set():
                try:
                    conn, _ = self.server.accept()
                except OSError:
                    return
                _threading.Thread(target=self._handle, args=(conn,),
                                  daemon=True).start()

        self.thread = _threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _handle(self, conn):
        conn.settimeout(10)
        try:
            while True:
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(1)
                    if not chunk:
                        return
                    line += chunk
                command = line.decode().strip().lower()
                time.sleep(self.DELAY)          # the slow part
                if command == "get_group_id":
                    conn.sendall(b"group_id=7\r\ninput> ")
                elif command == "get_video_lock":
                    conn.sendall(b"video_lock=1\r\ninput> ")
                elif command == "exit":
                    return
                else:
                    conn.sendall(b"OK\r\ninput> ")
        except OSError:
            return
        finally:
            conn.close()

    def _shutdown(self):
        self._stop.set()
        try:
            self.server.close()
        except OSError:
            pass

    def test_slow_reply_is_still_parsed(self):
        status = veo.read_status("127.0.0.1", port=self.port, timeout=4.0)
        self.assertTrue(status.online, msg=status.error)
        self.assertEqual(status.group_id, 7)
        self.assertIs(status.video_lock, True)

    def test_short_timeout_never_invents_a_value(self):
        """The invariant that matters: the right value, or none. Never a wrong one.

        A tight timeout is now often survivable, because the read budget
        outlasts it -- but if it does give up, it must report nothing rather
        than something misread from a partial reply.
        """
        status = veo.read_status("127.0.0.1", port=self.port, timeout=0.3)
        self.assertIn(status.group_id, (None, 7))


class TestReplyPairing(unittest.TestCase):
    """Replies must stay matched to the commands that caused them.

    Regression test for the worst bug found on real hardware: the units send
    their prompt *after* each answer, so a reader that ends messages on a pause
    returns just the leftover prompt for command N and picks up N's real answer
    while reading N+1.  Everything then shifts by one -- a channel number lands
    in the video-lock field and reads as "signal present", and a device name
    turns up as a firmware version.  Silent, and entirely wrong.

    The server here emulates the real protocol: banner, NUL-padded prompt,
    command echo, answer, prompt -- with a delay long enough to defeat any
    pause-based framing.
    """

    DELAY = 0.6

    ANSWERS = {
        "get_group_id": "5",
        "get_video_lock": "Unlock",
        "get_device_name": "Recieve 09",
        "get_fw_version": "Rx Firmware     : V1.01.r0",
        "get_lan_status": "Link_Up",
    }

    def setUp(self):
        import socket as _socket
        import threading as _threading

        self.server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self.server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self._stop = _threading.Event()

        def serve():
            while not self._stop.is_set():
                try:
                    conn, _ = self.server.accept()
                except OSError:
                    return
                _threading.Thread(target=self._handle, args=(conn,),
                                  daemon=True).start()

        _threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _handle(self, conn):
        banner = (b"=" * 30 + b"\r\n" + b"========IPTV RX Server========"
                  + b"\r\n" + b"=" * 30 + b"\r\n\x00")
        prompt = b"input>\x00"
        conn.settimeout(15)
        try:
            conn.sendall(banner + prompt)
            while True:
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(1)
                    if not chunk:
                        return
                    line += chunk
                command = line.decode().strip()
                if command == "exit":
                    return
                time.sleep(self.DELAY)
                if command.startswith("set_group_id "):
                    self.ANSWERS["get_group_id"] = command.split()[1]
                    answer = "OK"
                else:
                    answer = self.ANSWERS.get(command, "unknown command")
                conn.sendall(line.strip() + b"\r\n" + answer.encode()
                             + b"\r\n" + prompt)
        except OSError:
            return
        finally:
            conn.close()

    def _shutdown(self):
        self._stop.set()
        try:
            self.server.close()
        except OSError:
            pass

    def test_every_field_matches_its_own_command(self):
        status = veo.read_status("127.0.0.1", port=self.port, timeout=4.0,
                                 with_details=True)
        self.assertTrue(status.online, msg=status.error)
        self.assertEqual(status.group_id, 5)
        # The give-away of a shift: this device reports Unlock, so a True here
        # would mean the channel number had been read as a lock state.
        self.assertIs(status.video_lock, False)
        self.assertEqual(status.device_name, "Recieve 09")
        self.assertEqual(status.fw_version, "V1.01.r0")
        self.assertEqual(status.lan_status, "Link_Up")

    def test_switch_verifies_against_the_right_reply(self):
        result = veo.set_group_id("127.0.0.1", 5, port=self.port, timeout=4.0,
                                  verify_delay=0.1)
        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.verified_group_id, 5)


class TestConfig(unittest.TestCase):
    def _write(self, payload: dict) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_loads_and_looks_up(self):
        path = self._write({
            "channels": [{"group_id": 1, "name": "Sales"},
                         {"group_id": 2, "name": "Ops"}],
            "receivers": [{"id": "tv1", "name": "TV 1", "ip": "10.0.0.1",
                           "expected_group_id": 2}],
        })
        cfg = config_mod.load(path)
        self.assertEqual(cfg.channel_name(2), "Ops")
        self.assertIsNone(cfg.channel_name(9))
        self.assertEqual(cfg.receiver("tv1").ip, "10.0.0.1")
        self.assertEqual(len(cfg.active_receivers), 1)

    def test_id_defaults_from_ip(self):
        path = self._write({"receivers": [{"ip": "10.0.0.5"}]})
        cfg = config_mod.load(path)
        self.assertEqual(cfg.receivers[0].id, "10-0-0-5")

    def test_rejects_duplicate_ip(self):
        path = self._write({"receivers": [{"id": "a", "ip": "10.0.0.1"},
                                          {"id": "b", "ip": "10.0.0.1"}]})
        with self.assertRaisesRegex(config_mod.ConfigError, "reuse ip"):
            config_mod.load(path)

    def test_rejects_duplicate_channel(self):
        path = self._write({
            "channels": [{"group_id": 1, "name": "A"}, {"group_id": 1, "name": "B"}],
            "receivers": [{"ip": "10.0.0.1"}],
        })
        with self.assertRaisesRegex(config_mod.ConfigError, "reuse group_id"):
            config_mod.load(path)

    def test_rejects_out_of_range_expected(self):
        path = self._write({"receivers": [{"ip": "10.0.0.1",
                                           "expected_group_id": 99}]})
        with self.assertRaisesRegex(config_mod.ConfigError, "out of range"):
            config_mod.load(path)

    def test_rejects_empty(self):
        with self.assertRaisesRegex(config_mod.ConfigError, "no receivers"):
            config_mod.load(self._write({"receivers": []}))

    def test_poll_interval_floor(self):
        path = self._write({"poll_interval_seconds": 1,
                            "receivers": [{"ip": "10.0.0.1"}]})
        self.assertEqual(config_mod.load(path).poll_interval_seconds, 10.0)

    def test_save_round_trip(self):
        path = self._write({
            "channels": [{"group_id": 3, "name": "Warehouse"}],
            "receivers": [{"id": "tv1", "name": "TV 1", "ip": "10.0.0.1"}],
        })
        cfg = config_mod.load(path)
        cfg.receivers[0].expected_group_id = 3
        cfg.save()
        self.assertEqual(config_mod.load(path).receivers[0].expected_group_id, 3)


class TestPollerSnapshot(unittest.TestCase):
    def _poller(self, receivers):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"channels": [{"group_id": 1, "name": "A"}],
                   "receivers": receivers}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        from eclermanager.poller import Poller
        return Poller(config_mod.load(Path(tmp.name)))

    def test_disabled_receivers_are_hidden(self):
        """A spare unit in a cupboard should not read as a permanent fault."""
        poller = self._poller([
            {"id": "live", "ip": "10.0.0.1", "expected_group_id": 1},
            {"id": "spare", "ip": "10.0.0.2", "enabled": False},
        ])
        snapshot = poller.snapshot()
        self.assertEqual([d["id"] for d in snapshot["devices"]], ["live"])
        self.assertEqual(snapshot["summary"]["total"], 1)
        self.assertEqual(snapshot["summary"]["offline"], 1)  # live one not polled yet

    def test_natural_sort_order(self):
        poller = self._poller([
            {"id": "b", "name": "TV 10", "ip": "10.0.0.10"},
            {"id": "a", "name": "TV 2", "ip": "10.0.0.2"},
        ])
        self.assertEqual([d["name"] for d in poller.snapshot()["devices"]],
                         ["TV 2", "TV 10"])


class TestPasswordHashing(unittest.TestCase):
    def test_round_trip(self):
        encoded = auth.hash_password("a good long password")
        self.assertTrue(auth.verify_password("a good long password", encoded))

    def test_wrong_password_rejected(self):
        encoded = auth.hash_password("a good long password")
        for wrong in ("A good long password", "a good long passwor", "", "x"):
            with self.subTest(wrong=wrong):
                self.assertFalse(auth.verify_password(wrong, encoded))

    def test_salt_differs_between_hashes(self):
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_malformed_hash_is_rejected_not_crashed(self):
        for bad in ("", "nonsense", "scrypt$x$y$z$q$r", "md5$1$2$3$a$b",
                    "scrypt$16384$8$1$onlyfive"):
            with self.subTest(bad=bad):
                self.assertFalse(auth.verify_password("whatever", bad))

    def test_empty_password_refused(self):
        with self.assertRaises(ValueError):
            auth.hash_password("")


class TestAccountNames(unittest.TestCase):
    """Names must survive an env file, a signed cookie and Basic auth."""

    def test_accepts_ordinary_service_accounts(self):
        for name in ("tvadmin", "tvadmin", "tv_admin", "ops.tv", "svc+tv",
                     "a", "user@example", "AB-12_x.y"):
            with self.subTest(name=name):
                self.assertTrue(auth.valid_user(name))

    def test_rejects_names_an_env_file_would_mangle(self):
        for name in ("", " leading", "trailing ", "has space", "with#hash",
                     "with\nnewline", "-starts-with-dash", '"quoted"',
                     "way" + "x" * 80):
            with self.subTest(name=name):
                self.assertFalse(auth.valid_user(name))

    def test_dashed_name_round_trips_through_env_and_session(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(f"ECLER_AUTH_USER=tvadmin\n"
                  f"ECLER_AUTH_PASSWORD_HASH={auth.hash_password('long-enough-pw')}\n"
                  f"ECLER_SESSION_SECRET=stable-secret\n")
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))

        loaded = auth.load(Path(tmp.name))
        self.assertEqual(loaded.user, "tvadmin")
        self.assertTrue(loaded.check_login("tvadmin", "long-enough-pw"))
        self.assertEqual(
            loaded.read_session(loaded.issue_session("tvadmin")), "tvadmin")

    def test_unusable_name_disables_login_rather_than_half_working(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(f'ECLER_AUTH_USER=has space\n'
                  f"ECLER_AUTH_PASSWORD_HASH={auth.hash_password('long-enough-pw')}\n")
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        loaded = auth.load(Path(tmp.name))
        self.assertFalse(loaded.enabled)
        self.assertTrue(any("cannot carry" in w for w in loaded.warnings))


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.auth = auth.Auth(
            user="svc",
            password_hash=auth.hash_password("hunter2hunter2"),
            session_secret=b"a" * 32,
        )

    def test_valid_token_round_trip(self):
        token = self.auth.issue_session("svc")
        self.assertEqual(self.auth.read_session(token), "svc")

    def test_tampered_token_rejected(self):
        token = self.auth.issue_session("svc")
        payload, _, signature = token.partition(".")
        forged = f"{payload}.{'A' * len(signature)}"
        self.assertIsNone(self.auth.read_session(forged))

    def test_token_from_another_secret_rejected(self):
        other = auth.Auth(user="svc", password_hash=self.auth.password_hash,
                          session_secret=b"b" * 32)
        self.assertIsNone(self.auth.read_session(other.issue_session("svc")))

    def test_expired_token_rejected(self):
        self.assertIsNone(self.auth.read_session(
            self.auth.issue_session("svc", now=0)))

    def test_garbage_tokens_rejected(self):
        for bad in ("", "no-dot", "!!!.!!!", "." , "a.b"):
            with self.subTest(bad=bad):
                self.assertIsNone(self.auth.read_session(bad))

    def test_login_checks_both_fields(self):
        self.assertTrue(self.auth.check_login("svc", "hunter2hunter2"))
        self.assertFalse(self.auth.check_login("svc", "wrong"))
        self.assertFalse(self.auth.check_login("other", "hunter2hunter2"))

    def test_disabled_auth_never_logs_anyone_in(self):
        self.assertFalse(auth.Auth().check_login("svc", "hunter2hunter2"))
        self.assertFalse(auth.Auth().enabled)


class TestEnvFile(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        tmp.write(text); tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_parses_common_shapes(self):
        values = auth.parse_env_file(self._write(
            "# comment\n"
            "\n"
            "ECLER_AUTH_USER=tvadmin\n"
            'export ECLER_SESSION_SECRET="quoted secret"\n'
            "ECLER_SESSION_HOURS=6   # trailing comment\n"
            "MALFORMED\n"
        ))
        self.assertEqual(values["ECLER_AUTH_USER"], "tvadmin")
        self.assertEqual(values["ECLER_SESSION_SECRET"], "quoted secret")
        self.assertEqual(values["ECLER_SESSION_HOURS"], "6")
        self.assertNotIn("MALFORMED", values)

    def test_hash_beats_plaintext_and_enables_login(self):
        encoded = auth.hash_password("plaintext-is-fine")
        loaded = auth.load(self._write(
            f"ECLER_AUTH_USER=svc\nECLER_AUTH_PASSWORD_HASH={encoded}\n"
            "ECLER_SESSION_SECRET=abc\n"))
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.check_login("svc", "plaintext-is-fine"))

    def test_plaintext_password_works_but_warns(self):
        loaded = auth.load(self._write(
            "ECLER_AUTH_USER=svc\nECLER_AUTH_PASSWORD=letmein123\n"
            "ECLER_SESSION_SECRET=abc\n"))
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.check_login("svc", "letmein123"))
        self.assertTrue(any("plaintext" in w for w in loaded.warnings))

    def test_user_without_password_leaves_login_disabled(self):
        loaded = auth.load(self._write("ECLER_AUTH_USER=svc\n"))
        self.assertFalse(loaded.enabled)
        self.assertTrue(loaded.warnings)

    def test_missing_secret_warns_and_is_ephemeral(self):
        loaded = auth.load(self._write(
            f"ECLER_AUTH_USER=svc\n"
            f"ECLER_AUTH_PASSWORD_HASH={auth.hash_password('abcdefgh')}\n"))
        self.assertTrue(loaded.enabled)
        self.assertTrue(loaded.secret_is_ephemeral)
        self.assertTrue(any("SESSION_SECRET" in w for w in loaded.warnings))


class TestLoginThrottle(unittest.TestCase):
    def test_blocks_after_repeated_failures(self):
        from eclermanager.server import LoginThrottle
        throttle = LoginThrottle(max_attempts=3, window=60)
        self.assertFalse(throttle.blocked("10.0.0.1"))
        for _ in range(3):
            throttle.record_failure("10.0.0.1")
        self.assertTrue(throttle.blocked("10.0.0.1"))
        self.assertFalse(throttle.blocked("10.0.0.2"))   # per client

    def test_success_clears_the_count(self):
        from eclermanager.server import LoginThrottle
        throttle = LoginThrottle(max_attempts=2, window=60)
        throttle.record_failure("10.0.0.1")
        throttle.record_failure("10.0.0.1")
        throttle.reset("10.0.0.1")
        self.assertFalse(throttle.blocked("10.0.0.1"))

    def test_attempts_age_out(self):
        from eclermanager.server import LoginThrottle
        throttle = LoginThrottle(max_attempts=2, window=0.05)
        throttle.record_failure("10.0.0.1")
        throttle.record_failure("10.0.0.1")
        self.assertTrue(throttle.blocked("10.0.0.1"))
        time.sleep(0.08)
        self.assertFalse(throttle.blocked("10.0.0.1"))


class TestHttpAuthGate(unittest.TestCase):
    """End-to-end: the login actually gates the server."""

    PASSWORD = "a-good-long-password"

    def setUp(self):
        import base64 as _b64
        import http.cookiejar
        import threading as _threading
        import urllib.request
        from eclermanager.poller import Poller
        from eclermanager.server import make_server

        self.b64 = _b64
        self.urllib = urllib.request

        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"channels": [{"group_id": 1, "name": "A"}],
                   "receivers": [{"id": "rx-01", "ip": "192.0.2.10",
                                  "expected_group_id": 1}]}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))

        self.auth = auth.Auth(
            user="svc",
            password_hash=auth.hash_password(self.PASSWORD),
            session_secret=b"c" * 32,
        )
        poller = Poller(config_mod.load(Path(tmp.name)))
        self.server = make_server(poller, "127.0.0.1", 0, self.auth)
        self.port = self.server.server_address[1]
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)

        self.jar = http.cookiejar.CookieJar()
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect(),
        )

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path, opener=None, headers=None):
        request = self.urllib.Request(self._url(path), headers=headers or {})
        try:
            return (opener or self.browser).open(request, timeout=5)
        except self.urllib.HTTPError as exc:
            return exc

    def test_dashboard_redirects_to_login_when_signed_out(self):
        response = self._get("/")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_api_returns_401_when_signed_out(self):
        self.assertEqual(self._get("/api/state").status, 401)

    def test_health_is_reachable_without_login(self):
        response = self._get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["auth"])

    def test_login_page_is_served(self):
        response = self._get("/login")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Service account", response.read())

    def _login(self, user, password):
        from urllib.parse import urlencode
        body = urlencode({"user": user, "password": password}).encode()
        request = self.urllib.Request(self._url("/login"), data=body)
        try:
            return self.browser.open(request, timeout=5)
        except self.urllib.HTTPError as exc:
            return exc

    def test_wrong_password_does_not_sign_in(self):
        response = self._login("svc", "wrong-password")
        self.assertEqual(response.headers["Location"], "/login?error=1")
        self.assertEqual(self._get("/api/state").status, 401)

    def test_wrong_user_does_not_sign_in(self):
        self._login("other", self.PASSWORD)
        self.assertEqual(self._get("/api/state").status, 401)

    def test_correct_login_grants_access(self):
        response = self._login("svc", self.PASSWORD)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assertTrue(any(c.name == "ecler_session" for c in self.jar))

        state = self._get("/api/state")
        self.assertEqual(state.status, 200)
        self.assertEqual(len(json.loads(state.read())["devices"]), 1)

        dashboard = self._get("/")
        self.assertEqual(dashboard.status, 200)

    def test_session_cookie_is_httponly_and_samesite(self):
        self._login("svc", self.PASSWORD)
        cookie = next(c for c in self.jar if c.name == "ecler_session")
        self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))
        self.assertEqual(cookie.get_nonstandard_attr("SameSite"), "Strict")

    def test_logout_clears_the_session(self):
        self._login("svc", self.PASSWORD)
        self.assertEqual(self._get("/api/state").status, 200)
        request = self.urllib.Request(self._url("/logout"), data=b"")
        try:
            self.browser.open(request, timeout=5)
        except self.urllib.HTTPError:
            pass
        self.assertEqual(self._get("/api/state").status, 401)

    def test_basic_auth_works_for_scripting(self):
        token = self.b64.b64encode(f"svc:{self.PASSWORD}".encode()).decode()
        response = self._get("/api/state",
                             headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status, 200)

    def test_basic_auth_with_wrong_password_refused(self):
        token = self.b64.b64encode(b"svc:nope").decode()
        response = self._get("/api/state",
                             headers={"Authorization": f"Basic {token}"})
        self.assertEqual(response.status, 401)

    def test_malformed_basic_header_refused(self):
        for header in ("Basic !!!!", "Basic", "Bearer abc", "Basic " + "A" * 5):
            with self.subTest(header=header):
                response = self._get("/api/state",
                                     headers={"Authorization": header})
                self.assertEqual(response.status, 401)

    def test_forged_session_cookie_refused(self):
        forged = self.auth.issue_session("svc")[:-6] + "AAAAAA"
        response = self._get("/api/state", headers={"Cookie": f"ecler_session={forged}"})
        self.assertEqual(response.status, 401)


class _NoRedirect(__import__("urllib.request", fromlist=["HTTPRedirectHandler"]).HTTPRedirectHandler):
    """Keep 302s visible so the tests can assert on them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestRename(unittest.TestCase):
    """Renaming must persist, keep the id, and reject unusable labels."""

    def setUp(self):
        import threading as _threading
        import urllib.request
        from eclermanager.poller import Poller
        from eclermanager.server import make_server

        self.urllib = urllib.request
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"channels": [{"group_id": 1, "name": "Reception"}],
                   "receivers": [{"id": "rx-01", "name": "rx-01",
                                  "ip": "192.0.2.10",
                                  "expected_group_id": 1}]}, tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.addCleanup(lambda: self.config_path.unlink(missing_ok=True))

        self.poller = Poller(config_mod.load(self.config_path))
        self.server = make_server(self.poller, "127.0.0.1", 0, auth.Auth())
        self.port = self.server.server_address[1]
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, payload):
        request = self.urllib.Request(
            f"http://127.0.0.1:{self.port}/api/receivers/rx-01/name",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return self.urllib.urlopen(request, timeout=5)
        except self.urllib.HTTPError as exc:
            return exc

    def test_rename_persists_to_the_config_file(self):
        response = self._post({"name": "Canteen", "location": "First floor"})
        self.assertEqual(response.status, 200)
        saved = config_mod.load(self.config_path).receivers[0]
        self.assertEqual(saved.name, "Canteen")
        self.assertEqual(saved.location, "First floor")

    def test_id_is_unchanged_by_a_rename(self):
        self._post({"name": "Canteen"})
        self.assertEqual(config_mod.load(self.config_path).receivers[0].id, "rx-01")
        # and the device is still addressable under the original id
        self.assertEqual(self._post({"name": "Canteen 2"}).status, 200)

    def test_location_is_optional_and_may_be_cleared(self):
        self._post({"name": "Canteen", "location": "First floor"})
        self._post({"name": "Canteen", "location": ""})
        self.assertEqual(config_mod.load(self.config_path).receivers[0].location, "")

    def test_empty_name_refused(self):
        self._post({"name": "Keep me"})
        for bad in ("", "   ", "\t"):
            with self.subTest(bad=bad):
                self.assertEqual(self._post({"name": bad}).status, 400)
        self.assertEqual(config_mod.load(self.config_path).receivers[0].name,
                         "Keep me")

    def test_overlong_name_refused(self):
        self.assertEqual(self._post({"name": "x" * 65}).status, 400)

    def test_non_string_name_refused(self):
        for bad in (5, None, [], {}):
            with self.subTest(bad=bad):
                self.assertEqual(self._post({"name": bad}).status, 400)

    def test_missing_name_refused(self):
        self.assertEqual(self._post({"location": "nowhere"}).status, 400)

    def test_control_characters_are_stripped(self):
        """A newline would corrupt a regenerated devices.txt."""
        self._post({"name": "Can\nteen\t", "location": "A\rB"})
        saved = config_mod.load(self.config_path).receivers[0]
        self.assertEqual(saved.name, "Canteen")
        self.assertEqual(saved.location, "AB")

    def test_note_persists(self):
        """A deliberate choice must be recorded, or it looks like drift later."""
        self._post({"name": "Desk 4", "location": "Floor 1",
                    "note": "on Reception at the desk owner's request"})
        saved = config_mod.load(self.config_path).receivers[0]
        self.assertEqual(saved.note,
                         "on Reception at the desk owner's request")

    def test_note_survives_a_config_round_trip(self):
        """Regression: notes in the file used to be dropped silently on save."""
        self._post({"name": "Desk 4", "note": "keep on Reception"})
        reloaded = config_mod.load(self.config_path)
        reloaded.save()
        self.assertEqual(config_mod.load(self.config_path).receivers[0].note,
                         "keep on Reception")

    def test_note_is_optional_and_clearable(self):
        self._post({"name": "Desk 4", "note": "temporary"})
        self._post({"name": "Desk 4", "note": ""})
        self.assertEqual(config_mod.load(self.config_path).receivers[0].note, "")

    def test_rename_is_logged(self):
        self._post({"name": "Canteen"})
        kinds = [event["kind"] for event in self.poller.events]
        self.assertIn("renamed", kinds)


class TestInventoryExport(unittest.TestCase):
    """The export must round-trip back through tools/scan.py --names."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import scan
        from eclermanager.poller import Poller

        self.scan = scan
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({
            "channels": [
                {"group_id": 1, "name": "Reception",
                 "transmitter_ip": "10.0.1.1", "note": "Dashboard Reception"},
                {"group_id": 3, "name": "Canteen", "transmitter_ip": "10.0.1.3"},
            ],
            "receivers": [
                {"id": "rx-01", "name": "Canteen", "ip": "10.0.2.1",
                 "location": "First floor", "expected_group_id": 1},
                {"id": "rx-02", "name": "Reception desk", "ip": "10.0.2.2",
                 "expected_group_id": 1},
                {"id": "rx-19", "name": "Canteen screen", "ip": "10.0.2.19",
                 "expected_group_id": 3},
                {"id": "rx-07", "name": "spare", "ip": "10.0.2.7",
                 "enabled": False},
            ],
        }, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        self.poller = Poller(config_mod.load(Path(tmp.name)))

    def _round_trip(self):
        text = self.poller.inventory_text()
        out = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        out.write(text); out.close()
        self.addCleanup(lambda: Path(out.name).unlink(missing_ok=True))
        return text, self.scan.load_names(Path(out.name))

    def test_names_survive_the_round_trip(self):
        _, names = self._round_trip()
        self.assertEqual(names["10.0.2.1"], "Canteen")
        self.assertEqual(names["10.0.2.2"], "Reception desk")
        self.assertEqual(names["10.0.2.19"], "Canteen screen")

    def test_location_rides_along_as_a_comment_and_is_stripped(self):
        text, names = self._round_trip()
        self.assertIn("# First floor", text)
        self.assertEqual(names["10.0.2.1"], "Canteen")   # not "Canteen First floor"

    def test_transmitters_are_included(self):
        _, names = self._round_trip()
        self.assertEqual(names["10.0.1.1"], "tx-ch1-reception")
        self.assertEqual(names["10.0.1.3"], "tx-ch3-canteen")

    def test_grouped_by_channel_with_headings(self):
        text, _ = self._round_trip()
        self.assertIn("channel 1: Reception (2 TVs)", text)
        self.assertIn("channel 3: Canteen (1 TVs)", text)
        self.assertIn("no expected channel (1)", text)

    def test_disabled_receiver_is_marked(self):
        text, names = self._round_trip()
        self.assertIn("disabled", text)
        self.assertEqual(names["10.0.2.7"], "spare")

    def test_renaming_then_exporting_reflects_the_new_name(self):
        self.poller.set_labels("rx-02", "Warehouse north", "Loading bay")
        _, names = self._round_trip()
        self.assertEqual(names["10.0.2.2"], "Warehouse north")


class TestConfigBackupRestore(unittest.TestCase):
    """Export the whole config, and restore it without restarting."""

    def setUp(self):
        import threading as _threading
        import urllib.request
        from eclermanager.poller import Poller
        from eclermanager.server import make_server

        self.urllib = urllib.request
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({
            "channels": [{"group_id": 1, "name": "Reception"}],
            "receivers": [
                {"id": "rx-01", "name": "Canteen", "ip": "10.0.2.1",
                 "location": "First floor", "expected_group_id": 1},
                {"id": "rx-02", "name": "Reception", "ip": "10.0.2.2",
                 "expected_group_id": 1},
            ],
        }, tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.addCleanup(self._cleanup)

        self.poller = Poller(config_mod.load(self.config_path))
        self.server = make_server(self.poller, "127.0.0.1", 0, auth.Auth())
        self.port = self.server.server_address[1]
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.config_path.unlink(missing_ok=True)
        for leftover in self.config_path.parent.glob(
                self.config_path.stem + ".json.*.bak"):
            leftover.unlink(missing_ok=True)

    def _get(self, path):
        with self.urllib.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return response.status, response.read().decode(), response.headers

    def _post_config(self, payload):
        request = self.urllib.Request(
            f"http://127.0.0.1:{self.port}/api/config",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return self.urllib.urlopen(request, timeout=5)
        except self.urllib.HTTPError as exc:
            return exc

    def test_export_returns_a_complete_reimportable_config(self):
        status, body, headers = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        exported = json.loads(body)
        self.assertEqual(len(exported["receivers"]), 2)
        self.assertEqual(exported["receivers"][0]["name"], "Canteen")
        self.assertEqual(exported["receivers"][0]["location"], "First floor")
        self.assertEqual(exported["channels"][0]["name"], "Reception")
        # and it is accepted straight back
        self.assertEqual(self._post_config(exported).status, 200)

    def test_round_trip_after_renames_restores_the_names(self):
        _, body, _ = self._get("/api/config")
        good = json.loads(body)

        self.poller.set_labels("rx-01", "WRONG", "nowhere")
        self.assertEqual(config_mod.load(self.config_path).receivers[0].name,
                         "WRONG")

        self._post_config(good)
        self.assertEqual(config_mod.load(self.config_path).receivers[0].name,
                         "Canteen")
        self.assertEqual(self.poller.states["rx-01"].receiver.name, "Canteen")

    def test_import_keeps_a_timestamped_backup(self):
        _, body, _ = self._get("/api/config")
        payload = json.loads(body)
        payload["receivers"][0]["name"] = "Renamed by import"
        result = json.loads(self._post_config(payload).read())
        self.assertTrue(result["backup"])
        backup = Path(result["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(
            json.loads(backup.read_text())["receivers"][0]["name"], "Canteen")

    def test_invalid_import_changes_nothing(self):
        for bad in ({"receivers": []},
                    {"receivers": [{"id": "a", "ip": "10.0.0.1"},
                                    {"id": "a", "ip": "10.0.0.2"}]},
                    {"receivers": [{"ip": "10.0.0.1", "expected_group_id": 99}]},
                    {"channels": [{"group_id": 1, "name": "x"},
                                   {"group_id": 1, "name": "y"}],
                     "receivers": [{"ip": "10.0.0.1"}]},
                    []):
            with self.subTest(bad=bad):
                response = self._post_config(bad)
                self.assertEqual(response.status, 400)
        # untouched
        self.assertEqual(len(config_mod.load(self.config_path).receivers), 2)
        self.assertEqual(len(self.poller.states), 2)

    def test_reload_keeps_state_for_surviving_receivers(self):
        self.poller.states["rx-01"].consecutive_no_signal = 4
        _, body, _ = self._get("/api/config")
        payload = json.loads(body)
        payload["receivers"][0]["name"] = "Canteen renamed"
        self._post_config(payload)
        # same id, so the counters carry over rather than resetting
        self.assertEqual(self.poller.states["rx-01"].consecutive_no_signal, 4)
        self.assertEqual(self.poller.states["rx-01"].receiver.name,
                         "Canteen renamed")

    def test_reload_adds_and_removes_receivers(self):
        _, body, _ = self._get("/api/config")
        payload = json.loads(body)
        payload["receivers"] = [
            payload["receivers"][0],
            {"id": "rx-99", "name": "New one", "ip": "10.0.2.99",
             "expected_group_id": 1},
        ]
        self._post_config(payload)
        self.assertEqual(set(self.poller.states), {"rx-01", "rx-99"})
        kinds = [event["kind"] for event in self.poller.events]
        self.assertIn("imported", kinds)


class TestDiscovery(unittest.TestCase):
    """Finding VEO devices that are not yet in the config."""

    def setUp(self):
        from eclermanager import discovery
        from eclermanager.poller import Poller

        self.discovery = discovery
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({
            "discovery_ranges": ["10.0.2.1-4"],
            "channels": [{"group_id": 1, "name": "Reception",
                          "transmitter_ip": "10.0.1.1"}],
            "receivers": [{"id": "rx-01", "name": "Known", "ip": "10.0.2.1",
                           "expected_group_id": 1}],
        }, tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.addCleanup(lambda: self.config_path.unlink(missing_ok=True))
        self.poller = Poller(config_mod.load(self.config_path))

    def _fake_sweep(self, statuses):
        """Replace the network sweep with a fixed set of results."""
        original = self.discovery.sweep
        self.discovery.sweep = lambda hosts, **kw: statuses
        self.addCleanup(lambda: setattr(self.discovery, "sweep", original))

    @staticmethod
    def _device(host, group_id=2, **kw):
        return veo.DeviceStatus(host=host, online=True, group_id=group_id,
                                video_lock=True, fw_version="V1.01.r0", **kw)

    def test_finds_only_devices_not_in_the_config(self):
        self._fake_sweep([
            self._device("10.0.2.1"),      # already a receiver
            self._device("10.0.1.1"),      # already a transmitter
            self._device("10.0.2.2"),      # new
            self._device("10.0.2.3"),      # new
        ])
        found = self.poller.discover()
        self.assertEqual([d["ip"] for d in found], ["10.0.2.2", "10.0.2.3"])

    def test_non_veo_devices_are_ignored(self):
        """The TV VLAN carries televisions; they must not appear as receivers."""
        television = veo.DeviceStatus(host="10.0.2.9", online=True)
        self._fake_sweep([television, self._device("10.0.2.2")])
        found = self.poller.discover()
        self.assertEqual([d["ip"] for d in found], ["10.0.2.2"])

    def test_suggested_id_comes_from_the_address(self):
        self._fake_sweep([self._device("10.0.2.7")])
        found = self.poller.discover()
        self.assertEqual(found[0]["suggested_id"], "rx-07")

    def test_device_name_is_used_when_it_reports_one(self):
        self._fake_sweep([self._device("10.0.2.5", device_name="Recieve 05")])
        found = self.poller.discover()
        self.assertEqual(found[0]["suggested_name"], "Recieve 05")

    def test_no_ranges_configured_is_an_error(self):
        self.poller.config.discovery_ranges = []
        with self.assertRaisesRegex(ValueError, "no ranges"):
            self.poller.discover()

    def test_explicit_ranges_override_the_config(self):
        seen = {}
        original = self.discovery.sweep

        def spy(hosts, **kw):
            seen["hosts"] = hosts
            return []

        self.discovery.sweep = spy
        self.addCleanup(lambda: setattr(self.discovery, "sweep", original))
        self.poller.discover(["10.9.9.1-2"])
        self.assertEqual(seen["hosts"], ["10.9.9.1", "10.9.9.2"])

    def test_a_second_scan_is_refused_while_one_runs(self):
        self.poller.discovery_running = True
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.poller.discover()

    def test_running_flag_is_cleared_even_if_the_sweep_fails(self):
        original = self.discovery.sweep

        def boom(hosts, **kw):
            raise OSError("network went away")

        self.discovery.sweep = boom
        self.addCleanup(lambda: setattr(self.discovery, "sweep", original))
        with self.assertRaises(OSError):
            self.poller.discover()
        self.assertFalse(self.poller.discovery_running)

    def test_adding_persists_and_starts_polling(self):
        self._fake_sweep([self._device("10.0.2.2", group_id=3)])
        self.poller.discover()
        receiver = self.poller.add_discovered("10.0.2.2")

        self.assertEqual(receiver.id, "rx-02")
        # Adopts what it is currently showing, so it is not instantly "drifted".
        self.assertEqual(receiver.expected_group_id, 3)
        self.assertIn("rx-02", self.poller.states)
        saved = config_mod.load(self.config_path)
        self.assertEqual([r.ip for r in saved.receivers],
                         ["10.0.2.1", "10.0.2.2"])
        # and it is no longer offered as a discovery
        self.assertEqual(self.poller.discovered, [])

    def test_adding_twice_is_refused(self):
        self._fake_sweep([self._device("10.0.2.2")])
        self.poller.discover()
        self.poller.add_discovered("10.0.2.2")
        with self.assertRaisesRegex(ValueError, "already in the config"):
            self.poller.add_discovered("10.0.2.2")

    def test_adding_something_not_scanned_is_refused(self):
        with self.assertRaises(KeyError):
            self.poller.add_discovered("10.0.2.99")

    def test_id_collision_gets_a_suffix(self):
        """Two addresses ending .01 in different subnets must not collide."""
        self._fake_sweep([self._device("10.99.99.1")])
        self.poller.discover()
        receiver = self.poller.add_discovered("10.99.99.1")
        self.assertEqual(receiver.id, "rx-01-2")     # rx-01 is taken

    def test_snapshot_exposes_discovery_state(self):
        snapshot = self.poller.snapshot()
        self.assertIn("discovered", snapshot)
        self.assertEqual(snapshot["discovery"]["ranges"], ["10.0.2.1-4"])
        self.assertFalse(snapshot["discovery"]["running"])


class TestWideSweepGuard(unittest.TestCase):
    """A sweep wide enough to disturb the network must not run by accident.

    On 2026-09-09 a 65k-address sweep of 10.0.0.0/16 knocked every receiver
    off its multicast stream for about 35 seconds and timed two out entirely:
    probing addresses that do not exist makes the gateway ARP for each one.
    """

    def _poller(self):
        from eclermanager.poller import Poller
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"receivers": [{"id": "rx-01", "ip": "10.0.0.1"}]}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        poller = Poller(config_mod.load(Path(tmp.name)))
        # Never actually touch the network in these tests.
        from eclermanager import discovery
        original = discovery.sweep
        discovery.sweep = lambda hosts, **kw: []
        self.addCleanup(lambda: setattr(discovery, "sweep", original))
        return poller

    def test_a_slash_16_is_refused(self):
        poller = self._poller()
        with self.assertRaisesRegex(ValueError, "wide sweep"):
            poller.start_discovery(["10.0.0.0/16"])
        self.assertFalse(poller.discovery_running)

    def test_the_refusal_explains_why(self):
        poller = self._poller()
        with self.assertRaises(ValueError) as caught:
            poller.start_discovery(["10.0.0.0/16"])
        message = str(caught.exception)
        self.assertIn("ARP", message)
        self.assertIn("10.0.2.1-254", message)   # suggests the alternative

    def test_force_allows_it(self):
        poller = self._poller()
        addresses = poller.start_discovery(["10.0.0.0/16"], force=True)
        self.assertEqual(addresses, 65534)
        if poller._discovery_thread:
            poller._discovery_thread.join(timeout=5)

    def test_a_subnet_needs_no_force(self):
        poller = self._poller()
        self.assertEqual(poller.start_discovery(["10.0.2.1-254"]), 254)
        if poller._discovery_thread:
            poller._discovery_thread.join(timeout=5)


class TestPrescanLocking(unittest.TestCase):
    """The port sweep must serialise with polling on a per-host basis.

    Without this, a sweep can connect and disconnect in the middle of a poll,
    which leaves these units accepting TCP while answering nothing -- looking
    exactly like an unsupported command.
    """

    def test_port_open_holds_the_host_lock(self):
        import threading as _threading
        from eclermanager import discovery

        host = "192.0.2.77"
        lock = veo.host_lock(host)
        lock.acquire()
        finished = _threading.Event()

        def probe():
            discovery.port_open(host, 9999, 0.2)
            finished.set()

        thread = _threading.Thread(target=probe, daemon=True)
        thread.start()
        # Held elsewhere, so the probe must wait rather than barge in.
        self.assertFalse(finished.wait(timeout=0.6))
        lock.release()
        self.assertTrue(finished.wait(timeout=5))
        thread.join(timeout=5)


class TestIdentifyHold(unittest.TestCase):
    """Holding a screen dark, for finding it on foot.

    A six-second blink works for a screen in front of you and not for one two
    floors away, so a hold parks the receiver on an unused channel until it is
    released.  The important guarantee is that nothing else undoes it.
    """

    def _poller(self, **overrides):
        from eclermanager.poller import Poller
        payload = {
            "channels": [{"group_id": 1, "name": "Reception"},
                         {"group_id": 2, "name": "Office"}],
            "receivers": [{"id": "rx-01", "name": "rx-01", "ip": "192.0.2.10",
                           "expected_group_id": 1}],
        }
        payload.update(overrides)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        poller = Poller(config_mod.load(Path(tmp.name)))
        # Pretend it is online and on its expected channel.
        poller.states["rx-01"].status = veo.DeviceStatus(
            host="192.0.2.10", online=True, group_id=1, video_lock=True)
        # Stand in for the network: record the switch and reflect it in state.
        self.switches = []

        def fake_set_channel(receiver_id, group_id, *, source="manual"):
            self.switches.append((receiver_id, group_id, source))
            state = poller.states[receiver_id]
            state.status.group_id = group_id
            return veo.SwitchResult(host=state.receiver.ip, requested=group_id,
                                    ok=True, verified_group_id=group_id,
                                    message="ok")

        poller.set_channel = fake_set_channel
        return poller

    def test_hold_parks_it_on_a_spare_channel(self):
        poller = self._poller()
        poller.hold_identify("rx-01")
        state = poller.states["rx-01"]
        self.assertEqual(state.held_from_group_id, 1)
        self.assertEqual(state.status.group_id, 63)     # highest unused
        self.assertTrue(state.as_dict(poller.config)["held"])

    def test_a_held_receiver_is_not_reported_as_drifted(self):
        """Otherwise the walk fills the dashboard with faults that are not."""
        poller = self._poller()
        poller.hold_identify("rx-01")
        self.assertFalse(poller.states["rx-01"].drifted)
        self.assertEqual(poller.snapshot()["summary"]["drifted"], 0)
        self.assertEqual(poller.snapshot()["summary"]["held"], 1)

    def test_auto_repair_leaves_a_hold_alone(self):
        poller = self._poller(auto_repair=True, auto_repair_after_polls=1)
        poller.hold_identify("rx-01")
        before = len(self.switches)
        poller._self_heal()
        poller._self_heal()
        self.assertEqual(len(self.switches), before)

    def test_repair_all_leaves_a_hold_alone(self):
        poller = self._poller()
        poller.hold_identify("rx-01")
        before = len(self.switches)
        poller.repair_all()
        self.assertEqual(len(self.switches), before)

    def test_release_restores_the_original_channel(self):
        poller = self._poller()
        poller.hold_identify("rx-01")
        poller.release_identify("rx-01")
        state = poller.states["rx-01"]
        self.assertIsNone(state.held_from_group_id)
        self.assertEqual(state.status.group_id, 1)
        self.assertEqual(self.switches[-1][1], 1)

    def test_holding_twice_is_refused(self):
        poller = self._poller()
        poller.hold_identify("rx-01")
        with self.assertRaisesRegex(ValueError, "already being held"):
            poller.hold_identify("rx-01")

    def test_releasing_something_not_held_is_refused(self):
        poller = self._poller()
        with self.assertRaisesRegex(ValueError, "not being held"):
            poller.release_identify("rx-01")

    def test_cannot_hold_a_receiver_of_unknown_channel(self):
        """With nothing to restore, a hold would strand the screen."""
        poller = self._poller()
        poller.states["rx-01"].status = veo.DeviceStatus(
            host="192.0.2.10", online=True)
        poller.states["rx-01"].receiver.expected_group_id = None
        with self.assertRaisesRegex(ValueError, "nothing"):
            poller.hold_identify("rx-01")

    def test_release_all_restores_everything(self):
        poller = self._poller()
        poller.hold_identify("rx-01")
        results = poller.release_all_held()
        self.assertEqual(len(results), 1)
        self.assertEqual(poller.snapshot()["summary"]["held"], 0)


class TestSwitchMacDiff(unittest.TestCase):
    """Parsing pasted switch output for tools/macdiff.py."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import macdiff
        self.macdiff = macdiff

    def test_parses_per_port_queries(self):
        """The real shape: a command line naming the port, then a one-row table."""
        text = """SSH@switch-a# show mac-address eth 1/1/33
Total active entries from port 1/1/33 = 1
MAC-Address     Port                 Type         VLAN
001a.96aa.bb04  1/1/33               Dynamic      52
SSH@switch-a# show mac-address eth 1/1/34
Total active entries from port 1/1/34 = 1
MAC-Address     Port                 Type         VLAN
001a.96aa.bb03  1/1/34               Dynamic      52
"""
        pairs = self.macdiff.parse_switch_output(text)
        self.assertEqual(pairs, [("00:1a:96:aa:bb:04", "1/1/33"),
                                 ("00:1a:96:aa:bb:03", "1/1/34")])

    def test_accepts_colon_and_bare_notations(self):
        text = ("00:1a:96:aa:bb:06  1/1/28  Dynamic 52\n"
                "001a96aabb05       1/1/36  Dynamic 52\n")
        pairs = self.macdiff.parse_switch_output(text)
        self.assertEqual([m for m, _ in pairs],
                         ["00:1a:96:aa:bb:06", "00:1a:96:aa:bb:05"])

    def test_deduplicates_but_keeps_distinct_ports(self):
        text = ("001a.96aa.bb04 1/1/33 Dynamic 52\n"
                "001a.96aa.bb04 1/1/33 Dynamic 52\n"
                "001a.96aa.bb04 1/1/40 Dynamic 52\n")
        self.assertEqual(len(self.macdiff.parse_switch_output(text)), 2)

    def test_ignores_prose_and_prompts(self):
        text = ("SSH@switch-a# show version\n"
                "Copyright 2024, some vendor\n"
                "Total active entries from port 1/1/33 = 1\n")
        self.assertEqual(self.macdiff.parse_switch_output(text), [])


class TestMacSniffParsing(unittest.TestCase):
    """Frame parsing for tools/sniffmac.py.

    This finds the IP of a device known only by MAC -- needed because multicast
    is delivered at layer 2, so a receiver holding an address from the wrong
    subnet still displays its channel while being invisible to an IP scan.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import sniffmac
        self.sniffmac = sniffmac

    def test_normalises_every_mac_notation(self):
        for text in ("001a.96aa.bb04", "00:1a:96:aa:bb:04", "001A96AABB04",
                     "00-1a-96-aa-bb-04"):
            with self.subTest(text=text):
                self.assertEqual(self.sniffmac.normalise_mac(text),
                                 "00:1a:96:aa:bb:04")

    def _ethernet(self, src_mac, ethertype, payload):
        return (bytes.fromhex("ffffffffffff") + bytes.fromhex(src_mac)
                + struct.pack("!H", ethertype) + payload)

    def _arp(self, mac, ip, opcode=1):
        return (struct.pack("!HHBBH", 1, 0x0800, 6, 4, opcode)
                + bytes.fromhex(mac) + socket.inet_aton(ip)
                + b"\x00" * 6 + socket.inet_aton("192.168.1.1"))

    def _ipv4(self, src_ip, protocol):
        return (bytes([0x45, 0, 0, 32, 0, 0, 0, 0, 1, protocol, 0, 0])
                + socket.inet_aton(src_ip) + socket.inet_aton("224.0.0.1"))

    def test_arp_reveals_an_off_subnet_address(self):
        """The factory default, which an IP scan of 10.0.x.x cannot reach."""
        frame = self._ethernet("001a96aabb04", 0x0806,
                               self._arp("001a96aabb04", "192.168.1.12"))
        self.assertEqual(self.sniffmac.parse(frame),
                         ("00:1a:96:aa:bb:04", "192.168.1.12", "ARP request"))

    def test_arp_reply_is_labelled_as_such(self):
        frame = self._ethernet("001a96aabb04", 0x0806,
                               self._arp("001a96aabb04", "10.0.2.9", opcode=2))
        self.assertEqual(self.sniffmac.parse(frame)[2], "ARP reply")

    def test_igmp_join_reveals_the_address(self):
        frame = self._ethernet("001a96aabb01", 0x0800,
                               self._ipv4("10.0.2.5", 2))
        self.assertEqual(self.sniffmac.parse(frame),
                         ("00:1a:96:aa:bb:01", "10.0.2.5", "IGMP join"))

    def test_dhcp_discover_means_no_static_address(self):
        frame = self._ethernet("001a96aabb03", 0x0800,
                               self._ipv4("0.0.0.0", 17))
        mac, ip, how = self.sniffmac.parse(frame)
        self.assertEqual(ip, "0.0.0.0")
        self.assertIn("DHCP", how)

    def test_arp_probe_with_no_address_yet(self):
        frame = self._ethernet("001a96aabb04", 0x0806,
                               self._arp("001a96aabb04", "0.0.0.0"))
        self.assertEqual(self.sniffmac.parse(frame)[1], "0.0.0.0")

    def test_vlan_tagged_frames_are_handled(self):
        inner = struct.pack("!H", 52) + struct.pack("!H", 0x0806)
        frame = (bytes.fromhex("ffffffffffff") + bytes.fromhex("001a96aabb04")
                 + struct.pack("!H", 0x8100) + inner
                 + self._arp("001a96aabb04", "192.168.1.12"))
        self.assertEqual(self.sniffmac.parse(frame)[1], "192.168.1.12")

    def test_truncated_and_irrelevant_frames_are_ignored(self):
        self.assertIsNone(self.sniffmac.parse(b"short"))
        self.assertIsNone(self.sniffmac.parse(
            self._ethernet("001a96aabb04", 0x86dd, b"\x00" * 40)))   # IPv6
        self.assertIsNone(self.sniffmac.parse(
            self._ethernet("001a96aabb04", 0x0806, b"\x00" * 4)))    # short ARP


class TestArpScanFrames(unittest.TestCase):
    """Frame building and parsing for tools/arpscan.py.

    ARP is the probe of last resort for a receiver that displays its channel
    but never initiates traffic: it answers an ARP for its own address anyway,
    and ARP is link-local so the target's subnet does not matter.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import arpscan
        self.arpscan = arpscan
        self.src = bytes.fromhex("001122ccdd01")

    def test_request_is_a_valid_42_byte_arp_frame(self):
        frame = self.arpscan.build_request(self.src, "0.0.0.0", "192.168.1.12")
        self.assertEqual(len(frame), 42)
        self.assertEqual(frame[:6], b"\xff" * 6)                # broadcast
        self.assertEqual(frame[6:12], self.src)
        self.assertEqual(struct.unpack("!H", frame[12:14])[0], 0x0806)
        self.assertEqual(struct.unpack("!H", frame[20:22])[0], 1)  # request
        self.assertEqual(socket.inet_ntoa(frame[38:42]), "192.168.1.12")

    def test_parses_a_reply_from_any_subnet(self):
        arp = (struct.pack("!HHBBH", 1, 0x0800, 6, 4, 2)
               + bytes.fromhex("001a96aabb01")
               + socket.inet_aton("192.168.0.77")
               + self.src + socket.inet_aton("0.0.0.0"))
        frame = (self.src + bytes.fromhex("001a96aabb01")
                 + struct.pack("!H", 0x0806) + arp)
        self.assertEqual(self.arpscan.parse_reply(frame),
                         ("00:1a:96:aa:bb:01", "192.168.0.77"))

    def test_requests_and_other_traffic_are_not_mistaken_for_replies(self):
        request = self.arpscan.build_request(self.src, "0.0.0.0", "10.0.0.1")
        self.assertIsNone(self.arpscan.parse_reply(request))
        self.assertIsNone(self.arpscan.parse_reply(b"\x00" * 60))
        self.assertIsNone(self.arpscan.parse_reply(b"short"))

    def test_mac_normalisation_matches_switch_notation(self):
        self.assertEqual(self.arpscan.normalise_mac("001a.96aa.bb01"),
                         "00:1a:96:aa:bb:01")


class TestLfOnlyDevice(unittest.TestCase):
    """Some units reject the documented CRLF and need LF alone.

    Observed on 10.0.2.23 and .32: sending CRLF makes them close the
    connection ("Broken pipe"), while LF works normally.  Ecler's manual
    specifies CRLF and 27 units accept it, so CRLF stays the default and LF is
    the fallback -- learned per host so later polls go straight to it.
    """

    def setUp(self):
        import threading as _threading

        veo.forget_line_endings()
        self.addCleanup(veo.forget_line_endings)

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self._stop = _threading.Event()
        self.crlf_attempts = 0

        def serve():
            while not self._stop.is_set():
                try:
                    conn, _ = self.server.accept()
                except OSError:
                    return
                _threading.Thread(target=self._handle, args=(conn,),
                                  daemon=True).start()

        _threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _handle(self, conn):
        banner = (b"=" * 30 + b"\r\n" + b"========IPTV RX Server========"
                  + b"\r\n" + b"=" * 30 + b"\r\n")
        prompt = b"input>"
        answers = {"get_group_id": b"2", "get_video_lock": b"Lock",
                   "get_device_name": b"TV_OFFICE_23",
                   "get_fw_version": b"Rx Firmware     : V1.01.r0",
                   "get_lan_status": b"Link_Up",
                   "get_mac_address": b"00:1a:96:aa:bb:01"}
        conn.settimeout(10)
        try:
            conn.sendall(banner + prompt)
            buffer = b""
            while True:
                chunk = conn.recv(256)
                if not chunk:
                    return
                buffer += chunk
                # The behaviour being emulated: a CR anywhere is fatal.
                if b"\r" in buffer:
                    self.crlf_attempts += 1
                    conn.close()
                    return
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    command = line.decode().strip()
                    if command == "exit":
                        return
                    if command.startswith("set_group_id "):
                        answers["get_group_id"] = command.split()[1].encode()
                        reply = b"OK"
                    else:
                        reply = answers.get(command, b"unknown command")
                    conn.sendall(command.encode() + b"\n" + reply + b"\r\n"
                                 + prompt)
        except OSError:
            return
        finally:
            conn.close()

    def _shutdown(self):
        self._stop.set()
        try:
            self.server.close()
        except OSError:
            pass

    def test_status_is_read_despite_crlf_being_rejected(self):
        status = veo.read_status("127.0.0.1", port=self.port, timeout=3.0,
                                 with_details=True)
        self.assertTrue(status.online, msg=status.error)
        self.assertEqual(status.group_id, 2)
        self.assertIs(status.video_lock, True)
        self.assertEqual(status.device_name, "TV_OFFICE_23")
        self.assertEqual(status.mac_address, "00:1a:96:aa:bb:01")
        self.assertGreaterEqual(self.crlf_attempts, 1)   # it did try CRLF first

    def test_the_working_ending_is_remembered(self):
        veo.read_status("127.0.0.1", port=self.port, timeout=3.0)
        self.assertEqual(veo.line_ending_for("127.0.0.1"), veo.LF)
        before = self.crlf_attempts
        # A second poll must not waste a failed CRLF session.
        status = veo.read_status("127.0.0.1", port=self.port, timeout=3.0)
        self.assertEqual(status.group_id, 2)
        self.assertEqual(self.crlf_attempts, before)

    def test_the_raw_view_says_why(self):
        status = veo.read_status("127.0.0.1", port=self.port, timeout=3.0)
        self.assertIn("<line-ending>", status.raw)
        self.assertIn("LF", status.raw["<line-ending>"])

    def test_switching_channel_works_on_such_a_device(self):
        result = veo.set_group_id("127.0.0.1", 4, port=self.port, timeout=3.0,
                                  verify_delay=0.1)
        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.verified_group_id, 4)
        self.assertEqual(veo.line_ending_for("127.0.0.1"), veo.LF)

    def test_crlf_devices_are_unaffected(self):
        """The 27 working units must keep using the documented ending."""
        veo.forget_line_endings()
        self.assertEqual(veo.line_ending_for("192.0.2.50"), veo.CRLF)


class TestDeviceSettingsFromTheDashboard(unittest.TestCase):
    """Writing settings into the device, not just the dashboard's labels."""

    def setUp(self):
        import threading as _threading
        import urllib.request
        from eclermanager.poller import Poller
        from eclermanager.server import make_server

        self.urllib = urllib.request
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({
            "channels": [{"group_id": 2, "name": "Office"}],
            "receivers": [
                {"id": "rx-11", "name": "Canteen", "ip": "192.0.2.11",
                 "expected_group_id": 2},
                {"id": "rx-12", "name": "Other", "ip": "192.0.2.12",
                 "expected_group_id": 2},
            ],
        }, tmp)
        tmp.close()
        self.config_path = Path(tmp.name)
        self.addCleanup(lambda: self.config_path.unlink(missing_ok=True))

        self.poller = Poller(config_mod.load(self.config_path))
        self.server = make_server(self.poller, "127.0.0.1", 0, auth.Auth())
        self.port = self.server.server_address[1]
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, payload):
        request = self.urllib.Request(
            f"http://127.0.0.1:{self.port}/api/receivers/{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return self.urllib.urlopen(request, timeout=20)
        except self.urllib.HTTPError as exc:
            return exc

    def test_device_name_with_spaces_is_refused(self):
        """set_device_name stops at whitespace, so this must not be attempted."""
        response = self._post("rx-11/device-name", {"name": "TV RECEIVER 11"})
        self.assertEqual(response.status, 502)
        body = json.loads(response.read())
        self.assertIn("spaces", body["message"])
        self.assertIn("TV_RECEIVER_11", body["message"])

    def test_empty_device_name_is_refused(self):
        self.assertEqual(self._post("rx-11/device-name", {"name": "  "}).status,
                         400)

    def test_address_must_be_valid_ipv4(self):
        for payload in ({"ip": "nope", "netmask": "255.255.0.0",
                         "gateway": "10.0.0.1"},
                        {"ip": "10.0.2.11", "netmask": "nope",
                         "gateway": "10.0.0.1"},
                        {"ip": "10.0.2.11", "netmask": "255.255.0.0"}):
            with self.subTest(payload=payload):
                self.assertEqual(self._post("rx-11/address", payload).status, 400)

    def test_gateway_outside_the_subnet_is_refused(self):
        """Otherwise the device is left with no usable gateway."""
        response = self._post("rx-11/address", {
            "ip": "10.0.2.11", "netmask": "255.255.255.0",
            "gateway": "10.0.0.1"})
        self.assertEqual(response.status, 400)
        self.assertIn("outside", json.loads(response.read())["error"])

    def test_moving_to_an_address_another_receiver_uses_is_refused(self):
        response = self._post("rx-11/address", {
            "ip": "192.0.2.12", "netmask": "255.255.255.0",
            "gateway": "192.0.2.1"})
        self.assertEqual(response.status, 502)
        self.assertIn("already used", json.loads(response.read())["message"])

    def test_moving_to_its_own_address_is_a_no_op(self):
        response = self._post("rx-11/address", {
            "ip": "192.0.2.11", "netmask": "255.255.255.0",
            "gateway": "192.0.2.1"})
        self.assertEqual(response.status, 502)
        self.assertIn("already at", json.loads(response.read())["message"])

    def test_a_failed_move_leaves_the_config_alone(self):
        """The config must never point at an address nothing answers on."""
        response = self._post("rx-11/address", {
            "ip": "192.0.2.99", "netmask": "255.255.255.0",
            "gateway": "192.0.2.1"})
        self.assertEqual(response.status, 502)
        self.assertEqual(config_mod.load(self.config_path).receivers[0].ip,
                         "192.0.2.11")


class TestChannelTallies(unittest.TestCase):
    """Assigned and actual channel counts are different questions.

    Groups in the dashboard are keyed on the *expected* channel, so a heading
    reading "10 TVs" means ten receivers are assigned to it -- not that ten are
    showing it.  The snapshot reports both so the distinction is visible.
    """

    def _poller(self, receivers):
        from eclermanager.poller import Poller
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"channels": [{"group_id": 1, "name": "Reception"},
                                {"group_id": 2, "name": "Office"}],
                   "receivers": receivers}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Poller(config_mod.load(Path(tmp.name)))

    def test_tallies_separate_assigned_from_actual(self):
        poller = self._poller([
            {"id": "a", "ip": "10.0.0.1", "expected_group_id": 2},
            {"id": "b", "ip": "10.0.0.2", "expected_group_id": 2},
            {"id": "c", "ip": "10.0.0.3", "expected_group_id": 1},
        ])
        # Two assigned to Office, but one of them has drifted to Reception.
        poller.states["a"].status = veo.DeviceStatus(
            host="10.0.0.1", online=True, group_id=2)
        poller.states["b"].status = veo.DeviceStatus(
            host="10.0.0.2", online=True, group_id=1)
        poller.states["c"].status = veo.DeviceStatus(
            host="10.0.0.3", online=True, group_id=1)

        summary = poller.snapshot()["summary"]
        self.assertEqual(summary["by_expected_channel"], {"2": 2, "1": 1})
        self.assertEqual(summary["by_actual_channel"], {"2": 1, "1": 2})
        self.assertEqual(summary["drifted"], 1)

    def test_unreachable_receivers_tally_as_unknown(self):
        poller = self._poller([{"id": "a", "ip": "10.0.0.1",
                                "expected_group_id": 2}])
        summary = poller.snapshot()["summary"]
        self.assertEqual(summary["by_actual_channel"], {"unknown": 1})
        self.assertEqual(summary["by_expected_channel"], {"2": 1})


class TestNaturalSort(unittest.TestCase):
    """Sorting must survive any name a person can type into the rename box."""

    def _names(self, names):
        from eclermanager.poller import _sort_key
        receivers = [config_mod.Receiver(id=f"rx-{i}", name=name, ip="10.0.2.1")
                     for i, name in enumerate(names)]
        return [r.name for r in sorted(receivers, key=_sort_key)]

    def test_a_name_starting_with_a_digit_does_not_crash(self):
        """This took the whole dashboard down, permanently.

        Mixing int and str in the sort tuple raised TypeError out of sorted(),
        so /api/state returned 500 and the page showed nothing. The rename was
        saved to config.json first, so it survived restarts and could only be
        undone by editing the file by hand.
        """
        self._names(["2nd floor canteen", "Canteen", "Reception"])

    def test_numbers_still_sort_naturally(self):
        self.assertEqual(self._names(["TV 10", "TV 2", "TV 1"]),
                         ["TV 1", "TV 2", "TV 10"])

    def test_mixed_names_are_ordered_deterministically(self):
        first = self._names(["3 Gifts", "Aisle 1", "10 Bench", "Canteen"])
        second = self._names(["Canteen", "10 Bench", "Aisle 1", "3 Gifts"])
        self.assertEqual(first, second)


class TestScheduledDiscovery(unittest.TestCase):
    """Periodic scanning, when an interval is configured."""

    def _poller(self, **overrides):
        from eclermanager.poller import Poller
        payload = {"receivers": [{"id": "rx-01", "ip": "10.0.0.1"}]}
        payload.update(overrides)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(payload, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Poller(config_mod.load(Path(tmp.name)))

    def test_disabled_by_default(self):
        poller = self._poller(discovery_ranges=["10.0.0.1-2"])
        calls = []
        poller.discover = lambda *a, **k: calls.append(1)
        poller._maybe_discover()
        self.assertEqual(calls, [])

    def test_needs_ranges_as_well_as_an_interval(self):
        poller = self._poller(discovery_interval_hours=1)
        calls = []
        poller.discover = lambda *a, **k: calls.append(1)
        poller._maybe_discover()
        self.assertEqual(calls, [])

    def test_does_not_run_at_startup(self):
        """The interval is counted from start, not from 1970.

        last_discovery begins as None and is never persisted, so treating it
        as 0 made a full sweep due the moment the service started -- and again
        after every restart. A wide sweep once knocked every receiver in the
        building off its multicast stream for about 35 seconds.
        """
        poller = self._poller(discovery_ranges=["10.0.0.1-2"],
                              discovery_interval_hours=1)
        calls = []
        poller.start_discovery = lambda *a, **k: calls.append(1)
        poller._maybe_discover()
        self.assertEqual(calls, [])

    def test_runs_once_the_interval_has_passed(self):
        poller = self._poller(discovery_ranges=["10.0.0.1-2"],
                              discovery_interval_hours=1)
        calls = []
        poller.start_discovery = lambda *a, **k: calls.append(1)
        poller._started_at = time.time() - 3700
        poller._maybe_discover()
        self.assertEqual(len(calls), 1)

    def test_goes_through_the_guarded_entry_point(self):
        """The manual path refuses a sweep over 8192 addresses; so must this.

        The scheduled path called discover() directly and skipped the check
        entirely, so a range the dashboard would refuse was swept unattended.
        """
        poller = self._poller(discovery_ranges=["10.0.0.1-2"],
                              discovery_interval_hours=1)
        poller._started_at = time.time() - 3700
        refused = []
        def refuse(*a, **k):
            refused.append(1)
            raise ValueError("too many addresses")
        poller.start_discovery = refuse
        poller._maybe_discover()          # must not propagate
        self.assertEqual(len(refused), 1)

    def test_does_not_run_again_before_the_interval(self):
        poller = self._poller(discovery_ranges=["10.0.0.1-2"],
                              discovery_interval_hours=1)
        calls = []
        poller.start_discovery = lambda *a, **k: calls.append(1)
        poller.last_discovery = time.time()
        poller._maybe_discover()
        self.assertEqual(calls, [])


class TestHttpWithoutAuth(unittest.TestCase):
    """With no credentials configured, nothing is gated."""

    def setUp(self):
        import threading as _threading
        import urllib.request
        from eclermanager.poller import Poller
        from eclermanager.server import make_server

        self.urllib = urllib.request
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"receivers": [{"id": "rx-01", "ip": "192.0.2.10"}]}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        poller = Poller(config_mod.load(Path(tmp.name)))
        self.server = make_server(poller, "127.0.0.1", 0, auth.Auth())
        self.port = self.server.server_address[1]
        _threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_dashboard_and_api_are_open(self):
        for path in ("/", "/api/state"):
            with self.subTest(path=path):
                with self.urllib.urlopen(
                        f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                    self.assertEqual(response.status, 200)


class TestDashboardSmoke(unittest.TestCase):
    """Run the dashboard's JavaScript, so a runtime error cannot ship.

    `node --check` proves only that the file parses.  It cannot see a function
    that is called and never declared -- which is how a dashboard that parses
    cleanly threw on its first render and showed nothing but an error toast.
    """

    def test_dashboard_javascript_runs(self):
        import shutil
        import subprocess

        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        script = Path(__file__).resolve().parent / "smoke_dashboard.js"
        result = subprocess.run(["node", str(script)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
