# Troubleshooting

A field guide to what actually went wrong on a real 29-receiver fleet, and
how each was diagnosed — including the approaches that did **not** work, so
the same dead ends are not walked twice.

---

## Why do they lose the signal in the first place?

This is rare — the fleet runs for a year or more between incidents — and it
follows a **sudden power cycle**. One observed event (8 Sep 2026) pins down the
mechanism precisely:

- the power cut hit the part of the building holding the **transmitters**;
- exactly **one** receiver went dark, and it was in a part that **kept power** —
  so it never rebooted;
- it was still on the **correct channel**, just receiving nothing;
- **many other receivers on that same channel were unaffected**;
- unplugging and replugging that one receiver brought it straight back.

That rules out the two obvious theories. The Group ID clearly persists, so it is
not a settings-reversion problem. And it was not a VLAN-wide multicast failure
either, or its neighbours on the same stream would have dropped too.

What is left is a **per-receiver failure to re-acquire**: the transmitter
disappeared mid-stream underneath a receiver that stayed up, and that one unit
never re-joined when the stream came back. Whether a given receiver survives
looks like luck — a decoder or IGMP-membership state that depends on exactly
what it was doing at the instant the stream vanished. Nothing in the network
configuration will reliably prevent it, and with a once-a-year frequency it is
not worth chasing further.

So the goal is not prevention, it is **recovery without a ladder**.

### The fix, remotely

Unplugging works because it forces the receiver to re-join the multicast group
and re-initialise its decoder. Two ways to do that over the network:

- **Re-acquire** (the `re-acquire` link on each card) — bounces the receiver to
  an unused Group ID and straight back. About four seconds of black screen.
  This is the right first move.
- **Reboot** — the `reboot` link. Equivalent to the plug, but roughly a minute
  of black screen. Use it if a re-acquire does not take.

**Re-sending the same channel does not work**, which is worth knowing if you are
tempted to script something simpler. Firmware that already believes it is on
channel 3 treats `set_group_id 3` as a no-op: the command returns OK, the
read-back confirms channel 3, and the screen stays black. Verified against the
emulator, which models this behaviour deliberately. That is why re-acquire
bounces through another channel instead.

### After a power event

Open the dashboard and look for **`no signal`** chips — a receiver on the right
channel with no stream is precisely this failure. Click **re-acquire** on each.

Or let it handle itself:

```json
"auto_nudge_on_signal_loss": true,
"auto_repair_after_polls": 2
```

With that set, a receiver that reports no video lock for two consecutive polls
gets re-acquired automatically, and the event log records what happened. In
testing, two deliberately stuck receivers were detected and recovered inside a
single poll cycle without intervention.

*One caveat:* `no signal` is also what you see when a transmitter is genuinely
off or a dashboard PC is asleep. Re-acquiring then is harmless — it just reports
`still no signal - check the transmitter`, which is a useful distinction in
itself.

*Footnote on the network:* these are multicast streams and Ecler's spec sheet
requires *"IGMP and Jumbo Frames compliance"*. If dropouts ever begin happening
**without** a power event, that is when to ask whether the VEO VLAN has an IGMP
querier and whether snooping is consistent across every switch in the path.
Given the current once-a-year, power-linked pattern, it is not the culprit here.

## Reading a receiver's own IP off the screen (the OSD trick)

The fastest way to identify an unmanaged receiver, found on 2026-09-09 after
sniffing and ARP probing both failed:

**Set it to a channel no transmitter uses, using the physical `+`/`-` buttons.**
With no stream to show, the unit draws its own status page on the TV:

```
version:        V1.01.r0
IP address:     10.0.2.23
   IP address:  0.0.0.0
Group ID:       CH5
Device ID:      TV_OFFICE_23
Status:         Waiting for connection...
```

Its address, its name and its firmware, straight off the screen. Then press the
buttons back to its proper channel. No ladder work beyond reaching the buttons,
no network archaeology.

