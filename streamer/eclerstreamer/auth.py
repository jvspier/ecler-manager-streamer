"""Authentication for the streamer.

A copy of the manager's, deliberately: two small services with one
maintainer do not need a shared package, and the copy keeps the streamer
self-contained on a host where the manager is not installed. Its password
file is separate, which is the point -- separate credentials are the boundary
that stops a stray click in one tool reaching the other.

Original: Optional login for the dashboard.

Configured entirely through the environment, so no secret ever lands in
``config.json`` (which the dashboard rewrites) or in the project directory:

    ECLER_AUTH_USER            the service account name
    ECLER_AUTH_PASSWORD_HASH   scrypt hash, from tools/setpassword.py
    ECLER_AUTH_PASSWORD        plaintext alternative, for convenience
    ECLER_SESSION_SECRET       signs session cookies; generated if unset
    ECLER_SESSION_HOURS        how long a login lasts (default 12)

Values may come from the real environment or from an env file
(``/etc/eclerstreamer/eclerstreamer.env`` when deployed).  The environment wins.

Sessions are signed cookies rather than server-side state, so a restart does
not log everyone out -- provided ``ECLER_SESSION_SECRET`` is set.  If it is
generated at startup instead, sessions last only as long as the process.

Threat model: this keeps out someone who stumbles onto the port. It is a
password over plain HTTP on a trusted LAN, so it is not protection against
anyone able to watch the traffic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

SESSION_COOKIE = "eclerstreamer_session"
DEFAULT_SESSION_HOURS = 12.0

# scrypt cost: ~16 MB and well under a tenth of a second, which is plenty
# against offline guessing while staying imperceptible at login.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

#: Account names that survive an env file unchanged.  Dashes, dots, underscores,
#: "@" and "+" are all fine.  Excluded are the things this file format would
#: quietly mangle: surrounding whitespace (stripped), " #" (read as a comment),
#: matched quotes (unwrapped), and newlines (would split the line).
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")


def valid_user(name: str) -> bool:
    return bool(USER_RE.match(name))


# The streamer's own file, not the manager's. This was left pointing at
# /etc/eclermanager/eclermanager.env when the module was copied, which
# contradicted the docstring above and had two real consequences: running
# run.py by hand started with NO login even though one was configured (the
# unit works only because systemd injects the variables), and on a host
# carrying both products the streamer would have accepted the manager's
# credentials -- the exact boundary this separation exists to draw.
DEFAULT_ENV_PATHS = (
    Path("/etc/eclerstreamer/eclerstreamer.env"),
    Path(__file__).resolve().parent.parent / ".env",
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` lines.  Quotes optional, ``#`` starts a comment."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def hash_password(password: str) -> str:
    """Encode a password as ``scrypt$n$r$p$salt$key``."""
    if not password:
        raise ValueError("password may not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                         r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_BYTES)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash.  False on anything malformed."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(key_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass
class Auth:
    """Login configuration.  ``enabled`` is False when nothing is set up."""

    user: str = ""
    password_hash: str = ""
    session_secret: bytes = b""
    session_hours: float = DEFAULT_SESSION_HOURS
    secret_is_ephemeral: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.user and self.password_hash)

    # --- credentials -----------------------------------------------------
    def check_login(self, user: str, password: str) -> bool:
        """Verify a username and password in constant time where it matters."""
        if not self.enabled:
            return False
        # Always hash, so a wrong username is not faster than a wrong password.
        password_ok = verify_password(password, self.password_hash)
        user_ok = hmac.compare_digest(user.encode("utf-8"),
                                      self.user.encode("utf-8"))
        return user_ok and password_ok

    # --- sessions --------------------------------------------------------
    def issue_session(self, user: str, *, now: float | None = None) -> str:
        expires = (now if now is not None else time.time()) + self.session_hours * 3600
        payload = f"{user}|{int(expires)}".encode("utf-8")
        signature = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def read_session(self, token: str, *, now: float | None = None) -> str | None:
        """Return the username a valid token belongs to, else None."""
        if not token or not self.session_secret:
            return None
        payload_b64, _, signature_b64 = token.partition(".")
        if not signature_b64:
            return None
        try:
            payload = _unb64(payload_b64)
            signature = _unb64(signature_b64)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.session_secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            user, _, expires = payload.decode("utf-8").rpartition("|")
            if float(expires) < (now if now is not None else time.time()):
                return None
        except (ValueError, UnicodeDecodeError):
            return None
        return user or None

    def cookie_header(self, token: str) -> str:
        max_age = int(self.session_hours * 3600)
        # No Secure flag: this is served over plain HTTP on a LAN, and setting
        # it would stop the cookie being sent at all.
        return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={max_age}")

    @staticmethod
    def clear_cookie_header() -> str:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def load(env_file: str | os.PathLike | None = None) -> Auth:
    """Build an :class:`Auth` from the environment and an optional env file."""
    values: dict[str, str] = {}
    warnings: list[str] = []

    candidates = [Path(env_file)] if env_file else list(DEFAULT_ENV_PATHS)
    for path in candidates:
        try:
            if path.is_file():
                values.update(parse_env_file(path))
                break
        except OSError as exc:
            warnings.append(f"cannot read {path}: {exc}")
    if env_file and not Path(env_file).is_file():
        warnings.append(f"env file not found: {env_file}")

    # A real environment variable beats the file.
    for key in ("ECLER_AUTH_USER", "ECLER_AUTH_PASSWORD_HASH",
                "ECLER_AUTH_PASSWORD", "ECLER_SESSION_SECRET",
                "ECLER_SESSION_HOURS"):
        if os.environ.get(key):
            values[key] = os.environ[key]

    user = values.get("ECLER_AUTH_USER", "").strip()
    if user and not valid_user(user):
        warnings.append(
            f"ECLER_AUTH_USER {user!r} contains characters that an env file "
            "cannot carry reliably; login stays disabled"
        )
        user = ""
    password_hash = values.get("ECLER_AUTH_PASSWORD_HASH", "").strip()
    plaintext = values.get("ECLER_AUTH_PASSWORD", "")

    if not password_hash and plaintext:
        password_hash = hash_password(plaintext)
        warnings.append(
            "using ECLER_AUTH_PASSWORD (plaintext). Prefer "
            "ECLER_AUTH_PASSWORD_HASH from tools/setpassword.py"
        )
    if user and not password_hash:
        warnings.append(
            "ECLER_AUTH_USER is set but no password is; login stays disabled"
        )
    if password_hash and not user:
        warnings.append(
            "a password is set but ECLER_AUTH_USER is not; login stays disabled"
        )

    secret_text = values.get("ECLER_SESSION_SECRET", "").strip()
    ephemeral = False
    if secret_text:
        secret = secret_text.encode("utf-8")
    else:
        secret = secrets.token_bytes(32)
        ephemeral = True
        if user and password_hash:
            warnings.append(
                "ECLER_SESSION_SECRET is unset, so a generated one is used and "
                "everyone is logged out on restart"
            )

    try:
        session_hours = float(values.get("ECLER_SESSION_HOURS",
                                         DEFAULT_SESSION_HOURS))
    except ValueError:
        session_hours = DEFAULT_SESSION_HOURS
        warnings.append("ECLER_SESSION_HOURS is not a number; using the default")
    session_hours = max(0.25, min(24 * 30, session_hours))

    return Auth(
        user=user,
        password_hash=password_hash,
        session_secret=secret,
        session_hours=session_hours,
        secret_is_ephemeral=ephemeral,
        warnings=tuple(warnings),
    )
