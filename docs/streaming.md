# Retiring the source PCs

Replacing the kiosk PCs that feed the transmitters — and possibly the
transmitters themselves — with software streaming.

**Proven on real hardware 2026-09-10**: a live receiver shows a real dashboard,
streamed from a VM, and it looks like the hardware transmitter's output. See
"CONFIRMED: a real dashboard streams" below for the working configuration and
the five failure modes found on the way, each of which a test pattern hides.

Not yet built: the service that would run this unattended. The tools here are
`tools/pagesource.sh` and `tools/teststream.py`, both driven by hand.

---

## Retiring the ChromeBoxes

The research and reasoning that led to the test above, kept because the
trade-offs still apply when sizing the real thing.

**Today.** Each of the four transmitters has a ChromeBox attached in kiosk mode,
displaying a static dashboard URL over HDMI. The transmitter encodes that to
H.264 and multicasts it; receivers decode it to their TVs.

### Two possible routes

1. **Software streaming (preferred).** One machine generates all four streams
   itself and multicasts them. Retires the four ChromeBoxes *and* the four
   transmitters: 8 devices -> 1. Gated on one unknown, below.
2. **Keep the transmitters, replace only the ChromeBoxes.** A fallback if
   route 1 turns out not to work. Notes kept below.

### Route 1 needs no video outputs at all

The point that makes this work, and which earlier notes here got wrong: in the
software route **nothing is displayed on a physical output**. The pipeline is

```
Xvfb (virtual framebuffer) -> Chromium renders the dashboard into it
  -> ffmpeg x11grab captures it -> VAAPI H.264 encode -> UDP multicast
```

No HDMI, no DisplayPort, no adapters, no EDID. The "an Intel iGPU only drives
three displays" constraint that shaped the fallback route **does not apply
here**, so a ThinkCentre Tiny — three ports and all — is a perfectly good host.

### The bottlenecks, honestly ranked

1. ~~**Feasibility, not performance.** Will a VEO-XRI1C lock onto an
   ffmpeg-generated MPEG-TS stream?~~ **Answered on 2026-09-10: yes**, first
   attempt, raw MPEG-TS. See above.

2. **Screen capture — the real cost, not the encoding.** `x11grab` copies the
   entire framebuffer every frame: 1920x1080x4 bytes is about 8 MB, so roughly
   250 MB/s of memcpy per stream at 30fps, plus a BGRA-to-NV12 conversion. The
   encode itself is nearly free on Quick Sync, and Intel iGPUs have no
   concurrent-session limit.

   **The lever:** capture at a low framerate and let ffmpeg duplicate frames up
   to a compliant output rate. Dashboards barely change, duplicated frames
   encode to almost nothing, and capture cost drops several-fold:

   ```bash
   ffmpeg -f x11grab -framerate 5 -i :99 ... -r 25 -f mpegts udp://...
   ```

   Rough figures: four streams at 5fps capture is about 1-1.5 cores total; at
   30fps, 2-3 cores. A 6-core T-series i5 (i5-8400T, i5-9500T) handles either.

3. **RAM.** Four Chromium instances at 300-800 MB each plus framebuffers. 16 GB
   comfortable; an M720q takes 32 GB across two SODIMMs.

4. **Thermals — the one genuinely worth measuring.** A 1L chassis running four
   browsers and four encodes continuously, 24/7. A 35 W T-series chip should
   cope but may throttle. Do not put it in a sealed cabinet, and check
   temperatures under sustained load rather than assuming.

5. **Network placement.** The multicast source moves from the transmitters to
   this host, so it needs a leg on **VLAN 20**, exactly like the manager
   container's `net1`. Otherwise multicast has to be routed between VLANs,
   which involves whoever owns the switches.

**Not bottlenecks:** encoding (free on Quick Sync), bandwidth (4 x ~15 Mbps on
gigabit), and Chromium's rendering (a static dashboard only repaints when the
content changes, so it is near-idle after first paint).

**One design note:** keep four separate pipelines rather than one 4K desktop
cropped four ways. The total pixel count is the same, but separate processes
mean one crashing does not take down the other three.