**Worth trying before anything else** when a receiver is present in a switch MAC
table but absent from the manager. The same information can be had from the
dashboard's **hold dark** on a receiver it already knows, which is a free way to
confirm the OSD shows what you need before walking anywhere.

### What did not work, and why

Recorded so it is not repeated:

- **`tools/sniffmac.py` (passive listening)** heard all 27 managed receivers,
  because the manager polls them and they answer, and neither unmanaged one —
  even after two PoE power cycles. A settled receiver's only unprompted traffic
  is IGMP, and snooping switches absorb membership reports before they reach
  another port.
- **PoE cycling** produced nothing on the wire. Note these units also have a
  DC input, so a unit on a wall adapter is not affected by `no inline power`
  at all.
- **The off-subnet theory was wrong.** Both units turned out to hold ordinary
  `10.0.2.x` addresses inside the range that had been swept repeatedly. They
  are missing from the manager because they **do not answer TCP 9999**, not
  because they are unreachable. `.23` had even answered the first two scans and
  none since, which is the signature of a wedged control server rather than a
  network problem.

The lesson: when a device is visibly working, ask *it* rather than the network.

## When the screen count still will not add up

The scan is **authoritative for VEO devices in the ranges it covered**. If it
answered on port 9999 and speaks the protocol, it is listed. So once a range has
been swept cleanly, "more screens than receivers" has only three remaining
explanations:

1. **A receiver outside the ranges scanned.** Widen the sweep a subnet at a
   time — `10.0.1.1-254`, `10.0.2.1-254`, `10.0.3.1-254` — rather than
   reaching for a whole `/16`, which disturbs the video (see the wide-sweep
   warning in [usage.md](usage.md)). Widening
   is what turned up `.31` and `.33`, outside the `.1-30` the old spreadsheet
   implied.
2. **An HDMI splitter.** One receiver, several screens, identical content,
   nothing visible over the network. `identify` is the only way to see it: click
   it and count how many screens go dark.
3. **The screen is not fed by an Ecler at all** — its own mini PC, a smart TV
   browser pointed at the URL, or a transmitter's HDMI loop output.

**A distinction that matters when interpreting the dashboard:** the `signal`
chip means the *receiver* has locked onto its multicast stream. It says nothing
about whether a TV is attached, switched on, or showing anything. A powered
receiver behind a dark screen looks identical to a working one.

### Why pfSense cannot help find the receivers

Checked on 2026-09-09, and the answer is instructive: **the receivers do not
appear in pfSense's ARP table at all.**

ARP entries are created by conversation, not by existence. pfSense only holds an
entry for a host it has recently exchanged traffic with, and the receivers never
talk to it: they sit on VLAN 20 taking multicast from transmitters on the same
VLAN, and the manager reaches them directly at layer 2. Nothing they do crosses
the router, so it has no reason to ARP for them.

What *does* appear on that VLAN is revealing:

| Seen in ARP | Why |
|---|---|
| `10.0.2.200` (`00:1a:96:cc:…`) | the manager container — it has a gateway set on its TV-VLAN leg, so it does talk to the firewall |
| `10.0.9.x` | the televisions, which reach the internet for updates |
| `10.0.0.1` | pfSense itself |
| **no receivers, no transmitters** | they never route |

Useful side effect: this maps the VLAN's addressing. The televisions live around
`10.0.9.x`, the video-over-IP devices around `10.0.20-21.x`.

**So `tools/scan.py` is the authoritative tool here, not the firewall.** ARP
could only ever show a device that had recently spoken to pfSense, which for a
receiver means essentially never. If you do want to populate ARP for a subnet,
pinging it from **Diagnostics -> Command Prompt** forces resolution:

```sh
for i in $(jot 254 1); do ping -c1 -t1 10.0.2.$i >/dev/null 2>&1 & done; wait
arp -an | grep '10.0.21'
```

That is the same address-probing that caused the outage above, though, so keep
it to one `/24` at a time. The scan already does this better, and checks that
what answers really is a VEO.

## More screens than receivers: HDMI splitters

Found on 2026-09-09, and worth understanding before trusting any count.

