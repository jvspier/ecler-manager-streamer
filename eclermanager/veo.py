"""Protocol client for Ecler VEO-XTI1C / VEO-XRI1C H.264 video-over-IP extenders.

Control path
------------
The Ecler manual documents a "Telnet" server on **TCP port 9999** that accepts
``set_group_id <n>`` (n = 0..63) terminated with CRLF.  The Group ID is the
"channel": every transmitter streams on one Group ID and a receiver shows
whichever Group ID it is set to.

Ecler only documents the *set* half.  The units are built on the widely
rebranded Lenkeng LKV373A OEM platform, whose ``list`` command on the same port
reports this command set::

    set_group_id, get_group_id, set_dhcp, get_dhcp, set_uart_baudrate,
    get_uart_baudrate, set_static_ip, get_static_ip, set_mac_address,
    get_mac_address, get_lan_status, get_hdcp, get_video_lock, get_ip_config,
    set_session_key, set_device_name, get_device_name, set_video_bitrate,
    get_video_bitrate, set_downscale_mode, get_downscale_mode,
    set_video_out_mode, get_video_out_mode, set_streaming_mode,
    get_streaming_mode, get_fw_version, get_company_id, reboot, list, exit

Because that list is inferred from a sibling firmware rather than from Ecler's
own documentation, every read here is best-effort: unsupported commands degrade
to ``None`` instead of raising, and the raw device reply is always retained so
``tools/probe.py`` can show exactly what a real unit says.

Note: do not confuse this with the VEO-*2L* series, a different platform that
uses ``e e_reconnect::0002`` over a root login.  These commands are for 1C only.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field

DEFAULT_PORT = 9999
DEFAULT_TIMEOUT = 4.0
DEFAULT_CONNECT_TIMEOUT = 2.0

#: Ecler's manual specifies CRLF, and most units accept it.  Some do not: they
#: treat the CR as the terminator, choke on the following LF, and hang up --
#: observed on 10.0.2.23 and .32, which answer LF-only and close the session
#: on CRLF.  So CRLF is tried first and LF is the fallback, learned per host.
CRLF = b"\r\n"
LF = b"\n"
LINE_ENDINGS = (CRLF, LF)

_line_endings: dict[str, bytes] = {}
_line_endings_guard = threading.Lock()


def line_ending_for(host: str) -> bytes:
    """The line ending known to work for this host, else the documented CRLF."""
    with _line_endings_guard:
        return _line_endings.get(host, CRLF)


def remember_line_ending(host: str, ending: bytes) -> None:
    with _line_endings_guard:
        if _line_endings.get(host) != ending:
            _line_endings[host] = ending
            if ending == LF:
                log.info("%s needs LF line endings, not CRLF", host)


def forget_line_endings() -> None:
    """Clear what has been learned.  For tests."""
    with _line_endings_guard:
        _line_endings.clear()

log = logging.getLogger("eclermanager.veo")

GROUP_ID_MIN = 0
GROUP_ID_MAX = 63

# The cheap SoC in these boxes serves one control session at a time, so the
# poller and an interactive click must never talk to the same unit at once.
_host_locks: dict[str, threading.Lock] = {}
_host_locks_guard = threading.Lock()


def host_lock(host: str) -> threading.Lock:
    """Return the process-wide lock guarding control access to one device."""
    with _host_locks_guard:
        lock = _host_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _host_locks[host] = lock
        return lock


class VeoError(Exception):
    """Any failure talking to a VEO device."""


class VeoUnreachable(VeoError):
    """Could not open or complete a control session."""


_IAC = 255
_SB, _SE = 250, 240
_PROMPT_RE = re.compile(r"(?:input\s*>|/\s*#|[>#\$])\s*$")
#: Devices prefix the echoed command with their prompt: "input>get_group_id".
_PROMPT_PREFIX_RE = re.compile(r"^\s*(?:input\s*>|/\s*#)\s*")
#: Banner rule-off lines, e.g. "=====IPTV RX Server=====".
_BANNER_RE = re.compile(r"^[=*_\-\s]*$")


def strip_telnet_negotiation(data: bytes) -> bytes:
    """Drop IAC negotiation sequences and NUL padding.

    Real units emit the prompt as ``input>\x00``, and the connect banner ends
    with a NUL too; leaving those in makes every downstream regex awkward.
    """
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != _IAC:
            if b:                       # drop NUL padding
                out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        nxt = data[i + 1]
        if nxt == _IAC:          # escaped 0xFF
            out.append(_IAC)
            i += 2
        elif nxt == _SB:         # subnegotiation: skip to IAC SE
            j = i + 2
            while j + 1 < n and not (data[j] == _IAC and data[j + 1] == _SE):
                j += 1
            i = j + 2
        else:                    # WILL / WONT / DO / DONT + option byte
            i += 3
    return bytes(out)


class VeoSession:
    """A single short-lived control conversation with one device.

    Used as a context manager; holds :func:`host_lock` for its whole lifetime.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        line_ending: bytes | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.line_ending = (line_ending if line_ending is not None
                            else line_ending_for(host))
        self.greeting = ""
        self._sock: socket.socket | None = None
        self._lock = host_lock(host)
        self._locked = False

    def __enter__(self) -> "VeoSession":
        self._lock.acquire()
        self._locked = True
        try:
            self.open()
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
        self._release()

    def _release(self) -> None:
        if self._locked:
            self._locked = False
            self._lock.release()

    def open(self) -> None:
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise VeoUnreachable(f"{self.host}:{self.port} {exc}") from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        # Consume the banner *and* the prompt that follows it.  Leaving the
        # prompt buffered was the cause of replies arriving one command late.
        self.greeting = self._read(
            quiet=0.3,
            budget=min(4.0, self.timeout + 2.0),
            first_byte=min(2.0, self.timeout),
            accept_bare_prompt=True,
        )

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(b"exit" + self.line_ending)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def _message_complete(self, text: str) -> bool:
        """True when a full reply has arrived: content, then a prompt.

        The units emit ``input>`` *after* each answer, so the prompt is the
        protocol's own end-of-message marker.  Synchronising on it -- rather
        than on a pause in the bytes -- is what keeps replies matched to the
        commands that caused them.  A leading prompt left over from the
        previous exchange is discarded before the check, so it cannot be
        mistaken for the terminator.
        """
        body = _PROMPT_PREFIX_RE.sub("", text, count=1)
        if not _PROMPT_RE.search(body):
            return False
        return bool(_PROMPT_RE.sub("", body).strip())

    def _read(self, *, quiet: float, budget: float, first_byte: float,
              accept_bare_prompt: bool = False) -> str:
        """Accumulate one reply.

        Terminates on the prompt that follows the answer.  Falls back to a
        quiet gap for firmware that sends no prompt at all -- but never while
        the buffer holds nothing more than a prompt, because that is the state
        where the answer is still on its way.
        """
        assert self._sock is not None
        buf = bytearray()
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            text = strip_telnet_negotiation(bytes(buf)).decode("utf-8", "replace")
            if accept_bare_prompt:
                if _PROMPT_RE.search(text):
                    break               # draining to the first prompt
            elif self._message_complete(text):
                break
            self._sock.settimeout(min(quiet if buf else first_byte, remaining))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                # socket.timeout, not TimeoutError: the two are the same
                # object only from Python 3.10. On 3.9 and earlier this is a
                # plain OSError subclass, so `except TimeoutError` missed it,
                # the handler below turned a slow reply into "unreachable",
                # and any device that took a moment to answer read as offline.
                text = strip_telnet_negotiation(bytes(buf)).decode("utf-8", "replace")
                stripped = _PROMPT_PREFIX_RE.sub("", text, count=1)
                if accept_bare_prompt or _PROMPT_RE.sub("", stripped).strip():
                    break               # quiet, and we have something to show
                continue                # only a prompt so far: keep waiting
            except OSError as exc:
                raise VeoUnreachable(f"{self.host} read failed: {exc}") from exc
            if not chunk:
                break                   # peer closed
            buf.extend(chunk)
        return strip_telnet_negotiation(bytes(buf)).decode("utf-8", "replace")

    def command(self, cmd: str, *, quiet: float = 0.35,
                budget: float | None = None) -> str:
        """Send one command and return the device's raw reply text.

        Waits up to ``self.timeout`` for the reply to begin, then ``quiet``
        between chunks to decide it has ended.
        """
        if self._sock is None:
            raise VeoUnreachable(f"{self.host}: session is not open")
        try:
            self._sock.sendall(cmd.encode("ascii") + self.line_ending)
        except OSError as exc:
            raise VeoUnreachable(f"{self.host} write failed: {exc}") from exc
        return self._read(
            quiet=quiet,
            budget=self.timeout + 2.0 if budget is None else budget,
            first_byte=self.timeout,
        )


