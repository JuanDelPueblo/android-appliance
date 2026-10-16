"""Emulator gRPC: screenshots, clipboard, keys and the shared input stream."""

import asyncio
import itertools
import logging

import grpc
from google.protobuf import empty_pb2

from . import _proto  # noqa: F401  (puts the generated modules on sys.path)

import emulator_controller_pb2 as pb  # isort: skip
import emulator_controller_pb2_grpc as pb_grpc  # isort: skip

log = logging.getLogger(__name__)

# Unpaced keys are lost inside the emulator; 2 ms was enough when measured.
KEY_GAP = 0.005
CALL_TIMEOUT = 10.0


class InputStream:
    """One long-lived streamInputEvent call, shared by every caller.

    Events from all callers go out in order on one stream. A broken
    stream (for example after a gRPC error) is replaced on the next send.
    After close() every send fails: the channel is gone.
    """

    def __init__(self, stub: pb_grpc.EmulatorControllerStub, metadata: grpc.aio.Metadata | tuple[()] = ()):
        self._stub = stub
        self._metadata = metadata
        self._call: grpc.aio.StreamUnaryCall | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    def new_identifier(self) -> int:
        """A touch identifier that no other gesture uses."""
        return next(self._ids)

    async def send(self, *events: pb.InputEvent) -> None:
        async with self._lock:
            for event in events:
                await self._write(event)

    async def _write(self, event: pb.InputEvent) -> None:
        for attempt in range(2):
            # close() can run while this coroutine waits, so work on a
            # local reference.
            if self._closed:
                raise ConnectionError("the emulator input stream is closed")
            call = self._call
            try:
                if call is None or call.done():
                    call = self._call = self._stub.streamInputEvent(metadata=self._metadata)
                async with asyncio.timeout(CALL_TIMEOUT):
                    await call.write(event)
                return
            except (grpc.aio.AioRpcError, grpc.aio.BaseError, asyncio.InvalidStateError, TimeoutError) as e:
                if call is not None:
                    call.cancel()
                if self._call is call:
                    self._call = None
                if attempt:
                    raise ConnectionError(f"the emulator input stream failed: {e}") from e
                log.info("input stream broke (%s); opening a new one", e)

    async def close(self) -> None:
        self._closed = True
        call, self._call = self._call, None
        if call is not None:
            call.cancel()


class EmulatorGrpc:
    def __init__(self, port: int, token: str | None = None):
        """`token` is the emulator's gRPC token (-grpc-use-token). Anything
        in the guest can reach the port through 10.0.2.2, so the token is
        what keeps it out."""
        self.channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.stub = pb_grpc.EmulatorControllerStub(self.channel)
        self._metadata: tuple[tuple[str, str], ...] = (("authorization", f"Bearer {token}"),) if token else ()
        self.input = InputStream(self.stub, self._metadata)
        self._key_lock = asyncio.Lock()
        self._last_key = 0.0

    async def screenshot(self) -> tuple[bytes, int, int]:
        """Return a PNG of the display and its size."""
        image = await self.stub.getScreenshot(pb.ImageFormat(format=pb.ImageFormat.PNG), timeout=20, metadata=self._metadata)
        return image.image, image.format.width, image.format.height

    async def get_clipboard(self) -> str:
        return (await self.stub.getClipboard(empty_pb2.Empty(), timeout=CALL_TIMEOUT, metadata=self._metadata)).text

    async def set_clipboard(self, text: str) -> None:
        await self.stub.setClipboard(pb.ClipData(text=text), timeout=CALL_TIMEOUT, metadata=self._metadata)

    async def send_key(self, event: pb.KeyboardEvent) -> None:
        # Every key sender shares this pacing.
        async with self._key_lock:
            loop = asyncio.get_running_loop()
            if (wait := self._last_key + KEY_GAP - loop.time()) > 0:
                await asyncio.sleep(wait)
            try:
                await self.stub.sendKey(event, timeout=CALL_TIMEOUT, metadata=self._metadata)
            finally:
                self._last_key = loop.time()

    async def status(self) -> pb.EmulatorStatus:
        return await self.stub.getStatus(empty_pb2.Empty(), timeout=CALL_TIMEOUT, metadata=self._metadata)

    async def close(self) -> None:
        await self.input.close()
        await self.channel.close()
