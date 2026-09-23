# The VEO control protocol

How these devices actually behave on the wire, verified against real
hardware. Worth reading before changing `eclermanager/veo.py`: several of
these findings are counter-intuitive, and each was arrived at by getting
it wrong first.

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

1. **The front-panel LED does follow a change over port 9999.** Ecler's manual
   says a web-driven change leaves the LED on the old number, but a
   `set_group_id` from this dashboard was seen on the LED straight away
   (Group ID 9, 2026-09-23). Either way the dashboard reads the channel back
   from the device after every switch, so the read-back is what to trust.
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
