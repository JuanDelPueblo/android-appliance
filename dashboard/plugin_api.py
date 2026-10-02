"""Android controls and a same-origin noVNC bridge for the Hermes dashboard.

HTTP routes inherit Hermes authentication. WebSocket upgrades do not run its
HTTP middleware, so they require a short-lived, single-use display ticket
minted by an authenticated POST. Only the appliance's loopback URL is proxied.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

router = APIRouter()

PLUGIN_ID = "android-appliance"
API = f"/api/plugins/{PLUGIN_ID}"
FALLBACK_PATH = "/usr/local/bin/androidctl"
TICKET_TTL = 60
_tickets: Dict[str, tuple[float, str]] = {}

ACTIONS: Dict[str, List[str]] = {
    "start": ["start", "--no-wait"],
    "stop": ["stop"],
    "restart": ["restart", "--no-wait"],
}


def _settings() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        entry = ((load_config() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
        return entry.get("settings") or {}
    except Exception:
        return {}


def _plugin_enabled() -> bool:
    # HTTP has Hermes' runtime gate; repeat it for WebSocket upgrades.
    try:
        from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set

        return PLUGIN_ID in _get_enabled_set() and PLUGIN_ID not in _get_disabled_set()
    except Exception:
        return False


def _androidctl() -> Optional[str]:
    for candidate in (
        _settings().get("androidctl"),
        os.environ.get("ANDROIDCTL"),
        shutil.which("androidctl"),
        FALLBACK_PATH,
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run(args: List[str], timeout: int) -> Dict[str, Any]:
    exe = _androidctl()
    if exe is None:
        return {"ok": False, "error": "androidctl is not installed on this host"}
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "error": f"cannot run androidctl: {exc}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"androidctl {args[0]} timed out"}
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or f"androidctl exited with {proc.returncode}"}
    return {"ok": True, "output": proc.stdout.strip()}


def _public_display() -> Optional[str]:
    url = (_settings().get("display_url") or "").strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="display_url is malformed") from exc
    if (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password) or (
        url.startswith("/") and not url.startswith("//") and not parsed.scheme and not parsed.netloc
    ):
        return url
    raise HTTPException(status_code=503, detail="display_url must be an HTTP(S) URL or a same-origin path")


def _local_display() -> str:
    result = _run(["display"], timeout=10)
    if not result["ok"]:
        raise HTTPException(status_code=503, detail=result["error"])
    url = result["output"]
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="androidctl display reported a malformed URL") from exc
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password:
        raise HTTPException(status_code=503, detail="androidctl display must report a loopback HTTP URL; set display_url for a public URL")
    return url


def _display_url() -> str:
    public = _public_display()
    if public:
        return public
    _local_display()
    return API + "/display/vnc.html?autoconnect=true&resize=scale&reconnect=false"


@router.get("/status")
def status() -> Dict[str, Any]:
    result = _run(["status"], timeout=30)
    if result["ok"]:
        result.update(part.split("=", 1) for part in result.pop("output").split() if "=" in part)
    try:
        result["display_url"] = _display_url()
    except HTTPException as exc:
        result["display_url"] = None
        result["display_error"] = exc.detail
    return result


@router.post("/actions/{name}")
def action(name: str) -> Dict[str, Any]:
    if name not in ACTIONS:
        raise HTTPException(status_code=404, detail=f"unknown action {name}")
    return _run(ACTIONS[name], timeout=300)


@router.post("/display/session")
def display_session(request: Request) -> Dict[str, Any]:
    public = _public_display()
    if public:
        return {"ok": True, "url": public}
    local = _local_display()
    # Native cookie-authenticated dashboards can load iframe assets through
    # the API. Legacy token-only dashboards cannot put SDK headers on iframe
    # requests; use the local display when browsing locally, or an explicit
    # authenticated reverse proxy URL. Do not weaken Hermes' auth middleware.
    if not getattr(request.app.state, "auth_required", False):
        if request.url.hostname in ("localhost", "127.0.0.1", "::1"):
            return {"ok": True, "url": local}
        raise HTTPException(status_code=503, detail="Embedded remote display needs Hermes dashboard cookie authentication or a public display_url setting")
    now = time.monotonic()
    for ticket, (expires, _) in list(_tickets.items()):
        if expires <= now:
            _tickets.pop(ticket, None)
    if len(_tickets) >= 128:
        raise HTTPException(status_code=429, detail="Too many pending display connections; try again in a minute")
    ticket = secrets.token_urlsafe(32)
    _tickets[ticket] = (now + TICKET_TTL, request.headers.get("host", ""))
    # Clear saved noVNC host settings and use an absolute same-origin path.
    path = API + "/display/websockify?ticket=" + ticket
    query = urlencode({"autoconnect": "true", "resize": "scale", "reconnect": "false", "host": "", "port": "0", "path": path})
    return {"ok": True, "url": API + "/display/vnc.html?" + query}


@router.get("/display/{path:path}")
async def display_asset(path: str) -> Response:
    # Only noVNC files, never a client-selected host, redirect or WS target.
    if not path or any(part in (".", "..") for part in path.split("/")) or "\\" in path or ":" in path or path.startswith("/") or path == "websockify":
        raise HTTPException(status_code=404, detail="Unknown display asset")
    local = await run_in_threadpool(_local_display)
    origin = urlsplit(local)
    target = f"{origin.scheme}://{origin.netloc}/{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as client:
            async with client.get(target, allow_redirects=False) as upstream:
                if 300 <= upstream.status < 400:
                    raise HTTPException(status_code=502, detail="Unexpected display redirect")
                return Response(
                    content=await upstream.read(),
                    status_code=upstream.status,
                    headers={"Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Display is unavailable; try Reload Display") from exc


@router.websocket("/display/websockify")
async def display_websocket(ws: WebSocket) -> None:
    # Tickets are minted under HTTP auth, used once, and bound to Host/Origin.
    info = _tickets.pop(ws.query_params.get("ticket", ""), None)
    try:
        origin = urlsplit(ws.headers.get("origin", ""))
    except ValueError:
        await ws.close(code=1008)
        return
    host = ws.headers.get("host", "")
    if not info or info[0] <= time.monotonic() or info[1] != host or origin.scheme not in ("http", "https") or origin.netloc != host or not _plugin_enabled():
        await ws.close(code=1008)
        return
    close_code = 1000
    try:
        local = await run_in_threadpool(_local_display)
        target = "ws://" + urlsplit(local).netloc + "/websockify"
        # Frame traffic has no read deadline; only connection establishment
        # is bounded. No Hermes cookies, tokens or headers go to websockify.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10), trust_env=False) as client:
            async with client.ws_connect(target, protocols=["binary"], max_msg_size=16 * 1024 * 1024) as upstream:
                await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)

                async def to_display() -> None:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("bytes") is not None:
                            await upstream.send_bytes(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send_str(message["text"])

                async def to_browser() -> None:
                    async for message in upstream:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(message.data)
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            return

                tasks = [asyncio.create_task(to_display()), asyncio.create_task(to_browser())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    except (aiohttp.ClientError, asyncio.TimeoutError, HTTPException, WebSocketDisconnect):
        close_code = 1011
    finally:
        try:
            await ws.close(code=close_code)
        except (RuntimeError, WebSocketDisconnect):
            pass
