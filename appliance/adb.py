"""Async adb wrapper. Every call has a bounded timeout."""

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from .config import SERIAL

# Bound what one command can buffer; large data goes through push/pull.
OUTPUT_LIMIT = 64 * 1024 * 1024


class AdbError(Exception):
    pass


class AdbTimeout(AdbError):
    pass


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode(errors="replace").replace("\r\n", "\n")

    @property
    def error_text(self) -> str:
        return self.stderr.decode(errors="replace").strip()

    @property
    def output(self) -> str:
        return (self.text + self.stderr.decode(errors="replace")).strip()


async def _collect(stream: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await stream.read(65536):
        data += chunk
        if len(data) > limit:
            raise AdbError(f"the output exceeds {limit // (1024 * 1024)} MiB; use pull for large data")
    return bytes(data)


class Adb:
    def __init__(self, binary: str, env: dict[str, str], serial: str = SERIAL):
        self.binary = binary
        self.env = env
        self.serial = serial

    async def run(self, *args: str, timeout: float = 30, device: bool = True) -> Result:
        argv = [self.binary, "-s", self.serial, *args] if device else [self.binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *argv, env=self.env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )  # fmt: skip
        assert proc.stdout and proc.stderr
        try:
            async with asyncio.timeout(timeout):
                out, err = await asyncio.gather(_collect(proc.stdout, OUTPUT_LIMIT), _collect(proc.stderr, OUTPUT_LIMIT))
                code = await proc.wait()
        except TimeoutError:
            raise AdbTimeout(f"adb {' '.join(args[:2])} did not finish in {timeout:g}s") from None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        return Result(code, out, err)

    async def start_server(self) -> None:
        # The server writes the adb key on its first start; the emulator
        # passes the public key to a new AVD.
        result = await self.run("start-server", device=False)
        if result.exit_code:
            raise AdbError(f"adb start-server failed: {result.output}")

    async def kill_server(self) -> None:
        with contextlib.suppress(AdbError):
            await self.run("kill-server", device=False, timeout=10)

    async def state(self) -> str:
        """Return device, unauthorized, offline, or '' when no device yet."""
        try:
            result = await self.run("get-state", timeout=10)
        except AdbTimeout:
            return ""
        if result.exit_code == 0:
            return result.text.strip()
        for state in ("unauthorized", "offline"):
            if state in result.error_text:
                return state
        return ""

    async def shell(self, command: str, timeout: float = 30) -> Result:
        return await self.run("shell", command, timeout=timeout)

    async def getprop(self, name: str) -> str:
        try:
            return (await self.shell(f"getprop {name}", timeout=10)).text.strip()
        except AdbTimeout:
            return ""

    async def exec_out(self, command: str, timeout: float = 30) -> Result:
        return await self.run("exec-out", command, timeout=timeout)

    async def emu_kill(self) -> bool:
        """Ask the emulator to shut down. Return whether it accepted."""
        try:
            return (await self.run("emu", "kill", timeout=10)).exit_code == 0
        except AdbTimeout:
            return False

    async def install(self, apk: Path, flags: list[str], timeout: float = 600) -> Result:
        return await self.run("install", *flags, str(apk), timeout=timeout)

    async def push(self, local: Path, remote: str, timeout: float = 600) -> Result:
        return await self.run("push", str(local), remote, timeout=timeout)

    async def pull(self, remote: str, local: Path, timeout: float = 600) -> Result:
        return await self.run("pull", remote, str(local), timeout=timeout)
