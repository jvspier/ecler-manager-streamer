# Ecler VEO Manager

A dashboard for **Ecler VEO-XRI1C** H.264 video-over-IP receivers: see every TV,
what channel it is on, and whether it still has a picture — and switch channels
from your laptop instead of from a ladder.

Pure Python 3.11+ standard library. No `pip install`, no build step.

---

## Can these actually be managed over the network? Yes.

Findings from Ecler's own documentation plus the OEM platform these units are
built on:

| Capability | How | Status |
|---|---|---|
| Switch channel remotely | `set_group_id <0-63>` over **TCP port 9999** | Documented by Ecler (manual §8.3) |
| Line endings | CRLF per the manual — but **some units accept only LF** and close the session on CRLF | **Confirmed**: most units take CRLF, `10.0.2.23` and `.32` do not |
| Set a static IP | `set_static_ip ip <a> netmask <m> gateway <g>` — stored, applied at reboot | **Confirmed** from a unit's own usage message |
| Set the device's own name | `set_device_name <name>` — **stops at the first space**, so use underscores | **Confirmed**: "TV RECEIVER 11" stored as "TV" |
| Read current channel | `get_group_id` | **Confirmed** on firmware `Rx V1.01.r0` |
| Detect "lost its picture" | `get_video_lock` → `Lock` / `Unlock` | **Confirmed** |
| Device name | `get_device_name` → e.g. `Recieve 01` | **Confirmed** |
| Firmware / link state | `get_fw_version`, `get_lan_status` → `Link_Up` | **Confirmed** |
| IP configuration | `get_ip_config` | **Confirmed** |
| Remote reboot | `reboot` | Listed by the device's own `list` |
| Force a stream re-acquire | `set_group_id` to a spare channel and back | Built from the above |
| Web UI | `http://<ip>/`, login `admin` / `admin` | **Confirmed** — serves an "IPTV RX/TX" page |

### The wire format, as actually observed

Worth writing down, because it is not what you would guess and it defeats a
naive line-based parser:

```
connect  ->  ==============================
             ========IPTV RX Server========
             ==============================
             \x00
send     ->  get_group_id\r\n
receive  <-  input>\x00get_group_id\r\n1\r\n
```

Three details that matter, all handled in `veo.py` and pinned by tests in
`tests/test_veo.py::TestRealDeviceWireFormat`:

- the prompt is **NUL-padded** (`input>\x00`) and arrives *in front of* the
  echoed command, not after it;
- the connect banner is a block of `=` rules, which must not be mistaken for a
  reply — and it can arrive bundled with the first answer;
- values come back bare (`1`, `Lock`, `Link_Up`), not as `key=value`.

**Some units reject the documented CRLF.** The manual specifies `\r\n`, and 27
of the 29 receivers here accept it. Two do not: sending CRLF makes them close
the connection with a broken pipe, while plain `\n` works normally. This is
indistinguishable from a dead device unless you try both — `.23` sat outside the
manager for weeks because of it.

`veo.py` therefore tries CRLF first, falls back to LF, and **remembers which
works per host**, so only the first poll of an affected device pays for the
failed attempt. The `raw` view says so explicitly when a device needed the
fallback.

**These units serve one control session at a time.** Connecting, closing, and
immediately reconnecting leaves a unit accepting the TCP connection but
answering nothing — which looks exactly like an unsupported command. That is why
`tools/scan.py` probes and reads in a *single* session per host rather than
sweeping ports first, and retries a device that connects but stays mute.

Transmitters are less consistent than receivers here: one was observed sending
nothing and closing the connection part-way through a session, so
`read_status()` keeps whatever a device did say instead of discarding a partial
result.

The "channel" on the front-panel display is Ecler's **Group ID** (0–63). Every
transmitter streams on one Group ID; a receiver shows whichever Group ID it is
set to. Switching channels = setting the receiver's Group ID.

### Two things worth knowing before you trust the UI

1. **The front-panel LED does not follow a remote change.** Ecler's manual says
   so explicitly for web-driven changes. After switching from this dashboard the
   LED may still show the old number while the TV shows the new channel. The
   dashboard reads the channel back from the device after every switch, so *the
   dashboard is right and the LED is stale* — not the other way round.
