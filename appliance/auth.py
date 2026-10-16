"""API token, dashboard sessions and one-time login links.

Requests over the unix socket are trusted (the socket directory is mode
0700). TCP requests need the bearer token or a session cookie.
"""

import collections
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from .config import Config

SESSION_COOKIE = "appliance_session"
SESSION_TTL = 7 * 24 * 3600
LINK_TTL = 120
# Failed logins (and wrong tokens) allowed per client and window.
MAX_FAILURES = 5
FAILURE_WINDOW = 60.0


def _write_secret(path: Path, value: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.replace(tmp, path)


def secret(directory: Path, name: str) -> str:
    """Return the secret `name`, created on first use with mode 0600."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / name
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = ""
    if not value:
        value = secrets.token_urlsafe(32)
        _write_secret(path, value)
    return value


class Auth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.token = secret(cfg.secrets_dir, "api-token")
        self._session_key = secret(cfg.secrets_dir, "session-key").encode()
        self._links: dict[str, float] = {}
        self._failures: dict[str, collections.deque[float]] = {}

    def check_token(self, value: str) -> bool:
        return hmac.compare_digest(value.encode(), self.token.encode())

    def rotate(self) -> str:
        """Replace the token. This also ends every dashboard session."""
        self.token = secrets.token_urlsafe(32)
        _write_secret(self.cfg.secrets_dir / "api-token", self.token)
        key = secrets.token_urlsafe(32)
        _write_secret(self.cfg.secrets_dir / "session-key", key)
        self._session_key = key.encode()
        # A link made before the rotation must not log in afterwards.
        self._links.clear()
        return self.token

    def _mac(self, payload: str) -> str:
        return hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        payload = f"{int(time.time()) + SESSION_TTL}.{secrets.token_urlsafe(12)}"
        return f"{payload}.{self._mac(payload)}"

    def check_session(self, value: str) -> bool:
        expiry, _, rest = value.partition(".")
        nonce, _, mac = rest.partition(".")
        # The cookie comes from the client: isdigit() accepts digits such as
        # "²" that int() refuses, and compare_digest() takes ASCII text only.
        if not (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12) or not nonce or int(expiry) < time.time():
            return False
        return hmac.compare_digest(mac.encode(), self._mac(f"{expiry}.{nonce}").encode())

    def login_link(self) -> str:
        now = time.monotonic()
        self._links = {code: end for code, end in self._links.items() if end > now}
        code = secrets.token_urlsafe(24)
        self._links[code] = now + LINK_TTL
        host = self.cfg.listen_address
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        elif ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.cfg.port}/login?code={code}"

    def use_login_code(self, code: str) -> bool:
        """A login link works once, within LINK_TTL seconds."""
        end = self._links.pop(code, None)
        return end is not None and end > time.monotonic()

    def blocked(self, client: str) -> bool:
        failures = self._failures.get(client)
        if not failures:
            return False
        while failures and failures[0] < time.monotonic() - FAILURE_WINDOW:
            failures.popleft()
        return len(failures) >= MAX_FAILURES

    def failed(self, client: str) -> None:
        if len(self._failures) > 10000:
            self._failures.clear()
        self._failures.setdefault(client, collections.deque(maxlen=MAX_FAILURES)).append(time.monotonic())
