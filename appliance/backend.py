"""The operations: one implementation for every front end."""

import asyncio
import collections
import contextlib
import io
import logging
import re
import secrets
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import grpc
from PIL import Image

from . import emulator, input
from .adb import AdbError, AdbTimeout, Result
from .grpc_client import EmulatorGrpc
from .supervisor import Supervisor

log = logging.getLogger(__name__)

UI_ATTEMPTS = 10
UI_RETRY_DELAY = 1.0
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
COMPONENT = re.compile(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+")
FILE_TYPES = {"directory": "dir", "regular file": "file", "regular empty file": "file", "symbolic link": "link"}
UI_FLAGS = ("clickable", "long-clickable", "scrollable", "checkable", "checked", "focused", "selected", "password")


class OperationError(Exception):
    """An operation failed. `code` is the API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Screenshot:
    data: bytes
    content_type: str
    device_width: int
    device_height: int
    scale: float


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class LogBuffer(logging.Handler):
    """The recent daemon log, for GET /logs?source=daemon."""

    def __init__(self, size: int = 5000):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.extend(self.format(record).splitlines())


def quote(value: str) -> str:
    """Quote one argument for the device shell."""
    return "'" + value.replace("'", "'\\''") + "'"


def _png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])


def _convert(png: bytes, fmt: str, quality: int, max_size: int | None) -> tuple[bytes, float]:
    image = Image.open(io.BytesIO(png))
    scale = 1.0
    if max_size and max(image.size) > max_size:
        scale = max_size / max(image.size)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "jpeg":
        image.convert("RGB").save(out, "JPEG", quality=quality)
    else:
        image.save(out, "PNG")
    return out.getvalue(), scale


def compact(xml: str) -> list[dict[str, Any]]:
    """The UI nodes an agent can act on: text, descriptions, controls."""
    nodes = []
    for node in ElementTree.fromstring(xml).iter("node"):
        a = node.attrib
        m = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        flags = [f for f in UI_FLAGS if a.get(f) == "true"]
        if a.get("enabled") == "false":
            flags.append("disabled")
        cls = a.get("class", "")
        text, desc = a.get("text", ""), a.get("content-desc", "")
        actionable = {"clickable", "long-clickable", "scrollable", "checkable", "focused"} & set(flags)
        if x2 <= x1 or y2 <= y1 or not (text or desc or actionable or cls.endswith("EditText")):
            continue
        item: dict[str, Any] = {"class": cls.rsplit(".", 1)[-1]}
        if text:
            item["text"] = text
        if desc:
            item["desc"] = desc
        if rid := a.get("resource-id"):
            item["id"] = rid
        item["bounds"] = [x1, y1, x2, y2]
        item["center"] = [(x1 + x2) // 2, (y1 + y2) // 2]
        if flags:
            item["flags"] = flags
        nodes.append(item)
    return nodes


class Backend:
    def __init__(self, sup: Supervisor, daemon_log: LogBuffer | None = None):
        self.sup = sup
        self.cfg = sup.cfg
        self.adb = sup.adb
        self.daemon_log = daemon_log
        # The guest clipboard is global: one text entry at a time.
        self._text_lock = asyncio.Lock()

    # Lifecycle

    def status(self) -> dict[str, Any]:
        return self.sup.status()

    async def start(self, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        await self.sup.start(wait, self.cfg.boot_timeout if timeout is None else timeout)
        return self.status()

    async def stop(self, wait: bool = True) -> dict[str, Any]:
        await self.sup.stop(wait)
        return self.status()

    async def restart(self, wait: bool = True, timeout: float | None = None) -> dict[str, Any]:
        await self.sup.restart(wait, self.cfg.boot_timeout if timeout is None else timeout)
        return self.status()

    async def wait(self, timeout: float | None = None) -> dict[str, Any]:
        await self.sup.wait(self.cfg.boot_timeout if timeout is None else timeout)
        return self.status()

    @contextlib.asynccontextmanager
    async def _use(self, wake: bool = True) -> AsyncIterator[EmulatorGrpc]:
        """Hold a lease and turn transport failures into API errors."""
        async with self.sup.lease(wake) as stub:
            try:
                yield stub
            except AdbTimeout as e:
                raise OperationError("timeout", str(e)) from e
            except (AdbError, ConnectionError, grpc.aio.AioRpcError, grpc.aio.BaseError) as e:
                # BaseError also covers a call on a channel that a crash or a
                # stop closed meanwhile.
                detail = e.details() if isinstance(e, grpc.aio.AioRpcError) else str(e)
                raise OperationError("unavailable", f"the device did not answer: {detail}") from e

    async def _shell(self, command: str, timeout: float = 30) -> Result:
        return await self.adb.shell(command, timeout=timeout)

    # Screen

    async def screenshot(self, fmt: str = "png", quality: int = 85, max_size: int | None = None, passive: bool = False) -> Screenshot:
        """A passive screenshot (the dashboard's poster) only looks: it never
        starts Android and is not use."""
        async with self._use(not passive) as stub:
            try:
                png, width, height = await stub.screenshot()
            except (grpc.aio.AioRpcError, grpc.aio.BaseError) as e:
                log.warning("gRPC screenshot failed (%s): using adb screencap", e.code() if isinstance(e, grpc.aio.AioRpcError) else e)
                result = await self.adb.exec_out("screencap -p")
                if result.exit_code or not result.stdout.startswith(b"\x89PNG"):
                    raise OperationError("unavailable", "the screenshot failed") from e
                png = result.stdout
                width, height = _png_size(png)
        if fmt == "png" and not (max_size and max(width, height) > max_size):
            return Screenshot(png, "image/png", width, height, 1.0)
        data, scale = await asyncio.to_thread(_convert, png, fmt, quality, max_size)
        return Screenshot(data, f"image/{fmt}", width, height, scale)

    async def ui(self) -> str:
        async with self._use():
            dump = f"/data/local/tmp/window_dump.{secrets.token_hex(4)}.xml"
            try:
                # A fresh boot can answer "null root node" before a window exists.
                for _ in range(UI_ATTEMPTS):
                    await self._shell(f"rm -f {dump}")
                    if (await self._shell(f"uiautomator dump {dump}", timeout=60)).exit_code == 0:
                        xml = (await self.adb.exec_out(f"cat {dump}")).text
                        if "<hierarchy" in xml:
                            return xml
                    await asyncio.sleep(UI_RETRY_DELAY)
            finally:
                with contextlib.suppress(AdbError):
                    await self._shell(f"rm -f {dump}")
            raise OperationError("unavailable", "the UI dump did not produce a hierarchy")

    # Input

    async def tap(self, x: float, y: float) -> None:
        async with self._use() as stub:
            await input.play(stub.input, input.tap(stub.input.new_identifier(), x, y))

    async def long_press(self, x: float, y: float, duration_ms: int = 800) -> None:
        async with self._use() as stub:
            await input.play(stub.input, input.long_press(stub.input.new_identifier(), x, y, duration_ms))

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> None:
        async with self._use() as stub:
            await input.play(stub.input, input.swipe(stub.input.new_identifier(), x1, y1, x2, y2, duration_ms))

    async def text(self, text: str, method: str = "paste") -> None:
        if method not in ("paste", "keys"):
            raise OperationError("bad_request", f"method must be paste or keys, not '{method}'")
        if not text:
            raise OperationError("bad_request", "the text is empty")
        if method == "keys":
            try:
                input.ascii_keys(text)
            except ValueError as e:
                raise OperationError("bad_request", str(e)) from e
        async with self._use() as stub, self._text_lock:
            if method == "paste":
                await input.paste(stub, text)
            else:
                await input.type_keys(stub, text)

    async def key(self, name: str) -> None:
        try:
            key = input.resolve_key(name)
        except ValueError as e:
            raise OperationError("bad_request", str(e)) from e
        async with self._use() as stub:
            if not isinstance(key, str):
                await stub.send_key(key)
                return
            # Android ignores a name it does not know, as v1 did.
            result = await self._shell(f"input keyevent {key}")
            if result.exit_code:
                raise OperationError("unavailable", f"input keyevent failed: {result.output}")

    # Shell and apps

    async def shell(self, command: str, timeout: float = 300) -> ShellResult:
        if not command.strip():
            raise OperationError("bad_request", "the command is empty")
        async with self._use():
            try:
                result = await self._shell(command, timeout=timeout)
            except AdbTimeout as e:
                raise OperationError("timeout", f"the command did not finish in {timeout:g}s") from e
        return ShellResult(result.exit_code, result.stdout, result.stderr)

    async def apps(self, all: bool = False) -> list[str]:
        async with self._use():
            result = await self._shell("pm list packages" if all else "pm list packages -3")
        if result.exit_code:
            raise OperationError("unavailable", f"pm list packages failed: {result.output}")
        return sorted(line.removeprefix("package:").strip() for line in result.text.splitlines() if line.startswith("package:"))

    async def install(self, apk: Path, replace: bool = False, grant: bool = False, downgrade: bool = False, test: bool = False) -> str:
        flags = [flag for flag, on in (("-r", replace), ("-g", grant), ("-d", downgrade), ("-t", test)) if on]
        async with self._use():
            result = await self.adb.install(apk, flags)
        output = result.output
        if result.exit_code or "Failure" in output:
            raise OperationError("bad_request", output or "the install failed")
        return output

    async def _require_package(self, package: str) -> None:
        if not PACKAGE.fullmatch(package):
            raise OperationError("bad_request", f"invalid package name '{package}'")
        if not (await self._shell(f"pm path {package}")).text.strip():
            raise OperationError("not_found", f"package '{package}' is not installed")

    async def launch(self, package: str) -> None:
        async with self._use():
            await self._require_package(package)
            found = await self._shell(
                f"cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {package}"
            )
            lines = [line.strip() for line in found.text.splitlines() if COMPONENT.fullmatch(line.strip())]
            if not lines:
                raise OperationError("not_found", f"package '{package}' has no launcher activity")
            result = await self._shell(f"am start -n {lines[-1]}")
        if result.exit_code or "Error" in result.output:
            raise OperationError("unavailable", f"the launch failed: {result.output}")

    async def force_stop(self, package: str) -> None:
        async with self._use():
            await self._require_package(package)
            await self._shell(f"am force-stop {package}")

    async def uninstall(self, package: str, keep_data: bool = False) -> str:
        async with self._use():
            await self._require_package(package)
            result = await self._shell(f"pm uninstall {'-k ' if keep_data else ''}{package}")
        output = result.output
        if result.exit_code or "Success" not in output:
            raise OperationError("bad_request", output or "the uninstall failed")
        return output

    # Files

    def temp_path(self) -> Path:
        """A new temporary file on the state disk (/tmp is small)."""
        self.cfg.upload_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.upload_dir / f"transfer-{secrets.token_hex(8)}"

    @staticmethod
    def _check_path(path: str) -> None:
        if not path.startswith("/") or "\n" in path or "\0" in path:
            raise OperationError("bad_request", f"the device path must be absolute, not '{path}'")

    async def _file_type(self, path: str) -> str | None:
        result = await self._shell(f"stat -L -c %F {quote(path)}")
        return result.text.strip() if result.exit_code == 0 else None

    async def pull(self, path: str) -> Path:
        """Copy a device file to a temporary file; the caller removes it."""
        self._check_path(path)
        async with self._use():
            kind = await self._file_type(path)
            if kind is None:
                raise OperationError("not_found", f"{path} does not exist")
            if kind == "directory":
                raise OperationError("bad_request", f"{path} is a directory")
            local = self.temp_path()
            result = await self.adb.pull(path, local)
        if result.exit_code:
            local.unlink(missing_ok=True)
            raise OperationError("unavailable", f"the pull failed: {result.output}")
        return local

    async def push(self, local: Path, path: str, name: str | None = None) -> int:
        """Copy a file to the device. When `path` is a directory, the file
        goes into it under `name` (as adb push does)."""
        self._check_path(path)
        if name is not None and (not name or "/" in name or name in (".", "..") or "\0" in name):
            raise OperationError("bad_request", f"invalid file name '{name}'")
        if name is None and path.endswith("/"):
            raise OperationError("bad_request", "the device path must name a file, not a directory")
        async with self._use():
            if await self._file_type(path) == "directory":
                if name is None:
                    raise OperationError("bad_request", f"{path} is a directory; give the path of the file")
                path = f"{path.rstrip('/')}/{name}"
            elif path.endswith("/"):
                raise OperationError("bad_request", f"{path} is not a directory")
            result = await self.adb.push(local, path)
        if result.exit_code:
            raise OperationError("bad_request", f"the push failed: {result.output}")
        return local.stat().st_size

    async def list_files(self, path: str) -> list[dict[str, Any]]:
        self._check_path(path)
        q = quote(path)
        fmt = "'%F|%s|%Y|%n'"
        # The trailing slash makes find enter a symlinked directory (/sdcard).
        command = f"if [ -d {q} ]; then find {quote(path.rstrip('/') + '/')} -mindepth 1 -maxdepth 1 -exec stat -c {fmt} {{}} +; else stat -c {fmt} {q}; fi"
        async with self._use():
            result = await self._shell(command)
        if result.exit_code and not result.text.strip():
            if "No such file" in result.output:
                raise OperationError("not_found", f"{path} does not exist")
            raise OperationError("bad_request", f"cannot list {path}: {result.error_text}")
        entries = []
        for line in result.text.splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit():
                continue
            entries.append(
                {
                    "name": parts[3].rstrip("/").rsplit("/", 1)[-1],
                    "type": FILE_TYPES.get(parts[0], "other"),
                    "size": int(parts[1]),
                    "mtime": int(parts[2]),
                }
            )
        return sorted(entries, key=lambda e: e["name"])

    # Logs

    def logs(self, source: str = "emulator", lines: int = 200) -> str:
        if source == "emulator":
            return emulator.tail(emulator.latest_log(self.cfg), lines)
        if source == "daemon":
            return "\n".join(list(self.daemon_log.lines)[-lines:]) if self.daemon_log else ""
        raise OperationError("bad_request", f"source must be emulator or daemon, not '{source}'")
