"""The backend operations over the HTTP API (unix socket, or TCP and token).

The methods mirror Backend, so a front end (the MCP stdio server) can
use either.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2

from .backend import Screenshot, ShellResult

API = "/api/v1"


class ClientError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _raise_for(response: httpx2.Response) -> None:
    if response.status_code < 400:
        return
    try:
        err = response.json()["error"]
        code, message = err["code"], err["message"]
    except (ValueError, KeyError, TypeError):
        code, message = "internal", response.text.strip() or f"HTTP {response.status_code}"
    raise ClientError(response.status_code, code, message)


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            yield chunk


class Client:
    def __init__(self, socket_path: str | None = None, url: str | None = None, token: str | None = None):
        if (socket_path is None) == (url is None):
            raise ValueError("give either socket_path or url")
        transport = httpx2.AsyncHTTPTransport(uds=socket_path) if socket_path else None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # No client timeout: the daemon bounds every operation itself.
        self._http = httpx2.AsyncClient(
            base_url=(url or "http://appliance").rstrip("/") + API, transport=transport, headers=headers, timeout=None
        )

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        response = await self._http.request(method, path, **kwargs)
        _raise_for(response)
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return (await self._request(method, path, **kwargs)).json()

    # Lifecycle

    async def health(self) -> dict[str, Any]:
        return await self._json("GET", "/health")

    async def status(self) -> dict[str, Any]:
        return await self._json("GET", "/status")

    async def start(self, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return await self._json("POST", "/start", json={"wait": wait, "timeout": timeout})

    async def stop(self, wait: bool = True) -> dict[str, Any]:
        return await self._json("POST", "/stop", json={"wait": wait})

    async def restart(self, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        return await self._json("POST", "/restart", json={"wait": wait, "timeout": timeout})

    async def wait(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._json("POST", "/wait", json={"timeout": timeout})

    # Screen and input

    async def screenshot(self, fmt: str = "png", quality: int = 85, max_size: int | None = None) -> Screenshot:
        params: dict[str, Any] = {"format": fmt, "quality": quality}
        if max_size:
            params["max_size"] = max_size
        r = await self._request("GET", "/screenshot", params=params)
        return Screenshot(
            r.content,
            r.headers["content-type"],
            int(r.headers["x-device-width"]),
            int(r.headers["x-device-height"]),
            float(r.headers["x-image-scale"]),
        )

    async def ui(self) -> str:
        return (await self._request("GET", "/ui")).text

    async def ui_nodes(self) -> list[dict[str, Any]]:
        return (await self._json("GET", "/ui", params={"format": "compact"}))["nodes"]

    async def tap(self, x: float, y: float) -> None:
        await self._request("POST", "/input/tap", json={"x": x, "y": y})

    async def long_press(self, x: float, y: float, duration_ms: int = 800) -> None:
        await self._request("POST", "/input/long-press", json={"x": x, "y": y, "duration_ms": duration_ms})

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> None:
        body = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": duration_ms}
        await self._request("POST", "/input/swipe", json=body)

    async def text(self, text: str, method: str = "paste") -> None:
        await self._request("POST", "/input/text", json={"text": text, "method": method})

    async def key(self, name: str) -> None:
        await self._request("POST", "/input/key", json={"key": name})

    # Shell and apps

    async def shell(self, command: str, timeout: float = 300) -> ShellResult:
        data = await self._json("POST", "/shell", json={"command": command, "timeout": timeout})
        return ShellResult(data["exit_code"], data["stdout"].encode(), data["stderr"].encode())

    async def apps(self, all: bool = False) -> list[str]:
        return (await self._json("GET", "/apps", params={"all": int(all)}))["packages"]

    async def install(self, apk: Path, replace: bool = False, grant: bool = False, downgrade: bool = False, test: bool = False) -> str:
        params = {"replace": int(replace), "grant": int(grant), "downgrade": int(downgrade), "test": int(test)}
        headers = {"Content-Type": "application/vnd.android.package-archive"}
        return (await self._json("POST", "/apps/install", params=params, content=_chunks(apk), headers=headers))["output"]

    async def launch(self, package: str) -> None:
        await self._request("POST", f"/apps/{package}/launch")

    async def force_stop(self, package: str) -> None:
        await self._request("POST", f"/apps/{package}/stop")

    async def uninstall(self, package: str, keep_data: bool = False) -> str:
        return (await self._json("DELETE", f"/apps/{package}", params={"keep_data": int(keep_data)}))["output"]

    # Files and logs

    async def pull(self, path: str, local: Path) -> int:
        """Copy a device file to `local` and return its size."""
        size = 0
        async with self._http.stream("GET", "/files", params={"path": path}) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for(r)
            with open(local, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
                    size += len(chunk)
        return size

    async def push(self, local: Path, path: str) -> int:
        return (await self._json("PUT", "/files", params={"path": path}, content=_chunks(local)))["bytes"]

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        return (await self._json("GET", "/files/list", params={"path": path}))["entries"]

    async def logs(self, source: str = "emulator", lines: int = 200) -> str:
        return (await self._request("GET", "/logs", params={"source": source, "lines": lines})).text

    # Authentication (local socket only)

    async def login_link(self) -> str:
        return (await self._json("POST", "/auth/login-link"))["url"]

    async def token(self) -> str:
        return (await self._json("GET", "/auth/token"))["token"]

    async def rotate_token(self) -> str:
        return (await self._json("POST", "/auth/token/rotate"))["token"]