2. **Port 9999 has no authentication.** Anyone who can reach a receiver on that
   port can change what its TV shows. That is the device's design, not this
   tool's. Keep the VEO subnet away from general user access.

### Not applicable here

- The **VEO-XCTRLG2** hardware controller only manages the newer *G2* (H.265)
  units, not the H.264 `VEO-XRI1C`.
- Googling will surface the **VEO-XTI2L / XRI2L** TCP/IP control manual with
  commands like `e e_reconnect::0002`. That is a **different platform** — those
  commands do nothing on a 1C.

---

## Where to run it

The receivers sit on their own TV VLAN (20 in these examples) and are not
reachable from a normal
user VLAN — ICMP included, so a failed `ping` from your desk tells you nothing
about the device. Whatever runs this needs a foot in that VLAN.

### Host prerequisites, before you build anything

**1. The switch port to the Proxmox host must be a trunk that still carries the
VLAN you manage the host on.** Adding tagged VLAN 20 should be additive — but if
the host's own VLAN got replaced rather than kept, you lose the host. Confirm
the host is still reachable before going further.

**2. Check how your bridge handles VLAN tags:**

```bash
grep -A6 'iface vmbr0' /etc/network/interfaces
```

- `bridge-vlan-aware yes` → VLAN filtering. `tag=20` on a container NIC works
  directly. Make sure `bridge-vids` includes your TV VLAN (a bare
  `bridge-vids 2-4094`
  covers it).
- no such line → a traditional bridge. Proxmox still honours `tag=20` by
  auto-creating a `vmbr0v20` bridge and VLAN sub-interface behind the scenes, so
  **try it before changing anything.**

Do not flip a working bridge to VLAN-aware just to tidy it up: it needs a
network reload and can cost you host connectivity if the trunk is not exactly
right. Only touch it if tagging genuinely does not work.

**3. Prove the VLAN reaches a receiver** before installing anything. From inside
the new container (bash is in the Debian template, so this needs nothing
installed):

```bash
timeout 3 bash -c 'cat </dev/null >/dev/tcp/10.0.2.11/9999' \
  && echo "port 9999 reachable" || echo "NOT reachable"
```

If that fails, the problem is switch/VLAN/IP — not this software — and no amount
of configuring the dashboard will help.

### Container type: unprivileged, no nesting

```
--unprivileged 1 --features nesting=0
```

Both are what `deploy/create-container.sh` sets, and both are deliberate:

- **Unprivileged.** Nothing here needs host privileges. The VLAN tagging is done
  by the host on the veth, not by the container, and a Python process opening
  outbound TCP connections needs no capabilities at all — the systemd unit runs
  with an empty `CapabilityBoundingSet`.
- **No nesting.** Nesting exists for running Docker/Podman or containers inside
  the container. You are not doing that, and turning it on exposes the host's
  `procfs` and `sysfs` to the guest — a real security trade for zero benefit.
  Try without it first.

  Creating a Debian 13 container may print `WARN: Systemd 257 detected. You may
  need to enable nesting.` That is Proxmox hedging, not a failure — newer systemd
  wants mounts an unprivileged container cannot always make. Test it rather than
  guessing:

  ```bash
  pct exec <ctid> -- systemctl is-system-running     # running or degraded = fine
  pct exec <ctid> -- systemctl --failed
  ```

  If systemd is healthy, leave nesting off. If it is broken, or units fail with
  mount or cgroup errors, then `pct set <ctid> -features nesting=1` followed by a
  restart is the correct fix — accepting that it exposes host `procfs`/`sysfs` to
  the guest. For a single-purpose internal container that is a reasonable trade.

The systemd unit is written for this environment. It deliberately omits
`ProtectKernelTunables`, `ProtectKernelModules` and `ProtectControlGroups`:
each has to remount `/proc/sys` or `/sys/fs/cgroup`, which an unprivileged LXC
cannot do, so the service would die with:

```
Failed to set up mount namespacing: Permission denied
```

