"""The MCP server: the appliance operations as tools for any MCP agent.

`python3 -m appliance mcp` serves it over stdio inside the container and
talks to the daemon over its unix socket (`client.Client`), so an agent on
the host needs nothing but `androidctl mcp`. Screenshots are image
content; everything else is text, so the agent needs no shared files.
Standard output is the protocol: nothing else may write to it.
"""

import asyncio
import base64
import binascii
import json
import logging
import time
from functools import wraps
from pathlib import Path
from typing import Any

import httpx2
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ContentBlock, TextContent

from . import __version__
from .backend import compact
from .client import Client, ClientError

log = logging.getLogger(__name__)

# Every result goes through the agent's context window: keep it bounded.
MAX_SHELL_OUTPUT = 256 * 1024
MAX_PULL = 8 * 1024**2
MAX_PUSH = 64 * 1024**2
MAX_LOG_LINES = 5000
# A lifecycle tool waits this long for a boot, then returns the state.
DEFAULT_WAIT = 300.0
PROGRESS_INTERVAL = 2.0

INSTRUCTIONS = """Tools for one Android device (an emulator) in the Android appliance.

Android boots on demand: any tool that needs the device starts it and waits
for the boot (half a minute to a few minutes). Coordinates are device pixels;
android_screenshot reports the device size and the scale of the image it
returns, and android_ui gives the center of every element, which is the
best place to tap. Prefer android_ui over reading a screenshot when you need
to find or verify an element; take a screenshot to see the actual picture.
android_text pastes exact unicode into the focused field. android_shell runs
as the adb shell user. Pulled and pushed files travel inline, with size
limits, so use them for small files.
"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _decode(content: str, limit: int, what: str) -> bytes:
    try:
        data = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ToolError(f"{what} is not valid base64: {e}") from e
    if len(data) > limit:
        raise ToolError(f"{what} is {len(data)} bytes; the limit is {limit}")
    return data


def _text(data: bytes, limit: int) -> str:
    cut = len(data) > limit
    text = data[:limit].decode("utf-8", errors="replace")
    return text + f"\n[... {len(data) - limit} more bytes not shown]" if cut else text


def _json(value: Any) -> str:
    return json.dumps(value, indent=1, ensure_ascii=False)


def _ui_lines(nodes: list[dict[str, Any]]) -> str:
    lines = []
    for n in nodes:
        parts = [n["class"]]
        if "text" in n:
            parts.append(json.dumps(n["text"], ensure_ascii=False))
        if "desc" in n:
            parts.append(f"desc={json.dumps(n['desc'], ensure_ascii=False)}")
        if "id" in n:
            parts.append(f"id={n['id']}")
        parts.append(f"center={n['center'][0]},{n['center'][1]}")
        parts.append("bounds=" + ",".join(str(v) for v in n["bounds"]))
        if n.get("flags"):
            parts.append("flags=" + ",".join(n["flags"]))
        lines.append(" ".join(parts))
    return "\n".join(lines) if lines else "(no actionable elements)"


def _tool(fn):
    """Turn the daemon's errors into tool errors the agent can read."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ClientError as e:
            raise ToolError(f"{e} ({e.code})") from e
        except httpx2.HTTPError as e:
            raise ToolError(f"the appliance daemon does not answer: {e}") from e

    return wrapper