# --- reply parsing -------------------------------------------------------
# Firmware phrasing varies between rebrands ("group_id is 5", "groupid=05",
# a bare "05"), so parse loosely and keep the raw text either way.

_NOT_SUPPORTED_RE = re.compile(
    r"unknown|invalid|not support|unsupport|error|usage|command not found", re.I
)


def response_lines(response: str, command: str) -> list[str]:
    """Reply lines with the command echo and prompt noise removed."""
    cmd_head = command.split()[0].lower()
    lines = []
    for raw in response.replace("\r", "\n").split("\n"):
        line = _PROMPT_PREFIX_RE.sub("", raw)
        line = _PROMPT_RE.sub("", line).strip()
        if not line or _BANNER_RE.match(line):
            continue                      # blank, or a banner rule-off
        low = line.lower()
        if low == command.lower() or low.startswith(cmd_head + " "):
            continue                      # echoed command
        if low.startswith(cmd_head) and not re.search(r"\d", line):
            continue
        lines.append(line)
    return lines


def looks_unsupported(response: str, command: str) -> bool:
    return any(_NOT_SUPPORTED_RE.search(l) for l in response_lines(response, command))


def parse_int(response: str, command: str, lo: int, hi: int) -> int | None:
    """Pull an in-range integer out of a get_* reply."""
    lines = response_lines(response, command)
    if not lines or looks_unsupported(response, command):
        return None
    blob = " ".join(lines)
    # Prefer an explicit "key = 5" / "key is 5" / "key: 5" form.
    keyed = re.search(r"[A-Za-z_][\w ]*(?:=|:|\bis\b)\s*(\d{1,4})", blob)
    if keyed:
        value = int(keyed.group(1))
        if lo <= value <= hi:
            return value
    for match in re.finditer(r"\d{1,4}", blob):
        value = int(match.group())
        if lo <= value <= hi:
            return value
    return None


