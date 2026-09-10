"""Configuration loading for the VEO manager.

The config is plain JSON so it needs no third-party parser and can be written
back atomically when the dashboard edits an expected channel.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import veo

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(Exception):
    """The config file is missing, malformed, or internally inconsistent."""


@dataclass
class Channel:
    """A Group ID and the content a transmitter puts on it."""

    group_id: int
    name: str
    transmitter_ip: str | None = None
    note: str = ""
    #: The multicast group this channel streams to, e.g. 239.255.42.44.  Read
    #: it off a transmitter's web page with tools/probe.py.  Recorded here so
    #: a switch's IGMP snooping table can be matched to a channel by name.
    multicast_group: str | None = None

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "transmitter_ip": self.transmitter_ip,
            "note": self.note,
            "multicast_group": self.multicast_group,
        }


@dataclass
class Receiver:
    """One VEO-XRI1C behind a TV."""

    id: str
    name: str
    ip: str
    expected_group_id: int | None = None
    location: str = ""
    enabled: bool = True
    #: Free text for why this receiver is set up as it is -- "on Reception at
    #: the desk owner's request" -- so a deliberate choice is not mistaken for
    #: drift by whoever looks next.
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "expected_group_id": self.expected_group_id,
            "location": self.location,
            "enabled": self.enabled,
            "note": self.note,
        }


@dataclass
class Config:
    path: Path
    poll_interval_seconds: float = 30.0
    telnet_port: int = veo.DEFAULT_PORT
    timeout_seconds: float = veo.DEFAULT_TIMEOUT
    max_workers: int = 8
    auto_repair: bool = False
    auto_repair_after_polls: int = 2
    auto_nudge_on_signal_loss: bool = False
    bounce_via_group_id: int | None = None
    identify_dwell_seconds: float = 6.0
    #: Where a factory-reset receiver appears.  Documented default for the
    #: VEO-XRI1C; the transmitter's is 192.168.1.11.
    setup_address: str = "192.168.1.12"
    setup_netmask: str = "255.255.0.0"
    setup_gateway: str = "10.0.0.1"
    discovery_ranges: list[str] = field(default_factory=list)
    discovery_interval_hours: float = 0.0      # 0 = only when asked
    channels: list[Channel] = field(default_factory=list)
    receivers: list[Receiver] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- lookups ---------------------------------------------------------
    def receiver(self, receiver_id: str) -> Receiver:
        for rx in self.receivers:
            if rx.id == receiver_id:
                return rx
        raise KeyError(receiver_id)

    def channel_for_multicast_group(self, address: str) -> Channel | None:
        """Which channel streams to this multicast address, if recorded.

        Lets a switch's IGMP snooping output be read in channel names rather
        than raw group addresses.
        """
        for channel in self.channels:
            if channel.multicast_group == address:
                return channel
        return None

    def channel_name(self, group_id: int | None) -> str | None:
        if group_id is None:
            return None
        for ch in self.channels:
            if ch.group_id == group_id:
                return ch.name
        return None

    def bounce_via(self, target_group_id: int) -> int:
        """A Group ID to bounce a receiver through when forcing a re-acquire.

        Prefers the configured one; otherwise the highest Group ID no
        transmitter of ours uses, so the momentary switch shows nothing
        recognisable rather than another department's dashboard.
        """
        configured = self.bounce_via_group_id
        if configured is not None and configured != target_group_id:
            return configured
        used = {c.group_id for c in self.channels} | {target_group_id}
        for candidate in range(veo.GROUP_ID_MAX, veo.GROUP_ID_MIN - 1, -1):
            if candidate not in used:
                return candidate
        raise ConfigError("no free Group ID available to bounce through")

    @property
    def active_receivers(self) -> list[Receiver]:
        return [rx for rx in self.receivers if rx.enabled]

    # --- persistence -----------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "poll_interval_seconds": self.poll_interval_seconds,
            "telnet_port": self.telnet_port,
            "timeout_seconds": self.timeout_seconds,
            "max_workers": self.max_workers,
            "auto_repair": self.auto_repair,
            "auto_repair_after_polls": self.auto_repair_after_polls,
            "auto_nudge_on_signal_loss": self.auto_nudge_on_signal_loss,
            "bounce_via_group_id": self.bounce_via_group_id,
            "identify_dwell_seconds": self.identify_dwell_seconds,
            "setup_address": self.setup_address,
            "setup_netmask": self.setup_netmask,
            "setup_gateway": self.setup_gateway,
            "discovery_ranges": self.discovery_ranges,
            "discovery_interval_hours": self.discovery_interval_hours,
            "channels": [c.as_dict() for c in self.channels],
            "receivers": [r.as_dict() for r in self.receivers],
        }

    def save(self) -> None:
        """Atomically rewrite the config file."""
        with self._lock:
            payload = json.dumps(self.as_dict(), indent=2) + "\n"
            directory = self.path.parent
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise


def load(path: str | os.PathLike | None = None) -> Config:
    """Read and validate a config file."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(
            f"No config at {config_path}.\n"
            "Copy config.example.json to config.json and fill in your IPs."
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    return parse(data, config_path)