**The dashboard counts receivers, not screens.** Those are not the same number.
A VEO-XRI1C has a single HDMI output, but an external 1-to-N HDMI splitter can
drive several TVs from it — and because every screen then shows *identical*
content, a splitter is completely invisible over the network. Nothing in the
protocol reveals it.

The same trick appears on the source side: a splitter between a ChromeBox and
its transmitter lets a local screen show the dashboard directly, bypassing the
video-over-IP path entirely. (The VEO-XTI1C also has an HDMI local loop output,
which achieves the same thing without a splitter.) That is the likely reason the
big Canteen screen kept working after its apparent receiver was reassigned: it
was never being fed by that receiver.

So a channel can legitimately have **more physical screens than receivers**, and
a channel with a transmitter and *zero* receivers can still be driving a screen.

### The identify button is a splitter detector

No new tooling needed. **Click identify and watch how many screens go dark.**

- one screen blinks -> that receiver drives that screen alone
- two or more blink together -> they share one receiver through a splitter
- the screen you expected does not blink -> it is fed from somewhere else
  entirely: a local splitter, a transmitter loop-out, or its own PC

That distinguishes all three cases in a few seconds each, and it is worth doing
during the naming walk rather than as a separate exercise. Name what the
receiver drives, e.g. `Office floor 1 (x3 via splitter)`.

### Finding receivers that were never in the spreadsheet

Confirmed on 2026-09-09: 13 screens in the office areas against 10 receivers
assigned to that channel, so some are genuinely unaccounted for. The original
sweep covered `10.0.2.1-30` because that is what the inherited spreadsheet
listed — and that spreadsheet had already proved incomplete once, missing
`.24`-`.30`.

Scan the whole receiver subnet and compare against the live config:

```bash
pct exec 108 -- python3 /opt/eclermanager/tools/scan.py \
    10.0.2.1-254 \
    --config /etc/eclermanager/config.json \
    --names /opt/eclermanager/devices.txt
```

`--config` marks every device that answers but is **not** in the config with a
`*`, and lists them separately at the end, along with anything in the config
that did not answer. That is the audit, in one command.

Two details that matter on this network:

- **The TV VLAN carries the televisions themselves**, so answering on port 9999
  does not make something a VEO. The scan now checks that a device actually
  speaks the protocol — a Group ID, firmware, MAC, or a VEO model name — and
  reports anything else as `not a VEO?`, excluded from the receiver counts.
- **MAC addresses are reported**, and the scan prints the OUI prefixes it saw.
  Searching the firewall's ARP table for those finds VEO devices anywhere on
  the network, including outside any range guessed here — which is the reliable
  way to cover a whole `10.0.0.0/16` rather than sweeping 65k addresses.

For ranges over 512 addresses the scan sweeps for open ports first at high
concurrency, then pauses and opens real sessions only to the hits. The pause is
deliberate: these units serve one session at a time, and reconnecting
immediately after the sweep leaves them accepting TCP while answering nothing.

## "Assigned" is not "showing"

A distinction that causes real confusion when counting screens.

Groups are keyed on each receiver's **expected** channel, so a heading reading
*"Office — 10 assigned"* means **ten receivers are assigned to Office**, not
that ten screens are showing it. A receiver assigned to Office but sitting on
Reception still appears under Office, amber. A receiver assigned to Reception
stays under Reception even if it is physically an office screen.

The heading shows both whenever they differ — *"10 assigned · 8 on it now"* —
and `/api/state` reports `summary.by_expected_channel` and
`summary.by_actual_channel` side by side.

**And `expected_group_id` is a guess, not a fact.** It was seeded from whatever
each receiver happened to be showing during the very first scan, which includes
any drift that was already there. So the assignment records where a TV *was*
when the tool first met it, not where anyone decided it should be. Correcting
those is what the naming walk is for.

### Could the manager be reading channels wrongly?

It could once, and did: a reply-pairing bug shifted every value by one command,
so a channel number was reported as a video-lock state. That is fixed and pinned
by tests against the exact bytes these units send, including a deliberately slow
device.