_TRUE_WORDS = re.compile(
    r"\b(?:lock(?:ed)?|yes|true|on|up|valid|present|active|enabled?)\b", re.I
)
# Checked first, so "disabled" cannot be read as "enabled".
_FALSE_WORDS = re.compile(
    r"\b(?:unlock(?:ed)?|no|false|off|down|invalid|absent|none|no ?signal"
    r"|disabled?)\b", re.I
)


def parse_bool(response: str, command: str) -> bool | None:
    """Interpret a get_video_lock / get_lan_status style reply."""
    lines = response_lines(response, command)
    if not lines or looks_unsupported(response, command):
        return None
    blob = " ".join(lines)
    if _FALSE_WORDS.search(blob):
        return False
    if _TRUE_WORDS.search(blob):
        return True
    tail = re.findall(r"\b([01])\b", blob)
    if tail:
        return tail[-1] == "1"
    return None


#: Strips a leading key from a reply: "device_name=x", "Rx Firmware : x",
#: "lan_status is x".  Two deliberate details: the key must start with a letter,
#: so a value that is itself colon-separated (a MAC address, an IP) is left
#: intact; and the quantifier is lazy, so it stops at the *first* separator
#: rather than eating "mac is 00" out of "mac is 00:19:F5:...".
_KEY_PREFIX_RE = re.compile(r"^[A-Za-z][\w .-]*?(?:=|:|\bis\b)\s*")


def parse_text(response: str, command: str) -> str | None:
    lines = response_lines(response, command)
    if not lines or looks_unsupported(response, command):
        return None
    value = lines[-1]
    value = _KEY_PREFIX_RE.sub("", value).strip().strip('"')
    return value or None


# --- device operations ---------------------------------------------------