They would also protect nothing — with no capabilities the service cannot reach
host kernel state regardless. If you ever move this to bare metal, add them back
for a little extra depth. And if you do hit that error from some *other*
sandboxing option, relaxing that option is the right fix — not enabling nesting.

### What the container's networking should look like

| | net0 (your VLAN) | net1 (TV VLAN) |
|---|---|---|
| Bridge | `vmbr0` | `vmbr0` |
| VLAN tag | your mgmt VLAN, or none if untagged | `20` |
| IPv4 | DHCP, or static + gateway | **static, no gateway** |
| Purpose | you reach `:8477`; apt reaches the internet | reaches the Eclers |

Three rules that matter:

- **Exactly one default gateway, on net0.** A gateway on net1 as well gives you
  two default routes and intermittently broken connectivity. The TV leg does not
  need one: the receivers are on its own subnet, so the on-link route is enough.
- **Static on net1.** VLAN 20 almost certainly has no DHCP server, given the
  Eclers are statically addressed. DHCP there means no address at all.
- **Leave MTU at 1500.** The jumbo-frame requirement in Ecler's spec sheet is
  for the *video* path between transmitters and receivers. This dashboard only
  exchanges short text commands on port 9999 and never carries video, so it has
  no use for jumbo frames.

In the Proxmox GUI that second NIC is: *Container → Network → Add → Bridge
`vmbr0`, VLAN Tag `52`, IPv4 Static, address `10.0.2.250/24`, Gateway left
**empty***.

### Setting the container up by hand

The Create CT wizard only defines **one** network interface, so net1 is added
afterwards. Wizard tab by tab:

| Tab | Setting |
|---|---|
| General | CTID (any free), hostname `eclermanager`, **Unprivileged container: checked**, Nesting: **unchecked** (under Advanced, or Options → Features later). A root password is optional — `pct enter` works without one. |
| Template | `debian-13-standard` |
| Disks | 4 GB is plenty |
| CPU | 1 core |
| Memory | 512 MB, swap 256 MB |
| Network | **net0 only** — your normal VLAN, with the gateway. DHCP is fine here. |
| DNS | leave blank to inherit the host's |
| Confirm | tick *Start after created* |

Then add the second interface: **Container → Network → Add**

- Bridge `vmbr0`, VLAN Tag `20`
- IPv4: **Static**, e.g. `10.0.2.250/16` (match the prefix your VLAN uses)
- Gateway: **leave empty.** The GUI puts a Gateway field right there and it is
  easy to fill in out of habit — don't. net0 already carries the only default
  route you want, and a second one makes outbound connectivity a coin flip.
  Because the receivers fall inside the prefix you just configured, they are
  on-link and need no gateway at all. Fixing it afterwards:
  `pct set <ctid> -net1 name=eth1,bridge=vmbr0,tag=20,ip=10.0.2.250/16`

Also set **Options → Start at boot: Yes**, which the wizard does not ask about.

Then verify, in this order:

```bash
pct exec <ctid> -- ip -br addr     # an address on BOTH eth0 and eth1
pct exec <ctid> -- ip route        # exactly ONE default route, via eth0
pct exec <ctid> -- timeout 3 bash -c 'cat </dev/null >/dev/tcp/10.0.2.11/9999' \
  && echo reachable
```

### Getting the code in and installed

No SSH into the container is needed — `pct push` and `pct exec` run from the
Proxmox host, so the container never has to accept a login. Note the `scp`
target below is the **Proxmox host**, not the container.

From your workstation, with the project checked out locally:

```bash
tar czf /tmp/eclermanager.tar.gz -C ~/dev/eclermanager --exclude=__pycache__ .
scp /tmp/eclermanager.tar.gz root@<proxmox-host>:/tmp/     # the HOST
```

The easy mix-up: `<proxmox-host>` is the machine whose shell prompt says
`root@<your-node>` and whose address you use for the Proxmox web UI — **not** the
container's own IP from its net0. Sending the tarball to the container's address
lands you at a password prompt it has no answer for. The files go laptop → host
→ container, and `pct push` does that last leg from the host side.

Then on the Proxmox host:

```bash
pct exec <ctid> -- mkdir -p /root/eclermanager
pct push <ctid> /tmp/eclermanager.tar.gz /root/eclermanager.tar.gz
pct exec <ctid> -- tar xzf /root/eclermanager.tar.gz -C /root/eclermanager
pct exec <ctid> -- bash /root/eclermanager/deploy/install.sh \
    --config /root/eclermanager/config.discovered.json
```

**Redeploys keep your login and your config.** `install.sh` never rewrites
`/etc/eclermanager/eclermanager.env` — it only corrects its ownership and mode —
so the service account survives an upgrade. `config.json` is likewise left alone
unless you pass `--config`, and even then the previous one is copied to
`config.json.<timestamp>.bak` first, because that file holds every rename and
"should be" edit made from the dashboard. Once you have started renaming, treat
the live `config.json` as authoritative and stop passing `--config`.

**Install the config with `--config`, not afterwards.** The service reads its
config once at startup, so a config copied into place after the restart is
ignored until the next one — and the dashboard sits there showing the previous
fleet, looking healthy and being wrong. `--config` puts it in place first, and
validates it before touching anything so a broken file cannot leave the service
crash-looping. If you do replace the config by hand, follow it with
`systemctl restart eclermanager`.

#### If you do want SSH into the container

The Debian template already runs `sshd`; what it does not have is a way to
authenticate root. Debian's default is `PermitRootLogin prohibit-password`, so
**installing a key needs no config change at all** — which is also the option
worth having. From your workstation:

```bash
scp ~/.ssh/id_ed25519.pub root@<proxmox-host>:/tmp/
```

Then on the Proxmox host:

```bash
pct exec <ctid> -- mkdir -p -m 700 /root/.ssh
pct push <ctid> /tmp/id_ed25519.pub /root/.ssh/authorized_keys --perms 600
```

That is it — `ssh root@<container-ip>` now works. Password login would instead
mean setting a password *and* loosening the policy:

```bash
pct exec <ctid> -- passwd root
pct exec <ctid> -- bash -c 'echo "PermitRootLogin yes" > /etc/ssh/sshd_config.d/99-root.conf'
pct exec <ctid> -- systemctl restart ssh    # may be socket-activated; harmless either way
```

Prefer the key. A container that is reachable from your whole office VLAN with a
root password is a worse trade than one that only takes a key.

`install.sh` does the rest — python3, service user, `/opt/eclermanager`,
`/etc/eclermanager/config.json`, the systemd unit — and stops short of starting
the service while the config is still the example, telling you to run the scan
first. Re-run it any time to upgrade the code; your config is left alone.

### Option A — Proxmox LXC with two NICs (recommended)

Give the container one leg on the VLAN you work from and one tagged into the TV
VLAN. You reach the dashboard from your desk; it reaches the Eclers. No router
or firewall changes needed.

`deploy/create-container.sh` does this. Run it **on the Proxmox host, as root,
from the project directory**. It shows you the exact `pct create` it intends to
run and waits for confirmation:

```bash
MGMT_VLAN=<the VLAN you work from> \
TV_IP=10.0.2.250/24 \
./deploy/create-container.sh --deploy
```

| Variable | What it is | How to find it |
|---|---|---|
| `MGMT_VLAN` | VLAN tag for the leg you reach the dashboard on. Omit if that VLAN is untagged on your bridge. | your switch config |
| `TV_IP` | A free static address for the TV VLAN leg, with prefix. | pick an unused one in the Ecler subnet |
| `TV_VLAN` | TV VLAN tag (default `20`). | — |
| `BRIDGE` | Proxmox bridge (default `vmbr0`). | `ip -br link` |
| `ROOTFS_STORAGE` | Storage for the disk (default `local-lvm`). | `pvesm status` |
| `CTID` | Container ID. Defaults to the next free one. | — |

**Set `TV_IP` to a static address.** It defaults to DHCP, but your Eclers are
statically addressed, which suggests VLAN 20 has no DHCP server — in which case
that leg would come up with no address at all and nothing would be reachable.
The script warns you if you leave it on DHCP.