### If it works: a separate tool, not more of this one

Decided up front, because it is easier to keep two things separate than to pull
them apart later. The streamer should be its **own service with its own small
web page**, alongside this manager rather than inside it.

The reason is that they have different lifecycles:

| | Ecler VEO Manager | the streamer |
|---|---|---|
| Used | operationally — switch channels, spot faults, identify TVs | set and forget: define four streams once, rarely touch them |
| Changes | whenever a TV moves or drifts | when a dashboard URL changes |
| Fails how | one TV shows the wrong thing | every TV on that channel goes dark |

Mixing set-and-forget configuration into an operational dashboard makes both
worse: the manager's page gets heavier for something looked at twice a year, and
the streamer's settings end up buried behind cards.

**How they should tie together — loosely.** The shared concept is the channel:
this manager knows *which TVs should be on channel N*, and the streamer knows
*what is being sent to channel N*. So the streamer exposes its status over HTTP
(which streams are running, on which multicast groups, at what framerate, and
whether ffmpeg is alive), and this manager reads that and shows it in the group
headings — "Reception, from streamer, 5 fps, healthy" in place of today's
"from 10.0.1.1".

That keeps both independently deployable and separately restartable, and means
neither has to be running for the other to work.

### Per-dashboard framerate and bitrate

The four dashboards differ in how much they move, so they do not need the same
settings. What they are, as of 2026-09:

| Dashboard | Content | Capture |
|---|---|---|
| Reception | data, graphs, text, icons; essentially static | 5 fps |
| Warehouse | same shape | 5 fps |
| Office | revenue, event photos, menu; slide changes with fades | 15 fps |
| Canteen | similar to Office | 15 fps |

**Output every stream at 1080p30.** Capture rate and output rate are separate —
ffmpeg duplicates frames to fill — and 30 divides cleanly by both 5 and 15, so
each captured frame is duplicated a whole number of times. That matters:
**uneven division causes judder**, and judder is more visible during a fade than
a low-but-even framerate is. Capturing 15 into a 25 fps output would duplicate
some frames and not others, which reads as a stutter mid-crossfade. 15 into 30
is exactly two of each.

15 fps is comfortably enough for fades. A one-second fade gets 15 distinct
steps, and fades are opacity ramps rather than motion, so they tolerate low
framerates far better than panning or video would.

**Fades stress the bitrate, not the framerate.** A static dashboard compresses
to almost nothing because every frame is nearly identical. A full-screen
crossfade is close to the worst case for inter-frame prediction: every pixel
changes slightly, every frame. So set the bitrate per stream rather than
uniformly — roughly 2-4 Mbps for the two static ones, 8-10 Mbps for Office and
Canteen so fades do not go blocky. The hardware transmitters cap at 15 Mbps
today, so there is headroom.

**Set the keyframe interval to 1-2 seconds.** A receiver joining a multicast
stream can only begin decoding at a keyframe, so the GOP length *is* the
channel-switch latency. A 10-second GOP means a TV shows black for up to ten
seconds after a switch, which would make this tool's channel buttons and
`identify` feel broken. The hardware transmitters chose this; in software it is
yours to get wrong.

**A free lever on the content side:** the dashboard HTML is ours, so lengthening
the CSS transitions helps more than raising the framerate. A 2-second fade at
5 fps gets 10 steps and looks fine; a 0.3-second fade at 5 fps gets barely two
and looks broken. If a uniform 5 fps is desirable, slow the transitions instead.

**Resulting load:** 5+5+15+15 = 40 fps of 1080p capture, against 120 if
everything ran at 30. Roughly 1-1.5 cores all in, including Chromium and encode
overhead.

### Fallback route: keep the transmitters, replace only the ChromeBoxes

Only relevant if route 1 turns out not to work. Kept because it involves no
unknowns at all: the transmitters carry on doing exactly what they do today, and
the same day-to-day pain goes away — four kiosk boxes to keep updated, and a URL
change meaning you go and touch hardware.

In this route there is **no ffmpeg, no screen capture, no VAAPI, no Quick
Sync**. The new machine is just a PC with four displays each showing a kiosk
browser — ordinary digital signage. Here the **display-output count is the
constraint**, which is what makes it awkward on the hardware to hand.

