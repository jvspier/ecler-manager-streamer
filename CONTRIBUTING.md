# Contributing

## The most useful thing you can send

**I can only test against what I have**: VEO-XRI1C receivers and VEO-XTI1C
transmitters, all on firmware `V1.01.r0`. Ecler ships other models, and the
control protocol is undocumented — everything in
[docs/protocol.md](docs/protocol.md) was worked out by watching real devices
answer, and several findings were counter-intuitive.

So if your unit behaves differently, the single most valuable thing you can
send is what it actually said:

```bash
python3 tools/probe.py <ip-of-your-device>
```

That tries every known command, both line endings, and prints the raw bytes.
Paste the whole output into an issue, with your model and firmware version.
That turns "it doesn't work" into something I can write a test for.

Known reasons a device may parse differently:

- **Line endings.** Some units reject the documented CRLF and answer LF only,
  closing the connection otherwise. Handled per host, but the detection could
  well miss a variant.
- **Prompt shape.** Replies are framed by the prompt the device sends after
  each answer. A firmware with a different prompt would break framing, and the
  values would still look plausible — which is the dangerous kind of wrong.
- **Extra commands.** `get_video_lock` and `get_group_id` are not in Ecler's
  documentation. Other models may expose more, or fewer.

## Running the tests

No hardware needed:

```bash
python3 -m unittest discover -s tests            # manager
node tests/smoke_dashboard.js

python3 -m unittest discover -s streamer/tests   # streamer
node streamer/tests/smoke_ui.js
```

There is an emulator for the awkward parts:

```bash
python3 tools/mock_veo.py --fleet 4 --chaos 20
```

`--chaos` makes devices drift and lose their streams the way real ones do.

To check a Python version rather than assume it:

```bash
python3 tools/check_python.py
```

## If you are changing the protocol client

Read [docs/protocol.md](docs/protocol.md) first. Several behaviours look like
bugs and are not, and at least one plausible-looking change re-introduces a
fault that took a day to find: framing replies on a pause in the bytes rather
than on the prompt pairs every answer with the wrong command, silently, and
the values still look reasonable.

Add a test to `tests/test_veo.py` that reproduces the device behaviour against
the emulator. Every quirk in there is a real device that did a real thing.

## Scope

This is a tool for a specific job that I run in a real building. Pull requests
that fix a bug, support another model, or make the protocol handling more
robust are very welcome. Larger features may not be — open an issue first and
we can talk about whether it belongs here or in your fork.