**The TV leg deliberately gets no gateway.** Two default routes would break
routing; `net1` only needs the on-link route to its own subnet, which is where
the receivers live. If your receivers are in a *different* subnet that VLAN 20
routes to, add a static route inside the container afterwards.

After it is created, three checks worth running in order:

```bash
pct exec <ctid> -- ip -br addr           # an address on BOTH eth0 and eth1
pct exec <ctid> -- ip route              # exactly ONE default route, via eth0
pct exec <ctid> -- timeout 3 bash -c 'cat </dev/null >/dev/tcp/10.0.2.11/9999' \
  && echo reachable
```

Useful flags: `--dry-run` prints the command and stops; `--deploy` also pushes
the code in and runs the installer; `-y` skips the confirmation prompt.

Without `--deploy` it prints the four commands to push the project in yourself.
Either way the work inside the container is done by `deploy/install.sh`, which:

- installs `python3` if missing and checks it is 3.11+;
- creates a system user `eclermanager`;
- installs the code read-only to `/opt/eclermanager`;
- puts your config at `/etc/eclermanager/config.json`, owned by the service user
  (it must stay writable — the dashboard rewrites it when you change a
  "Should be" value);
- keeps the event log in `/var/lib/eclermanager/events.jsonl`;
- installs a hardened systemd unit and starts it — unless the config is still
  the example, in which case it stops and tells you to run the scan first.

It is idempotent: re-run it to upgrade the code, and your config is left alone.

**Keep the container an endpoint, not a route.** With an address in each VLAN it
could bridge them if IP forwarding were ever switched on. Debian ships with it
off; confirm and pin it:

```bash
pct exec <ctid> -- sysctl net.ipv4.ip_forward      # want: 0
pct exec <ctid> -- sh -c 'echo "net.ipv4.ip_forward = 0" > /etc/sysctl.d/99-no-forward.conf'
```

Then the only thing crossing between VLANs is TCP 8477 to this dashboard. Bear
in mind it has **no login** — anyone who can reach that port can switch every
TV. On a trusted internal VLAN that is usually fine; if not, use Option B.

### Option B — LXC only in VLAN 20

Single NIC, tagged into the TV VLAN. Better isolation, but needs a firewall
rule permitting
your VLAN to reach the container on 8477 — so it involves whoever owns the
network.

### Testing from your laptop

Putting your wifi into VLAN 20 works, and you do **not** have to disable
ethernet. Tell NetworkManager to keep the wifi off your default route and out of
your DNS, so only VLAN-20 traffic uses it:

```bash
nmcli connection modify "<wifi-connection>" ipv4.never-default yes \
                                            ipv6.never-default yes \
                                            ipv4.ignore-auto-dns yes
nmcli connection up "<wifi-connection>"
ip route get 10.0.2.11        # should show your wifi interface
```

Your ethernet keeps the default route and DNS; the on-link route for the TV
subnet arrives with the wifi lease. If the receivers are in a *different* subnet
that VLAN 20 routes to, add it explicitly:

```bash
nmcli connection modify "<wifi-connection>" +ipv4.routes "10.0.2.0/24"
```

## Quick start

### 1. Prove the protocol against one real unit (30 seconds)

Before configuring anything, confirm what your firmware actually supports. This
only reads; it changes nothing.

```bash
python3 tools/probe.py 10.0.2.11
```

You want to see, near the bottom:

```
parsed current channel : 1
parsed video lock      : True
```

- **`COULD NOT PARSE`, but the raw `get_group_id` block above shows a sensible
  number** → the reply is phrased in a way the parser does not recognise yet.
  The raw text is printed; the parser lives in `eclermanager/veo.py`
  (`parse_int`) and is easy to extend.
- **`get_group_id` returns `unknown command`** → your firmware only accepts the
  documented `set_group_id`. Switching still works; live channel readback does
  not. Set `expected_group_id` for each TV and use the dashboard as a
  "push the right channel to everything" button.
- **Connection refused / timeout** → wrong IP, unit unpowered, or port 9999 is
  blocked between your machine and the VEO subnet.

### 2. Find out what is actually out there