The transmitters need no modification: HDMI in is already the interface. But an
M720q has DisplayPort 1.2 + HDMI 1.4 standard plus an **optional third video
port** (Flex IO: 2nd DP, 2nd HDMI, VGA, serial, or USB-C with DP), and Lenovo
rates it for **three** independent displays. UHD 630 caps at three pipes anyway.
So one Tiny cannot feed four transmitters.

**Best version: two Tinys, two transmitters each.** 8 devices to 6, no purchase
since they are already on the shelf, standard DP + HDMI on each box so no
optional port module or riser or GPU or DisplayLink, and a dead box takes out
**two** dashboards rather than four.

Other variants, none as good:

| Approach | Devices | Notes |
|---|---|---|
| One Tiny: optional 3rd port + 1 USB DisplayLink | 5 | Gets four from one box; DisplayLink sits outside the iGPU's three-pipe limit (Linux `evdi`), but that one channel is a software framebuffer |
| One Tiny + PCIe riser + 4-output low-profile GPU | 5 | **Avoid.** The riser exists (01AJ940 / 01AJ929) but the M920q one is 110 mm, the BIOS checks for a specific device ID and POSTs "PCIe Device Not Supported" otherwise, there is no aux power, and the M720q riser reportedly lacks lane shielding. Too fragile for something that runs unattended for a year |
| An SFF + 4-output GPU (e.g. used Quadro P620, ~EUR 40-70) | 5 | Clean, but needs an SFF chassis — the fleet is Tiny, so not applicable |
| 4K output -> 2x2 video wall splitter | 6 | One output, four 1080p quadrants. Cheap, but adds a box in the chain and low-end ones can be quirky or 4K30-only |
| 3 channels on one Tiny, keep 1 ChromeBox | 6 | Simplest, but leaves a kiosk box in service, which defeats the point |

Newer Tiny generations have four display pipes in the iGPU, but the chassis
still exposes at most three ports, so a fourth output would still need MST or
DisplayLink.

**A bonus of this route:** the transmitters present EDID, so X sees real
displays and no Xvfb or dummy plugs are needed.

### Why the transmitters cannot be hooked up any other way

Asked and answered, so it does not resurface. The platform is an ASPEED-class
SoC — the `astparam` command in the sibling firmware is the tell — with a serial
debug console and its settings in an `iptv.ini` on a few MB of flash. The input
side is a physical HDMI receiver chip wired into a hardware encode pipeline.
There is no "network video in" path to re-route, because the encoder's input is
silicon rather than software, and the SoC has tens of MB of RAM, no GPU and no
X, so it could never run a browser. Whatever firmware you put on it, it remains
an HDMI encoder.

That is fine: HDMI in is exactly the interface the fallback route needs, and
route 1 does not need the transmitters at all.

### Even in the full software route, the transmitters are not e-waste

Keep one or two as an **ad-hoc HDMI input channel**: plug in a laptop or a
camera, set any receivers to that Group ID, and push a live source to every TV
in the building for a presentation or an event. Streaming a URL cannot do that.
They are also the fallback path if the streaming host dies.

### If you do go further: replacing the transmitters as well

```
now:     ChromeBox -> HDMI -> VEO-XTI1C -> multicast -> 25 receivers
maybe:   one host: headless Chromium -> ffmpeg -> same multicast -> 25 receivers
```

**Feeding a URL to a receiver is not possible.** The VEO-XRI1C is a pure H.264
decoder: no browser, no HTML, nothing that takes a URL. But the receivers do not
care what *produces* their stream, only that a compatible one arrives on the
multicast group they have joined. So the ChromeBox *and* the transmitter could
in principle both be replaced by software.

Ecler's manual says a transmitter's output is receivable in VLC as
`udp://@239.255.42.42:5004`, i.e. MPEG-TS over UDP multicast — which ffmpeg
produces natively.

### CONFIRMED: a real dashboard streams, and looks like the hardware transmitter

