"""The load bars on the streamer page: CPU, memory and GPU from /proc."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eclerstreamer import metrics  # noqa: E402

# The shape of a real /proc/<pid>/fdinfo entry on the streamer VM (i915).
FDINFO = """pos:\t0
flags:\t02100002
drm-driver:\ti915
drm-client-id:\t{client}
drm-pdev:\t0000:06:10.0
drm-engine-copy:\t0 ns
drm-engine-render:\t{render} ns
drm-engine-video:\t{video} ns
drm-engine-video-enhance:\t0 ns
"""


class FakeHost:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name) / "proc"
        self.sys = Path(self.tmp.name) / "sys"
        self.proc.mkdir()
        card = self.sys / "class" / "drm" / "card1"
        card.mkdir(parents=True)
        (card / "gt_act_freq_mhz").write_text("467\n")
        (card / "gt_RP0_freq_mhz").write_text("1000\n")
        (self.proc / "meminfo").write_text(
            "MemTotal:        8000000 kB\nMemFree:  1 kB\nMemAvailable:    5000000 kB\n")

    def cpu(self, busy, idle, steal=0):
        # user nice system idle iowait irq softirq steal guest guest_nice
        (self.proc / "stat").write_text(
            f"cpu  {busy} 0 0 {idle} 0 0 0 {steal} 0 0\ncpu0 1 2 3 4\n")

    def ffmpeg(self, pid, group, client, render, video, comm="ffmpeg"):
        base = self.proc / str(pid)
        (base / "fdinfo").mkdir(parents=True, exist_ok=True)
        (base / "comm").write_text(comm + "\n")
        (base / "cmdline").write_bytes(
            b"ffmpeg\0-c:v\0h264_vaapi\0-f\0mpegts\0"
            + f"udp://{group}:5004?ttl=4".encode() + b"\0")
        (base / "fdinfo" / "7").write_text(
            FDINFO.format(client=client, render=int(render), video=int(video)))
        (base / "fdinfo" / "0").write_text("pos:\t0\nflags:\t02\n")   # not DRM

    def sampler(self):
        return metrics.Sampler(proc=self.proc, sys_root=self.sys,
                               uid=os.getuid())


class TestReadings(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.tmp.cleanup)

    def test_memory_used_is_what_new_programs_cannot_have(self):
        mem = metrics.read_memory(self.host.proc)
        self.assertEqual(mem["total"], 8000000 * 1024)
        self.assertEqual(mem["used"], 3000000 * 1024)

    def test_gpu_clock(self):
        self.assertEqual(metrics.read_gpu_clock(self.host.sys),
                         {"mhz": 467, "max_mhz": 1000})

    def test_only_our_ffmpeg_drm_clients_count(self):
        self.host.ffmpeg(100, "239.255.42.48", 5, 10, 20)
        self.host.ffmpeg(200, "239.255.42.49", 6, 1, 2, comm="chromium")
        clients = metrics.read_gpu_clients(self.host.proc, os.getuid())
        self.assertEqual(list(clients), ["5"])
        self.assertEqual(clients["5"]["group"], "239.255.42.48")
        self.assertEqual(clients["5"]["ns"]["video"], 20)
        self.assertEqual(metrics.read_gpu_clients(self.host.proc, os.getuid() + 1), {})


class TestRates(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.addCleanup(self.host.tmp.cleanup)

    def _two_samples(self, sampler, seconds, change):
        sampler.sample()
        change()
        sampler._previous["at"] -= seconds      # as if `seconds` had passed
        return sampler.sample()

    def test_first_sample_has_no_rates(self):
        self.host.cpu(100, 100)
        result = self.host.sampler().sample()
        self.assertIsNone(result["cpu"])
        self.assertIsNotNone(result["memory"])

    def test_cpu_busy_and_steal(self):
        self.host.cpu(1000, 3000, steal=0)
        sampler = self.host.sampler()

        def later():
            # +300 busy (incl. 50 steal) and +700 idle: 30% busy, 5% steal
            self.host.cpu(1250, 3700, steal=50)
        cpu = self._two_samples(sampler, 5, later)["cpu"]
        self.assertEqual(cpu["busy"], 30.0)
        self.assertEqual(cpu["steal"], 5.0)

    def test_gpu_busy_per_engine_and_per_channel(self):
        self.host.cpu(0, 0)
        self.host.ffmpeg(100, "239.255.42.48", 5, render=0, video=0)
        self.host.ffmpeg(101, "239.255.42.49", 6, render=0, video=0)
        sampler = self.host.sampler()

        def later():   # over 5 s: ch 6 video 1.0 s busy, ch 7 video 1.5 s
            self.host.ffmpeg(100, "239.255.42.48", 5, render=0.5e9, video=1.0e9)
            self.host.ffmpeg(101, "239.255.42.49", 6, render=0.5e9, video=1.5e9)
        gpu = self._two_samples(sampler, 5, later)["gpu"]
        self.assertEqual(gpu["engines"]["video"], 50.0)
        self.assertEqual(gpu["engines"]["render"], 20.0)
        self.assertEqual(gpu["groups"]["239.255.42.48"]["video"], 20.0)
        self.assertEqual(gpu["groups"]["239.255.42.49"]["video"], 30.0)

    def test_a_restarted_stream_counts_from_zero(self):
        """A new ffmpeg is a new DRM client whose counter began at zero."""
        self.host.cpu(0, 0)
        self.host.ffmpeg(100, "239.255.42.48", 5, render=0, video=9e12)
        sampler = self.host.sampler()

        def later():
            import shutil
            shutil.rmtree(self.host.proc / "100")
            self.host.ffmpeg(300, "239.255.42.48", 9, render=0, video=1e9)
        gpu = self._two_samples(sampler, 5, later)["gpu"]
        self.assertEqual(gpu["engines"]["video"], 20.0)

    def test_no_baseline_means_no_fake_spike(self):
        """Clients that existed before we could see them must not count their
        whole lifetime as this interval."""
        self.host.cpu(0, 0)
        sampler = self.host.sampler()

        def later():
            self.host.ffmpeg(100, "239.255.42.48", 5, render=9e12, video=9e12)
        self.assertIsNone(self._two_samples(sampler, 5, later)["gpu"])

    def test_never_above_100(self):
        self.host.cpu(0, 0)
        self.host.ffmpeg(100, "239.255.42.48", 5, render=0, video=0)
        sampler = self.host.sampler()

        def later():
            self.host.ffmpeg(100, "239.255.42.48", 5, render=0, video=9e9)
        gpu = self._two_samples(sampler, 5, later)["gpu"]
        self.assertEqual(gpu["engines"]["video"], 100.0)


if __name__ == "__main__":
    unittest.main()
