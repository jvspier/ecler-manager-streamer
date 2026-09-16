"""HTTP server: a small JSON API plus the streamer's page.

Deliberately thin. systemd owns the streams, so everything here is either
reading state or asking systemd to do something. If this process dies, every
stream keeps running.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, config as config_mod, control
from .auth import SESSION_COOKIE, Auth

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
PUBLIC_PATHS = {"/login", "/healthz"}
MAX_BODY_BYTES = 64 * 1024

_DASHBOARD_ROUTE = re.compile(r"^/api/dashboards/(\d{1,2})(?:/([a-z-]{1,16}))?$")
_PREVIEW_ROUTE = re.compile(r"^/api/preview/(\d{1,2})\.png$")

# A screenshot costs a second of ffmpeg, so serve a recent one rather than
# grabbing again for every card on every poll.
PREVIEW_MAX_AGE = 20.0


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Previews:
    """Most recent screenshot per channel, grabbed on demand."""

    def __init__(self, tools_dir: Path) -> None:
        self.tools_dir = tools_dir
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, bytes]] = {}

    def get(self, channel: int, display: str) -> bytes | None:
        now = time.time()
        with self._lock:
            cached = self._cache.get(channel)
            if cached and now - cached[0] < PREVIEW_MAX_AGE:
                return cached[1]

        script = self.tools_dir / "pagesource.sh"
        if not script.exists():
            return None
        with tempfile.NamedTemporaryFile(suffix=".png") as handle:
            done = subprocess.run(
                ["bash", str(script), "--display", display,
                 "--screenshot", handle.name],
                capture_output=True, text=True, timeout=30, check=False)
            data = Path(handle.name).read_bytes()
        # A blank display still produces a valid PNG, just a tiny one; treat
        # anything that small as "nothing rendered" rather than showing black.
        if done.returncode != 0 or len(data) < 5000:
            return None
        with self._lock:
            self._cache[channel] = (time.time(), data)
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = f"eclerstreamer/{__version__}"
    protocol_version = "HTTP/1.1"
    config_path: Path
    auth: Auth
    previews: Previews
    lock: threading.Lock
    _body: bytes = b""

    # --- plumbing --------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _take_body(self) -> bytes:
        """Read the whole request body exactly once, always.

        This has to happen for every POST, not only the ones that want a
        body. On a keep-alive connection an unread body stays in the socket
        and the next request is parsed starting from it -- so a POST whose
        handler ignored its "{}" made the following GET arrive as a method
        called "{}GET", answered with 501 Not Implemented. The symptom lands
        on an unrelated request, which makes it very hard to place.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(min(length, MAX_BODY_BYTES))

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from exc
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self._body
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return data

    # --- auth ------------------------------------------------------------
    def _session_user(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            try:
                jar = SimpleCookie()
                jar.load(raw_cookie)
            except CookieError:
                jar = None
            if jar is not None:
                morsel = jar.get(SESSION_COOKIE)
                if morsel is not None:
                    user = self.auth.read_session(morsel.value)
                    if user:
                        return user
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
        if not self.auth.enabled or path in PUBLIC_PATHS:
            return True
        if self._session_user():
            return True
        if path.startswith("/api/"):
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "login required")
        else:
            self._redirect("/login")
        return False

    # --- config ----------------------------------------------------------
    def _load(self) -> config_mod.Config:
        try:
            return config_mod.load(self.config_path)
        except (OSError, ValueError) as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from None

    def _state(self) -> dict:
        cfg = self._load()
        return {
            "version": __version__,
            "systemd": control.available(),
            "config": {
                "local_addr": cfg.local_addr, "interface": cfg.interface,
                "manager_url": cfg.manager_url, "qmin": cfg.qmin,
                "no_bframes": cfg.no_bframes, "notes": cfg.notes,
            },
            "dashboards": [
                dict(d.to_dict(),
                     display_name=d.display_name,
                     multicast=config_mod.multicast_for(d.channel),
                     status=control.status(d.channel),
                     progress=control.progress(d.channel))
                for d in cfg.dashboards
            ],
            "server_time": time.time(),
        }

    # --- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if not self._require_auth(path):
                return
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/login":
                return self._serve_static("login.html")
            if path == "/healthz":
                return self._send_json({"ok": True})
            if path == "/api/state":
                return self._send_json(self._state())
            preview = _PREVIEW_ROUTE.match(path)
            if preview:
                return self._serve_preview(int(preview.group(1)))
            self._send_error_json(HTTPStatus.NOT_FOUND, "no such path")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception:                        # pragma: no cover
            log.exception("GET %s failed", path)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "server error")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Before any dispatch, so no handler can leave one behind.
        self._body = self._take_body()
        try:
            if path == "/login":
                return self._do_login()
            if not self._require_auth(path):
                return
            if path == "/logout":
                return self._redirect("/login", cookie=self.auth.clear_cookie_header())
            if path == "/api/config":
                return self._update_config()
            if path == "/api/dashboards":
                return self._add_dashboard()
            route = _DASHBOARD_ROUTE.match(path)
            if route:
                channel, action = int(route.group(1)), route.group(2)
                if action is None:
                    return self._update_dashboard(channel)
                return self._dashboard_action(channel, action)
            self._send_error_json(HTTPStatus.NOT_FOUND, "no such path")
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except Exception:                        # pragma: no cover
            log.exception("POST %s failed", path)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "server error")

    # --- handlers --------------------------------------------------------
    def _update_config(self) -> None:
        body = self._read_json_body()
        with self.lock:
            cfg = self._load()
            for key in ("local_addr", "interface", "manager_url", "notes"):
                if key in body:
                    setattr(cfg, key, str(body[key] or "").strip())
            if "qmin" in body and body["qmin"] is not None:
                cfg.qmin = max(0, int(body["qmin"]))
            if "no_bframes" in body:
                cfg.no_bframes = bool(body["no_bframes"])
            cfg.save()
        self._send_json({"ok": True})

    def _add_dashboard(self) -> None:
        body = self._read_json_body()
        try:
            channel = int(body.get("channel"))
        except (TypeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "channel must be a number") from None
        with self.lock:
            cfg = self._load()
            if cfg.dashboard(channel) is not None:
                raise ApiError(HTTPStatus.CONFLICT,
                               f"channel {channel} already has a dashboard")
            if not 0 <= channel <= 63:
                raise ApiError(HTTPStatus.BAD_REQUEST, "channel must be 0-63")
            cfg.dashboards.append(config_mod.Dashboard(
                channel=channel,
                name=str(body.get("name", "") or ""),
                url=str(body.get("url", "") or ""),
                enabled=False,          # never start something unreviewed
            ))
            cfg.dashboards.sort(key=lambda d: d.channel)
            cfg.save()
        self._send_json({"ok": True, "channel": channel})

    def _update_dashboard(self, channel: int) -> None:
        body = self._read_json_body()
        with self.lock:
            cfg = self._load()
            dash = cfg.dashboard(channel)
            if dash is None:
                raise ApiError(HTTPStatus.NOT_FOUND, f"no dashboard on channel {channel}")
            for key in ("name", "url", "note", "bitrate", "size"):
                if key in body:
                    setattr(dash, key, str(body[key] or ""))
            if "enabled" in body:
                dash.enabled = bool(body["enabled"])
            for key in ("fps", "display"):
                if key in body and body[key] is not None:
                    setattr(dash, key, int(body[key]))
            if "capture_fps" in body and body["capture_fps"] is not None:
                dash.capture_fps = float(body["capture_fps"])
            cfg.save()
        self._send_json({"ok": True, "dashboard": dash.to_dict()})

    def _dashboard_action(self, channel: int, action: str) -> None:
        if action == "delete":
            with self.lock:
                cfg = self._load()
                if cfg.dashboard(channel) is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "no such dashboard")
                # Stop it first: removing the config under a running unit
                # leaves a stream nothing can describe.
                control.act(channel, "stop")
                cfg.dashboards = [d for d in cfg.dashboards if d.channel != channel]
                cfg.save()
            return self._send_json({"ok": True})

        try:
            ok, message = control.act(channel, action)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        if not ok:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, message)
        self._send_json({"ok": True, "message": message})

    def _serve_preview(self, channel: int) -> None:
        cfg = self._load()
        dash = cfg.dashboard(channel)
        if dash is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no such dashboard")
        try:
            data = self.previews.get(channel, dash.display_name)
        except subprocess.SubprocessError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from None
        if data is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "nothing rendered on that display")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _do_login(self) -> None:
        raw = self._body.decode("utf-8", "replace")
        fields = dict(
            (part.split("=", 1) + [""])[:2] for part in raw.split("&") if part)
        from urllib.parse import unquote_plus
        user = unquote_plus(fields.get("user", ""))
        password = unquote_plus(fields.get("password", ""))
        if self.auth.check_login(user, password):
            token = self.auth.issue_session(user)
            log.info("login ok for %r", user)
            return self._redirect("/", cookie=self.auth.cookie_header(token))
        log.warning("login failed for %r from %s", user,
                    self.client_address[0] if self.client_address else "?")
        time.sleep(1.0)
        self._redirect("/login?failed=1")

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            return self._send_error_json(HTTPStatus.NOT_FOUND, "no such file")
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(config_path: Path, host: str, port: int,
                auth: Auth | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "config_path": Path(config_path),
        "auth": auth if auth is not None else Auth(),
        "previews": Previews(Path(__file__).resolve().parent.parent / "tools"),
        # One writer at a time: two browser tabs saving at once would
        # otherwise read, edit and write the same file over each other.
        "lock": threading.Lock(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