Tested 2026-09-10 on a live VEO-XRI1C, from a Debian 13 VM. The working
configuration, end to end:

```
tools/pagesource.sh --url '<dashboard url>'
tools/teststream.py --channel 5 --local-addr <ip on the TV VLAN> \
    --from-display :99 --capture-fps 30 --fps 30 \
    --bitrate 6M --no-bframes --detach
```

Steady state: `q=18.0`, `bitrate=6899 kbits/s` flat, `speed=0.999x`, 0.61 CPU
cores and ~700 MiB of memory for the whole chain (browser, Xvfb, encoder).

**Five things were wrong before it looked right, and every one of them was
found by measuring rather than reasoning.** They are listed because each
failure mode is invisible in a test pattern.

**1. `-re` must never be applied to a live capture.** x11grab already paces
itself at `-framerate`; `-re` adds a second pacer and the two fight over frame
timing. ffmpeg's own documentation says not to use it with a live input. The
symptom is judder on the television while the encoder reports a perfectly
healthy `speed=1.0x`, because frames are uneven rather than late.

**2. Chromium sleeps when it thinks nobody is watching.** Under a bare Xvfb
there is no window manager or compositor to tell it otherwise, so it
backgrounds its own renderer: measured at **0% CPU** while a slideshow with
crossfades was supposedly animating. Needs
`--disable-renderer-backgrounding`, `--disable-backgrounding-occluded-windows`
and `--disable-background-timer-throttling`. Verify in `top` -- the browser
must show real CPU while the page animates.

**3. A dashboard is nearly static, so the bitrate collapses and then bursts.**
Left alone the encoder spent **1.76 Mbit/s of a 10M cap** and then had to
burst on every slide transition, which is the worst case for a hardware
decoder. `nal-hrd=cbr` pads the elementary stream and `-muxrate` pads the
transport stream, giving a constant arrival rate whatever the picture does.

**4. The pointer is captured unless excluded at the capture.** Xvfb
`-nocursor` does *not* work: it suppresses only the server's default root
cursor, and the browser sets its own cursor on its own window. Use ffmpeg's
`-draw_mouse 0`.

**5. Photographic slides tore, and it was a conformance limit.** Two of the
dashboard's slides tore consistently, both photographic; the flat graphic ones
never did. Content-dependent, not time-dependent.

The stream was proven innocent first: a 110 MB capture taken straight off the
wire with `-c copy` decoded with **zero errors** offline. (The
`non-existing PPS 0 referenced` lines that appear during such a capture are
just the cost of joining mid-GOP before the first keyframe, and never recur.)

The cause: a constant-rate budget far exceeds what flat graphics need, so
x264 falls to near-lossless -- `q=2.0` measured -- and on a photographic
slide that produces a keyframe of one to two megabytes, larger than the
biggest single coded frame H.264 level 4.0 permits. The stream declares level
4.0, so a decoder that sizes its buffers from the declared level truncates
the frame. With 98.5% of macroblocks skipped on a static slide nothing
repairs the damage until the next keyframe, which is oversized in exactly the
same way -- hence a torn band that persists for the whole slide rather than
clearing in a second.

`-qmin 18` fixes it, and is visually lossless for text and graphics at 1080p.

**A consequence worth knowing: quality and bitrate are now decoupled.** With
`q` sitting at the floor, the encoder would spend more bits if it were
allowed, so everything above what the picture needs becomes null padding.
`--qmin` sets the quality; `--bitrate` only sets the constant wire rate. 4M
gives the same picture as 6M, which matters when four streams run at once.

**The test pattern hid every one of these.** `testsrc` changes every pixel of
every frame, so it sits at the bitrate cap, produces a constant-rate
transport stream by accident, has no photographic content and needs no
browser at all. It proves the receiver locks on. It proves nothing about
whether a dashboard will look right.

### CONFIRMED: a receiver does lock onto a software stream

Tested 2026-09-10 against a live VEO-XRI1C. It locked on the **first attempt**,
with raw MPEG-TS over UDP — none of the fallback variants (RTP, baseline
profile, a silent audio track, a lower bitrate) were needed. The test pattern
appeared with its counter running.