@dataclass
class DeviceStatus:
    """One poll result for a single device."""

    host: str
    online: bool = False
    group_id: int | None = None
    video_lock: bool | None = None
    device_name: str | None = None
    fw_version: str | None = None
    lan_status: str | None = None
    mac_address: str | None = None
    dhcp: bool | None = None
    error: str | None = None
    latency_ms: float | None = None
    checked_at: float = field(default_factory=time.time)
    raw: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = {
            "host": self.host,
            "online": self.online,
            "group_id": self.group_id,
            "video_lock": self.video_lock,
            "device_name": self.device_name,
            "fw_version": self.fw_version,
            "lan_status": self.lan_status,
            "mac_address": self.mac_address,
            "dhcp": self.dhcp,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms else None,
            "checked_at": self.checked_at,
        }
        return data


#: Commands issued on every poll, cheapest and most useful first.
POLL_COMMANDS = ("get_group_id", "get_video_lock")
#: Commands issued only on the first successful contact with a device.
#: ``get_dhcp`` matters more than it looks: a receiver left on DHCP where no
#: DHCP server exists falls back to the factory-default address on its next
#: reboot, losing whatever address it was reachable at.  That is exactly how
#: 10.0.2.32 vanished on 2026-09-09.
DETAIL_COMMANDS = ("get_device_name", "get_fw_version", "get_lan_status",
                   "get_mac_address", "get_dhcp")


def read_status(
    host: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    with_details: bool = False,
) -> DeviceStatus:
    """Poll one device.  Never raises: failures land in ``status.error``.

    If the documented CRLF gets nothing out of a device that is plainly there,
    the session is retried once with LF and the working ending is remembered
    for this host.  Some units close the connection on CRLF entirely.
    """
    commands = list(POLL_COMMANDS) + (list(DETAIL_COMMANDS) if with_details else [])
    first = line_ending_for(host)
    attempts = [first] + [e for e in LINE_ENDINGS if e != first]

    status = DeviceStatus(host=host)
    for attempt, ending in enumerate(attempts):
        status = _read_status_once(host, port, timeout, commands, ending)
        if status.group_id is not None:
            remember_line_ending(host, ending)
            if attempt:
                status.raw["<line-ending>"] = (
                    "CRLF got nothing; this device needs LF"
                )
            return status
        if not status.online:
            return status                 # unreachable: another ending will not help
    return status


def _read_status_once(host: str, port: int, timeout: float,
                      commands: list[str], ending: bytes) -> DeviceStatus:
    status = DeviceStatus(host=host)
    started = time.monotonic()
    try:
        with VeoSession(host, port, timeout=timeout,
                        line_ending=ending) as session:
            # The connection itself succeeded, so the unit is up even if it
            # goes on to say nothing useful.
            status.online = True
            if session.greeting.strip():
                status.raw["<greeting>"] = session.greeting
            for cmd in commands:
                try:
                    reply = session.command(cmd)
                except VeoError as exc:
                    # Some units hang up part-way through a session.  Keep
                    # whatever they did tell us rather than discarding it.
                    status.error = f"{cmd}: {exc}"
                    break
                status.raw[cmd] = reply
                if cmd == "get_group_id":
                    status.group_id = parse_int(reply, cmd, GROUP_ID_MIN, GROUP_ID_MAX)
                elif cmd == "get_video_lock":
                    status.video_lock = parse_bool(reply, cmd)
                elif cmd == "get_device_name":
                    status.device_name = parse_text(reply, cmd)
                elif cmd == "get_fw_version":
                    status.fw_version = parse_text(reply, cmd)
                elif cmd == "get_lan_status":
                    status.lan_status = parse_text(reply, cmd)
                elif cmd == "get_mac_address":
                    status.mac_address = parse_text(reply, cmd)
                elif cmd == "get_dhcp":
                    status.dhcp = parse_bool(reply, cmd)
    except VeoError as exc:
        status.error = str(exc)
    except OSError as exc:                      # belt and braces
        status.error = f"{type(exc).__name__}: {exc}"
    status.latency_ms = (time.monotonic() - started) * 1000.0
    status.checked_at = time.time()
    return status


@dataclass
class SwitchResult:
    """Outcome of a channel change, including the read-back verification."""

    host: str
    requested: int
    ok: bool
    verified_group_id: int | None = None
    video_lock: bool | None = None
    message: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "requested": self.requested,
            "ok": self.ok,
            "verified_group_id": self.verified_group_id,
            "video_lock": self.video_lock,
            "message": self.message,
        }


