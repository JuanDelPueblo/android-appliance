"""python3 -m appliance serve | healthcheck | check-config"""

import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
import shutil
import signal
import socket
import sys
import time
from collections.abc import Generator
from pathlib import Path

import uvicorn

from . import __version__, api, config
from .auth import Auth
from .backend import Backend, LogBuffer
from .client import Client
from .supervisor import Supervisor

log = logging.getLogger("appliance")

# Surplus connections of the TCP listener get 503; the unix socket is
# trusted and has no limit.
MAX_CONNECTIONS = 256
# A connection that sends no complete request in this time is closed.
REQUEST_TIMEOUT = 15.0


class _Server(uvicorn.Server):
    # The daemon handles SIGTERM itself: Android stops before the API.
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def _unix_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Create the socket with mode 0600: no window with wider permissions.
    umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(umask)
    return sock


def _server(app: object, limit: int | None = None) -> _Server:
    return _Server(
        uvicorn.Config(
            app,  # type: ignore[arg-type]
            lifespan="off",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=5,
            limit_concurrency=limit,
        )
    )


async def _close_silent(server: uvicorn.Server) -> None:
    """Close connections that wait too long for a request.

    uvicorn times out a connection only after a response, so a client that
    connects and sends nothing (or a byte a minute) would hold it for ever.
    A connection is waiting when no request is in progress and the
    keep-alive timer is not armed. The attributes are those of both uvicorn
    HTTP protocols; a WebSocket connection has none of them.
    """
    waiting_since: dict[object, float] = {}
    while True:
        await asyncio.sleep(REQUEST_TIMEOUT / 3)
        now = time.monotonic()
        waiting = [
            p
            for p in list(server.server_state.connections)
            if hasattr(p, "cycle") and (p.cycle is None or p.cycle.response_complete) and p.timeout_keep_alive_task is None
        ]
        waiting_since = {p: waiting_since.get(p, now) for p in waiting}
        for p, since in waiting_since.items():
            if now - since >= REQUEST_TIMEOUT:
                p.transport.close()


async def serve(cfg: config.Config) -> int:
    buffer = LogBuffer()
    buffer.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(buffer)

    cfg.appliance_dir.mkdir(parents=True, exist_ok=True)
    # The AVD holds the accounts of the device: other users of the host get no access.
    with contextlib.suppress(OSError):
        os.chmod(cfg.data_dir, 0o700)
    lock = os.open(cfg.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Held for the whole life of the daemon: no second daemon (in
        # another container or on the host) may use this state.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("another appliance daemon uses %s", cfg.data_dir)
        return 1
    shutil.rmtree(cfg.upload_dir, ignore_errors=True)

    log.info("android appliance %s starting", __version__)
    sup = Supervisor(cfg)
    app = api.create_app(Backend(sup, buffer), Auth(cfg))
    tcp = socket.create_server(("0.0.0.0", cfg.http_port))
    unix = _unix_socket(cfg.socket_path)
    servers = [(_server(app, MAX_CONNECTIONS), tcp), (_server(api.local(app)), unix)]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(server.serve(sockets=[sock])) for server, sock in servers]
    watchdog = asyncio.create_task(_close_silent(servers[0][0]))
    stopped = asyncio.create_task(stop.wait())
    log.info("listening on 0.0.0.0:%d and %s", tcp.getsockname()[1], cfg.socket_path)
    done, _ = await asyncio.wait([stopped, *tasks], return_when=asyncio.FIRST_COMPLETED)
    failed = stopped not in done
    if failed:
        log.error("an API listener stopped unexpectedly")
    else:
        log.info("signal received: stopping android, then the daemon")

    await sup.shutdown()
    for server, _ in servers:
        server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)
    stopped.cancel()
    watchdog.cancel()
    cfg.socket_path.unlink(missing_ok=True)
    log.info("daemon stopped")
    return 1 if failed else 0


async def _health(cfg: config.Config) -> bool:
    async with Client(socket_path=str(cfg.socket_path)) as client:
        return bool((await asyncio.wait_for(client.health(), 10)).get("ok"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m appliance", description="Android appliance control daemon")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the daemon (the container's main process)")
    commands.add_parser("healthcheck", help="exit 0 when the daemon answers on its unix socket")
    commands.add_parser("check-config", help="exit 0 when the configuration is valid (install.sh runs it)")
    args = parser.parse_args(argv)
    # The healthcheck runs often; it prints only problems.
    level = logging.INFO if args.command == "serve" else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        cfg = config.load()
    except (config.ConfigError, OSError) as e:
        print(f"appliance: {e}", file=sys.stderr)
        return 1
    if args.command == "check-config":
        return 0
    if args.command == "serve":
        return asyncio.run(serve(cfg))
    try:
        return 0 if asyncio.run(_health(cfg)) else 1
    except Exception as e:
        print(f"appliance: unhealthy: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