To check any individual card yourself, use **raw** — it shows the exact bytes
that receiver replied with on the last poll, so the reported channel can be read
straight out of the device's own answer.

## What the switch's IGMP table tells you

`show ip multicast vlan 20` on the Ruckus ICX7150 gave three useful things
(2026-09-09, switch `switch-a`):

**1. Which channel a port is receiving, without needing its IP.** Every port
with a receiver showed `group: 239.255.42.44`. Channels are therefore
identifiable straight from the switch, which matters for a receiver the manager
cannot see. The observed mapping, with `239.255.42.42` as the manual's default:

| Channel | Multicast group | Evidence |
|---|---|---|
| 1 Reception | `239.255.42.43` | **confirmed** — `switch-b` port 1/1/28 is B2B, which the manager has on channel 1 |
| 2 Office | `239.255.42.44` | **confirmed** — `switch-a` ports 1/1/33-35 are known Office receivers |
| 3 Canteen | `239.255.42.45` | inferred from the pattern |
| 4 Warehouse | `239.255.42.46` | inferred from the pattern |

So `group = 239.255.42.(42 + channel)`, confirmed on two channels from two
switches by cross-checking against receivers the manager already knew. The
remaining two follow the pattern; `tools/probe.py <transmitter-ip>` reads the
group off a transmitter's own web page if you want them confirmed too.

**This is how the office screen count was finally resolved.** Two receivers were
present in switch MAC tables but absent from the manager, and their ports were
joined to `239.255.42.44` — Office. That accounted for the last two of thirteen
office screens without any splitters or non-Ecler sources being involved:

| | Screens |
|---|---|
| Known receivers assigned to Office | 10 |
| B2B on Reception, deliberately | 1 |
| Two unmanaged receivers, both on Office | 2 |
| **Total** | **13** | Channels carry a `multicast_group` field in the
config, shown in the dashboard group heading, so switch output can be read in
channel names rather than raw addresses.

**2. A querier exists.** `router ports: lg1(160) 10.0.0.20` and a query
interval of 125s, so the earlier worry about a missing IGMP querier was
unfounded. That theory is closed.

**3. Every receiver is an IGMPv2 client on a VLAN configured for IGMPv3.**

```
Version=3, Intervals: Query=125, Group Age=260, ...
VL20: dft V3, vlan cfg active, track, ...
  e1/1/33  has 1 grp, QR, default V3
  **** Warning! has V2 client (life=260),
```

`dft V3` means the version is inherited from a global setting, and FastIron's
own default is v2 — so v3 was configured deliberately somewhere.

**Worth flagging, not worth rushing.** The fleet runs fine day to day, so this
is a latent risk rather than an active fault. But it is a plausible mechanism
for the rare dropouts that started this project: with `Group Age=260`, a v2
membership that a v3-configured snooper fails to track expires after about four
minutes and the switch stops forwarding — leaving a receiver on the correct
channel with no picture, which is exactly the observed symptom.

If it is ever worth changing, the targeted fix is to set VLAN 20's IGMP snooping
version to 2 so it matches the clients, rather than changing anything globally.
FastIron allows the version per VLAN, and its own default is v2. **Confirm the
exact syntax interactively before committing** — in `vlan 20` context, type
`multicast ?` — and treat it as a change for whoever owns the network, with the
warning output above as the evidence.

## Can a transmitter say how many receivers are watching it?

**No, and this is inherent to multicast rather than a limit of these units.** A
transmitter sends UDP to a group address. There is no session, no handshake and
no client list, so it cannot know whether anything is listening — which is
exactly why the Canteen transmitter streamed to zero receivers indefinitely
without complaint.

What *does* know:

- **The switches.** IGMP snooping records which ports joined which multicast
  group, which is the authoritative "who is receiving channel N" and would also
  reveal a receiver this tool has never found. On a managed switch,
  `show ip igmp snooping groups` or the equivalent.
