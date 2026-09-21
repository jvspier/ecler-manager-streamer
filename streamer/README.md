# Ecler Streamer

Renders web dashboards headlessly and streams them as H.264 over multicast, so
a VEO receiver shows them without a PC attached to a transmitter.

**Maturity.** Newer than the manager and less exercised: it drives three
channels on one machine, survives reboots unattended, and its 47 tests run
without hardware — but it has not yet replaced the hardware transmitters in
daily service. The encoder settings below are the ones proven on real
receivers; the rest is ordinary software.

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

1. **Add dashboard** in the web UI, give it a channel number.
2. Paste the URL, **Save**.
3. **Enable**, then `systemctl enable --now dashboard-stream@<channel>` so it
   comes back after a reboot.

The card then shows a live screenshot of what that channel is actually
displaying, which is the one thing a status line cannot tell you.

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

Measured, one 1080p30 stream of a heavy page (slideshow, photos, animation):

| | |
|---|---|
| CPU | 0.61 cores on an i3-8100T (3.1GHz) |
| Memory | ~700 MB over a ~250 MB base |

A static dashboard of bars and text costs noticeably less, and can run at
`capture_fps` 10 — sampling a still page 30 times a second is pure waste.
Four streams fit comfortably in 6 vCPU and 8 GB.

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

**A test pattern hides every one of these.** `testsrc` changes every pixel of
every frame, so it sits at the bitrate cap, is constant-rate by accident, has
no photographic content, and needs no browser. It proves a receiver locks on
and nothing more.