def set_group_id(
    host: str,
    group_id: int,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    verify: bool = True,
    verify_delay: float = 1.5,
) -> SwitchResult:
    """Set a receiver's Group ID (channel) and read it back to confirm.

    The device applies ``set_group_id`` live, front-panel LED included (seen
    2026-09-23, despite the manual saying a web change leaves it stale); the
    read-back is what the result reports either way.
    """
    if not GROUP_ID_MIN <= group_id <= GROUP_ID_MAX:
        raise ValueError(
            f"group_id {group_id} out of range {GROUP_ID_MIN}..{GROUP_ID_MAX}"
        )
    first = line_ending_for(host)
    attempts = [first] + [e for e in LINE_ENDINGS if e != first]
    for attempt, ending in enumerate(attempts):
        result = _set_group_id_once(host, group_id, port, timeout, verify,
                                    verify_delay, ending)
        if result.ok:
            remember_line_ending(host, ending)
            return result
        # A device that hangs up or says nothing may want the other line
        # ending; one that answered and disagreed will not.
        if result.verified_group_id is not None:
            return result
        if attempt == len(attempts) - 1:
            return result
    return result


def _set_group_id_once(host: str, group_id: int, port: int, timeout: float,
                       verify: bool, verify_delay: float,
                       ending: bytes) -> SwitchResult:
    result = SwitchResult(host=host, requested=group_id, ok=False)
    command = f"set_group_id {group_id}"
    try:
        with VeoSession(host, port, timeout=timeout,
                        line_ending=ending) as session:
            reply = session.command(command)
            result.raw[command] = reply
            if looks_unsupported(reply, command):
                result.message = f"device rejected the command: {reply.strip()[:200]}"
                return result
            if not verify:
                result.ok = True
                result.message = "sent (not verified)"
                return result
            time.sleep(verify_delay)
            back = session.command("get_group_id")
            result.raw["get_group_id"] = back
            result.verified_group_id = parse_int(
                back, "get_group_id", GROUP_ID_MIN, GROUP_ID_MAX
            )
            lock_reply = session.command("get_video_lock")
            result.raw["get_video_lock"] = lock_reply
            result.video_lock = parse_bool(lock_reply, "get_video_lock")
    except VeoError as exc:
        result.message = str(exc)
        return result

    if result.verified_group_id is None:
        result.ok = True                        # command accepted, read-back mute
        result.message = "sent; device did not report its Group ID back"
    elif result.verified_group_id == group_id:
        result.ok = True
        result.message = f"confirmed on channel {group_id}"
    else:
        result.message = (
            f"device reports channel {result.verified_group_id}, expected {group_id}"
        )
    return result


def bounce(
    host: str,
    *,
    target_group_id: int,
    via_group_id: int,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    dwell: float = 2.0,
    verify_delay: float = 2.5,
) -> SwitchResult:
    """Force a receiver to re-acquire its stream without a power cycle.

    Switches to ``via_group_id`` and straight back to ``target_group_id``.  That
    makes the unit leave and re-join the multicast group and re-initialise its
    decoder -- the network equivalent of unplugging it, which is the observed fix
    for a receiver that stays dark after its transmitter restarts underneath it.

    Re-sending the *same* Group ID is deliberately not used here: firmware that
    short-circuits "already on that channel" would make it a no-op.

    Both writes happen in one session, so nothing can interleave between them.
    """
    for name, value in (("target_group_id", target_group_id),
                        ("via_group_id", via_group_id)):
        if not GROUP_ID_MIN <= value <= GROUP_ID_MAX:
            raise ValueError(
                f"{name} {value} out of range {GROUP_ID_MIN}..{GROUP_ID_MAX}"
            )
    if target_group_id == via_group_id:
        raise ValueError(
            f"via_group_id must differ from target_group_id ({target_group_id})"
        )

    result = SwitchResult(host=host, requested=target_group_id, ok=False)
    try:
        with VeoSession(host, port, timeout=timeout,
                        line_ending=line_ending_for(host)) as session:
            away = f"set_group_id {via_group_id}"
            reply = session.command(away)
            result.raw[away] = reply
            if looks_unsupported(reply, away):
                result.message = f"device rejected the command: {reply.strip()[:200]}"
                return result
            time.sleep(dwell)

            back = f"set_group_id {target_group_id}"
            result.raw[back] = session.command(back)
            time.sleep(verify_delay)

            confirm = session.command("get_group_id")
            result.raw["get_group_id"] = confirm
            result.verified_group_id = parse_int(
                confirm, "get_group_id", GROUP_ID_MIN, GROUP_ID_MAX
            )
            lock_reply = session.command("get_video_lock")
            result.raw["get_video_lock"] = lock_reply
            result.video_lock = parse_bool(lock_reply, "get_video_lock")
    except VeoError as exc:
        result.message = str(exc)
        return result

    landed = result.verified_group_id
    if landed is not None and landed != target_group_id:
        result.message = (
            f"bounced via {via_group_id} but landed on {landed}, "
            f"expected {target_group_id}"
        )
        return result

    result.ok = True
    if result.video_lock is True:
        result.message = f"re-acquired channel {target_group_id}, signal is back"
    elif result.video_lock is False:
        result.message = (
            f"re-acquired channel {target_group_id} but still no signal "
            "- check the transmitter"
        )
    else:
        result.message = f"bounced via {via_group_id}, back on {target_group_id}"
    return result


