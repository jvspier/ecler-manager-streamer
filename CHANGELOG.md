# Changelog

Both tools share one version number, so a version means the same code on the
manager and the streamer.

## 1.1.0 — 2026-10-06

### Manager

- **Channels can be named, added and retired from the page** (*Channels…*),
  each with an optional one-click button on every card. Nothing ties a role
  such as "Production" to a channel number.
- **Move a whole group** of receivers to another channel in one go, one at a
  time in the background, with the new expected channel written first so
  auto-repair never sees a half-moved group as drift.
- **Software streams are understood.** A channel can be marked as not
  reporting video lock; its TVs show the streamer's own verdict instead
  (running, slow, stalled, down, off), read from the streamer's
  `/api/streams`. Stream outages, recoveries and restarts that fell between
  two checks are logged as events.
- Channels no TV is on fold into one line instead of a heading each.
- Fixed: the page's refresh no longer steals focus from a text box or closes
  an open dropdown.

### Streamer

- **Hardware encoding on an Intel GPU** (VAAPI), chosen per channel. The
  stream keeps the same shape as x264. Over a week on two channels showing
  the same site, x264 dropped 3641 frames in bursts while a video played and
  the GPU dropped none. See `streamer/docs/gpu.md`.
- **CPU, RAM and GPU load bars** on the page, and a per-channel GPU share,
  without root.
- **`/api/streams`**, a public read-only status endpoint for the manager: a
  verdict per channel and encoder figures, no URLs or settings.
- Fixed: pages that show a video with sound froze on its first frame.
  Chromium blocked autoplay because nobody clicks a wall.
- Only channels 1-7 are accepted. Their multicast address follows
  `239.255.42.(42+N)`; channel 9 turned out to use `.57`, so above 7 the
  address is unknown and a stream there would reach no TV.
- The stream units admit GPU render nodes (and nothing else) instead of
  hiding every device.
- The page's refresh no longer steals focus, and an unsaved restart
  interval is no longer put back by it.
- `tools/teststream.py --mimic-veo` reproduces a hardware transmitter's
  transport-stream layout, for experiments.

### Docs

- The front-panel LED does follow a channel change made over the network,
  despite Ecler's manual.
- `tools/probe.py` warns against probing a receiver the manager is polling.

## 1.0.0 — 2026-09-21

First public release.
