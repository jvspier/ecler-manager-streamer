# Deploying it

Where to run the manager, how to build a container for it, how to install
it as a service, and how to put a login in front of it.

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
  the container. The manager does not, and turning it on exposes the host's
  `procfs` and `sysfs` to the guest — a real security trade for zero benefit.
  Try without it first.

  **One real exception: a browser.** If you ever render pages in this container
  — see [streaming.md](streaming.md) — it *does* need `nesting=1`. Chromium's
  zygote clones with `CLONE_NEWUSER`, `CLONE_NEWPID` and `CLONE_NEWNET`, which
  an unprivileged container blocks, and `--no-sandbox` does not help because
  the process model uses those namespaces regardless. The symptom is specific
  and misleading: a window appears, the browser is gone a second later, and the
  log fills with dbus errors that have nothing to do with the cause. That is
  also a reason to keep browser work on a machine that does nothing else,
  rather than taking the nesting trade on a host shared with anything.

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

## Updating from git (recommended)

Once the code is in a git repository, the container can pull it directly, which
is quicker and less error-prone than copying tarballs around.

### One-time setup

**The container only ever pulls.** Nothing is pushed from it, and it needs no
write access to anything. The only question is how it authenticates to *read*,
which depends on whether the repository is public:

**Public repository — no credential needed:**

```bash
apt-get update && apt-get install -y git
rm -rf /root/eclermanager
git clone https://github.com/<owner>/<repo>.git /root/eclermanager
bash /root/eclermanager/deploy/install.sh
```

**Private repository — a read-only deploy key.** GitHub requires a credential
even to read a private repo. A "deploy key" is its name for a credential scoped
to one repository; with write access unchecked it can *only* read, which is all
this needs.

In the container:

```bash
apt-get update && apt-get install -y git
ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C "eclermanager-deploy"
cat /root/.ssh/id_ed25519.pub
```

On GitHub, add that public key at *Settings → Deploy keys → Add deploy key*, and
leave **"Allow write access" unchecked**. That is the whole point: if the
container is ever compromised, the key cannot be used to alter the repository or
to reach any other repository.

Back in the container:

```bash
ssh -o StrictHostKeyChecking=accept-new -T git@github.com
    # "successfully authenticated, but GitHub does not provide shell access" = success
rm -rf /root/eclermanager
git clone git@github.com:<owner>/<repo>.git /root/eclermanager
bash /root/eclermanager/deploy/install.sh
```

A personal access token over HTTPS works too, and some people find it easier to
picture — but it lives in the container's git config and expires, where a
read-only deploy key does neither.

### Every update after that

```bash
pct exec <ctid> -- bash /root/eclermanager/deploy/update.sh
```

That is the whole workflow: commit and push from your workstation, then run
that one line. It fetches, prints the commits and the diffstat, **runs the test
suite, and installs only if the tests pass** — so a broken commit cannot take
down the running dashboard. Then `install.sh` does its usual work, which leaves
`config.json` and the login file alone.

Useful flags: `--dry-run` shows what would change and stops; `--branch <name>`
pulls something other than the current branch; `--no-tests` skips the test run,
which is not recommended.

It refuses to run if there are uncommitted changes in the checkout, rather than
discarding them — so a quick fix made directly in the container is not silently
lost. Commit it, or `git -C /root/eclermanager checkout .` to drop it.

### Copying a tarball instead

Still fine, and the only option before the code is in a repository:

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