You do not need to hand-type fifteen entries, or open the cupboard to work out
which spare receivers are live. From a machine that can reach the VLAN:

```bash
python3 tools/scan.py 10.0.1.1-10 10.0.2.1-30 \
    --names devices.txt \
    --transmitter 10.0.1.1 --transmitter 10.0.1.2 --transmitter 10.0.1.3 \
    --write-config config.json
```

`devices.txt` is your fleet inventory — plain `<ip> <name>` lines, which is how
this tends to be kept anyway, so a table pasted out of pfSense works unchanged.
Names from it are used for the dashboard labels and receiver ids, instead of the
scanner guessing from the address. Transmitters and receivers can live on
different subnets; just list both ranges.

This opens one TCP connection per address to port 9999 and issues read-only
`get_*` commands. It changes nothing. Output looks like:

```
IP              role          chan  signal  device name           firmware
10.0.2.11     receiver?     1     yes     -                     v1.0.x
10.0.2.12     receiver?     4     NO      -                     v1.0.x
```

**On "which receivers are in use":** a receiver with no PoE — in a box, or
unplugged like the one in the photos — will not answer, so it simply will not
appear. Anything that answers is powered and on the network. What the scan
*cannot* see is whether a TV is attached or switched on, so sanity-check the
list against your labels.

For a unit you want to keep in the config but not poll, set
`"enabled": false` on it; it is then hidden from the dashboard rather than shown
as permanently offline.

### 3. Check the generated config

```bash
$EDITOR config.json
```

Rename the channels, fill in locations, and check `expected_group_id` on each
TV — the scan copies whatever each receiver *happened* to be showing, so if one
was already wrong when you scanned, that wrong value became its expectation.

### 4. Run it

```bash
python3 run.py
# → http://127.0.0.1:8477/
```

To let colleagues reach it, `python3 run.py --host 0.0.0.0` — but read the
warning it prints: no login, and it can switch every TV in the building.

### Try it without hardware

```bash
python3 tools/mock_veo.py --fleet 4 --chaos 20   # emulates 4 receivers that misbehave
python3 run.py --config /path/to/a/mock-config.json
```

The mock listens on `127.0.0.2`–`127.0.0.5:9999` and emulates three different
firmware reply phrasings, so the parser gets a real workout.

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
should be — which is the IGMP snooping and querier question in the section
below, now with evidence behind it rather than speculation.

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

**Recent events** at the bottom is collapsed by default and answers "how often
does this actually happen?" — every drift, signal loss, offline period, switch
and re-acquire, with a timestamp. Run with `--event-log events.jsonl` to keep
that history across restarts.

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

## When the screen count still will not add up

The scan is **authoritative for VEO devices in the ranges it covered**. If it
answered on port 9999 and speaks the protocol, it is listed. So once a range has
been swept cleanly, "more screens than receivers" has only three remaining
explanations:

1. **A receiver outside the ranges scanned.** Widen the sweep a subnet at a
   time — `10.0.1.1-254`, `10.0.2.1-254`, `10.0.3.1-254` — rather than
   reaching for a whole `/16`, which disturbs the video (see above). Widening
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
   cure (see below).
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

## Redeploying (this installation)

Container **108** on **pve1**, dashboard at
<http://10.0.5.20:8477/>. From the project directory on the workstation:

```bash
cd ~/dev/eclermanager && tar czf /tmp/eclermanager.tar.gz --exclude=__pycache__ .
scp /tmp/eclermanager.tar.gz root@pve1:/tmp/ && \
ssh root@pve1 '
  pct push 108 /tmp/eclermanager.tar.gz /root/eclermanager.tar.gz
  pct exec 108 -- tar xzf /root/eclermanager.tar.gz -C /root/eclermanager
  pct exec 108 -- bash /root/eclermanager/deploy/install.sh
'
```

**No `--config`.** That is the only flag that touches `config.json`, which holds
every name and expected channel set from the dashboard. Code updates never need
it. The login in `/etc/eclermanager/eclermanager.env` is never touched either.

Take a **Backup** from the dashboard before any redeploy that you have doubts
about — it is the one file that restores everything.

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