- **The IGMP querier** for the VLAN, if pfSense holds that role.

**Sniffing IGMP from the container is not a reliable substitute:** snooping
switches consume membership reports rather than flooding them, so a listener on
another port generally sees nothing.

**The practical alternative needs no switch access at all.** This manager knows
every receiver's channel, so if twelve screens show a dashboard and only ten
receivers are on that channel, the surplus screens are not fed by an Ecler. Use
**hold dark** on each receiver in turn: any screen that never goes blank for
*any* receiver is driven by something else — its own mini PC, a smart TV
browser, or a transmitter's HDMI loop output.

## The one receiver that cannot be re-addressed

`10.0.2.32` (MAC `00:1A:96:AA:BB:05`) **will not store an address change.**
Established on 2026-09-09 after trying every route:

| Attempt | Result |
|---|---|
| `set_static_ip` over port 9999, CRLF | replied `Ok`, changed nothing |
| `set_static_ip` over port 9999, LF | replied `Ok`, `get_ip_config` unchanged |
| Reboot after either | came back at `192.168.1.12` |
| Web UI IP block + Submit + Reboot | came back at `192.168.1.12` |

Its DHCP checkbox is off and it is statically configured, so this is not a DHCP
fallback — the write simply never reaches flash. Its channel *does* persist
(`Group 02` survives reboots), so the flash is not dead, just this field.

**Firmware is not the answer.** Ecler publishes `VEO-XRI1C.pkg` **v1.00r0
(Feb 2019)** as the only receiver firmware, and every unit here reports
**V1.01.r0** — newer than the published release. Flashing it would be a
downgrade onto older code, and the official path is their Control Center
utility rather than the web UI's upgrade field.

### Resolved: the unit was faulty, not the firmware

The screen was given a spare receiver, which then took a static address on the
first attempt — `set_static_ip` over the control port, followed by a reboot,
confirmed answering at the new address. So this firmware stores addresses
perfectly well and the old unit's flash was at fault. It is marked
`enabled: false` in the config with a "do not reinstall" note.

**A detail worth keeping for the next spare:** the replacement, straight out of
a box, showed video correctly but answered nothing on the network — the switch
counters told the story, `0 unicasts` and `0 broadcasts` sent against 86
broadcasts received, so it was ignoring ARP entirely while its multicast path
worked. **A factory reset fixed it** (hold the reset pin ~10s while powered;
the display blinks and shows `00`). Worth doing to any shelved unit before
concluding it is dead.

### The three real options

1. **Swap the unit for a spare** — the clean fix. Several receivers in the
   config never answer (`.7`, `.9`, `.11`, `.14`) and are believed to be
   spares, plus at least one on a shelf. A working unit takes a static address
   normally, and this one becomes the spare or scrap. Costs one visit to the
   screen.
2. **Manage it where it is.** It answers the control protocol perfectly at
   `192.168.1.12` — channel, signal, name, MAC, firmware, and channel
   switching all work. The manager can poll it there as long as the container
   keeps an address in that subnet, which is what
   `deploy/eclermanager-extra-ip.service` is for. Nothing about the dashboard
   changes; the receiver is simply listed with an unusual address.
3. **Report it to Ecler.** There is unusually specific evidence to hand: a
   control port that rejects the documented CRLF and accepts LF, and an IP
   field that acknowledges writes without storing them. If they have a build
   later than V1.01.r0 this is the case for asking. Slow, and worth doing in
   parallel with option 1 rather than instead of it.

### Installing the extra address permanently

Only needed for option 2:

```bash
pct push <ctid> deploy/eclermanager-extra-ip.service \
    /etc/systemd/system/eclermanager-extra-ip.service
pct exec <ctid> -- systemctl daemon-reload
pct exec <ctid> -- systemctl enable --now eclermanager-extra-ip
pct exec <ctid> -- ip -br addr show dev eth1
```

It runs before `eclermanager.service`, uses `ip addr replace` so a restart is
never an error, and removes the address on stop. Edit the unit if the address
or interface differs.
