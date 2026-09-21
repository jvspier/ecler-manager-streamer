# Running the manager in Docker

Both ways of running the manager are supported and neither is the "real" one.
Pick on how you like to run things:

| | |
|---|---|
| **systemd** ([docs/deployment.md](deployment.md)) | One script, a service account, updates by `git pull`. Best if the machine already runs other services this way. |
| **Docker** (this page) | One image, two volumes, no service account to create. Best if everything else you run is a container. |

The **streamer is deliberately not containerised** — see
[streamer/README.md](../streamer/README.md). Its lifecycle model is systemd,
and it needs a real network interface to send multicast from, so a container
would give up most of what containers are for.

## Quick start

```bash
git clone https://github.com/jvspier/ecler-manager-streamer.git
cd ecler-manager-streamer
docker compose up -d
```

Then open `http://<this host>:8477/`.

On first run there is no `config.json`, so the entrypoint copies
`config.example.json` into your mounted volume and starts anyway — the UI
comes up, the example receivers show as offline, and you can either edit
`./config/config.json` or use **Scan network** in the UI to find your own.

## Host networking, and why

`docker-compose.yml` uses `network_mode: host`. That is not laziness:

- The manager reaches receivers on **TCP 9999**. Behind a bridge that works
  for addresses you already know.
- But **discovery sweeps a range** to find devices you do not know about, and
  from a NAT'd bridge network that is useless.

If host networking is not acceptable, a **macvlan** interface on the
receivers' VLAN is the other sensible choice. A plain bridge is not — polling
will work and discovery will quietly find nothing.

## Setting a login

Without credentials the manager runs open to anyone who can reach the port,
and says so in its log on every start. Generate the variables:

```bash
python3 tools/setpassword.py --user admin --print
```

That prints `ECLER_AUTH_USER`, `ECLER_AUTH_PASSWORD_HASH`,
`ECLER_SESSION_SECRET` and `ECLER_SESSION_HOURS` without writing anything.
Put them in a `.env` beside `docker-compose.yml`, which the compose file
already reads.

**Keep the session secret.** Without it a fresh one is generated on every
start, so every restart logs everyone out.

## Volumes

| Path | Holds |
|---|---|
| `/etc/eclermanager` | `config.json` — your receivers, channels and settings |
| `/var/lib/eclermanager` | `events.jsonl` — every drift, signal loss, switch and re-acquire |

The config is the only irreplaceable one. The event log is worth keeping for
answering "how often does this actually happen?", which is the question that
justifies self-healing.

The image runs as uid **10001**, so if you bind-mount host directories they
need to be writable by it.

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

Your config and event log are in volumes and are not touched.

## Notes

- **Podman** prints `HEALTHCHECK is not supported for OCI image format` at
  build time. Harmless — the image works; podman simply ignores the
  healthcheck unless you build with `--format docker`.
- The image has **no dependencies to install**. It is `python:3.13-slim` plus
  this repository's files, because the application is the standard library.
  The tests run on Python 3.8 through 3.14 if you would rather use a
  different base.