## Login

Off by default. Turn it on and the dashboard requires a service account, which
keeps out anyone who stumbles onto port 8477.

```bash
# in the container
python3 /opt/eclermanager/tools/setpassword.py --user tvadmin \
    --env-file /etc/eclermanager/eclermanager.env --owner eclermanager
systemctl restart eclermanager
```

It prompts for the password twice — never taken from the command line, where it
would end up in shell history — and writes:

```
ECLER_AUTH_USER=tvadmin
ECLER_AUTH_PASSWORD_HASH=scrypt$16384$8$1$…      # the password is NOT stored
ECLER_SESSION_SECRET=…                            # signs session cookies
ECLER_SESSION_HOURS=12
```

mode `640`, owned by the service user, read by systemd via `EnvironmentFile=`.
The password is kept only as an scrypt hash, so the file leaking (a backup, a
snapshot) does not hand over the password itself. The session secret is stable,
so a restart does not sign everyone out.

**Scripts keep working** — HTTP Basic auth is accepted on every endpoint:

```bash
curl -u tvadmin:… -X POST http://<host>:8477/api/repair
```

`/api/health` stays open, so an uptime check needs no credentials.

Failed logins are throttled per client address (8 per 5 minutes). Session
cookies are `HttpOnly` and `SameSite=Strict`, which also blocks cross-site
requests from carrying them.

### What this is and is not

It is a lock on the door: someone who finds the port cannot do anything without
the account. It is **not** a secure channel — the login is posted over plain
HTTP, so anyone able to watch traffic between your laptop and the container can
read the password. On a trusted internal VLAN that is usually the right
trade-off; if it is not, put a TLS reverse proxy in front.

Prefer the hash. `ECLER_AUTH_PASSWORD` (plaintext) is accepted for convenience
and logs a warning at startup.

**Account names**: letters, digits and `. _ - @ +`, starting with a letter or
digit, up to 64 characters — so `tvadmin` is fine. `setpassword.py` rejects
anything an env file would quietly mangle (surrounding spaces, `#`, quotes,
newlines) rather than writing a name that half works, and the server refuses to
enable a login configured with one.

Other knobs: `--env-file <path>` points at a different file, `--no-auth`
ignores any configured login for local testing.

## HTTP API

