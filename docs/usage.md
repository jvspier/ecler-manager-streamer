# Using the dashboard

What each control does, and the workflows worth knowing: naming the fleet
by location, commissioning a new unit, self-healing and backups.

---

## Using the dashboard

TVs are grouped by the transmitter they are meant to be showing, one section per
channel:

```
▸ 1  Reception     from 10.0.1.1 · 10 TVs   ●●●●●●●●●●   all good
▾ 4  Warehouse   from 10.0.1.4 ·  5 TVs   1 wrong channel
     [ rx-05 ] [ rx-06 ] [ rx-08 ] [ rx-25 ] [ rx-26 ]
```

**Groups start collapsed, except any with a problem.** A section with a TV on
the wrong channel, without a signal, or unreachable opens itself, so the first
look shows what needs attention and nothing else. Healthy groups stay as one
line with a dot per TV — green, amber, red — so a glance still tells you they
are fine. Click a heading to toggle it; **Expand all** / **Collapse all** do the
lot. Your choices are remembered in the browser, per group.

Grouping follows the channel a TV is *meant* to show, not the one it happens to
be on, so a drifted TV stays with its neighbours and is highlighted rather than
silently moving to another section.

Each card carries:

- the TV's name, IP and location, with a dot for reachability;
- **just a `signal` chip when all is well.** The channel number and name only
  appear when something is wrong — if the TV is where it belongs, the group
  heading already said what it is showing;
- an amber card and a `should be …` chip when the reported channel is not the
  expected one, and a red card when the unit did not answer;
- one button per channel — click to switch. ★ marks the expected channel. Every
  switch is read back from the device and reported honestly, including when the
  device accepted the command without actually changing;
- **Should be**, which changes the expected channel and writes it to
  `config.json`;
- **identify**, which blinks the TV — it drops to no-signal for a few seconds,
  then returns — so you can tell which physical screen a card belongs to. Watch
  how *many* screens blink: two at once means they share this receiver through
  an HDMI splitter;
- **hold dark**, which parks the screen on an unused channel and *leaves it
  there* until you press **Restore**. Six seconds is useless for a screen two
  floors away; a hold lets you set it, walk the building, find the blank
  screen, and put it back. Held cards are outlined in blue with a Restore
  button, the header offers **Restore held screens**, and a held receiver is
  **not** counted as drifted — nor touched by `auto_repair` or **Repair
  drifted**, so the walk cannot be undone under you;
- **re-acquire**, which bounces it to a spare channel and back, forcing it to
  re-join its stream: the remote equivalent of unplugging it, and the fix for a
  TV on the right channel showing nothing;
- **device settings** holds the things that are not everyday operations: an
  **Any channel (0-63)** field for a Group ID that no transmitter serves yet —
  a test stream, or a channel added before its source exists — plus setting the
  name stored in the device and changing its IP address. The channel buttons
  above only cover what is in your config, which is right for daily use and
  wrong when you need channel 5 once;
