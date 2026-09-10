"""Finding VEO devices on the network.

Shared by ``tools/scan.py`` (the audit CLI) and the dashboard's own discovery,
so the two cannot drift apart in what counts as a VEO.

The important subtlety: on this network the televisions sit on the same VLAN as
the receivers, so a device answering on port 9999 is not necessarily a VEO.
:func:`is_veo` requires it to actually speak the protocol.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor

from . import veo

#: Above this many addresses, sweep for open ports before opening sessions.
PRESCAN_OVER = 512

#: Concurrency for that sweep.  Deliberately modest.  Probing addresses that do
#: not exist makes the gateway ARP for each one, and a fast wide sweep turns
#: that into an ARP flood: on 2026-09-09 a 65k-address sweep of 10.0.0.0/16
#: knocked every receiver in the building off its multicast stream for about 35
#: seconds and timed two of them out completely.  Scanning must stay gentle
#: enough not to disturb the video it is meant to be looking after.
PRESCAN_CONCURRENCY = 32

#: Addresses per batch, with a pause between batches, so a wide sweep is spread
#: out over time rather than delivered all at once.
PRESCAN_BATCH = 256
PRESCAN_BATCH_PAUSE = 0.35

#: These units serve one control session at a time, and reconnecting straight
#: after the sweep leaves them accepting TCP while answering nothing.  So wait.
PRESCAN_PAUSE = 3.0

#: A sweep bigger than this is refused unless explicitly forced: a whole /16 is
#: rarely what anyone means, and it is the size that caused the outage above.
LARGE_SWEEP = 8192


def parse_targets(specs: list[str]) -> list[str]:
    """Expand CIDRs, ``a.b.c.d-e`` ranges and bare addresses into a host list."""
    hosts: list[str] = []
    seen: set[str] = set()

    def add(address: str) -> None:
        if address not in seen:
            seen.add(address)
            hosts.append(address)

    for spec in specs:
        spec = spec.strip()
        if not spec:
            continue
        if "/" in spec:
            for host in ipaddress.ip_network(spec, strict=False).hosts():
                add(str(host))
        elif "-" in spec:
            head, _, tail = spec.rpartition(".")
            first, _, last = tail.partition("-")
            if not head or not first.isdigit() or not last.isdigit():
                raise ValueError(f"cannot parse range {spec!r} (use 10.0.0.1-50)")
            for octet in range(int(first), int(last) + 1):
                add(f"{head}.{octet}")
        else:
            add(str(ipaddress.ip_address(spec)))
    return hosts


def port_open(host: str, port: int, timeout: float) -> bool:
    """Whether a TCP port accepts a connection.

    Takes the per-host lock, so this cannot land in the middle of a poll.  That
    matters: these units serve one control session at a time, and a connect
    that interleaves with a real session leaves the device accepting TCP while
    answering nothing.
    """
    with veo.host_lock(host):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


def is_veo(status: veo.DeviceStatus) -> bool:
    """Whether this really is a VEO, rather than something else on the port.

    A VEO answers the protocol: it reports a Group ID, or a firmware version,
    MAC, or model name through the same command set.  A device that accepts the
    connection and says nothing recognisable is not one.
    """
    return any((
        status.group_id is not None,
        status.fw_version,
        status.mac_address,
        "VEO" in (status.device_name or "").upper(),
    ))


def classify(status: veo.DeviceStatus, transmitters: set[str]) -> str:
    """Transmitter, receiver, or not a VEO at all."""
    if not is_veo(status):
        return "not a VEO?"
    if status.host in transmitters:
        return "transmitter"
    name = (status.device_name or "").upper()
    if "XTI" in name:
        return "transmitter"
    if "XRI" in name:
        return "receiver"
    return "receiver?"


def read_with_retry(host: str, *, port: int, timeout: float,
                    retries: int = 1) -> veo.DeviceStatus:
    """Read one device, retrying a connected-but-mute unit.

    A unit that accepts the connection but returns nothing is usually still
    busy with a previous session, so a pause and one more try often suffices.
    """
    status = veo.read_status(host, port=port, timeout=timeout, with_details=True)
    attempt = 0
    while attempt < retries and status.online and status.group_id is None:
        attempt += 1
        time.sleep(1.5)
        status = veo.read_status(host, port=port, timeout=timeout,
                                 with_details=True)
    return status


def is_large(hosts: list[str]) -> bool:
    """Whether a sweep of these hosts is big enough to disturb the network."""
    return len(hosts) > LARGE_SWEEP


def sweep(
    hosts: list[str],
    *,
    port: int = veo.DEFAULT_PORT,
    timeout: float = 4.0,
    connect_timeout: float = 1.0,
    concurrency: int = 12,
    retries: int = 1,
    prescan_over: int = PRESCAN_OVER,
    prescan_concurrency: int = PRESCAN_CONCURRENCY,
    prescan_pause: float = PRESCAN_PAUSE,
    on_progress=None,
) -> list[veo.DeviceStatus]:
    """Query every host and return the statuses of those that answered.

    For a small range, one session per host and only one.  For a large mostly
    empty range, sweep for open ports first and then session only the hits,
    after a pause (see :data:`PRESCAN_PAUSE`).
    """
    if not hosts:
        return []

    targets = hosts
    if len(hosts) > prescan_over:
        targets = []
        total = len(hosts)
        with ThreadPoolExecutor(max_workers=prescan_concurrency,
                                thread_name_prefix="prescan") as pool:
            for start in range(0, total, PRESCAN_BATCH):
                batch = hosts[start:start + PRESCAN_BATCH]
                if on_progress:
                    on_progress(
                        f"sweeping {start + len(batch)}/{total} addresses, "
                        f"{len(targets)} found")
                flags = list(pool.map(
                    lambda host: port_open(host, port, connect_timeout), batch))
                targets += [host for host, is_open in zip(batch, flags) if is_open]
                if start + PRESCAN_BATCH < total:
                    # Spread the ARP load rather than delivering it all at once.
                    time.sleep(PRESCAN_BATCH_PAUSE)
        if on_progress:
            on_progress(f"{len(targets)} open; pausing {prescan_pause:g}s")
        if targets:
            time.sleep(prescan_pause)

    if not targets:
        return []
    if on_progress:
        on_progress(f"querying {len(targets)} device(s)")

    workers = max(1, min(concurrency, len(targets)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="discover") as pool:
        statuses = list(pool.map(
            lambda host: read_with_retry(host, port=port, timeout=timeout,
                                         retries=retries),
            targets,
        ))
    answered = [status for status in statuses if status.online]
    answered.sort(key=lambda s: sort_key(s.host))
    return answered


def sort_key(host: str) -> tuple:
    """Sort addresses numerically, with non-addresses last."""
    try:
        return (0,) + tuple(int(part) for part in host.split("."))
    except ValueError:
        return (1, host)
