# Ecler Streamer

Renders web dashboards headlessly and streams them as H.264 over multicast, so
a VEO receiver shows them without a PC attached to a transmitter.

**Maturity.** Newer than the manager, and now in daily service: it has
replaced one of the site's four hardware transmitters outright (every TV on
that channel watches the stream), and drives three channels from one small
machine, encoding on its integrated GPU. It survives reboots unattended, and
its tests run without hardware. The encoder settings below are the ones proven
on real receivers; the rest is ordinary software.

It is a **separate service** from the Ecler Manager, deliberately. The manager
is operational — you open it when a TV drops off. The streamer is
set-and-forget. Keeping them apart means a stray click in the tool you use
daily cannot stop a wall of screens.

## How it fits together

```
systemd ── dashboard-stream@5 ── stream.py ── teststream.py ── ffmpeg ──► 239.255.42.47
                                                  └─ pagesource.sh ── Xvfb + Chromium
eclerstreamer.service ── web UI on :8478 ── systemctl start/stop/restart
```

**systemd owns the streams; the web UI is only a remote control.** It can
crash, hang, or be stopped for an upgrade and not one frame stops. That is the
whole reason for the split.

**Not containerised, on purpose.** The manager has a Dockerfile; this does
not. Its lifecycle model *is* systemd — the per-channel units own the streams
and the web UI is only a remote control, which is what lets the UI crash or be
upgraded without stopping a frame. And multicast has to leave by a specific
interface with a specific source address, so a container would need host
networking anyway, giving up most of what a container is for. If you want one,
open an issue and say what you need it for.

## Install

On a Debian host with a leg on the TV VLAN:

```bash
apt-get install -y python3 ffmpeg xvfb chromium git sudo \
                   fonts-liberation fonts-dejavu-core fonts-noto-color-emoji
git clone <repo> /root/ecler && bash /root/ecler/streamer/deploy/install.sh
```

**On a fresh minimal Debian, set DNS by hand and make it stick:**

```bash
echo "nameserver <your resolver>" > /etc/resolv.conf
chattr +i /etc/resolv.conf
```

`dns-nameservers` in `/etc/network/interfaces` needs the `resolvconf` package,
which a minimal install does not have — so it silently does nothing and every
`apt-get` fails with "Temporary failure resolving". Writing the file directly
works, but something during installation may overwrite it again; the immutable
bit stops that. Nothing on a host with static interfaces and a fixed resolver
legitimately needs to rewrite it.

`sudo` is in that list on purpose and a minimal Debian install does not have
it: the web service runs unprivileged and reaches systemd through a rule in
`/etc/sudoers.d/eclerstreamer`, scoped to five verbs on `dashboard-stream@*`
and nothing else. The service account gains no other privilege.

Then set `local_addr` in the UI's **Host settings**, or in
`/etc/eclerstreamer/config.json` to this host's
address on the TV VLAN. Without it multicast leaves by the default route,
which is the management VLAN, and the receivers never see it — with no error
anywhere. It is the single most common way to get a silent failure here.

Set a login before exposing it:

```bash
python3 tools/setpassword.py --env /etc/eclerstreamer/eclerstreamer.env --user admin
```

## Running a dashboard

