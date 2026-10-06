"""Tests for the streaming tools.

These cover the parts of teststream.py that were expensive to learn and are
easy to undo by accident: the pacing rules, the constant-rate transport
stream, the quality floor, and the pre-flight checks.

    python3 -m unittest discover -s streamer/tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


class TestStreamPacing(unittest.TestCase):
    """-re belongs on a generated input and nowhere near a live capture.

    Getting this wrong does not fail loudly: the encoder still reports
    speed=1.0x and the test pattern still animates, but frames reach the
    receiver unevenly and animation on a real page judders. It cost a round
    trip to a television to spot, so it is pinned here.
    """

    @staticmethod
    def _build(argv):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import teststream

        args = teststream.build_parser().parse_args(argv)
        # main() settles where the pixels come from before calling
        # build_command; mirror just that one assignment.
        args.capture_display = args.from_display
        return teststream.build_command(args)

    def test_live_capture_is_not_paced_with_re(self):
        cmd = self._build(["--group", "239.255.42.47",
                           "--from-display", ":99"])
        self.assertIn("x11grab", cmd)
        self.assertNotIn("-re", cmd)

    def test_generated_pattern_is_paced_with_re(self):
        cmd = self._build(["--group", "239.255.42.47"])
        self.assertIn("-re", cmd)
        # and it pages the generator, not the encoder: -re precedes -i
        self.assertLess(cmd.index("-re"), cmd.index("-i"))

    def test_a_static_page_is_padded_to_a_constant_rate(self):
        """Hardware decoders show artifacts when an idle stream bursts."""
        cmd = self._build(["--group", "239.255.42.47",
                           "--from-display", ":99", "--bitrate", "10M"])
        self.assertEqual(cmd[cmd.index("-minrate") + 1], "10M")
        self.assertIn("nal-hrd=cbr:force-cfr=1", cmd)
        # The transport stream is padded too, a little above the video rate.
        muxrate = int(cmd[cmd.index("-muxrate") + 1])
        self.assertGreater(muxrate, 10_000_000)
        self.assertLess(muxrate, 13_000_000)

    def test_vbr_opts_out_of_the_padding(self):
        cmd = self._build(["--group", "239.255.42.47",
                           "--from-display", ":99", "--vbr"])
        self.assertNotIn("-minrate", cmd)
        self.assertEqual(cmd[cmd.index("-muxrate") + 1], "0")

    def test_a_dead_display_is_refused_before_ffmpeg_runs(self):
        """The raw failure is unreadable, so catch it early."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import teststream

        # :77 has no socket under /tmp/.X11-unix, so it is refused.
        self.assertFalse(teststream.display_is_live(":77"))
        # A remote or otherwise non-numeric display is not ours to judge by a
        # local socket, so it is allowed through to ffmpeg.
        self.assertTrue(teststream.display_is_live("host:0"))

    def test_a_quality_floor_keeps_keyframes_within_the_declared_level(self):
        """Oversized keyframes tore photographic slides on real hardware."""
        cmd = self._build(["--group", "239.255.42.47", "--from-display", ":99"])
        self.assertEqual(cmd[cmd.index("-qmin") + 1], "18")
        # and the level it has to stay inside is still declared
        self.assertEqual(cmd[cmd.index("-level") + 1], "4.0")

    def test_qmin_zero_disables_the_floor(self):
        cmd = self._build(["--group", "239.255.42.47", "--from-display", ":99",
                           "--qmin", "0"])
        self.assertNotIn("-qmin", cmd)

    def test_output_is_paced_a_little_above_the_mux_rate(self):
        """-muxrate paces the stream's timestamps, not the bytes on the wire."""
        cmd = self._build(["--group", "239.255.42.47", "--from-display", ":99",
                           "--bitrate", "6M"])
        url = cmd[-1]
        muxrate = int(cmd[cmd.index("-muxrate") + 1])
        paced = int(url.split("bitrate=")[-1].split("&")[0])
        self.assertGreater(paced, muxrate)          # never the constraint
        self.assertLess(paced, muxrate * 2)         # but still pacing
        self.assertIn("burst_bits=", url)

    def test_no_pacing_opts_out(self):
        cmd = self._build(["--group", "239.255.42.47", "--from-display", ":99",
                           "--no-pacing"])
        self.assertNotIn("burst_bits", cmd[-1])

    def test_progress_is_journal_friendly_when_not_on_a_terminal(self):
        """ffmpeg's redrawn status line reaches journald as one blob."""
        import io
        import teststream
        real = sys.stdout
        try:
            sys.stdout = io.StringIO()          # not a tty
            cmd = self._build(["--group", "239.255.42.47",
                               "--from-display", ":99"])
        finally:
            sys.stdout = real
        self.assertIn("-nostats", cmd)
        self.assertEqual(cmd[cmd.index("-progress") + 1], "pipe:1")

    def test_a_terminal_keeps_the_live_status_line(self):
        import io
        import teststream

        class Tty(io.StringIO):
            def isatty(self):
                return True

        real = sys.stdout
        try:
            sys.stdout = Tty()
            cmd = self._build(["--group", "239.255.42.47",
                               "--from-display", ":99"])
        finally:
            sys.stdout = real
        self.assertNotIn("-progress", cmd)
        self.assertNotIn("-nostats", cmd)

    def test_parse_bitrate_suffixes(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import teststream

        for text, expected in [("10M", 10_000_000), ("6m", 6_000_000),
                               ("800k", 800_000), ("1500000", 1_500_000),
                               ("2.5M", 2_500_000)]:
            with self.subTest(text=text):
                self.assertEqual(teststream.parse_bitrate(text), expected)

    def test_pointer_is_excluded_from_a_live_capture(self):
        cmd = self._build(["--group", "239.255.42.47",
                           "--from-display", ":99"])
        self.assertEqual(cmd[cmd.index("-draw_mouse") + 1], "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMimicVeo(unittest.TestCase):
    """--mimic-veo copies what ffprobe shows of a VEO-XTI1C stream, to find
    out whether one of those differences is what sets a receiver's lock."""

    _build = staticmethod(TestStreamPacing._build)

    def _after(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_everything_ffprobe_showed_is_copied(self):
        cmd = self._build(["--group", "239.255.42.51", "--mimic-veo"])
        self.assertEqual(self._after(cmd, "-profile:v"), "baseline")
        self.assertEqual(self._after(cmd, "-c:a"), "libmp3lame")
        # below 32 kHz, so ffmpeg declares stream type 0x04 like the hardware
        self.assertEqual(self._after(cmd, "-ar"), "24000")
        self.assertEqual(self._after(cmd, "-mpegts_service_id"), "256")
        self.assertEqual(self._after(cmd, "-mpegts_start_pid"), "2001")
        self.assertEqual(self._after(cmd, "-mpegts_transport_stream_id"), "128")
        self.assertIn("service_name=AIR_CH_521_6M", cmd)
        # the options belong to the muxer, so before -f mpegts
        self.assertLess(cmd.index("-mpegts_service_id"), cmd.index("mpegts"))

    def test_default_stream_is_unchanged(self):
        cmd = self._build(["--group", "239.255.42.51"])
        self.assertNotIn("-mpegts_service_id", cmd)
        self.assertIn("-an", cmd)
        self.assertEqual(self._after(cmd, "-profile:v"), "main")


class TestVaapiEncoder(unittest.TestCase):
    """The GPU path: same stream shape as x264, different engine."""

    _build = staticmethod(TestStreamPacing._build)

    def _after(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_encodes_on_the_gpu_with_the_same_stream_shape(self):
        cmd = self._build(["--group", "239.255.42.51", "--encoder", "vaapi",
                           "--from-display", ":106", "--capture-fps", "30"])
        self.assertEqual(self._after(cmd, "-c:v"), "h264_vaapi")
        self.assertEqual(self._after(cmd, "-rc_mode"), "CBR")
        self.assertEqual(self._after(cmd, "-bf"), "0")
        self.assertEqual(self._after(cmd, "-level"), "40")
        self.assertEqual(self._after(cmd, "-qmin"), "18")
        self.assertIn("-muxrate", cmd)
        # colours converted on the GPU, not the CPU
        self.assertIn("scale_vaapi=format=nv12", self._after(cmd, "-vf"))
        # the device is a global option, so it precedes every input
        self.assertLess(cmd.index("-vaapi_device"), cmd.index("-i"))

    def test_no_x264_options_reach_the_gpu_encoder(self):
        cmd = self._build(["--group", "239.255.42.51", "--encoder", "vaapi"])
        for x264_only in ("-preset", "-x264-params", "-pix_fmt"):
            self.assertNotIn(x264_only, cmd)

    def test_baseline_maps_to_constrained_baseline(self):
        cmd = self._build(["--group", "239.255.42.51", "--encoder", "vaapi",
                           "--profile", "baseline"])
        self.assertEqual(self._after(cmd, "-profile:v"), "constrained_baseline")

    def test_x264_remains_the_default(self):
        cmd = self._build(["--group", "239.255.42.51"])
        self.assertEqual(self._after(cmd, "-c:v"), "libx264")
        self.assertNotIn("-vaapi_device", cmd)


class TestUnknownAddresses(unittest.TestCase):
    """Above 7 the address is not 239.255.42.(42+N): refuse, never guess."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import teststream
        self.teststream = teststream

    def test_teststream_refuses_to_derive_above_7(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.teststream.group_for_channel(9, "/nonexistent/config.json")
        self.assertIsNone(got)
        self.assertIn("239.255.42.57", err.getvalue())

    def test_teststream_still_derives_1_to_7(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.teststream.group_for_channel(6, "/nonexistent/config.json"),
                "239.255.42.48")

    def test_stream_service_will_not_start_above_7(self):
        """Exit 2: the unit stops instead of restarting for ever."""
        import json
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"dashboards": [
                {"channel": 9, "url": "https://x/", "enabled": True}]}))
            done = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent.parent / "stream.py"),
                 "--channel", "9", "--config", str(path), "--dry-run"],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2)
        self.assertIn("outside 1-7", done.stderr)