- **rename**, which edits the name, location and a free-text **note** in place
  and writes them to `config.json` — Enter saves, Escape cancels. Use it during
  the identify walk, and use the note for anything deliberate ("on Reception at
  the desk owner's request") so it is not later mistaken for drift. The id stays
  as it was, so links and in-flight actions keep working;
- **raw**, the exact bytes the device replied with on the last poll — the first
  thing to look at when something seems off. This is what revealed the
  NUL-padded prompt that had every reply shifted by one command;
- **reboot**, for when a re-acquire is not enough.

**Scan network** in the header looks for VEO devices on the network that are
**not** in the config, and shows them in a section at the top with an **Add to
config** button each (or **Add all**). Ranges come from `discovery_ranges` in the
config; if none are set it asks. A device is only offered if it actually speaks
the VEO protocol, so the televisions sharing VLAN 20 are not mistaken for
receivers. Adding one adopts the channel it is currently showing as its expected
channel, so it does not immediately report as drifted — rename it afterwards.

Scanning is **on demand by default**. Set `discovery_interval_hours` to have the
poller also scan periodically; it is `0` (off) unless you ask for it, because a
fleet this static does not need continuous scanning.

**Keep sweeps to subnets that might hold devices.** The button asks which
ranges to use, prefilled from the config. A `/24` takes under a minute and is
harmless. **A whole VLAN is not**: see the warning below. Sweeps run on a
background thread, so the request returns immediately and the button shows
progress.

### A wide sweep disturbs the video — measured, not theoretical

On 2026-09-09 a sweep of `10.0.0.0/16` (65,534 addresses) knocked **every**
receiver in the building off its multicast stream for about 35 seconds, and
timed two of them out completely. The event log caught it precisely: 17
`signal lost` events within four seconds, then `signal restored` across the
board 35 seconds later.

The cause is not the port probes themselves but the addresses that do not
exist: the gateway ARPs for each one, and 65k of those in quick succession is an
ARP flood that the switching fabric forwards multicast through.

Three things changed as a result:

- **The port sweep now takes the same per-host lock as polling**, so it can no
  longer land in the middle of a poll. That was a real bug: these units serve
  one control session at a time, and an interleaved connect leaves them
  accepting TCP while answering nothing.
- **The sweep is paced** — 32 concurrent probes in batches of 256 with a pause
  between batches, rather than 256 at once — so the ARP load is spread out.
- **Sweeps over 8192 addresses are refused** unless explicitly forced, with the
  reason. The dashboard shows that reason and asks for confirmation, so a `/16`
  cannot happen by accident.

**It also says something about the network.** A 35-second fleet-wide multicast
interruption under scan load suggests the multicast path is more fragile than it
should be — see the IGMP snooping and querier notes in
[troubleshooting.md](troubleshooting.md), now with evidence behind them rather
than speculation.

**And it was a live test of the tool.** Nothing needed intervention: every
receiver re-joined on its own, and the two that timed out came back. Had
`auto_nudge_on_signal_loss` been enabled, it would have started re-acquiring
receivers that were about to recover anyway — an argument for leaving the
watchdogs off, or for raising `auto_repair_after_polls` so a transient blip is
ridden out rather than reacted to.

### Spares / In storage

A receiver that is not in service — on a shelf, or set aside pending repair —
should not sit on the dashboard reporting as offline. **set aside** on any card
moves it to the **Spares / In storage** lane: it stops being polled, stops
counting towards the offline and drifted totals, and keeps its name, location
and note. **Return to service** brings it back, and it can be renamed while it
is there, so a note like *"factory reset restored it; good spare"* is recorded
against the right unit.

This is a config change only, so it works on a device that is unplugged — which
is the usual case for something in a box.

**Sort** in the header orders the cards inside each group, by name or by IP
address. Names sort naturally (`rx-2` before `rx-10`) and IPs numerically by
octet (`.2`, `.9`, `.10`, `.100`). Your choice is remembered in the browser.

**Repair drifted** in the header pushes every off-channel TV back to its
expected channel in one click.

### Channels, and moving a whole group

**Channels…** in the header lists every channel the manager knows: its Group
ID, name, the transmitter or streamer sending it, and a note. Add one there
when a new source appears, such as a software stream on channel 6. Nothing
fixes a channel's role: "Production" is just the name given to a Group ID, and
it can live on any of them. **button** decides whether the channel gets a
one-click button on every card. Untick it for a channel you are retiring, and
it stays available under **Should be** and **Any channel** (and remains on the
cards of TVs still meant to be on it). A channel that TVs are still expected
on cannot be removed; move them first. A channel no TV is meant to be on, and no
TV is on, gets no heading of its own; it is listed in one **On no TV** line
under the groups instead.

An expanded group has a **Move these N to** bar. It switches every receiver in
that group to the chosen channel in the background, one at a time. With
**also make it their expected channel** ticked (the default), the new channel
is written as every receiver's expectation *before* any of them is switched.
That way Repair and auto-repair never see a half-moved group as drift and
send it back. A unit that is offline or does not answer is reported and
skipped, and the rest carry on. Progress shows in a banner, and the result,
with any failures, arrives as a notification.

One at a time, about three seconds each, so a batch does exactly what
switching them by hand does.

### "no signal" on a software stream

A VEO's **video lock** flag is set by the hardware transmitters. On a
software stream it simply keeps whatever the previous channel left it with,
in either direction, for as long as the receiver stays there. A receiver that
arrives from a locked channel shows **signal**. One that arrives from an
unlocked channel (or via the empty channel a re-acquire uses) shows **no
signal**, although the picture is fine. This was established from the event
log on 2026-09-23. It first looked like a problem with batch moves, but a
manual switch from the same unlocked channel did exactly the same.

So untick **lock** for the streamer's channels under **Channels…**. Their
receivers are then left out of the no-signal count, the signal events and
auto-nudge.

What replaces the check is the streamer itself. Enter its address at the top
of **Channels…** (e.g. `http://10.0.0.21:8478`, the one you open it on). Each
poll the manager reads the streamer's `/api/streams` (public and read-only:
channel numbers and encoder figures, no URLs or settings). Every TV on a
channel the streamer serves then shows that stream's state:

| Chip | Meaning |
|---|---|
| **stream · 30 fps** | running and keeping up |
| **stream slow** | encoding slower than real time; frames are being lost |
| **stream stalled** | the process runs, but ffmpeg stopped reporting progress |
| **stream down** | meant to be streaming, but not running |
| **stream off** | disabled on the streamer |
| **streamer unreachable** | the streamer is not answering |

Anything but the first also marks the group and the header, and changes are
logged in **Recent events**. A restart quick enough to fall between two
polls still shows up there, as **stream restarted**: the streamer reports
when each stream started, and the manager notices when that moves. It watches the source, not each receiver's
decoder: a single TV with a bad cable still needs eyes on it. Without a
streamer address, those TVs show **signal n/a**.

**Recent events** at the bottom is collapsed by default and answers "how often
does this actually happen?" — every drift, signal loss, offline period, switch
and re-acquire, with a timestamp. Run with `--event-log events.jsonl` to keep
that history across restarts.

## Naming the fleet by location

The devices carry no names of their own, and an inherited spreadsheet label like
`tv_receiver_07` tells you nothing when a card goes amber. What you want to read
is *where the TV is*.

`devices.txt` is where that lives: `<ip> <name>` lines, read by
`tools/scan.py --names`, and those names become the dashboard labels and the
receiver ids in `config.json`.

**You do not need a ladder to work out which receiver is which screen.** Each
card has an **identify** button: it drops that TV to an unused channel for a few
seconds — the screen goes to no-signal — then puts it back on its own. So:

1. Run the scan and start the dashboard with whatever placeholder names you have.
2. Walk the building with a laptop or phone on the dashboard.
3. For each unnamed card, click **identify** and note which screen blinks.
4. Click **rename** on that card and type where it is. Saved immediately.

Renaming from the dashboard writes to `config.json` in the container, which is
the live source of truth from then on.

**Get a second copy of that work.** `devices.txt` is only read when *generating*
a config, so it goes stale the moment you start renaming. **Export names** in the
header downloads the live fleet back out in `devices.txt` format — grouped by
channel, locations carried as comments, and it round-trips through
`tools/scan.py --names`. Drop it over the file in the project and your naming
work lives in two places instead of one container:

```bash
curl -u <user>:<password> http://<host>:8477/api/inventory > devices.txt
```

Worth doing at the end of a naming session. A plain copy of the config works too:

```bash
pct pull <ctid> /etc/eclermanager/config.json ./config.backup.json
```

Adjust how long the blink lasts with `"identify_dwell_seconds"` (default 6,
clamped to 1–30). Longer if you are walking between rooms.

The same trick identifies your transmitters. The scan reports each one's Group
ID; a TV sitting on that channel shows you what that transmitter is streaming,
which is how `10.0.1.1` becomes "Reception" rather than "transmitter 1".

## Recording *why* a TV is set up as it is

Every receiver has an optional **note**, edited in the same inline editor as its
name and location. Use it for anything deliberate that would otherwise look like
a mistake to whoever looks next:

> `rx-31` — *"on Reception at the desk owner's request"*

Without that, a TV showing the wrong dashboard for a good reason is
indistinguishable from one that drifted, and the next person to tidy up will
"fix" it. The note appears on the card and is included in a **Backup**.

## Setting up a new or repaired device

**Set up new device** in the header. A factory-reset receiver always appears at
the same address, so commissioning one is a repeatable flow rather than a hunt.