class Tools:
    """The tool implementations, over one API client."""

    def __init__(self, client: Client):
        self.client = client

    async def _wait_boot(self, ctx: Context | None, timeout: float) -> str:
        """Wait for the current boot, with progress, at most `timeout`
        seconds; a boot that takes longer returns the current state."""
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            remaining = timeout - elapsed
            if remaining <= 0:
                status = await self.client.status()
                status["note"] = f"still {status['state']} after {timeout:g}s; call android_wait to keep waiting"
                return _json(status)
            try:
                return _json(await self.client.wait(min(PROGRESS_INTERVAL, remaining)))
            except ClientError as e:
                if e.code == "conflict":
                    # No boot to join: a restart is still stopping, or Android is down.
                    status = await self.client.status()
                    if status["state"] not in ("stopping", "restarting"):
                        if status.get("last_error"):
                            raise ToolError(f"android is {status['state']}: {status['last_error']}") from e
                        raise ToolError(f"{e} ({e.code})") from e
                    await asyncio.sleep(min(PROGRESS_INTERVAL, max(remaining, 0)))
                elif e.code != "timeout":
                    raise
                else:
                    status = await self.client.status()
            if ctx is not None:
                await ctx.report_progress(min(elapsed, timeout), timeout, f"android is {status['state']} ({status.get('phase') or '...'})")

    # Lifecycle

    @_tool
    async def status(self) -> str:
        return _json(await self.client.status())

    @_tool
    async def start(self, ctx: Context, wait: bool = True, timeout: float = DEFAULT_WAIT) -> str:
        status = await self.client.start(wait=False)
        if not wait or status["state"] == "running":
            return _json(status)
        return await self._wait_boot(ctx, timeout)

    @_tool
    async def stop(self) -> str:
        return _json(await self.client.stop(wait=True))

    @_tool
    async def restart(self, ctx: Context, wait: bool = True, timeout: float = DEFAULT_WAIT) -> str:
        status = await self.client.restart(wait=False)
        if not wait:
            return _json(status)
        return await self._wait_boot(ctx, timeout)

    @_tool
    async def wait(self, ctx: Context, timeout: float = DEFAULT_WAIT) -> str:
        return await self._wait_boot(ctx, timeout)

    # Screen

    @_tool
    async def screenshot(self, format: str = "jpeg", quality: int = 80, max_size: int = 1280) -> list[ContentBlock]:
        if format not in ("jpeg", "png"):
            raise ToolError("format must be jpeg or png")
        shot = await self.client.screenshot(format, max(1, min(quality, 100)), max_size if max_size > 0 else None)
        width, height = shot.device_width, shot.device_height
        note = f"device {width}x{height} pixels"
        if shot.scale != 1.0:
            note += f"; the image is scaled by {shot.scale:.4g}: multiply image coordinates by {1 / shot.scale:.4g} before tapping"
        return [Image(data=shot.data, format=format).to_image_content(), TextContent(type="text", text=note)]

    @_tool
    async def ui(self, format: str = "compact") -> str:
        if format not in ("compact", "json", "xml"):
            raise ToolError("format must be compact, json or xml")
        xml = await self.client.ui()
        if format == "xml":
            return xml
        nodes = compact(xml)
        return _json(nodes) if format == "json" else _ui_lines(nodes)

    # Input

    @_tool
    async def tap(self, x: float, y: float) -> str:
        await self.client.tap(x, y)
        return f"tapped {x:g},{y:g}"

    @_tool
    async def long_press(self, x: float, y: float, duration_ms: int = 800) -> str:
        await self.client.long_press(x, y, duration_ms)
        return f"held {x:g},{y:g} for {duration_ms} ms"

    @_tool
    async def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> str:
        await self.client.swipe(x1, y1, x2, y2, duration_ms)
        return f"swiped {x1:g},{y1:g} to {x2:g},{y2:g} in {duration_ms} ms"

    @_tool
    async def text(self, text: str, method: str = "paste") -> str:
        await self.client.text(text, method)
        return f"typed {len(text)} characters"

    @_tool
    async def key(self, key: str) -> str:
        await self.client.key(key)
        return f"sent {key}"

    # Shell and apps

    @_tool
    async def shell(self, command: str, timeout: float = 300) -> str:
        result = await self.client.shell(command, timeout)
        out = _text(result.stdout, MAX_SHELL_OUTPUT)
        err = _text(result.stderr, MAX_SHELL_OUTPUT)
        if result.exit_code == 0 and not err:
            return out
        return f"exit code {result.exit_code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"

    @_tool
    async def apps(self, all: bool = False) -> str:
        return "\n".join(await self.client.apps(all)) or "(no third-party apps)"

    @_tool
    async def launch(self, package: str) -> str:
        await self.client.launch(package)
        return f"launched {package}"

    @_tool
    async def force_stop(self, package: str) -> str:
        await self.client.force_stop(package)
        return f"stopped {package}"

    @_tool
    async def uninstall(self, package: str, keep_data: bool = False) -> str:
        return await self.client.uninstall(package, keep_data)

    @_tool
    async def install(self, apk_base64: str, replace: bool = False, grant: bool = False, downgrade: bool = False, test: bool = False) -> str:
        data = _decode(apk_base64, MAX_PUSH, "apk_base64")
        path = _spool(data)
        try:
            return await self.client.install(path, replace, grant, downgrade, test)
        finally:
            path.unlink(missing_ok=True)

    # Files

    @_tool
    async def ls(self, path: str) -> str:
        entries = await self.client.list_files(path)
        if not entries:
            return "(empty)"
        return "\n".join(f"{e['type']:<5} {e['size']:>10} {e['name']}" for e in entries)

    @_tool
    async def pull(self, path: str, encoding: str = "auto") -> str:
        if encoding not in ("auto", "text", "base64"):
            raise ToolError("encoding must be auto, text or base64")
        local = _spool(b"")
        try:
            size = await self.client.pull(path, local)
            if size > MAX_PULL:
                raise ToolError(f"{path} is {size} bytes; the limit is {MAX_PULL}")
            data = local.read_bytes()
        finally:
            local.unlink(missing_ok=True)
        if encoding == "base64":
            return _b64(data)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            if encoding == "text":
                raise ToolError(f"{path} is not UTF-8 text; use encoding=base64") from None
            return f"[binary, {len(data)} bytes, base64]\n{_b64(data)}"

    @_tool
    async def push(self, path: str, text: str | None = None, content_base64: str | None = None) -> str:
        if (text is None) == (content_base64 is None):
            raise ToolError("give either text or content_base64")
        data = text.encode() if text is not None else _decode(content_base64 or "", MAX_PUSH, "content_base64")
        if len(data) > MAX_PUSH:
            raise ToolError(f"the content is {len(data)} bytes; the limit is {MAX_PUSH}")
        local = _spool(data)
        try:
            size = await self.client.push(local, path)
        finally:
            local.unlink(missing_ok=True)
        return f"wrote {size} bytes to {path}"

    # Logs

    @_tool
    async def logs(self, source: str = "emulator", lines: int = 200) -> str:
        return await self.client.logs(source, max(1, min(lines, MAX_LOG_LINES)))