**So the feasibility gate is passed**, and the unknowns below are answered:
raw MPEG-TS is accepted, no audio track is required, and `main` profile is
fine.

The configuration that worked, exactly:

```
ffmpeg -re
  -f lavfi -i testsrc=size=1920x1080:rate=5
  -c:v libx264 -profile:v main -level 4.0 -pix_fmt yuv420p
  -preset veryfast -b:v 6M -maxrate 6M -bufsize 6M
  -g 45 -keyint_min 45 -sc_threshold 0 -r 30 -an
  -f mpegts -muxrate 0
  'udp://239.255.42.47:5004?ttl=4&pkt_size=1316&overrun_nonfatal=1&localaddr=<tv-vlan-address>'
```

Four details in there are load-bearing:

- **`pkt_size=1316`** (7 x 188). MPEG-TS packets are 188 bytes and ffmpeg's
  default 1472-byte payload is not a multiple of that.
- **`localaddr`** pinned to the TV-VLAN leg. Without it the packets leave by the
  default route and the receiver shows "Waiting for connection" with no error
  anywhere.
- **`-g 45` with `-r 30`**, i.e. a keyframe every 1.5s. This *is* the
  channel-switch latency: a receiver can only start decoding at a keyframe.
- **`yuv420p`** 8-bit. Anything else is a gamble on a decoder this simple.

**What is still unproven:** a real page rendered headlessly rather than a test
pattern (the same pipeline with a different source, so likely straightforward);
stability over days rather than minutes; four concurrent streams; and the CPU
cost on the intended hardware.

### The unknowns, as they stood before the test

1. Whether the receiver needs RTP encapsulation or accepts raw MPEG-TS. The
   manual mentions both and is ambiguous.
2. Whether it is fussy about H.264 profile/level, GOP structure or PMT/PID
   layout. Cheap decoders often are.
3. Whether it needs an audio track present in order to lock.
4. Whether the group-ID-to-multicast-address mapping can be derived (read it
   off a transmitter: `tools/probe.py <tx-ip>` reports any `239.x.x.x` address
   and port on its web page).

### The experiment

`tools/teststream.py` exists for this. It only sends packets to a multicast
group — it touches no transmitter and no receiver.

**Where to run it: the manager container.** It already has a leg on the TV VLAN,
which is the only requirement, and a single software-encoded test pattern is
trivial load. No new container, no iGPU needed.

```bash
pct exec 108 -- apt-get update
pct exec 108 -- apt-get install -y ffmpeg
```

**Pin the outgoing interface.** Multicast has no route of its own, so on a host
with more than one leg the kernel sends it out the **default-route** interface —
which on a manager container is the management VLAN, not the TV VLAN. Nothing
reports an error; the receiver simply shows "Waiting for connection". Always
pass `--interface`:

```bash
--interface eth1
```

The tool warns when it sees several interfaces and no pin.

**Step 1 — stream a test pattern to an unused channel.** Channels 1-4 are the
real dashboards, so 5 is free (confirmed: a receiver set to CH5 shows
"Waiting for connection…").

```bash
pct exec <ctid> -- python3 -u /opt/eclermanager/tools/teststream.py \
    --channel 5 --interface eth1 --detach
```

**Use `--detach` through `pct exec`.** That does not forward Ctrl-C into the
container, so a foreground stream cannot be stopped from the terminal that
started it. Detached, it runs in its own session with a pidfile:

```bash
... teststream.py --status          # is it alive, and what does ffmpeg say?
... teststream.py --stop            # stop it
```

Both address the stream by `--port`, so neither needs the channel repeating.
If a foreground one is already stuck, `pkill -f teststream.py`.

`--channel N` resolves the address itself — from the config if recorded, else
from the pattern confirmed on this network, `239.255.42.(42+N)`. It **refuses a
channel a real transmitter serves**, so a test cannot collide with a live
dashboard. Add `--dry-run` to see the ffmpeg command without sending anything.

**Step 2 — point one TV at it.** Use the dashboard: switch any TV to channel 5
and look at the screen. Allow a few seconds, since a receiver can only begin
decoding at a keyframe. One click on that card's starred channel puts it back,
and with both watchdogs off nothing will fight the change. The card shows amber
"wrong channel" for the duration, which is correct.

