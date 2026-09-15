"""Configuration for the streamer.

One JSON file is the single source of truth. The systemd units take only a
channel number and read everything else from here, so there are no per-channel
env files to drift out of step with the web UI.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("/etc/eclerstreamer/config.json")

# X display numbers are arbitrary integers; deriving one from the channel
# keeps them unique without having to store an allocation anywhere.
DISPLAY_BASE = 100


# Confirmed against the switch's IGMP tables for channels 1 and 2, and the
# pattern holds for the rest. Derived here rather than read from the manager
# so a stream can start with the manager unreachable.
MULTICAST_BASE = 42


def multicast_for(channel: int) -> str:
    return f"239.255.42.{MULTICAST_BASE + int(channel)}"


@dataclass
class Dashboard:
    """One web page, rendered and streamed to one channel."""

    channel: int
    name: str = ""
    url: str = ""
    enabled: bool = True
    display: int | None = None
    # Matches the output rate by default. Capturing below it is a real saving
    # on a static page -- nothing to sample 30 times a second -- but it caps
    # how smooth animation can be, and a first dashboard that judders reads as
    # a broken tool rather than as a setting to tune. Correct by default,
    # optimised deliberately.
    capture_fps: float = 30.0
    fps: int = 30
    bitrate: str = "6M"
    size: str = "1920x1080"
    note: str = ""

    @property
    def display_number(self) -> int:
        return DISPLAY_BASE + self.channel if self.display is None else self.display

    @property
    def display_name(self) -> str:
        return f":{self.display_number}"

    def to_dict(self) -> dict:
        return {
            "channel": self.channel, "name": self.name, "url": self.url,
            "enabled": self.enabled, "display": self.display,
            "capture_fps": self.capture_fps, "fps": self.fps,
            "bitrate": self.bitrate, "size": self.size, "note": self.note,
        }


@dataclass
class Config:
    path: Path = DEFAULT_PATH
    # Which leg the multicast leaves by. On a multi-homed host this is not
    # optional: without it the stream goes out the default route, which is the
    # management VLAN, and the receivers never see it.
    local_addr: str = ""
    interface: str = ""
    # Read-only link to the manager, for channel names. The streamer must
    # never be required for the manager to work -- the dependency is one way.
    manager_url: str = ""
    qmin: int = 18
    no_bframes: bool = True
    dashboards: list[Dashboard] = field(default_factory=list)

    def dashboard(self, channel: int) -> Dashboard | None:
        return next((d for d in self.dashboards if d.channel == channel), None)

    def to_dict(self) -> dict:
        return {
            "local_addr": self.local_addr, "interface": self.interface,
            "manager_url": self.manager_url, "qmin": self.qmin,
            "no_bframes": self.no_bframes,
            "dashboards": [d.to_dict() for d in self.dashboards],
        }

    def save(self) -> None:
        """Atomic write: a half-written config would take every stream down."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, prefix=".config-", suffix=".json",
            delete=False, encoding="utf-8")
        try:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.path)
        except BaseException:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
            raise


def parse(data: dict, path: Path = DEFAULT_PATH) -> Config:
    """Build a Config from parsed JSON.

    Split from load() so an uploaded config can be validated before anything
    touches the disk.
    """
    cfg = Config(path=path)
    cfg.local_addr = str(data.get("local_addr", "") or "")
    cfg.interface = str(data.get("interface", "") or "")
    cfg.manager_url = str(data.get("manager_url", "") or "").rstrip("/")
    cfg.qmin = max(0, int(data.get("qmin", cfg.qmin)))
    cfg.no_bframes = bool(data.get("no_bframes", cfg.no_bframes))

    seen: set[int] = set()
    for entry in data.get("dashboards", []) or []:
        channel = int(entry["channel"])
        if not 0 <= channel <= 63:
            raise ValueError(f"channel {channel} is outside 0-63")
        if channel in seen:
            raise ValueError(f"channel {channel} appears twice")
        seen.add(channel)
        cfg.dashboards.append(Dashboard(
            channel=channel,
            name=str(entry.get("name", "") or ""),
            url=str(entry.get("url", "") or ""),
            enabled=bool(entry.get("enabled", True)),
            display=(int(entry["display"]) if entry.get("display") is not None
                     else None),
            capture_fps=float(entry.get("capture_fps", 15.0)),
            fps=int(entry.get("fps", 30)),
            bitrate=str(entry.get("bitrate", "6M")),
            size=str(entry.get("size", "1920x1080")),
            note=str(entry.get("note", "") or ""),
        ))
    cfg.dashboards.sort(key=lambda d: d.channel)
    return cfg


def load(path: Path | str = DEFAULT_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        return Config(path=path)
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # A traceback here would be read by whoever is on a console at the
        # time, so say which file and where, not "Expecting value".
        raise ValueError(
            f"{path} is not valid JSON: {exc.msg} at line {exc.lineno} "
            f"column {exc.colno}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} should contain a JSON object")
    return parse(data, path=path)