1. **Factory reset the unit** — hold the reset pin ~10s while powered, until
   the display shows `00`. Do this even to a brand-new spare: a shelved unit can
   show video perfectly while ignoring the network entirely, and a reset is the
   cure — see [troubleshooting.md](troubleshooting.md).
2. Make sure the container has an address in that subnet. This is why the extra
   address is worth keeping rather than adding it each time:
   ```bash
   pct exec <ctid> -- ip addr add 192.168.1.240/24 dev eth1
   ```
3. Click **Set up new device**. It reads whatever is on `192.168.1.12` and shows
   its name, MAC, firmware and current channel — so you can confirm it is the
   unit you think before changing anything.
4. Fill in a dashboard name, the name to store in the device, the address, and
   the channel. Netmask and gateway are prefilled from `setup_netmask` /
   `setup_gateway` in the config.
5. **Set up and add to the fleet.**

It then does, reporting each step separately:

| Step | |
|---|---|
| device name | written first, while it is still reachable at the setup address |
| channel | set if it differs from what you chose |
| address | stored — this firmware applies it at reboot |
| reboot | to apply the address |
| confirmed | **waits for it to answer at the new address** |

**Only then is it added to the config.** If it never answers there, nothing is
added and the message says where the device still is — so a failed setup cannot
leave the manager pointing at an address nothing is on.

The setup address and the prefilled netmask/gateway are configurable:

```json
"setup_address": "192.168.1.12",
"setup_netmask": "255.255.0.0",
"setup_gateway": "10.0.0.1"
```

`192.168.1.12` is the documented factory default for a receiver; a transmitter
is `192.168.1.11`.

### Changing an existing device's address or name

**device settings** on any card, under the other links. It edits what is stored
*in the device*, as opposed to **rename**, which edits the dashboard's own
label:

- **Set name** writes the device's own name — the one in its web page, its OSD
  and `scan.py`'s "names the devices report for themselves". `set_device_name`
  stops at the first space, so a name with spaces is refused up front with the
  underscore version suggested, rather than silently storing the first word.
- **Change IP** moves the device and **follows it in the config**, which is the
  reason to do it here rather than by hand: the config entry is rewritten only
  once the device answers at the new address. It refuses an address another
  receiver already uses, and a gateway outside the new subnet.

## Self-healing (opt-in, off by default)

In `config.json`:

```json
"auto_repair": true,                 // wrong channel -> put it back
"auto_nudge_on_signal_loss": true,   // no video lock -> re-send the same channel
"auto_repair_after_polls": 2         // only after N consecutive bad polls
```

`auto_repair` puts a TV found on the wrong channel back where it belongs.
`auto_nudge_on_signal_loss` handles the failure actually seen in this building: a
receiver on the correct channel with no stream gets re-acquired automatically.

**The catch:** with `auto_repair` on, anyone who deliberately changes a channel
(from the dashboard, the IR remote, or the buttons) gets overridden within a
minute or two. If you want a TV to hold a different channel, change its **Should
be** value rather than fighting the watchdog. Start with both off, watch the
event log for a week, then decide.

---

## Backup and restore

Two header controls, for two different jobs:

- **Export names** downloads a `devices.txt` — human-readable, round-trips
  through `tools/scan.py --names`, good for keeping in the repo.
- **Backup** downloads the *complete* config: every name, IP, location, channel,
  expected channel and setting, as `eclermanager-config-<timestamp>.json`.
- **Restore…** takes one of those files back. It replaces everything.

Restore is deliberately careful, because it is the button you reach for when
something is already wrong:

1. The uploaded config is **validated before anything on disk is touched** —
   duplicate IPs or ids, out-of-range channels, a missing `receivers` list, or a
   file that is not a JSON object are all refused with the reason, and the
   running config is left exactly as it was.
2. The config being replaced is copied to
   `config.json.<timestamp>.bak` in the container first.
3. The new config is adopted **without restarting the service**. Receivers that
   survive the change keep their status and drift counters, so a restore does
   not blank out what is known about the fleet.

Verified end-to-end: back up, deliberately corrupt three names, restore, names
come back — and a config with duplicate IPs is refused with
`receivers reuse ip(s)` while the fleet keeps running.

Worth taking a Backup after a naming session, and keeping it somewhere that is
not the container:

```bash
curl -u <user>:<password> http://<host>:8477/api/config > eclermanager-backup.json
```
