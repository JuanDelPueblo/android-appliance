"""A WebSocket client for an ASGI app, in the test's own event loop.

Starlette's TestClient runs the app in another thread and loop, where the
supervisor and its gRPC channels of a test cannot follow. This client
speaks ASGI to the app directly.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp


class WebSocketClient:
    def __init__(self, app: ASGIApp, path: str = "/api/v1/session", headers: dict[str, str] | None = None):
        self.close_code: int | None = None
        self.accepted = False
        self.messages: list[dict[str, Any]] = []
        self._app = app
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        all_headers = {"host": "127.0.0.1:6090", **{k.lower(): v for k, v in (headers or {}).items()}}
        self._scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("127.0.0.1", 6090),
            "client": ("127.0.0.1", 50000),
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "subprotocols": [],
            "headers": [(k.encode(), v.encode("latin-1")) for k, v in all_headers.items()],
        }
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "WebSocketClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> "WebSocketClient":
        """Open the connection; accepted is False when the app refuses it."""
        self._task = asyncio.create_task(self._app(self._scope, self._inbox.get, self._outbox.put))
        await self._inbox.put({"type": "websocket.connect"})
        first = await asyncio.wait_for(self._outbox.get(), 10)
        if first["type"] == "websocket.accept":
            self.accepted = True
        else:
            self.close_code = first.get("code", 1000)
        return self

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    async def send(self, message: dict[str, Any] | str | bytes) -> None:
        if isinstance(message, bytes):
            await self._inbox.put({"type": "websocket.receive", "bytes": message})
        else:
            text = message if isinstance(message, str) else json.dumps(message)
            await self._inbox.put({"type": "websocket.receive", "text": text})

    async def receive(self, timeout: float = 10) -> dict[str, Any]:
        """The next message of the server (a close raises ConnectionError)."""
        message = await asyncio.wait_for(self._outbox.get(), timeout)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)
            raise ConnectionError(f"closed with {self.close_code}")
        data = json.loads(message["text"])
        self.messages.append(data)
        return data

    async def next(self, timeout: float = 10) -> dict[str, Any]:
        """The next message that is not a hello, a status or a display hash."""
        return await self.until(lambda m: m["type"] not in ("hello", "status", "display"), timeout)

    async def until(self, match: Callable[[dict[str, Any]], bool], timeout: float = 10) -> dict[str, Any]:
        """Read messages until one matches; the messages before it stay in `messages`."""
        async with asyncio.timeout(timeout):
            while True:
                message = await self.receive(timeout)
                if match(message):
                    return message

    async def of_type(self, type_: str, timeout: float = 10, **fields: Any) -> dict[str, Any]:
        return await self.until(lambda m: m["type"] == type_ and all(m.get(k) == v for k, v in fields.items()), timeout)

    async def quiet(self, seconds: float = 0.3) -> list[dict[str, Any]]:
        """The messages, other than a hello, a status or a display hash, that arrive in the next `seconds`."""
        found = []
        end = asyncio.get_running_loop().time() + seconds
        while (left := end - asyncio.get_running_loop().time()) > 0:
            try:
                message = await self.receive(left)
            except (TimeoutError, ConnectionError):
                break
            if message["type"] not in ("hello", "status", "display"):
                found.append(message)
        return found

    async def closed(self, timeout: float = 10) -> int:
        """Wait for the server to close the connection; return the code."""
        async with asyncio.timeout(timeout):
            while True:
                try:
                    await self.receive(timeout)
                except ConnectionError:
                    assert self.close_code is not None
                    return self.close_code

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._inbox.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self._task, 10)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