def reboot(
    host: str, *, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT
) -> str:
    """Reboot a device.  Takes roughly 30-60s to come back."""
    with VeoSession(host, port, timeout=timeout) as session:
        return session.command("reboot", budget=min(3.0, timeout))


def set_device_name(host: str, name: str, *, port: int = DEFAULT_PORT,
                    timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str, str]:
    """Set the name a device reports for itself.

    Returns ``(ok, stored, message)``.  ``set_device_name`` stops at the first
    whitespace, so a name with spaces would silently store as its first word;
    that is rejected here rather than surprising the caller.
    """
    name = name.strip()
    if not name:
        return (False, "", "a name is required")
    if name.split()[0] != name:
        return (False, "", "the device's own name cannot contain spaces "
                           f"(it would store as {name.split()[0]!r}); "
                           f"try {'_'.join(name.split())!r}")

    command = f"set_device_name {name}"
    try:
        with VeoSession(host, port, timeout=timeout) as session:
            reply = session.command(command)
            if looks_unsupported(reply, command):
                return (False, "", f"device rejected it: {reply.strip()[:120]}")
            stored = parse_text(session.command("get_device_name"),
                               "get_device_name") or ""
    except VeoError as exc:
        return (False, "", str(exc))

    if stored == name:
        return (True, stored, f"the device now reports {stored!r}")
    if stored:
        return (False, stored,
                f"the device reports {stored!r}, not {name!r}"
                + (f" — truncated at {len(stored)} characters"
                   if name.startswith(stored) else ""))
    return (False, "", "the device did not report a name back")


def set_static_ip(host: str, ip: str, netmask: str, gateway: str, *,
                  port: int = DEFAULT_PORT,
                  timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Store a new static address.  It applies when the device restarts.

    ``get_ip_config`` reports the *running* configuration, so it cannot confirm
    this; the only proof is the device answering at the new address after a
    reboot.  Callers should reboot and then check.
    """
    command = f"set_static_ip ip {ip} netmask {netmask} gateway {gateway}"
    try:
        with VeoSession(host, port, timeout=timeout) as session:
            reply = session.command(command)
            if looks_unsupported(reply, command):
                return (False, f"device rejected it: {reply.strip()[:160]}")
            return (True, "stored; it applies when the device restarts")
    except VeoError as exc:
        return (False, str(exc))


def probe(host: str, *, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT,
          extra: tuple[str, ...] = ()) -> dict[str, str]:
    """Run an exploratory command sweep and return every raw reply."""
    commands = ("list", "help", "get_group_id", "get_video_lock", "get_device_name",
                "get_fw_version", "get_lan_status", "get_ip_config", "get_hdcp",
                "get_company_id", "get_streaming_mode") + extra
    out: dict[str, str] = {}
    with VeoSession(host, port, timeout=timeout) as session:
        out["<greeting>"] = session.greeting
        for cmd in commands:
            out[cmd] = session.command(cmd)
    return out