def _spool(data: bytes) -> Path:
    """A private temporary file for one transfer (the client streams files)."""
    import tempfile

    fd, name = tempfile.mkstemp(prefix="appliance-mcp-")
    with open(fd, "wb") as f:
        f.write(data)
    return Path(name)


def create_server(client: Client) -> MCPServer:
    tools = Tools(client)
    server = MCPServer(name="android-appliance", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")
    add = server.add_tool
    add(tools.status, "android_status", description="The state of Android: stopped, starting, running, stopping or restarting; boot readiness, idle time, the last error.")
    add(tools.start, "android_start", description="Start Android and wait until it is usable (bounded by timeout; the state is returned either way).")
    add(tools.stop, "android_stop", description="Shut Android down gracefully and free its memory. Apps and data stay on the disk.")
    add(tools.restart, "android_restart", description="Stop Android, then start it again and wait for the boot.")
    add(tools.wait, "android_wait", description="Wait for a boot in progress, at most timeout seconds.")
    add(tools.screenshot, "android_screenshot", description="The screen as an image (jpeg or png), scaled so that its longest side is max_size pixels, with the device size and scale.")
    add(tools.ui, "android_ui", description="The UI hierarchy: compact (one actionable element per line with its center, the default), json, or the raw uiautomator xml.")
    add(tools.tap, "android_tap", description="Tap a point, in device pixels.")
    add(tools.long_press, "android_long_press", description="Touch and hold a point for duration_ms.")
    add(tools.swipe, "android_swipe", description="Swipe from one point to another in duration_ms (scroll, drag, unlock).")
    add(tools.text, "android_text", description="Type text into the focused field: paste (exact unicode, the default) or keys (ASCII key events).")
    add(tools.key, "android_key", description="Send a key: HOME, BACK, APP_SWITCH, ENTER, TAB, DEL, VOLUME_UP, POWER, a letter, or a keycode number.")
    add(tools.shell, "android_shell", description="Run a shell command on the device as the shell user and return its output (bounded).")
    add(tools.apps, "android_apps", description="List the installed packages: third-party apps, or all with all=true.")
    add(tools.launch, "android_launch", description="Open an app by package name.")
    add(tools.force_stop, "android_force_stop", description="Force-stop an app.")
    add(tools.uninstall, "android_uninstall", description="Uninstall an app; keep_data keeps its data.")
    add(tools.install, "android_install", description="Install an APK given as base64 (up to 64 MiB). Flags: replace, grant permissions, downgrade, test APK.")
    add(tools.ls, "android_ls", description="List a device directory, or stat one file: type, size, name.")
    add(tools.pull, "android_pull", description="Read a device file (up to 8 MiB): UTF-8 text, or base64 for binary content.")
    add(tools.push, "android_push", description="Write a device file from text or base64 content (up to 64 MiB). A directory path takes the file under its own name.")
    add(tools.logs, "android_logs", description="The last lines of the emulator log or of the appliance daemon log.")
    return server


async def serve_stdio(socket_path: Path) -> None:
    async with Client(socket_path=str(socket_path)) as client:
        await create_server(client).run_stdio_async()