A real TV is the right test target — a spare receiver in a box has no PoE, so
it cannot answer at all.

**Before the variants, prove the stream is reaching the VLAN at all.** The
switch's IGMP snooping table is the arbiter: if the group appears there, the
packets arrived and the question really is about the decoder. If it does not,
the stream never left the host and no amount of encoder tuning will help.

```
show ip multicast vlan <tv-vlan>        ! the group should be listed
```

Also confirm the container is actually sending, and out of which leg:

```bash
apt-get install -y tcpdump
tcpdump -ni eth1 -c 5 host 239.255.42.47      # should show packets
tcpdump -ni eth0 -c 5 host 239.255.42.47      # should show nothing
```

**Step 3 — if the stream is on the VLAN and it still stays black, work through
the variants.** Each tests a
different guess about what the decoder wants:

```bash
--variant rtp          # RTP instead of raw MPEG-TS; the manual mentions both
--audio                # a silent audio track, in case one must be present
--profile baseline     # simpler H.264 than main
--bitrate 3M           # in case it dislikes the rate
```

One detail already handled, worth knowing because it is easy to get wrong by
hand: the UDP payload is set to **1316 bytes** (7 x 188). MPEG-TS packets are
188 bytes and ffmpeg's default 1472-byte payload is not a multiple of that —
exactly the sort of thing a cheap decoder refuses to parse.

**Step 4 — stream a real page instead of a test pattern.** One command does the
whole pipeline: `--url` renders the page on a virtual display via
`tools/pagesource.sh`, captures it, and takes the browser down again on
`--stop`.

```bash
pct exec <ctid> -- python3 -u /opt/eclermanager/tools/teststream.py \
    --channel 5 --local-addr <tv-vlan-address> \
    --url 'https://your-dashboard/' --detach
```

Use `--from-display :99` instead when you already have a page up and want to
reuse it — which is what you want while iterating on how the page looks, since
it avoids restarting the browser for every attempt.

```bash
apt-get install -y xvfb chromium fonts-liberation fonts-dejavu-core
bash /opt/eclermanager/tools/pagesource.sh --url https://your-dashboard/
```

**In an unprivileged container, Chromium needs `--no-zygote`.** It normally
pre-forks a "zygote" process and clones render processes from it using
`CLONE_NEWUSER`, `CLONE_NEWPID` and `CLONE_NEWNET`. The container blocks that
**even with `nesting=1` and `--no-sandbox`**, and the browser dies a second or
two after drawing its first window, logging `Failed to send GetTerminationStatus
message to zygote` among a great deal of unrelated dbus noise.

`pagesource.sh` passes `--no-zygote` for this reason, and it is harmless
elsewhere — process spawning is marginally slower, which does not matter for a
browser showing one page.

`nesting=1` alone was **not** sufficient, which is worth knowing before taking
that trade: nesting exposes the host's procfs and sysfs to the guest. With
`--no-zygote` in place, rendering worked in an unprivileged container without
nesting.

Nothing appears on a physical output — Xvfb is a framebuffer in memory. Check
what actually rendered *before* streaming it anywhere, because a page that has
not finished loading, or one showing a login screen, looks identical to a
working one from the outside:

```bash
apt-get install -y imagemagick
DISPLAY=:99 import -window root /tmp/page.png
```

Look at that PNG. Then capture it:

```bash
python3 /opt/eclermanager/tools/teststream.py \
    --channel 5 --interface eth1 --url --display :99
```

Stop the browser and display when done:

```bash
bash /opt/eclermanager/tools/pagesource.sh --stop
```

**What to look for on the TV**, beyond "is it there":

- **Text sharpness.** 1080p H.264 at a few Mbps is unkind to thin fonts. If a
  dashboard's small text looks soft, raising the bitrate helps more than raising
  the framerate.
- **Fade smoothness**, if the page animates between slides. 5 fps gives a
  one-second fade five steps; lengthening the CSS transition costs nothing and
  helps more than a higher capture rate.
