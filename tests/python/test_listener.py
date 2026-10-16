"""The TCP listener: the connection limit and the watchdog for connections
that send no request."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

from appliance import __main__ as daemon


async def app(scope, receive, send) -> None:
    size = 0
    while True:
        message = await receive()
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok"})


@contextlib.asynccontextmanager
async def listener(limit: int | None = None) -> AsyncIterator[int]:
    sock = socket.create_server(("127.0.0.1", 0))
    server = daemon._server(app, limit)
    tasks = [asyncio.create_task(server.serve(sockets=[sock])), asyncio.create_task(daemon._close_silent(server))]
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        tasks[1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def closed_within(reader: asyncio.StreamReader, seconds: float) -> bool:
    try:
        return await asyncio.wait_for(reader.read(100), seconds) == b""
    except TimeoutError:
        return False


async def test_silent_and_slow_connections_are_closed(monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT", 0.6)
    async with listener() as port:
        silent_reader, silent = await asyncio.open_connection("127.0.0.1", port)
        slow_reader, slow = await asyncio.open_connection("127.0.0.1", port)

        async def drip() -> None:
            # A byte every 0.1 s: never a complete request line.
            for byte in b"GET /x HTTP/1.1\r\nHost: a":
                slow.write(bytes([byte]))
                await slow.drain()
                await asyncio.sleep(0.1)

        dripping = asyncio.create_task(drip())
        assert await closed_within(silent_reader, 3)
        assert await closed_within(slow_reader, 3)
        dripping.cancel()
        # An ordinary request is answered.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n")
        assert b"200 OK" in await asyncio.wait_for(reader.read(), 3)
        for w in (silent, slow, writer):
            w.close()


async def test_a_slow_upload_is_not_cut_off(monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT", 0.6)
    async with listener() as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"POST /up HTTP/1.1\r\nHost: a\r\nContent-Length: 6\r\nConnection: close\r\n\r\n")
        for part in (b"ab", b"cd", b"ef"):
            await asyncio.sleep(0.5)
            writer.write(part)
            await writer.drain()
        assert b"200 OK" in await asyncio.wait_for(reader.read(), 3)
        writer.close()


async def test_connections_over_the_limit_get_503(monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT", 60)
    async with listener(limit=3) as port:
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: a\r\nConnection: close\r\n\r\n")
        assert b"503" in await asyncio.wait_for(reader.read(), 3)
        for _, w in [*idle, (reader, writer)]:
            w.close()