1. **Add dashboard** in the web UI, give it a channel number from **1 to 7**
   (see [Channel numbers](#channel-numbers) for why not higher).
2. Paste the URL, **Save**.
3. **Enable**. That one button turns it on in the config, starts it, and
   enables its unit so it comes back after a reboot.
4. If the host has an Intel GPU passed through, set **Encoder** to
   *Graphics chip* under **settings** and restart the channel.

The card then shows a live screenshot of what that channel is actually
displaying, which is the one thing a status line cannot tell you.

## Replacing a hardware transmitter

The simplest migration keeps the channel number. Stream the dashboard **on the
channel the transmitter used**, and nothing else changes: the TVs stay where
they are, and anything that switches them (an AV controller's presentation
mode, a schedule) keeps sending them to the same number. Receivers do not care
what sends a stream, only which channel they are on.

**Never two senders on one channel.** Switch the transmitter off, or move it
to another channel, *before* the streamer starts sending there. Two streams on
one multicast group interleave, and every screen on that channel shows
garbage. Unplugging only the PC behind the transmitter is not enough: the
transmitter goes on sending a black picture.

Then, in the manager's **Channels…**, untick **lock** for that channel (see
below), and set its source to the streamer.

## Channel numbers

A channel (Group ID) N is multicast group `239.255.42.(42+N)` — but that is
only proven for **1 to 7**: 1 and 2 from the switches' IGMP tables, 5, 6 and 7
by receivers showing streams. A receiver on channel 9 joined `239.255.42.57`,
not the `.51` the formula gives. Above 7 the mapping is unknown, and a stream
sent to a guessed address reaches no TV and reports no error. So the streamer
accepts channels 1-7 only, and `tools/teststream.py --channel` refuses to
guess above 7 (pass `--group` with an address read off the switch).

## Monitoring

**From the manager.** A VEO's video-lock flag means nothing on a software
stream: it keeps whatever the previous channel left it with. So in the
manager, untick **lock** for the streamer's channels and enter the streamer's
address at the top of **Channels…**. Each TV on those channels then shows the
stream's real state, read from the streamer's public `/api/streams`: *ok*,
*slow* (encoding below real time), *stalled* (the process runs but ffmpeg has
stopped producing), *down* or *off*. Changes are logged in the manager's
events, including a restart that fell between two checks.

`/api/streams` needs no login, by design: it is what the manager reads. It
carries channel numbers, a verdict and encoder figures (fps, speed, dropped
frames, start time), and no URLs, names or settings. It is cached for 5
seconds, so polling it cannot pile up work.

**On the streamer page.** Each card shows the encoder's own figures: fps,
speed, and frames dropped since the stream started. The header has **CPU**,
**RAM** and **GPU** bars, and each GPU channel a `gpu %` chip. Frames dropped
one at a time, roughly hourly, are clock drift and harmless; hundreds at once
mean the machine fell behind. `docs/gpu.md` shows how to read exactly when
from the progress file.

## Sessions and scheduled restarts

**The browser profile is kept between runs.** A dashboard behind a login stays
logged in across a restart. It used to be wiped every start, on the reasoning
that a kiosk browser has nothing worth keeping — which was wrong in the one
way that mattered, and left Google-authenticated dashboards showing a device
authorisation page instead of the dashboard.

`--fresh-profile` wipes it deliberately, when a profile really is the problem.

**Memory creeps** — in the browser, the virtual display and the encoder alike,
measured at roughly 750 MB a week across three channels. If nothing else
restarts the host, set **Restart channels every** in Host settings. A timer
checks nightly and restarts **at most one channel**, longest-running first, so
the channels stagger themselves across different nights and only one screen is
ever briefly dark.

Pick the interval to suit the pages: a month is comfortable on 8 GB with three
channels. Watch `free -m` for a few weeks and decide rather than guessing.

## Sizing

Measured, one 1080p30 stream of a heavy page (slideshow, photos, animation),
encoded on the CPU with x264:

| | |
|---|---|
| CPU | 0.61 cores on an i3-8100T (3.1GHz) |
| Memory | ~700 MB over a ~250 MB base |

**With an Intel integrated GPU, encode there.** Three 1080p30 channels on an
i5-7400T (4 vCPU, 8 GB) used about 70% of the CPU on x264, and when a page
played a video on two channels at once the encoder fell behind and dropped
nearly every frame for twenty seconds. On the GPU (HD Graphics 630) the same
three channels use about 20% of the CPU and half the GPU at a low clock, and a
week side by side showed 3641 frames dropped in bursts on x264 against none on
the GPU. Each dashboard has an **Encoder** setting; see
[docs/gpu.md](docs/gpu.md) for passing the GPU through and the measurements.

A static dashboard of bars and text costs noticeably less, and can run at
`capture_fps` 10 — sampling a still page 30 times a second is pure waste.

## The settings that matter, and why

These were expensive to find. `docs/streaming.md` has the full account.

| Setting | Why |
|---|---|
| no `-re` on the capture | x11grab paces itself; a second pacer makes frames arrive unevenly and animation judder, while the encoder still reports `speed=1.0x` |
| `nal-hrd=cbr` + `-muxrate` | a near-static page collapses to a fraction of its budget then bursts on a slide change, which is the worst case for a hardware decoder |
| `-qmin 18` | without a floor the encoder goes near-lossless and a photographic keyframe exceeds what H.264 level 4.0 allows, so the decoder truncates it and the picture tears |
| `bitrate=`/`burst_bits=` on the socket | `-muxrate` paces the stream's timestamps, not the bytes; a whole frame written at line rate overruns a small receiver buffer |
| `-draw_mouse 0` | Xvfb `-nocursor` does not work — the browser sets its own cursor on its own window |
| browser anti-throttling flags | Chromium backgrounds a renderer it thinks nobody is watching, and under a bare Xvfb nothing tells it otherwise. It sat at 0% CPU while a slideshow was supposedly animating |
| `--autoplay-policy=no-user-gesture-required` | Chromium will not autoplay a video with sound until someone clicks the page, and nobody clicks a wall. A page that showed an announcement video froze on its first frame until the channel was restarted |
| hardware encoding (VAAPI), where there is a GPU | the same stream shape as x264 — level 4.0, fixed GOP, no B-frames, `qmin`, constant rate — at a fraction of the CPU, so a page that plays video no longer starves the encoder |

**A test pattern hides every one of these.** `testsrc` changes every pixel of
every frame, so it sits at the bitrate cap, is constant-rate by accident, has
no photographic content, and needs no browser. It proves a receiver locks on
and nothing more.
