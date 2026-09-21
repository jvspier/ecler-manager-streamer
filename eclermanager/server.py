"""HTTP server: JSON API plus the static dashboard."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, veo
from . import config as config_mod
from .auth import Auth
from .poller import Poller

log = logging.getLogger("eclermanager.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
MAX_BODY_BYTES = 64 * 1024
MAX_LABEL_LENGTH = 64

#: Login throttling: attempts per client address inside the window.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300.0

#: Reachable without logging in.
PUBLIC_PATHS = frozenset({"/login", "/logout", "/api/health"})


class LoginThrottle:
    """Counts recent failed logins per client address."""

    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS,
                 window: float = LOGIN_WINDOW_SECONDS) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, client: str, now: float) -> deque[float]:
        attempts = self._failures[client]
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._prune(client, time.monotonic())) >= self.max_attempts

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._prune(client, time.monotonic()).append(time.monotonic())

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

#: The action may contain a hyphen ("device-name"), which \w does not cover.
_RECEIVER_ROUTE = re.compile(r"^/api/receivers/([\w.@:-]{1,64})/([\w-]{1,32})$")


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = f"eclermanager/{__version__}"
    protocol_version = "HTTP/1.1"
    poller: Poller                      # injected via the server instance
    auth: Auth
    throttle: LoginThrottle

    # --- plumbing --------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload: dict | list, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body_text: str, filename: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json_body(self) -> dict:
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return data

    def _client(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _session_user(self) -> str | None:
        """The logged-in user, from a session cookie or Basic credentials."""
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            try:
                jar = SimpleCookie()
                jar.load(raw_cookie)
            except CookieError:
                jar = None
            if jar is not None:
                from .auth import SESSION_COOKIE
                morsel = jar.get(SESSION_COOKIE)
                if morsel is not None:
                    user = self.auth.read_session(morsel.value)
                    if user:
                        return user

        # Basic auth keeps scripting simple: curl -u user:pass ...
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
            user, _, password = decoded.partition(":")
            if self.auth.check_login(user, password):
                return user
        return None

    def _require_auth(self, path: str) -> bool:
        """True to continue.  Otherwise the response has already been sent."""
        if not self.auth.enabled or path in PUBLIC_PATHS:
            return True
        if self._session_user():
            return True
        if path.startswith("/api/"):
            # No WWW-Authenticate: it would make browsers pop up their own
            # login box over the dashboard's fetch calls.
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "login required")
        else:
            self._redirect("/login")
        return False

    def _redirect(self, location: str, *, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _check_origin(self) -> None:
        """Block cross-site writes from a page the user happens to have open."""
        origin = self.headers.get("Origin")
        if not origin:
            return
        host = self.headers.get("Host", "")
        if urlparse(origin).netloc != host:
            raise ApiError(HTTPStatus.FORBIDDEN, "cross-origin request refused")

    # --- routing ---------------------------------------------------------
    def do_GET(self) -> None:                       # noqa: N802
        try:
            path = urlparse(self.path).path
            if not self._require_auth(path):
                return
            if path == "/login":
                self._serve_login()
            elif path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path == "/api/state":
                self._send_json(self.poller.snapshot())
            elif path == "/api/health":
                self._send_json({"ok": True, "version": __version__,
                                 "auth": self.auth.enabled})
            elif path == "/api/config":
                self._send_text(
                    json.dumps(self.poller.config.as_dict(), indent=2) + "\n",
                    f"eclermanager-config-{time.strftime('%Y%m%d-%H%M%S')}.json",
                )
            elif path == "/api/setup":
                address = None
                query = parse_qs(urlparse(self.path).query)
                if query.get("address"):
                    address = query["address"][0]
                self._send_json(self.poller.inspect_setup_address(address))
            elif path == "/api/inventory":
                self._send_text(self.poller.inventory_text(), "devices.txt")
            elif path == "/api/whoami":
                self._send_json({"user": self._session_user(),
                                 "auth": self.auth.enabled})
            elif (match := _RECEIVER_ROUTE.match(path)) and match.group(2) == "raw":
                receiver_id = match.group(1)
                self._require_receiver(receiver_id)
                self._send_json({
                    "receiver_id": receiver_id,
                    "raw": self.poller.raw_for(receiver_id),
                })
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception as exc:                     # never leak a traceback
            log.exception("GET %s failed", self.path)
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}"
            )

    def do_POST(self) -> None:                      # noqa: N802
        try:
            self._check_origin()
            path = urlparse(self.path).path
            if path == "/login":
                self._post_login()
                return
            if path == "/logout":
                self._redirect("/login", cookie=self.auth.clear_cookie_header())
                return
            if not self._require_auth(path):
                return
            if path == "/api/refresh":
                self.poller.refresh_soon()
                self._send_json({"ok": True})
                return
            if path == "/api/config":
                self._post_config()
                return
            if path == "/api/setup":
                self._post_setup()
                return
            if path == "/api/discover":
                self._post_discover()
                return
            if path == "/api/discovered/add":
                self._post_add_discovered()
                return
            if path == "/api/release-all":
                results = self.poller.release_all_held()
                self._send_json({"ok": all(r.ok for r in results),
                                 "released": [r.as_dict() for r in results]})
                return
            if path == "/api/repair":
                results = self.poller.repair_all()
                self._send_json({
                    "ok": all(r.ok for r in results),
                    "repaired": [r.as_dict() for r in results],
                })
                return
            match = _RECEIVER_ROUTE.match(path)
            if not match:
                self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
                return
            receiver_id, action = match.group(1), match.group(2)
            self._require_receiver(receiver_id)
            if action == "channel":
                self._post_channel(receiver_id)
            elif action == "expected":
                self._post_expected(receiver_id)
            elif action == "name":
                self._post_name(receiver_id)
            elif action == "device-name":
                data = self._read_json_body()
                if "name" not in data:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'name'")
                name = self._clean_label(data["name"], "name")
                ok, message = self.poller.set_device_name(receiver_id, name)
                self._send_json(
                    {"ok": ok, "message": message},
                    status=HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY)
            elif action == "address":
                self._post_address(receiver_id)
            elif action == "enabled":
                data = self._read_json_body()
                if "enabled" not in data:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'enabled'")
                enabled = bool(data["enabled"])
                self.poller.set_enabled(receiver_id, enabled)
                self._send_json({"ok": True, "receiver_id": receiver_id,
                                 "enabled": enabled})
            elif action == "bounce":
                result = self.poller.bounce(receiver_id)
                self._send_json(
                    result.as_dict(),
                    status=HTTPStatus.OK if result.ok else HTTPStatus.BAD_GATEWAY,
                )
            elif action == "hold":
                self._send_result(self.poller.hold_identify(receiver_id))
            elif action == "release":
                self._send_result(self.poller.release_identify(receiver_id))
            elif action == "identify":
                result = self.poller.identify(receiver_id)
                self._send_json(
                    result.as_dict(),
                    status=HTTPStatus.OK if result.ok else HTTPStatus.BAD_GATEWAY,
                )
            elif action == "reboot":
                self._send_json({
                    "ok": True,
                    "reply": self.poller.reboot(receiver_id),
                })
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown action {action}")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except veo.VeoError as exc:
            self._send_error_json(HTTPStatus.BAD_GATEWAY, str(exc))
        except Exception as exc:
            log.exception("POST %s failed", self.path)
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}"
            )

    # --- handlers --------------------------------------------------------
    def _require_receiver(self, receiver_id: str) -> None:
        if receiver_id not in self.poller.states:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no receiver {receiver_id!r}")

    def _parse_group_id(self, data: dict, *, allow_null: bool = False) -> int | None:
        if "group_id" not in data:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'group_id'")
        value = data["group_id"]
        if value is None:
            if allow_null:
                return None
            raise ApiError(HTTPStatus.BAD_REQUEST, "'group_id' may not be null")
        try:
            group_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"'group_id' must be an integer, got {value!r}"
            ) from exc
        if not veo.GROUP_ID_MIN <= group_id <= veo.GROUP_ID_MAX:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"'group_id' must be {veo.GROUP_ID_MIN}..{veo.GROUP_ID_MAX}",
            )
        return group_id

    def _post_channel(self, receiver_id: str) -> None:
        data = self._read_json_body()
        group_id = self._parse_group_id(data)
        result = self.poller.set_channel(receiver_id, group_id)
        self._send_json(
            result.as_dict(), status=HTTPStatus.OK if result.ok else HTTPStatus.BAD_GATEWAY
        )

    def _post_expected(self, receiver_id: str) -> None:
        data = self._read_json_body()
        group_id = self._parse_group_id(data, allow_null=True)
        self.poller.set_expected(receiver_id, group_id)
        self._send_json({"ok": True, "receiver_id": receiver_id, "group_id": group_id})

    def _clean_label(self, value: object, field: str, *,
                     allow_empty: bool = False) -> str:
        """A label safe to store and display: trimmed, single line, bounded."""
        if not isinstance(value, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{field!r} must be a string")
        # Strip control characters so a name cannot smuggle in newlines that
        # would corrupt a regenerated inventory file.
        cleaned = "".join(c for c in value if c.isprintable()).strip()
        if not cleaned and not allow_empty:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{field!r} may not be empty")
        if len(cleaned) > MAX_LABEL_LENGTH:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"{field!r} is longer than {MAX_LABEL_LENGTH} characters",
            )
        return cleaned

    def _post_setup(self) -> None:
        """Commission a factory-default device and add it to the fleet."""
        import ipaddress

        data = self._read_json_body()
        fields = {}
        for field in ("ip", "netmask", "gateway"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, f"missing {field!r}")
            try:
                parsed = ipaddress.ip_address(value.strip())
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST,
                               f"{field} {value!r} is not an IP address") from exc
            if parsed.version != 4:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be IPv4")
            fields[field] = str(parsed)

        try:
            network = ipaddress.ip_network(
                f"{fields['ip']}/{fields['netmask']}", strict=False)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if ipaddress.ip_address(fields["gateway"]) not in network:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           f"gateway {fields['gateway']} is outside {network}")

        name = self._clean_label(data.get("name", ""), "name", allow_empty=True)
        device_name = None
        if data.get("device_name"):
            device_name = self._clean_label(data["device_name"], "device_name")
        group_id = None
        if data.get("expected_group_id") is not None:
            # _parse_group_id reads a key called "group_id"; this payload names
            # it expected_group_id, so hand it across under the expected name.
            group_id = self._parse_group_id(
                {"group_id": data["expected_group_id"]})
        from_address = data.get("from_address")
        if from_address is not None and not isinstance(from_address, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'from_address' must be text")

        try:
            result = self.poller.commission(
                ip=fields["ip"], netmask=fields["netmask"],
                gateway=fields["gateway"], name=name,
                expected_group_id=group_id, device_name=device_name,
                from_address=from_address)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        self._send_json(result,
                        status=HTTPStatus.OK if result["ok"]
                        else HTTPStatus.BAD_GATEWAY)

    def _post_address(self, receiver_id: str) -> None:
        import ipaddress

        data = self._read_json_body()
        fields = {}
        for field in ("ip", "netmask", "gateway"):
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, f"missing {field!r}")
            try:
                parsed = ipaddress.ip_address(value.strip())
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST,
                               f"{field} {value!r} is not an IP address") from exc
            if parsed.version != 4:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be IPv4")
            fields[field] = str(parsed)

        try:
            network = ipaddress.ip_network(
                f"{fields['ip']}/{fields['netmask']}", strict=False)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           f"{fields['ip']}/{fields['netmask']} is not a valid "
                           f"network: {exc}") from exc
        if ipaddress.ip_address(fields["gateway"]) not in network:
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                f"gateway {fields['gateway']} is outside {network}, so the "
                "device would have no usable gateway")

        ok, message = self.poller.change_address(
            receiver_id, fields["ip"], fields["netmask"], fields["gateway"])
        self._send_json({"ok": ok, "message": message},
                        status=HTTPStatus.OK if ok else HTTPStatus.BAD_GATEWAY)

    def _post_name(self, receiver_id: str) -> None:
        data = self._read_json_body()
        if "name" not in data:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'name'")
        name = self._clean_label(data["name"], "name")
        location = None
        if "location" in data:
            location = self._clean_label(data["location"], "location",
                                         allow_empty=True)
        note = None
        if "note" in data:
            note = self._clean_label(data["note"], "note", allow_empty=True)
        self.poller.set_labels(receiver_id, name, location, note)
        self._send_json({"ok": True, "receiver_id": receiver_id,
                         "name": name, "location": location, "note": note})

    def _send_result(self, result: veo.SwitchResult) -> None:
        self._send_json(result.as_dict(),
                        status=HTTPStatus.OK if result.ok
                        else HTTPStatus.BAD_GATEWAY)

    def _post_discover(self) -> None:
        """Scan for VEO devices that are not in the config."""
        data = self._read_json_body()
        ranges = data.get("ranges")
        if ranges is not None:
            if isinstance(ranges, str):
                ranges = [ranges]
            if not isinstance(ranges, list) or any(
                    not isinstance(item, str) for item in ranges):
                raise ApiError(HTTPStatus.BAD_REQUEST,
                               "'ranges' must be a list of strings")
        try:
            # Started in the background: a whole-VLAN sweep takes minutes, far
            # longer than an HTTP request should be held open.  The dashboard
            # follows progress through /api/state.
            addresses = self.poller.start_discovery(
                ranges, force=bool(data.get("force")))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        except RuntimeError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        self._send_json({"ok": True, "started": True, "addresses": addresses},
                        status=HTTPStatus.ACCEPTED)

    def _post_add_discovered(self) -> None:
        data = self._read_json_body()
        ip = data.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing 'ip'")
        try:
            receiver = self.poller.add_discovered(ip.strip())
        except KeyError as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, str(exc.args[0])) from exc
        except ValueError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        self._send_json({"ok": True, "receiver": receiver.as_dict()})

    def _post_config(self) -> None:
        """Replace the whole config from an uploaded export.

        Validated before anything is written, and the config it replaces is
        kept as a timestamped backup -- importing the wrong file should be
        recoverable, not final.
        """
        data = self._read_json_body()
        current = self.poller.config
        try:
            replacement = config_mod.parse(data, current.path)
        except config_mod.ConfigError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"rejected: {exc}") from exc

        backup = None
        if current.path.exists():
            backup = current.path.with_suffix(
                f".json.{time.strftime('%Y%m%d-%H%M%S')}.bak")
            try:
                backup.write_bytes(current.path.read_bytes())
            except OSError as exc:
                raise ApiError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"could not back up the current config, nothing changed: {exc}",
                ) from exc

        try:
            replacement.save()
        except OSError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR,
                           f"could not write the config: {exc}") from exc

        self.poller.reload(replacement)
        log.info("config imported from %s (backup: %s)", self._client(), backup)
        self._send_json({
            "ok": True,
            "receivers": len(replacement.receivers),
            "channels": len(replacement.channels),
            "backup": str(backup) if backup else None,
        })

    def _serve_login(self) -> None:
        if not self.auth.enabled:
            self._redirect("/")
            return
        if self._session_user():
            self._redirect("/")
            return
        self._serve_static("login.html")

    def _post_login(self) -> None:
        if not self.auth.enabled:
            self._redirect("/")
            return
        client = self._client()
        if self.throttle.blocked(client):
            log.warning("login throttled for %s", client)
            self._redirect("/login?error=throttled")
            return

        length_header = self.headers.get("Content-Length", "0")
        try:
            length = min(int(length_header), MAX_BODY_BYTES)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        fields = parse_qs(raw.decode("utf-8", "replace"))
        user = (fields.get("user") or [""])[0]
        password = (fields.get("password") or [""])[0]

        if self.auth.check_login(user, password):
            self.throttle.reset(client)
            log.info("login succeeded for %r from %s", user, client)
            token = self.auth.issue_session(user)
            self._redirect("/", cookie=self.auth.cookie_header(token))
            return

        self.throttle.record_failure(client)
        log.warning("login failed for %r from %s", user, client)
        self._redirect("/login?error=1")

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        # relative_to(), not is_relative_to(): the latter arrived in Python
        # 3.9, and this is the only thing in either product that needed it.
        # Same guarantee -- it raises ValueError when the resolved path has
        # escaped the static directory, which is the traversal case.
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        if not target.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(poller: Poller, host: str, port: int,
                auth: Auth | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "poller": poller,
        "auth": auth if auth is not None else Auth(),
        "throttle": LoginThrottle(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
