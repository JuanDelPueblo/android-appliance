"""coturn: the relay for the browser's video, and the TURN credentials.

The browser connects only to the TURN port and relays to the emulator,
so media uses one known port whatever the network. The relay may reach
the container's own addresses and nothing else.
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import time

from .auth import _write_secret, secret
from .config import TURN_PORT, Config

log = logging.getLogger(__name__)

CREDENTIAL_TTL = 24 * 3600
RELAY_PORTS = (49152, 49251)
REALM = "android-appliance"
# A browser that is killed never frees its relays, and a relay stays until its
# lifetime is over (10 minutes by default, while the range has 100 ports and
# the quota is 80): cap the lifetime. A live browser refreshes at half of it.
ALLOCATION_LIFETIME = 60
# One stream allocates a relay for each of its two TURN URLs.
USER_QUOTA = 4
TOTAL_QUOTA = 80
# A turnserver that dies is started again after a growing delay, which
# starts over once one has run for STABLE seconds.
FIRST_DELAY = 1.0
MAX_DELAY = 30.0
STABLE = 30.0
STOP_GRACE = 5.0
# The port can be exposed to a network: its log must not flood ours.
LOG_LINES = 100
LOG_WINDOW = 60.0


# A session keeps one user name, so that user-quota is a quota per session:
# fresh names on every stream start would let one session hold every relay.
# The name is replaced when its credential has less than this left.
RENEW_BEFORE = 3600


def new_user(ttl: int = CREDENTIAL_TTL, now: float | None = None) -> str:
    """A TURN REST API user name: it carries its expiry."""
    expiry = int((time.time() if now is None else now) + ttl)
    return f"{expiry}:{secrets.token_hex(8)}"


def renew_user(user: str | None, now: float | None = None) -> str:
    """`user`, unless it is missing or expires soon."""
    now = time.time() if now is None else now
    if user and (expiry := user.partition(":")[0]).isdigit() and int(expiry) - now > RENEW_BEFORE:
        return user
    return new_user(now=now)


def credentials(key: str, user: str | None = None, ttl: int = CREDENTIAL_TTL, now: float | None = None) -> tuple[str, str]:
    """A TURN REST API credential: the user name carries its expiry, and
    the password is an HMAC of it that coturn can check with the secret."""
    username = user or new_user(ttl, now)
    digest = hmac.new(key.encode(), username.encode(), hashlib.sha1).digest()
    return username, base64.b64encode(digest).decode()


def parse_addresses(output: str) -> list[str]:
    """The addresses in the output of 'ip -o addr show scope global'."""
    return [m.group(1) for m in re.finditer(r"\binet6? ([0-9A-Fa-f:.]+)/\d+", output)]


async def container_addresses() -> list[str]:
    """The global addresses of the container. The emulator's WebRTC host
    candidates are exactly these (measured under pasta)."""
    proc = await asyncio.create_subprocess_exec(
        "ip", "-o", "addr", "show", "scope", "global",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    out, _ = await proc.communicate()
    return parse_addresses(out.decode(errors="replace"))


def config_text(key: str, addresses: list[str], userdb: str, pidfile: str) -> str:
    """The turnserver.conf. Peers are denied unless they are one of the
    container's own addresses (an address that is both denied and allowed
    is allowed)."""
    # The relay address: the first IPv4 address, else the first IPv6 one.
    relay = next((a for a in addresses if ":" not in a), addresses[0] if addresses else None)
    lines = [
        f"listening-port={TURN_PORT}",
        f"min-port={RELAY_PORTS[0]}",
        f"max-port={RELAY_PORTS[1]}",
        *([f"relay-ip={relay}"] if relay else []),
        "use-auth-secret",
        f"static-auth-secret={key}",
        f"realm={REALM}",
        "fingerprint",
        "no-tls",
        "no-tcp-relay",
        "no-multicast-peers",
        "no-software-attribute",
        "stale-nonce=600",
        f"max-allocate-lifetime={ALLOCATION_LIFETIME}",
        f"user-quota={USER_QUOTA}",
        f"total-quota={TOTAL_QUOTA}",
        "denied-peer-ip=0.0.0.0-255.255.255.255",
        "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        *[f"allowed-peer-ip={a}" for a in addresses],
        # coturn writes a user database and a PID file; the root file system is read-only.
        f"userdb={userdb}",
        f"pidfile={pidfile}",
        "log-file=stdout",
        "simple-log",
    ]
    return "\n".join(lines) + "\n"


def urls(host: str, port: int) -> list[str]:
    """The TURN URLs of a host: UDP first, TCP for networks that block UDP."""
    host = f"[{host}]" if ":" in host else host
    return [f"turn:{host}:{port}?transport={transport}" for transport in ("udp", "tcp")]


class Turn:
    """The turnserver process, kept alive for the life of the daemon."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._secret = secret(cfg.secrets_dir, "turn-secret")
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._lines: list[float] = []
        self._suppressed = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def ice_servers(self, host: str, user: str | None = None) -> list[dict[str, object]]:
        """The ICE servers for a browser, with the credential of `user` (a
        name from `new_user`; a fresh one without). `host` is the name of
        the page, unless the configuration names one."""
        if not (name := self.cfg.turn_host or host):
            raise ValueError("the Host header of the request is not a host name; set APPLIANCE_TURN_HOST")
        username, credential = credentials(self._secret, user)
        return [{"urls": urls(name, self.cfg.turn_port), "username": username, "credential": credential}]

    async def rotate(self) -> None:
        """Replace the secret and restart turnserver: every credential that
        was issued stops working, and every relay allocation is dropped."""
        self._secret = secrets.token_urlsafe(32)
        _write_secret(self.cfg.secrets_dir / "turn-secret", self._secret)
        if self._task is not None:
            await self.stop()
            await self.start()

    async def start(self) -> None:
        """Start turnserver; a failure is logged and tried again."""
        self._task = asyncio.create_task(self._supervise(await self._spawn()))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._terminate()

    async def _spawn(self) -> asyncio.subprocess.Process | None:
        cfg = self.cfg
        conf = cfg.tmp_dir / "appliance" / "turnserver.conf"
        try:
            addresses = await container_addresses()
        except OSError as e:
            log.warning("cannot list the container's addresses (%s); WebRTC cannot connect without them", e)
            addresses = []
        if not addresses:
            log.warning("the container has no global address: WebRTC cannot connect")
        text = config_text(self._secret, addresses, str(conf.with_name("turndb")), str(conf.with_name("turnserver.pid")))
        try:
            conf.parent.mkdir(parents=True, exist_ok=True)
            # The file holds the secret.
            fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            proc = await asyncio.create_subprocess_exec(
                cfg.turnserver_bin, "-c", str(conf),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )  # fmt: skip
        except OSError as e:
            log.error("cannot run turnserver: %s", e)
            return None
        self._proc = proc
        self._reader = asyncio.create_task(self._log_output(proc))
        log.info("turnserver started (pid %d, relay ports %d-%d)", proc.pid, *RELAY_PORTS)
        return proc

    async def _supervise(self, proc: asyncio.subprocess.Process | None) -> None:
        loop = asyncio.get_running_loop()
        delay = FIRST_DELAY / 2
        while True:
            started = loop.time()
            if proc is not None:
                code = await proc.wait()
                if self._reader is not None:
                    await self._reader
                log.warning("turnserver exited with status %d", code)
            delay = FIRST_DELAY if loop.time() - started > STABLE else min(delay * 2, MAX_DELAY)
            await asyncio.sleep(delay)
            proc = await self._spawn()

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def _log_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            if line := raw.decode(errors="replace").strip():
                self._log_line(line)

    def _log_line(self, line: str) -> None:
        now = time.monotonic()
        self._lines = [t for t in self._lines if now - t < LOG_WINDOW]
        if len(self._lines) < LOG_LINES:
            self._lines.append(now)
            if self._suppressed:
                log.info("turnserver: %d more lines suppressed", self._suppressed)
                self._suppressed = 0
            log.info("turnserver: %s", line)
        else:
            self._suppressed += 1
