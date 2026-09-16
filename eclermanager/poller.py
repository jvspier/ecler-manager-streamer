"""Background polling, drift detection and channel control."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import discovery, veo
from .config import Config, Receiver

log = logging.getLogger("eclermanager.poller")

MAX_EVENTS = 500


@dataclass
class ReceiverState:
    """Everything the dashboard knows about one receiver."""

    receiver: Receiver
    status: veo.DeviceStatus | None = None
    consecutive_drift: int = 0
    consecutive_no_signal: int = 0
    consecutive_offline: int = 0
    last_action: str = ""
    last_action_at: float | None = None
    details_read: bool = False
    busy: bool = False
    #: Channel to restore when an identify hold is released.  Set means held.
    held_from_group_id: int | None = None

    @property
    def group_id(self) -> int | None:
        return self.status.group_id if self.status else None

    @property
    def drifted(self) -> bool:
        """True when the device reports a channel other than the expected one.

        A receiver being held dark for identification is off-channel on
        purpose, so it does not count as drifted -- otherwise the walk would
        light up the dashboard with faults that are not faults.
        """
        expected = self.receiver.expected_group_id
        return (
            self.held_from_group_id is None
            and expected is not None
            and self.status is not None
            and self.status.online
            and self.status.group_id is not None
            and self.status.group_id != expected
        )

    def as_dict(self, config: Config) -> dict:
        status = self.status
        data = {
            **self.receiver.as_dict(),
            "online": bool(status and status.online),
            "group_id": status.group_id if status else None,
            "channel_name": config.channel_name(status.group_id if status else None),
            "expected_channel_name": config.channel_name(
                self.receiver.expected_group_id
            ),
            "video_lock": status.video_lock if status else None,
            "device_name": status.device_name if status else None,
            "fw_version": status.fw_version if status else None,
            "mac_address": status.mac_address if status else None,
            "dhcp": status.dhcp if status else None,
            "error": status.error if status else None,
            "latency_ms": (
                round(status.latency_ms, 1) if status and status.latency_ms else None
            ),
            "checked_at": status.checked_at if status else None,
            "drifted": self.drifted,
            "consecutive_no_signal": self.consecutive_no_signal,
            "consecutive_offline": self.consecutive_offline,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "busy": self.busy,
            "held": self.held_from_group_id is not None,
            "held_from_group_id": self.held_from_group_id,
        }
        return data


class Poller:
    """Polls every receiver on an interval and applies optional self-healing."""

    def __init__(self, config: Config, event_log_path: Path | None = None) -> None:
        self.config = config
        self.event_log_path = event_log_path
        self.states: dict[str, ReceiverState] = {
            rx.id: ReceiverState(receiver=rx) for rx in config.receivers
        }
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.discovered: list[dict] = []
        self.discovery_running = False
        # Seeded with the start time, not None. Left unset, a configured
        # interval made `(None or 0) + interval` a moment in 1970, so a full
        # sweep began within seconds of every service start or restart --
        # and the value is never persisted, so every restart did it again.
        # A sweep of a /16 once knocked every receiver in the building off
        # its multicast stream for about 35 seconds.
        self.last_discovery: float | None = None
        self._started_at = time.time()
        self.discovery_message = ""
        self.discovery_scanned = 0
        self.discovery_error = ""
        self._discovery_thread: threading.Thread | None = None
        self.last_poll_started: float | None = None
        self.last_poll_finished: float | None = None
        self.poll_count = 0
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="veo-poller", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def refresh_soon(self) -> None:
        """Ask the poll loop to run a cycle now."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:                       # keep the loop alive
                log.exception("poll cycle failed")
            try:
                self._maybe_discover()
            except Exception:
                log.exception("scheduled scan failed")
            self._wake.wait(self.config.poll_interval_seconds)
            self._wake.clear()

    def _maybe_discover(self) -> None:
        """Run a scan if one is due, when an interval has been configured."""
        interval = self.config.discovery_interval_hours
        if interval <= 0 or not self.config.discovery_ranges:
            return
        due = (self.last_discovery or self._started_at) + interval * 3600
        if time.time() < due:
            return
        # Through the guarded entry point, not discover(): the scheduled path
        # skipped the size check that the manual one applies, so a range that
        # the dashboard would refuse was swept automatically instead.
        try:
            self.start_discovery()
        except ValueError as exc:
            log.warning("scheduled scan refused: %s", exc)

    # --- polling ---------------------------------------------------------
    def poll_once(self) -> None:
        receivers = [
            state
            for state in self.states.values()
            if state.receiver.enabled and not state.busy
        ]
        if not receivers:
            return
        with self._lock:
            self.last_poll_started = time.time()
        workers = min(self.config.max_workers, len(receivers))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="veo") as pool:
            futures = {
                pool.submit(
                    veo.read_status,
                    state.receiver.ip,
                    port=self.config.telnet_port,
                    timeout=self.config.timeout_seconds,
                    with_details=not state.details_read,
                ): state
                for state in receivers
            }
            for future, state in futures.items():
                try:
                    status = future.result()
                except Exception as exc:            # read_status shouldn't raise
                    log.warning("unexpected poll error for %s: %s",
                                state.receiver.ip, exc)
                    status = veo.DeviceStatus(
                        host=state.receiver.ip, error=f"{type(exc).__name__}: {exc}"
                    )
                self._apply_status(state, status)
        with self._lock:
            self.last_poll_finished = time.time()
            self.poll_count += 1
        self._self_heal()

    def _apply_status(self, state: ReceiverState, status: veo.DeviceStatus) -> None:
        with self._lock:
            previous = state.status
            if previous is not None:
                # Keep first-contact replies (device name, firmware) visible in the
                # raw view, which later polls no longer ask for.
                for command, reply in previous.raw.items():
                    status.raw.setdefault(command, reply)
            state.status = status
            rx = state.receiver

            if status.online:
                if status.device_name or status.fw_version:
                    state.details_read = True
                if previous is not None and not previous.online:
                    self._log_event(rx, "online", "came back online")
                state.consecutive_offline = 0
            else:
                state.consecutive_offline += 1
                if previous is None or previous.online:
                    self._log_event(
                        rx, "offline", f"unreachable ({status.error or 'no reply'})"
                    )

            if status.online and status.group_id is not None:
                if previous and previous.online and previous.group_id is not None \
                        and previous.group_id != status.group_id:
                    self._log_event(
                        rx,
                        "channel_changed",
                        f"channel changed {previous.group_id} -> {status.group_id}",
                    )
                if state.drifted:
                    state.consecutive_drift += 1
                    if state.consecutive_drift == 1:
                        self._log_event(
                            rx,
                            "drift",
                            f"on channel {status.group_id}, expected "
                            f"{rx.expected_group_id}",
                        )
                else:
                    state.consecutive_drift = 0

            if status.online and status.video_lock is False:
                state.consecutive_no_signal += 1
                if state.consecutive_no_signal == 1:
                    self._log_event(rx, "signal_lost", "no video lock")
            elif status.online and status.video_lock is True:
                if state.consecutive_no_signal:
                    self._log_event(rx, "signal_restored", "video lock restored")
                state.consecutive_no_signal = 0

    # --- self-healing ----------------------------------------------------
    def _self_heal(self) -> None:
        cfg = self.config
        if not (cfg.auto_repair or cfg.auto_nudge_on_signal_loss):
            return
        threshold = cfg.auto_repair_after_polls
        for state in list(self.states.values()):
            rx = state.receiver
            if (not rx.enabled or state.busy or rx.expected_group_id is None
                    or state.held_from_group_id is not None):
                continue
            if cfg.auto_repair and state.drifted and state.consecutive_drift >= threshold:
                self._log_event(rx, "auto_repair",
                                f"auto-setting channel {rx.expected_group_id}")
                self.set_channel(rx.id, rx.expected_group_id, source="auto-repair")
            elif (
                cfg.auto_nudge_on_signal_loss
                and not state.drifted
                and state.consecutive_no_signal >= threshold
            ):
                self._log_event(rx, "auto_nudge",
                                "forcing re-acquire after signal loss")
                try:
                    self.bounce(rx.id, source="auto-nudge")
                except ValueError as exc:
                    log.warning("cannot nudge %s: %s", rx.name, exc)

    # --- actions ---------------------------------------------------------
    def set_channel(
        self, receiver_id: str, group_id: int, *, source: str = "manual"
    ) -> veo.SwitchResult:
        """Switch one receiver and record the verified outcome."""
        state = self.states[receiver_id]
        rx = state.receiver
        with self._lock:
            state.busy = True
        try:
            result = veo.set_group_id(
                rx.ip,
                group_id,
                port=self.config.telnet_port,
                timeout=self.config.timeout_seconds,
            )
        finally:
            with self._lock:
                state.busy = False
        with self._lock:
            state.last_action = f"{source}: {result.message}"
            state.last_action_at = time.time()
            if result.ok and state.status is not None:
                # Reflect the change straight away instead of waiting for a poll.
                state.status.group_id = (
                    result.verified_group_id
                    if result.verified_group_id is not None
                    else group_id
                )
                if result.video_lock is not None:
                    state.status.video_lock = result.video_lock
                state.consecutive_drift = 0
            self._log_event(
                rx,
                "switch" if result.ok else "switch_failed",
                f"{source} -> channel {group_id}: {result.message}",
            )
        return result

    def bounce(self, receiver_id: str, *, source: str = "manual",
               dwell: float | None = None) -> veo.SwitchResult:
        """Force one receiver to re-acquire its current channel.

        This is the remote equivalent of unplugging it: the observed fix for a
        receiver left dark after its transmitter restarted underneath it.
        """
        state = self.states[receiver_id]
        rx = state.receiver
        target = state.group_id if state.group_id is not None else rx.expected_group_id
        if target is None:
            raise ValueError(
                f"{rx.name}: no known channel to re-acquire; set an expected channel"
            )
        via = self.config.bounce_via(target)
        with self._lock:
            state.busy = True
        try:
            bounce_kwargs = {} if dwell is None else {"dwell": dwell}
            result = veo.bounce(
                rx.ip,
                target_group_id=target,
                via_group_id=via,
                port=self.config.telnet_port,
                timeout=self.config.timeout_seconds,
                **bounce_kwargs,
            )
        finally:
            with self._lock:
                state.busy = False
        with self._lock:
            state.last_action = f"{source} re-acquire: {result.message}"
            state.last_action_at = time.time()
            if result.ok and state.status is not None:
                state.status.group_id = result.verified_group_id or target
                if result.video_lock is not None:
                    state.status.video_lock = result.video_lock
                    if result.video_lock:
                        state.consecutive_no_signal = 0
            kind = "identify" if source == "identify" else "reacquire"
            self._log_event(
                rx,
                kind if result.ok else f"{kind}_failed",
                f"{source} of channel {target} via {via}: {result.message}",
            )
        return result

    def identify(self, receiver_id: str) -> veo.SwitchResult:
        """Blink one TV so you can see which physical screen it is.

        Mechanically the same as a re-acquire, but it holds the off-channel state
        long enough to spot from across a room: the screen drops to no-signal for
        a few seconds, then comes back on its own.  This is how you map receivers
        to locations without a ladder.
        """
        return self.bounce(
            receiver_id, source="identify",
            dwell=self.config.identify_dwell_seconds,
        )

    def hold_identify(self, receiver_id: str) -> veo.SwitchResult:
        """Park one TV on an unused channel and leave it there.

        The six-second blink is fine for a screen in front of you and useless
        for one two floors away.  This holds the screen dark so it can be found
        on foot, and :meth:`release_identify` puts it back.
        """
        state = self.states[receiver_id]
        rx = state.receiver
        if state.held_from_group_id is not None:
            raise ValueError(f"{rx.name} is already being held")
        current = state.group_id if state.group_id is not None else rx.expected_group_id
        if current is None:
            raise ValueError(
                f"{rx.name}: its channel is unknown, so there would be nothing "
                "to restore. Set an expected channel first"
            )
        via = self.config.bounce_via(current)
        result = self.set_channel(receiver_id, via, source="identify-hold")
        if result.ok:
            with self._lock:
                state.held_from_group_id = current
                state.last_action = (
                    f"held dark on channel {via} — release to restore "
                    f"channel {current}"
                )
                state.consecutive_drift = 0
        return result

    def release_identify(self, receiver_id: str) -> veo.SwitchResult:
        """Restore a receiver that was being held dark."""
        state = self.states[receiver_id]
        with self._lock:
            restore_to = state.held_from_group_id
        if restore_to is None:
            raise ValueError(f"{state.receiver.name} is not being held")
        result = self.set_channel(receiver_id, restore_to, source="identify-release")
        if result.ok:
            with self._lock:
                state.held_from_group_id = None
        return result

    def release_all_held(self) -> list[veo.SwitchResult]:
        """Restore every receiver currently held dark."""
        held = [state.receiver.id for state in self.states.values()
                if state.held_from_group_id is not None]
        results = []
        for receiver_id in held:
            try:
                results.append(self.release_identify(receiver_id))
            except (ValueError, veo.VeoError) as exc:
                log.warning("cannot release %s: %s", receiver_id, exc)
        return results

    def reboot(self, receiver_id: str) -> str:
        state = self.states[receiver_id]
        rx = state.receiver
        with self._lock:
            state.busy = True
        try:
            reply = veo.reboot(
                rx.ip, port=self.config.telnet_port, timeout=self.config.timeout_seconds
            )
        finally:
            with self._lock:
                state.busy = False
        with self._lock:
            state.last_action = "reboot sent"
            state.last_action_at = time.time()
            self._log_event(rx, "reboot", "reboot command sent")
        return reply

    def repair_all(self) -> list[veo.SwitchResult]:
        """Set every drifted receiver back to its expected channel."""
        targets = [
            state
            for state in self.states.values()
            if state.receiver.enabled and state.drifted and not state.busy
            and state.held_from_group_id is None
        ]
        results: list[veo.SwitchResult] = []
        if not targets:
            return results
        workers = min(self.config.max_workers, len(targets))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repair") as pool:
            futures = [
                pool.submit(
                    self.set_channel,
                    state.receiver.id,
                    state.receiver.expected_group_id,
                    source="repair-all",
                )
                for state in targets
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    log.warning("repair failed: %s", exc)
        return results

    def set_expected(self, receiver_id: str, group_id: int | None) -> None:
        """Change and persist a receiver's expected channel."""
        state = self.states[receiver_id]
        with self._lock:
            state.receiver.expected_group_id = group_id
            state.consecutive_drift = 0
        self.config.save()
        self._log_event(state.receiver, "config", f"expected channel set to {group_id}")

    def set_labels(self, receiver_id: str, name: str,
                   location: str | None = None,
                   note: str | None = None) -> None:
        """Rename a receiver and persist it.

        The id is deliberately left alone: it addresses the device in URLs and
        in the dashboard's own state, so changing it on a rename would break
        links and lose any in-flight action.
        """
        state = self.states[receiver_id]
        previous = state.receiver.name
        with self._lock:
            state.receiver.name = name
            if location is not None:
                state.receiver.location = location
            if note is not None:
                state.receiver.note = note
        self.config.save()
        where = f" ({location})" if location else ""
        self._log_event(state.receiver, "renamed",
                        f"{previous!r} -> {name!r}{where}")

    def inspect_setup_address(self, address: str | None = None) -> dict:
        """Look at whatever is sitting on the factory-default address.

        A reset receiver always appears at the same place, so commissioning a
        new or repaired unit is a repeatable flow rather than a hunt.
        """
        host = address or self.config.setup_address
        status = veo.read_status(host, port=self.config.telnet_port,
                                 timeout=self.config.timeout_seconds,
                                 with_details=True)
        known = next((rx for rx in self.config.receivers if rx.ip == host), None)
        return {
            "address": host,
            "online": status.online,
            "error": status.error,
            "device_name": status.device_name,
            "group_id": status.group_id,
            "video_lock": status.video_lock,
            "fw_version": status.fw_version,
            "mac_address": status.mac_address,
            "dhcp": status.dhcp,
            "already_in_config": known.id if known else None,
            "suggested_id": _suggest_id(host),
            "defaults": {
                "netmask": self.config.setup_netmask,
                "gateway": self.config.setup_gateway,
            },
        }

    def commission(self, *, ip: str, netmask: str, gateway: str,
                   name: str, expected_group_id: int | None,
                   device_name: str | None = None,
                   from_address: str | None = None,
                   wait: float = 90.0) -> dict:
        """Set a factory-default device up and add it to the fleet.

        Order matters: the device's own name is written first, while it is
        still reachable at the setup address, because the address change
        reboots it.
        """
        source = from_address or self.config.setup_address
        if any(rx.ip == ip for rx in self.config.receivers):
            raise ValueError(f"{ip} is already used by a receiver in the config")
        if expected_group_id is not None and not (
                veo.GROUP_ID_MIN <= expected_group_id <= veo.GROUP_ID_MAX):
            raise ValueError(f"channel {expected_group_id} out of range")

        steps: list[dict] = []

        def record(step: str, ok: bool, message: str) -> None:
            steps.append({"step": step, "ok": ok, "message": message})

        status = veo.read_status(source, port=self.config.telnet_port,
                                 timeout=self.config.timeout_seconds,
                                 with_details=True)
        if not status.online:
            raise ValueError(f"nothing is answering at {source}: "
                             f"{status.error or 'no reply'}")
        record("found", True, f"{status.device_name or 'unnamed'} at {source}")

        if device_name:
            ok, _, message = veo.set_device_name(
                source, device_name, port=self.config.telnet_port,
                timeout=self.config.timeout_seconds)
            record("device name", ok, message)

        if expected_group_id is not None and status.group_id != expected_group_id:
            result = veo.set_group_id(
                source, expected_group_id, port=self.config.telnet_port,
                timeout=self.config.timeout_seconds)
            record("channel", result.ok, result.message)

        ok, message = veo.set_static_ip(
            source, ip, netmask, gateway, port=self.config.telnet_port,
            timeout=self.config.timeout_seconds)
        record("address", ok, message)
        if not ok:
            self._log_config_event("setup_failed",
                                   f"{source}: could not set an address: {message}")
            return {"ok": False, "steps": steps,
                    "message": f"could not set the address: {message}"}

        try:
            veo.reboot(source, port=self.config.telnet_port,
                       timeout=self.config.timeout_seconds)
        except veo.VeoError:
            pass                        # it may reboot before replying
        record("reboot", True, "rebooting to apply the address")

        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(5)
            check = veo.read_status(ip, port=self.config.telnet_port,
                                    timeout=3.0, with_details=True)
            if check.online:
                record("confirmed", True, f"answering at {ip}")
                receiver = self._adopt(ip, name, expected_group_id, check)
                self._log_config_event(
                    "setup",
                    f"commissioned {receiver.id} at {ip} on channel "
                    f"{expected_group_id}")
                return {"ok": True, "steps": steps,
                        "receiver": receiver.as_dict(),
                        "message": f"{receiver.id} is set up at {ip}"}

        record("confirmed", False,
               f"did not answer at {ip} within {wait:g}s")
        self._log_config_event(
            "setup_failed",
            f"stored {ip} but it did not come up there; nothing added")
        return {"ok": False, "steps": steps,
                "message": f"the address was stored but {ip} did not answer "
                           f"within {wait:g}s, so nothing was added to the "
                           f"config. Check {source} — it may still be there."}

    def _adopt(self, ip: str, name: str, expected_group_id: int | None,
               status: veo.DeviceStatus) -> Receiver:
        """Add a confirmed device to the config and start polling it."""
        used = {rx.id for rx in self.config.receivers}
        base = _suggest_id(ip)
        receiver_id = base
        suffix = 2
        while receiver_id in used:
            receiver_id = f"{base}-{suffix}"
            suffix += 1
        receiver = Receiver(
            id=receiver_id,
            name=name or status.device_name or base,
            ip=ip,
            expected_group_id=expected_group_id,
        )
        with self._lock:
            self.config.receivers.append(receiver)
            state = ReceiverState(receiver=receiver, status=status)
            state.details_read = True
            self.states[receiver.id] = state
        self.config.save()
        self._log_event(receiver, "added", f"set up at {ip}")
        self.refresh_soon()
        return receiver

    def set_device_name(self, receiver_id: str, name: str) -> tuple[bool, str]:
        """Write a name into the device itself (not just the config label)."""
        state = self.states[receiver_id]
        rx = state.receiver
        with self._lock:
            state.busy = True
        try:
            ok, stored, message = veo.set_device_name(
                rx.ip, name, port=self.config.telnet_port,
                timeout=self.config.timeout_seconds)
        finally:
            with self._lock:
                state.busy = False
        with self._lock:
            state.last_action = f"device name: {message}"
            state.last_action_at = time.time()
            if ok and state.status is not None:
                state.status.device_name = stored
        self._log_event(rx, "device_name" if ok else "device_name_failed",
                        message)
        return (ok, message)

    def change_address(self, receiver_id: str, ip: str, netmask: str,
                       gateway: str, *, wait: float = 90.0) -> tuple[bool, str]:
        """Move a device to a new address, and follow it in the config.

        The point of doing this here rather than by hand: the config entry is
        updated to the new address only once the device actually answers there,
        so the manager never ends up pointing at an address nothing is on.
        """
        state = self.states[receiver_id]
        rx = state.receiver
        old_ip = rx.ip
        if ip == old_ip:
            return (False, f"{rx.name} is already at {ip}")
        if any(other.ip == ip for other in self.config.receivers
               if other.id != receiver_id):
            return (False, f"{ip} is already used by another receiver")

        with self._lock:
            state.busy = True
        try:
            ok, message = veo.set_static_ip(
                old_ip, ip, netmask, gateway, port=self.config.telnet_port,
                timeout=self.config.timeout_seconds)
            if not ok:
                self._log_event(rx, "address_failed", message)
                return (False, message)

            self._log_event(rx, "address",
                            f"{old_ip} -> {ip}; rebooting to apply")
            try:
                veo.reboot(old_ip, port=self.config.telnet_port,
                           timeout=self.config.timeout_seconds)
            except veo.VeoError:
                pass                       # it may reboot before replying

            deadline = time.time() + wait
            while time.time() < deadline:
                time.sleep(5)
                check = veo.read_status(ip, port=self.config.telnet_port,
                                        timeout=3.0)
                if check.online:
                    with self._lock:
                        rx.ip = ip
                        state.status = check
                        state.details_read = False
                    self.config.save()
                    self._log_event(
                        rx, "address",
                        f"confirmed at {ip}; config updated")
                    self.refresh_soon()
                    return (True, f"moved to {ip} and confirmed")
        finally:
            with self._lock:
                state.busy = False
                state.last_action_at = time.time()

        # The write was accepted, so the address may yet appear; say so
        # plainly rather than implying the device is lost.
        note = (f"stored {ip}, but it has not answered there within "
                f"{wait:g}s. The config still points at {old_ip}.")
        with self._lock:
            state.last_action = f"address: {note}"
        self._log_event(rx, "address_failed", note)
        return (False, note)

    def set_enabled(self, receiver_id: str, enabled: bool) -> None:
        """Include a receiver in polling, or leave it out.

        A disabled receiver is hidden from the dashboard rather than shown as
        permanently offline, which is right for a spare in a cupboard -- but it
        also has to be possible to bring one back.
        """
        state = self.states[receiver_id]
        with self._lock:
            if state.receiver.enabled == enabled:
                return
            state.receiver.enabled = enabled
            if enabled:
                state.status = None          # no stale reading from before
                state.consecutive_offline = 0
                state.consecutive_drift = 0
                state.consecutive_no_signal = 0
                state.details_read = False
        self.config.save()
        self._log_event(state.receiver, "enabled" if enabled else "disabled",
                        "now polled" if enabled else "no longer polled")
        if enabled:
            self.refresh_soon()

    def disabled_receivers(self) -> list[dict]:
        """Receivers excluded from polling, so the dashboard can offer them."""
        with self._lock:
            return [
                {**state.receiver.as_dict()}
                for state in sorted(self.states.values(),
                                    key=lambda s: _sort_key(s.receiver))
                if not state.receiver.enabled
            ]

    def raw_for(self, receiver_id: str) -> dict[str, str]:
        state = self.states[receiver_id]
        return dict(state.status.raw) if state.status else {}

    # --- reporting -------------------------------------------------------
    def _log_event(self, receiver: Receiver, kind: str, message: str) -> None:
        event = {
            "ts": time.time(),
            "receiver_id": receiver.id,
            "receiver_name": receiver.name,
            "kind": kind,
            "message": message,
        }
        self.events.append(event)
        log.info("%s [%s] %s", receiver.name, kind, message)
        if self.event_log_path is not None:
            try:
                with self.event_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event) + "\n")
            except OSError as exc:
                log.warning("cannot write event log: %s", exc)

    # --- discovery -------------------------------------------------------
    def start_discovery(self, ranges: list[str] | None = None,
                        *, force: bool = False) -> int:
        """Begin a scan in the background and return how many addresses it covers.

        Scanning a whole VLAN takes minutes, which is far too long to hold an
        HTTP request open, so the work happens on its own thread and the
        dashboard follows progress through the snapshot.
        """
        specs = ranges if ranges is not None else self.config.discovery_ranges
        if not specs:
            raise ValueError(
                "no ranges to scan. Set \"discovery_ranges\" in the config, "
                "e.g. [\"10.0.2.1-254\"]"
            )
        hosts = discovery.parse_targets(specs)      # raises on a bad range
        if discovery.is_large(hosts) and not force:
            raise ValueError(
                f"{len(hosts)} addresses is a very wide sweep. Probing "
                "addresses that do not exist makes the gateway ARP for each "
                "one, and that has been observed to knock the receivers off "
                "their multicast streams for around half a minute. Scan a "
                "subnet instead (e.g. 10.0.2.1-254), or pass force to "
                "accept the disruption."
            )
        with self._lock:
            if self.discovery_running:
                raise RuntimeError("a scan is already running")
            self.discovery_running = True
            self.discovery_message = f"queued: {len(hosts)} addresses"
            self.discovery_scanned = len(hosts)
            self.discovery_error = ""

        def run() -> None:
            try:
                self._discover_locked(hosts, len(hosts))
            except Exception as exc:                # never kill the thread quietly
                log.exception("scan failed")
                with self._lock:
                    self.discovery_error = f"{type(exc).__name__}: {exc}"
                self._log_config_event("scan_failed", str(exc))
            finally:
                with self._lock:
                    self.discovery_running = False
                    self.discovery_message = ""
                    self.last_discovery = time.time()

        thread = threading.Thread(target=run, name="veo-discovery", daemon=True)
        self._discovery_thread = thread
        thread.start()
        return len(hosts)

    def discover(self, ranges: list[str] | None = None) -> list[dict]:
        """Look for VEO devices on the network that are not in the config.

        Safe to run alongside polling: every session goes through the same
        per-host lock, so discovery and the poll loop cannot talk to one device
        at the same time.
        """
        specs = ranges if ranges is not None else self.config.discovery_ranges
        if not specs:
            raise ValueError(
                "no ranges to scan. Set \"discovery_ranges\" in the config, "
                "e.g. [\"10.0.2.1-254\"]"
            )
        with self._lock:
            if self.discovery_running:
                raise RuntimeError("a scan is already running")
            self.discovery_running = True
            self.discovery_message = "starting"

        try:
            hosts = discovery.parse_targets(specs)
        except ValueError:
            with self._lock:
                self.discovery_running = False
            raise

        try:
            return self._discover_locked(hosts, len(hosts))
        finally:
            with self._lock:
                self.discovery_running = False
                self.last_discovery = time.time()
                self.discovery_message = ""

    def _discover_locked(self, hosts: list[str], scanned: int) -> list[dict]:
        """The scan itself.  Assumes ``discovery_running`` is already set."""
        with self._lock:
            self.discovery_scanned = scanned

        def progress(message: str) -> None:
            with self._lock:
                self.discovery_message = message

        statuses = discovery.sweep(
            hosts,
            port=self.config.telnet_port,
            timeout=self.config.timeout_seconds,
            concurrency=self.config.max_workers,
            on_progress=progress,
        )

        known_ips = {rx.ip for rx in self.config.receivers}
        known_ips |= {c.transmitter_ip for c in self.config.channels
                      if c.transmitter_ip}

        found: list[dict] = []
        for status in statuses:
            if status.host in known_ips or not discovery.is_veo(status):
                continue
            found.append({
                "ip": status.host,
                "group_id": status.group_id,
                "channel_name": self.config.channel_name(status.group_id),
                "video_lock": status.video_lock,
                "device_name": status.device_name,
                "fw_version": status.fw_version,
                "mac_address": status.mac_address,
                "role": discovery.classify(status, set()),
                "suggested_id": _suggest_id(status.host),
                "suggested_name": status.device_name or _suggest_id(status.host),
            })

        with self._lock:
            self.discovered = found

        if found:
            self._log_config_event(
                "discovered",
                f"scanned {scanned} address(es): {len(found)} device(s) not in "
                f"the config ({', '.join(d['ip'] for d in found)})",
            )
        else:
            self._log_config_event(
                "discovered",
                f"scanned {scanned} address(es): nothing new — every VEO that "
                "answered is already in the config",
            )
        return found

    def add_discovered(self, ip: str) -> Receiver:
        """Add a discovered device to the config as a receiver."""
        if any(rx.ip == ip for rx in self.config.receivers):
            raise ValueError(f"{ip} is already in the config")
        with self._lock:
            entry = next((d for d in self.discovered if d["ip"] == ip), None)
        if entry is None:
            raise KeyError(f"{ip} is not in the last scan's results; scan again")

        used_ids = {rx.id for rx in self.config.receivers}
        receiver_id = entry["suggested_id"]
        suffix = 2
        while receiver_id in used_ids:
            receiver_id = f"{entry['suggested_id']}-{suffix}"
            suffix += 1

        receiver = Receiver(
            id=receiver_id,
            name=entry["suggested_name"],
            ip=ip,
            # Adopt whatever it is currently showing, so it does not immediately
            # report as drifted before anyone has said where it belongs.
            expected_group_id=entry["group_id"],
        )
        with self._lock:
            self.config.receivers.append(receiver)
            self.states[receiver.id] = ReceiverState(receiver=receiver)
            self.discovered = [d for d in self.discovered if d["ip"] != ip]
        self.config.save()
        self._log_event(receiver, "added",
                        f"added from a scan on channel {entry['group_id']}")
        self.refresh_soon()
        return receiver

    def _log_config_event(self, kind: str, message: str) -> None:
        event = {
            "ts": time.time(),
            "receiver_id": "-",
            "receiver_name": "scan",
            "kind": kind,
            "message": message,
        }
        self.events.append(event)
        log.info("scan [%s] %s", kind, message)
        if self.event_log_path is not None:
            try:
                with self.event_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event) + "\n")
            except OSError as exc:
                log.warning("cannot write event log: %s", exc)

    def reload(self, config: Config) -> None:
        """Adopt a new config in place.

        Receivers that survive the change keep their existing state, so an
        import does not blank out what is known about the fleet or restart the
        drift counters for devices that did not change.
        """
        with self._lock:
            previous = self.states
            self.config = config
            self.states = {}
            for receiver in config.receivers:
                existing = previous.get(receiver.id)
                if existing is not None:
                    existing.receiver = receiver     # keep status and counters
                    self.states[receiver.id] = existing
                else:
                    self.states[receiver.id] = ReceiverState(receiver=receiver)
            added = [r.id for r in config.receivers if r.id not in previous]
            removed = [rid for rid in previous if rid not in self.states]

        log.info("config reloaded: %d receiver(s), %d channel(s)",
                 len(config.receivers), len(config.channels))
        self.events.append({
            "ts": time.time(),
            "receiver_id": "-",
            "receiver_name": "config",
            "kind": "imported",
            "message": (f"config replaced: {len(config.receivers)} receivers, "
                        f"{len(config.channels)} channels"
                        + (f"; added {', '.join(added)}" if added else "")
                        + (f"; removed {', '.join(removed)}" if removed else "")),
        })
        self.refresh_soon()

    def inventory_text(self) -> str:
        """The live fleet as a devices.txt, so naming work has a second home.

        Round-trips through ``tools/scan.py --names``: locations ride along as
        trailing comments, which that parser strips.
        """
        import datetime

        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            "# Ecler VEO fleet inventory, exported from the dashboard "
            f"on {stamp}.",
            '# Format: "<ip> <name>" per line; # starts a comment.',
            "# Read by tools/scan.py --names when generating a config.",
            "",
        ]
        with self._lock:
            for channel in sorted(self.config.channels, key=lambda c: c.group_id):
                if channel.transmitter_ip:
                    lines.append(
                        f"{channel.transmitter_ip:<14} tx-ch{channel.group_id}"
                        f"-{_slug(channel.name)}"
                        f"{'   # ' + channel.note if channel.note else ''}"
                    )
            if any(c.transmitter_ip for c in self.config.channels):
                lines.append("")

            grouped: dict[int | None, list[ReceiverState]] = {}
            for state in self.states.values():
                grouped.setdefault(state.receiver.expected_group_id, []).append(state)

            def sort_key(group_id: int | None) -> tuple:
                return (group_id is None, group_id if group_id is not None else 0)

            for group_id in sorted(grouped, key=sort_key):
                members = sorted(grouped[group_id],
                                 key=lambda st: _sort_key(st.receiver))
                if group_id is None:
                    lines.append(f"# --- no expected channel "
                                 f"({len(members)}) ---")
                else:
                    name = self.config.channel_name(group_id) or "?"
                    lines.append(f"# --- channel {group_id}: {name} "
                                 f"({len(members)} TVs) ---")
                for state in members:
                    rx = state.receiver
                    comments = []
                    if rx.location:
                        comments.append(rx.location)
                    if state.status is not None and state.status.mac_address:
                        comments.append(state.status.mac_address)
                    if not rx.enabled:
                        comments.append("disabled")
                    elif state.status is not None and not state.status.online:
                        comments.append("did not answer")
                    suffix = "   # " + ", ".join(comments) if comments else ""
                    lines.append(f"{rx.ip:<14} {rx.name}{suffix}")
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def snapshot(self) -> dict:
        """A full JSON-serialisable view for the dashboard."""
        with self._lock:
            # Disabled receivers are never polled, so showing them would mean
            # showing them permanently offline.  Leave them out entirely.
            devices = [
                state.as_dict(self.config)
                for state in sorted(
                    self.states.values(), key=lambda s: _sort_key(s.receiver)
                )
                if state.receiver.enabled
            ]
            online = sum(1 for d in devices if d["online"])
            return {
                "devices": devices,
                "channels": [c.as_dict() for c in self.config.channels],
                "summary": {
                    "held": sum(1 for d in devices if d["held"]),
                    "total": len(devices),
                    "online": online,
                    "offline": len(devices) - online,
                    "drifted": sum(1 for d in devices if d["drifted"]),
                    "no_signal": sum(
                        1 for d in devices if d["online"] and d["video_lock"] is False
                    ),
                    # On DHCP with no DHCP server on the VLAN, these revert to
                    # the factory-default address on their next reboot.
                    "dhcp": sum(1 for d in devices if d["dhcp"] is True),
                    # Where receivers actually are, by channel, regardless of
                    # what they are assigned to.  Answers "so where did the
                    # office screens end up?" directly.
                    "by_actual_channel": _tally(
                        d["group_id"] for d in devices),
                    "by_expected_channel": _tally(
                        d["expected_group_id"] for d in devices),
                },
                "poll": {
                    "interval_seconds": self.config.poll_interval_seconds,
                    "last_started": self.last_poll_started,
                    "last_finished": self.last_poll_finished,
                    "count": self.poll_count,
                    "auto_repair": self.config.auto_repair,
                    "auto_nudge_on_signal_loss": self.config.auto_nudge_on_signal_loss,
                },
                "disabled": self.disabled_receivers(),
                "discovered": list(self.discovered),
                "discovery": {
                    "ranges": list(self.config.discovery_ranges),
                    "interval_hours": self.config.discovery_interval_hours,
                    "running": self.discovery_running,
                    "message": self.discovery_message,
                    "scanned": self.discovery_scanned,
                    "error": self.discovery_error,
                    "last": self.last_discovery,
                },
                "events": list(reversed(self.events)),
                "server_time": time.time(),
            }


def _tally(values) -> dict[str, int]:
    """Count occurrences, with None collected under "unknown"."""
    counts: dict[str, int] = {}
    for value in values:
        key = "unknown" if value is None else str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _suggest_id(ip: str) -> str:
    """A receiver id from an address: 10.0.2.7 -> rx-07."""
    last = ip.rsplit(".", 1)[-1]
    return f"rx-{int(last):02d}" if last.isdigit() else f"rx-{last}"


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "unnamed"


def _sort_key(receiver: Receiver) -> tuple:
    """Sort naturally so "TV 2" precedes "TV 10".

    Every part is tagged with its type before comparison. Without that, a name
    beginning with a digit yields a tuple starting with an int while its
    neighbour's starts with a str, and Python refuses to order the two --
    TypeError out of sorted(), a 500 from /api/state, and a dashboard showing
    nothing at all.

    That failure was permanent, not transient: the rename is written to
    config.json before the sort is ever attempted, so the dashboard stayed
    dead across restarts and could only be recovered by editing the file by
    hand. "2nd floor canteen" was enough to do it.
    """
    parts = re.split(r"(\d+)", receiver.name)
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.lower())
                 for part in parts if part != "")