- **Colour.** Xvfb at 24-bit into 8-bit 4:2:0 can shift saturated brand colours
  slightly. Usually invisible; worth a look on a page that is mostly one colour.

The browser flags in `pagesource.sh` are chosen for a container: `--no-sandbox`
because an unprivileged LXC cannot use Chromium's sandbox, `--disable-gpu` and
`--disable-dev-shm-usage` because there is no GPU and `/dev/shm` is small, and
`--kiosk` plus the various `--disable-*` so nothing is drawn over the page.

### Provisioning a dedicated streaming box

Rendering does work in an unprivileged LXC, once `--no-zygote` is passed — see
above. A dedicated box is still worth having, for reasons that have nothing to
do with whether it can be made to run:

- **Failure domains.** A streamer on the Proxmox host means one dead machine
  takes out both the dashboards and the tool you would use to diagnose them.
- **Memory.** Each stream needs its own browser, 300-800 MB apiece. That is a
  lot to add to a host running anything else.
- **Less to reason about.** A plain Debian install needs none of
  `--no-zygote`, `--no-sandbox`, `--disable-dev-shm-usage` or a nesting trade,
  and `/dev/dri` is simply there.

**1. Install Debian, minimal.** No desktop — this box renders pages to a
framebuffer, so it needs no display stack of its own. Give it a static address
on the TV VLAN. Keep it out of any Proxmox cluster: a single-purpose appliance
gains nothing from membership, and two nodes without a QDevice means either
being down blocks cluster changes.

**2. Packages.**

```bash
apt-get update
apt-get install -y xvfb chromium ffmpeg \
    fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
    intel-media-va-driver vainfo x11-utils imagemagick git
```

`fonts-noto-color-emoji` is worth having: a dashboard with emoji in its labels
renders them as empty boxes without it, which looks like a rendering fault.

**3. Confirm hardware encoding is available.**

```bash
vainfo | grep -i h264
ls -l /dev/dri/renderD128
```

You want `VAProfileH264Main` and `VAEntrypointEncSlice` in that output. Without
them the encode falls back to software, which works but costs several times the
CPU.

**4. Prove one stream by hand** before automating anything:

```bash
git clone <this repo> /opt/eclermanager
python3 /opt/eclermanager/tools/teststream.py \
    --channel 5 --local-addr <this box's TV-VLAN address> \
    --url 'https://your-dashboard/' \
    --capture-fps 15 --fps 30 --bitrate 10M --detach
```

Then set one TV to channel 5 and look at it. Check what is being captured
first if it looks wrong:

```bash
bash /opt/eclermanager/tools/pagesource.sh --screenshot /tmp/shot.png
```

**5. One systemd unit per stream**, once a stream is proven. A template unit
means one file for any number of dashboards:

```ini
# /etc/systemd/system/dashboard-stream@.service
[Unit]
Description=Dashboard stream on channel %i
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/dashboard-streams/%i.env
ExecStart=/usr/bin/python3 /opt/eclermanager/tools/teststream.py \
    --channel %i --url ${URL} --display :${DISPLAY_NUM} \
    --local-addr ${LOCAL_ADDR} --capture-fps ${CAPTURE_FPS} \
    --fps ${FPS} --bitrate ${BITRATE}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

With one env file per channel:

```bash
# /etc/dashboard-streams/5.env
URL=https://your-dashboard/
DISPLAY_NUM=99
LOCAL_ADDR=10.0.2.50
CAPTURE_FPS=15
FPS=30
BITRATE=10M
```

```bash
systemctl enable --now dashboard-stream@5
```

**Give each stream its own display number.** `:99`, `:100`, `:101` — two
browsers on one display would draw over each other.

`Restart=always` matters more here than in most services: a dead stream is a
black screen, and nobody notices a dashboard's *absence* quickly.

**6. Then build the streamer service.** Running four template units by hand
works, but the thing worth having is a small web page that lists dashboards,
takes a URL and a channel, and starts and supervises the streams itself —
reporting status over HTTP so this manager can show "channel 5, from streamer,
healthy" in its group headings. That belongs in its own repository: it is
set-and-forget where the manager is operational, and the two should not share a
release or a failure.

### Sizing, if it works

Researched, with a decision already made: **a dedicated node with a 7th or 8th
gen i5**, not the existing i3-7100 Proxmox node.

**The encoder is not the constraint.** Kaby Lake (7th gen) and Coffee Lake (8th
gen) both carry Gen 9.5 Quick Sync, which does H.264 8-bit encode, and — per
Jellyfin's hardware-acceleration documentation — *"unlike NVIDIA NVENC, there is
no concurrent encoding sessions limit on Intel iGPU"*. Four 1080p30 H.264
encodes on an HD/UHD 630 is untroubling, especially for low-motion dashboards.
So the i5 generation choice is about CPU cores, not encode capability: both have
the same encoder.

**The constraint is CPU, for Chromium and screen capture.** Per stream you pay
for a headless Chromium rendering the page, an `x11grab` capture, a pixel-format
conversion, and then a nearly free VAAPI encode. Roughly a third to a half of a
core each.

That is why the existing node is the wrong host: an **i3-7100 is 2 cores / 4
threads**, and it is already running the cluster's workload. Four Chromium
instances plus captures would saturate it. An i5-7500 (4C/4T) is enough; an
8th-gen i5 (6C/6T) is comfortable. 16 GB RAM either way — four Chromium
instances at 300-800 MB each is what actually fills memory.

**A trick that changes the CPU maths.** Dashboards do not need 30 captures per
second. Capture at a low input framerate and let ffmpeg duplicate frames up to a
compliant output rate:

```bash
ffmpeg -f x11grab -framerate 5 -i :99 ... -r 25 -f mpegts udp://...
```

The receiver still sees a standard 25fps stream (it supports 1080p at
24/25/30/50/60), duplicated frames encode to almost nothing, and capture cost
drops several-fold. This is the single biggest lever if CPU gets tight.

**Consider keeping it out of the cluster.** A single-purpose streaming appliance
gains little from cluster membership, and joining one adds quorum entanglement —
going from one node to two means either node being down leaves the other unable
to make cluster changes, unless you add a QDevice. Standalone keeps the
dashboards independent of anything happening to the cluster.

**Prove it before buying or building anything.** The receiver-locks-onto-a-
software-stream question can be answered with a *single* stream in an LXC on the
existing i3 node — that load is trivial and needs no new hardware. Only once it
locks does the dedicated i5 box become worth setting up.

**And note project 1 does not wait on any of this.** Retiring the ChromeBoxes
onto one SFF with a 4-output card involves no encoding and no unknowns, so it
can be done first, independently, and kept even if project 2 never happens.

### Passing the iGPU into an LXC

For the test container, on the Proxmox host, in `/etc/pve/lxc/<ctid>.conf`:

```
dev0: /dev/dri/renderD128,mode=0666
```

or the longer form that also works on older Proxmox:

```
lxc.cgroup2.devices.allow: c 226:0 rwm
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.mount.entry: /dev/dri/renderD128 dev/dri/renderD128 none bind,optional,create=file
```

`/dev/dri/renderD128` is the render node (major 226, minor 128) that carries
VAAPI; on an unprivileged container the in-container group needs to map to the
host's `render` group. Install `intel-media-va-driver` (iHD) or `i965-va-driver`
plus `vainfo` inside the container and confirm with:

```bash
vainfo | grep -i h264
```

Check first that the host has `/dev/dri/renderD128` at all — some ThinkCentre
BIOSes disable the iGPU when a discrete card is fitted.

### The trade to make deliberately

Consolidating 8 devices into 1 changes the blast radius: today a dead ChromeBox
takes out one dashboard, afterwards a dead host takes out all four on every TV.
Worth choosing on purpose — a spare imaged box, an HA'd LXC, or keeping one
transmitter and ChromeBox as a fallback for the dashboard that matters most.

**The upside beyond consolidation:** there are only four dashboards today because
there are four transmitters. In software a channel is just another process on
another Group ID, and there are 64 of them. Per-department or per-screen content
becomes a config change rather than a purchase.