Everything the UI does is available directly. With a login configured, pass
credentials as Basic auth (`curl -u user:pass …`); without one, no auth is
needed.

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/api/state` | – | full fleet snapshot + event log |
| `GET` | `/api/receivers/<id>/raw` | – | last raw device replies |
| `POST` | `/api/receivers/<id>/channel` | `{"group_id": 2}` | switch, with read-back verification |
| `POST` | `/api/receivers/<id>/expected` | `{"group_id": 2}` | set + persist expected channel |
| `POST` | `/api/receivers/<id>/bounce` | – | re-acquire: bounce to a spare channel and back |
| `POST` | `/api/receivers/<id>/hold` | – | hold the screen dark until released |
| `POST` | `/api/receivers/<id>/release` | – | restore a held screen |
| `POST` | `/api/release-all` | – | restore every held screen |
| `POST` | `/api/receivers/<id>/reboot` | – | reboot the receiver |
| `POST` | `/api/discover` | `{"ranges":[…]}` | scan for VEO devices not in the config |
| `POST` | `/api/discovered/add` | `{"ip":"…"}` | add a discovered device as a receiver |
| `GET` | `/api/setup` | – | what is on the factory-default address |
| `POST` | `/api/setup` | ip, netmask, gateway, name, … | commission it and add it to the fleet |
| `POST` | `/api/receivers/<id>/device-name` | `{"name":"…"}` | set the name stored in the device |
| `POST` | `/api/receivers/<id>/address` | ip, netmask, gateway | move it, and follow it in the config |
| `POST` | `/api/repair` | – | fix every drifted receiver |
| `POST` | `/api/refresh` | – | poll now instead of waiting |
| `GET` | `/api/health` | – | liveness; never requires a login |
| `GET` | `/api/inventory` | – | current names as a `devices.txt` |
| `GET` | `/api/config` | – | complete config, as a downloadable backup |
| `POST` | `/api/config` | config JSON | restore a config; validated, previous one backed up |
| `GET` | `/api/whoami` | – | who is signed in |
| `POST` | `/login` / `/logout` | form | sign in / out (browser flow) |

So a morning reset is just:

```bash
curl -X POST http://127.0.0.1:8477/api/repair
```

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

## Idea for later: retiring the ChromeBoxes

Not built, not tested — notes so the thinking is not lost.

**Today.** Each of the four transmitters has a ChromeBox attached in kiosk mode,
displaying a static dashboard URL over HDMI. The transmitter encodes that to
H.264 and multicasts it; receivers decode it to their TVs.

### Two possible routes

1. **Software streaming (preferred).** One machine generates all four streams
   itself and multicasts them. Retires the four ChromeBoxes *and* the four
   transmitters: 8 devices -> 1. Gated on one unknown, below.
2. **Keep the transmitters, replace only the ChromeBoxes.** A fallback if
   route 1 turns out not to work. Notes kept further down.

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

1. **Feasibility, not performance.** Will a VEO-XRI1C lock onto an
   ffmpeg-generated MPEG-TS stream? Unresolved. Everything below is moot until
   it is answered, and it is cheap to answer: one stream, one spare receiver,
   one unused Group ID.

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

### The unknowns, worst first

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

**Step 1 — stream a test pattern to an unused channel.** Channels 1-4 are the
real dashboards, so 5 is free (confirmed: a receiver set to CH5 shows
"Waiting for connection…").

```bash
pct exec 108 -- python3 -u /opt/eclermanager/tools/teststream.py --channel 5
```

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

**Step 3 — if it stays black, work through the variants.** Each tests a
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

**Step 4 — only if a test pattern works**, try a real page: run Xvfb plus a
kiosk browser on display `:99` and add `--url` to capture it instead.

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

## Run it permanently

In the LXC (or on any always-on box that can reach the VLAN):

```ini
# /etc/systemd/system/eclermanager.service
[Unit]
Description=Ecler VEO Manager
After=network-online.target

[Service]
WorkingDirectory=/opt/eclermanager
ExecStart=/usr/bin/python3 run.py --host 0.0.0.0 --event-log /var/log/eclermanager.jsonl
Restart=on-failure
DynamicUser=yes
StateDirectory=eclermanager

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now eclermanager
```

Running it on your workstation instead? Use `~/.config/systemd/user/` with
`WantedBy=default.target`, `systemctl --user enable --now eclermanager`, and
`loginctl enable-linger "$USER"` so it survives logout.

---

## Layout

```
run.py                      entry point
config.example.json         copy to config.json
eclermanager/veo.py         port 9999 protocol client + tolerant reply parsing
eclermanager/config.py      config load/validate/save
eclermanager/discovery.py   network sweep + "is this really a VEO?"
eclermanager/poller.py      background polling, drift detection, self-healing
eclermanager/server.py      JSON API + static file serving + login gate
eclermanager/auth.py        password hashing, signed session cookies
eclermanager/static/        the dashboard and login page (no dependencies)
tools/probe.py              verify a real device, dump raw replies
tools/scan.py               find live devices on a subnet, generate a config
tools/sniffmac.py           the reverse: find the IP of a MAC, passively
tools/macdiff.py            diff a switch MAC table against known receivers
tools/setip.py              set a device's static IP, verified after reboot
tools/setname.py            set the name a device reports for itself
tools/arpscan.py            find a MAC's IP by ARP, any subnet, when it is silent
tools/setpassword.py        create the dashboard service account
tools/teststream.py         multicast a test stream, to see if a receiver locks
deploy/create-container.sh  Proxmox: create the LXC (run on the host)
deploy/install.sh           install as a systemd service (run in the container)
deploy/eclermanager.service the systemd unit
tools/mock_veo.py           fake receivers for testing without hardware
tests/test_veo.py           python3 -m unittest discover -s tests
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers reply parsing across the firmware phrasings seen in the wild, telnet
negotiation stripping, config validation, and the offline path.