def parse(data: object, path: Path) -> Config:
    """Validate an already-decoded config.

    Split out from :func:`load` so an imported config can be checked before
    anything on disk is touched.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")

    config_path = path
    cfg = Config(path=config_path)
    cfg.poll_interval_seconds = max(
        10.0, float(data.get("poll_interval_seconds", cfg.poll_interval_seconds))
    )
    cfg.telnet_port = int(data.get("telnet_port", cfg.telnet_port))
    cfg.timeout_seconds = float(data.get("timeout_seconds", cfg.timeout_seconds))
    cfg.max_workers = max(1, int(data.get("max_workers", cfg.max_workers)))
    cfg.auto_repair = bool(data.get("auto_repair", False))
    cfg.auto_repair_after_polls = max(
        1, int(data.get("auto_repair_after_polls", cfg.auto_repair_after_polls))
    )
    cfg.auto_nudge_on_signal_loss = bool(data.get("auto_nudge_on_signal_loss", False))
    cfg.identify_dwell_seconds = max(
        1.0, min(30.0, float(data.get("identify_dwell_seconds", 6.0)))
    )
    cfg.setup_address = str(data.get("setup_address") or cfg.setup_address)
    cfg.setup_netmask = str(data.get("setup_netmask") or cfg.setup_netmask)
    cfg.setup_gateway = str(data.get("setup_gateway") or cfg.setup_gateway)

    ranges = data.get("discovery_ranges") or []
    if isinstance(ranges, str):
        ranges = [ranges]
    if not isinstance(ranges, list) or any(not isinstance(r, str) for r in ranges):
        raise ConfigError("discovery_ranges must be a list of strings")
    from . import discovery as _discovery
    for spec in ranges:
        try:
            _discovery.parse_targets([spec])
        except ValueError as exc:
            raise ConfigError(f"bad discovery range {spec!r}: {exc}") from exc
    cfg.discovery_ranges = ranges
    cfg.discovery_interval_hours = max(
        0.0, float(data.get("discovery_interval_hours", 0.0)))

    bounce_via = data.get("bounce_via_group_id")
    if bounce_via is not None:
        bounce_via = int(bounce_via)
        if not veo.GROUP_ID_MIN <= bounce_via <= veo.GROUP_ID_MAX:
            raise ConfigError(f"bounce_via_group_id {bounce_via} out of range")
        cfg.bounce_via_group_id = bounce_via

    for raw in data.get("channels", []):
        try:
            group_id = int(raw["group_id"])
            name = str(raw["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"bad channel entry {raw!r}: {exc}") from exc
        if not veo.GROUP_ID_MIN <= group_id <= veo.GROUP_ID_MAX:
            raise ConfigError(
                f"channel group_id {group_id} outside "
                f"{veo.GROUP_ID_MIN}..{veo.GROUP_ID_MAX}"
            )
        cfg.channels.append(
            Channel(
                group_id=group_id,
                name=name,
                transmitter_ip=raw.get("transmitter_ip") or None,
                note=str(raw.get("note", "")),
                multicast_group=raw.get("multicast_group") or None,
            )
        )

    duplicate_groups = _duplicates([c.group_id for c in cfg.channels])
    if duplicate_groups:
        raise ConfigError(f"channels reuse group_id(s): {sorted(duplicate_groups)}")

    for raw in data.get("receivers", []):
        try:
            ip = str(raw["ip"]).strip()
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"receiver entry {raw!r} needs an 'ip'") from exc
        if not ip:
            raise ConfigError(f"receiver entry {raw!r} has an empty 'ip'")
        receiver_id = str(raw.get("id") or ip.replace(".", "-"))
        expected = raw.get("expected_group_id")
        if expected is not None:
            expected = int(expected)
            if not veo.GROUP_ID_MIN <= expected <= veo.GROUP_ID_MAX:
                raise ConfigError(
                    f"receiver {receiver_id} expected_group_id {expected} out of range"
                )
        cfg.receivers.append(
            Receiver(
                id=receiver_id,
                name=str(raw.get("name") or receiver_id),
                ip=ip,
                expected_group_id=expected,
                location=str(raw.get("location", "")),
                enabled=bool(raw.get("enabled", True)),
                note=str(raw.get("note", "")),
            )
        )

    if not cfg.receivers:
        raise ConfigError(f"{config_path} lists no receivers")
    duplicate_ids = _duplicates([r.id for r in cfg.receivers])
    if duplicate_ids:
        raise ConfigError(f"receivers reuse id(s): {sorted(duplicate_ids)}")
    duplicate_ips = _duplicates([r.ip for r in cfg.receivers])
    if duplicate_ips:
        raise ConfigError(f"receivers reuse ip(s): {sorted(duplicate_ips)}")
    return cfg


def _duplicates(values: list) -> set:
    seen, dupes = set(), set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes
