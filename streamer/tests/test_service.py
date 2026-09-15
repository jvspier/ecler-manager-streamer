"""Tests for the streamer's config, runner and web service."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eclerstreamer import config as config_mod  # noqa: E402
from eclerstreamer import control  # noqa: E402
from eclerstreamer.server import make_server  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_multicast_matches_the_observed_mapping(self):
        # Confirmed against the switch's IGMP tables for channels 1 and 2.
        self.assertEqual(config_mod.multicast_for(1), "239.255.42.43")
        self.assertEqual(config_mod.multicast_for(2), "239.255.42.44")
        self.assertEqual(config_mod.multicast_for(5), "239.255.42.47")

    def test_display_is_derived_from_the_channel(self):
        dash = config_mod.Dashboard(channel=5)
        self.assertEqual(dash.display_name, ":105")
        self.assertEqual(config_mod.Dashboard(channel=5, display=99).display_name,
                         ":99")

    def test_duplicate_channels_are_refused(self):
        with self.assertRaises(ValueError):
            config_mod.parse({"dashboards": [{"channel": 5}, {"channel": 5}]})

    def test_channel_range_is_enforced(self):
        with self.assertRaises(ValueError):
            config_mod.parse({"dashboards": [{"channel": 64}]})

    def test_bad_json_names_the_file_and_the_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json")
            with self.assertRaises(ValueError) as caught:
                config_mod.load(path)
            self.assertIn(str(path), str(caught.exception))

    def test_save_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = config_mod.Config(path=path, local_addr="10.52.21.200")
            cfg.dashboards.append(config_mod.Dashboard(
                channel=5, name="Campus", url="https://x/", bitrate="4M"))
            cfg.save()
            again = config_mod.load(path)
            self.assertEqual(again.local_addr, "10.52.21.200")
            self.assertEqual(again.dashboards[0].bitrate, "4M")
            self.assertEqual(again.dashboards[0].name, "Campus")


class TestControl(unittest.TestCase):
    def test_unit_name(self):
        self.assertEqual(control.unit_for(5), "dashboard-stream@5.service")

    def test_only_known_verbs_are_accepted(self):
        """The sudo rule allows five verbs; nothing else should reach it."""
        with self.assertRaises(ValueError):
            control.act(5, "mask")

    def test_enable_is_allowed_so_a_stream_survives_a_reboot(self):
        self.assertIn("enable", control.ALLOWED)
        self.assertIn("disable", control.ALLOWED)

    def test_status_reports_boot_state_separately_from_running(self):
        """Running now and starting at boot are different questions."""
        self.assertIn("at_boot", control.status(5))


class TestRunner(unittest.TestCase):
    """stream.py turns a config entry into the proven teststream command."""

    def _dry_run(self, dashboards, **top):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"dashboards": dashboards, **top}))
            done = subprocess.run(
                [sys.executable, str(ROOT / "stream.py"), "--channel", "5",
                 "--config", str(path), "--dry-run"],
                capture_output=True, text=True, check=False)
            return done

    def test_builds_the_proven_command(self):
        done = self._dry_run(
            [{"channel": 5, "url": "https://x/", "enabled": True}],
            local_addr="10.52.21.200")
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout
        # The settings that were expensive to find: constant rate, the quality
        # floor that keeps keyframes inside the declared level, no B-frames,
        # and the egress address without which multicast leaves by the wrong leg.
        self.assertIn("--group 239.255.42.47", out)
        self.assertIn("--qmin 18", out)
        self.assertIn("--no-bframes", out)
        self.assertIn("--local-addr 10.52.21.200", out)
        self.assertIn("--display :105", out)

    def test_refuses_a_channel_with_no_url(self):
        done = self._dry_run([{"channel": 5, "enabled": True}])
        self.assertEqual(done.returncode, 2)
        self.assertIn("no URL", done.stderr)

    def test_refuses_a_disabled_channel(self):
        """Exiting 0 would read to systemd as a job well done."""
        done = self._dry_run([{"channel": 5, "url": "https://x/", "enabled": False}])
        self.assertEqual(done.returncode, 2)
        self.assertIn("disabled", done.stderr)


class TestUnitFiles(unittest.TestCase):
    """Directives that quietly break the service, pinned so they stay gone."""

    def _unit(self, name: str) -> str:
        return (ROOT / "deploy" / name).read_text()

    def test_web_service_does_not_set_nonewprivileges(self):
        """It and sudo are mutually exclusive: sudo works by gaining privilege."""
        body = "\n".join(line for line in self._unit("eclerstreamer.service").splitlines()
                          if not line.lstrip().startswith("#"))
        self.assertNotIn("NoNewPrivileges", body)

    def test_web_service_can_write_its_own_config(self):
        """ProtectSystem=full mounts /etc read-only; the service rewrites it."""
        unit = self._unit("eclerstreamer.service")
        self.assertIn("ProtectSystem=full", unit)
        self.assertIn("ReadWritePaths=/etc/eclerstreamer", unit)

    def test_misconfiguration_does_not_restart_for_ever(self):
        """stream.py exits 2 for what a restart cannot fix."""
        self.assertIn("RestartPreventExitStatus=2",
                      self._unit("dashboard-stream@.service"))


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps({
            "local_addr": "10.52.21.200",
            "dashboards": [{"channel": 5, "name": "Campus",
                            "url": "https://x/", "enabled": True}]}))
        self.server = make_server(self.path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_state_lists_dashboards_with_their_addresses(self):
        state = self._get("/api/state")
        self.assertEqual(len(state["dashboards"]), 1)
        self.assertEqual(state["dashboards"][0]["multicast"], "239.255.42.47")
        self.assertEqual(state["dashboards"][0]["display_name"], ":105")

    def test_editing_a_url_persists(self):
        self._post("/api/dashboards/5", {"url": "https://new/"})
        self.assertEqual(config_mod.load(self.path).dashboard(5).url,
                         "https://new/")

    def test_a_new_dashboard_starts_disabled(self):
        """Never start something nobody has looked at yet."""
        self._post("/api/dashboards", {"channel": 7, "url": "https://y/"})
        self.assertFalse(config_mod.load(self.path).dashboard(7).enabled)

    def test_duplicate_channel_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/dashboards", {"channel": 5})
        self.assertEqual(caught.exception.code, 409)

    def test_host_settings_are_writable(self):
        """local_addr is the commonest silent failure; it belongs in the UI."""
        self._post("/api/config", {"local_addr": "10.52.21.201", "qmin": 20})
        cfg = config_mod.load(self.path)
        self.assertEqual(cfg.local_addr, "10.52.21.201")
        self.assertEqual(cfg.qmin, 20)

    def test_unknown_channel_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/dashboards/9", {"url": "https://z/"})
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
