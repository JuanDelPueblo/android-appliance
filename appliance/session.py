"""The dashboard's WebSocket session: status, video streams and input.

One Hub serves every session. A Session is one WebSocket. It reads
JSON messages and sorts them into lanes that keep their own order:
touch and mouse go straight to the emulator's input stream, keys, buttons
and text wait in order (a paste takes 0.6 s and keys need a gap), wheel
notches add up into short drags, and the stream messages negotiate WebRTC.
"""

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

import grpc
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import __version__, input
from .backend import Backend, OperationError
from .config import HEIGHT, WIDTH
from .grpc_client import EmulatorGrpc, pb
from .supervisor import LifecycleError, State
from .turn import Turn
from .webrtc import GRPC_ERRORS, Bridge, Stream, StreamLimit

log = logging.getLogger(__name__)

MAX_SESSIONS = 16
# The largest message the daemon reads (an SDP is about 10 KB); the
# listener enforces it, and the session checks it again.
MAX_MESSAGE = 256 * 1024
MAX_TEXT = 64 * 1024
MAX_SDP = 64 * 1024
MAX_TOUCHES = input.MAX_CONTACTS
OUTBOX = 512
LANE = 256
# Messages per second (a pointer sends about 120): more closes the session.
RATE = 1500
BURST = 3000
# Error messages are for people: a flood of them is not.
ERROR_RATE = 1.0
ERROR_BURST = 5
# The status is sent again now and then, so the idle time stays current.
STATUS_REFRESH = 15.0
DISPLAY_INTERVAL = 1.0
# A 1:20 thumbnail of the 1080x1920 display.
THUMBNAIL = (54, 96)
# The dashboard refreshes the display hash once per second; a failed sample is just skipped.
SAMPLE_ERRORS = (*GRPC_ERRORS, ConnectionError)

# Android pastes on KEY_PASTE only when no modifier is down, and the Ctrl+V of
# a browser holds Control while its text arrives.
MODIFIERS = frozenset({"Control", "Shift", "Alt", "AltGraph", "Meta"})

POLICY_VIOLATION = 1008
TRY_AGAIN_LATER = 1013

Job = Callable[[], Awaitable[None]]


class Reject(Exception):
    """A message the session answers with an error message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def bad(message: str) -> Reject:
    return Reject("bad_request", message)


class Bucket:
    """A token bucket: `rate` per second, up to `burst` at once."""

    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = self.tokens = burst
        self._time = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self._time) * self.rate)
        self._time = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class Lane:
    """Runs jobs one after another, in the order they came."""

    def __init__(self) -> None:
        self._jobs: asyncio.Queue[Job] = asyncio.Queue(LANE)
        self._task = asyncio.create_task(self._run())

    def put(self, job: Job) -> bool:
        try:
            self._jobs.put_nowait(job)
        except asyncio.QueueFull:
            return False
        return True

    async def _run(self) -> None:
        while True:
            job = await self._jobs.get()
            try:
                await job()
            except Exception:
                log.exception("a session job failed")

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


def number(message: dict[str, Any], key: str) -> float:
    value = message.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise bad(f"'{key}' must be a number")
    return value


def string(message: dict[str, Any], key: str, limit: int) -> str:
    value = message.get(key)
    if not isinstance(value, str) or len(value) > limit:
        raise bad(f"'{key}' must be text of at most {limit} characters")
    try:
        value.encode()
    except UnicodeEncodeError as e:
        raise bad(f"'{key}' is not valid Unicode text") from e
    if "\0" in value:
        raise bad(f"'{key}' must not contain a NUL character")
    return value


def choice(message: dict[str, Any], key: str, options: tuple[str, ...] | dict[str, Any]) -> str:
    value = message.get(key)
    if value not in options:
        raise bad(f"'{key}' must be one of: {', '.join(options)}")
    return value  # type: ignore[return-value]


def jsep(data: object) -> dict[str, Any]:
    """The answer or candidate of a viewer, rebuilt from the fields that the
    emulator takes: nothing else (a `bye`, say) gets through."""
    if not isinstance(data, dict):
        raise bad("'data' must be an object")
    if data.get("type") == "answer":
        return {"type": "answer", "sdp": string(data, "sdp", MAX_SDP)}
    if not isinstance(data.get("candidate"), str):
        raise bad("'data' must be an answer or a candidate")
    index = data.get("sdpMLineIndex")
    mid = data.get("sdpMid")
    if isinstance(index, bool) or not (index is None or isinstance(index, int)) or not (mid is None or isinstance(mid, str)):
        raise bad("'sdpMid' must be text and 'sdpMLineIndex' a number")
    return {"candidate": string(data, "candidate", 4096), "sdpMid": mid, "sdpMLineIndex": index}


class Hub:
    """What the sessions share: the streams and the display sampler."""

    def __init__(self, backend: Backend, turn: Turn):
        self.backend = backend
        self.sup = backend.sup
        self.turn = turn
        self.bridge = Bridge()
        self.sessions: set[Session] = set()
        self.sup.subscribe(self._status_changed)
        self.sup.every(DISPLAY_INTERVAL, self._sample_display)

    def running_grpc(self) -> EmulatorGrpc | None:
        return self.sup.grpc if self.sup.state is State.RUNNING else None

    def count_viewers(self) -> None:
        self.sup.viewers = sum(s.streaming for s in self.sessions)

    def _status_changed(self) -> None:
        for session in list(self.sessions):
            session.status_changed()

    def end_sessions(self) -> None:
        """Close every session (the token was rotated: they hold the old one)."""
        for session in list(self.sessions):
            session.end()

    async def _sample_display(self) -> None:
        """Send the hash of a small screenshot to every viewer. A hash that
        changes while no new video frame arrives is a frozen stream."""
        viewers = [s for s in self.sessions if s.streaming]
        if not viewers or (emulator := self.running_grpc()) is None:
            return
        try:
            pixels = await emulator.thumbnail(*THUMBNAIL)
        except SAMPLE_ERRORS as e:
            log.debug("display sample failed: %s", e)
            return
        message = {"type": "display", "hash": hashlib.blake2b(pixels, digest_size=8).hexdigest(), "t": int(time.time() * 1000)}
        for session in viewers:
            session.send(message)

    async def close(self) -> None:
        await self.bridge.aclose()


class Session:
    def __init__(self, hub: Hub, ws: WebSocket, host: str):
        self.hub = hub
        self.ws = ws
        # The host name of the page, for the TURN URLs.
        self.host = host
        self.pointers = input.Pointers(self._new_identifier)
        self._outbox: asyncio.Queue[str] = asyncio.Queue(OUTBOX)
        self._rate = Bucket(RATE, BURST)
        self._errors = Bucket(ERROR_RATE, ERROR_BURST)
        self._stream: Stream | None = None
        # Keys and buttons that are down -> the event that lets them go.
        self._held: dict[str, pb.KeyboardEvent] = {}
        self._wheel: tuple[float, float, float, float] | None = None
        self._wheel_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._control: Lane | None = None
        self._keys: Lane | None = None
        self._close_code = 1000
        self._closed = False
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "ping": self._ping,
            "stream": self._stream_message,
            "jsep": self._jsep_message,
            "touch": self._touch,
            "mouse": self._mouse,
            "wheel": self._wheel_message,
            "key": self._key,
            "button": self._button,
            "text": self._text,
        }

    @property
    def streaming(self) -> bool:
        return self._stream is not None

    # Running

    async def run(self) -> None:
        await self.ws.accept()
        if len(self.hub.sessions) >= MAX_SESSIONS:
            await self.ws.close(TRY_AGAIN_LATER)
            return
        self.hub.sessions.add(self)
        self._control, self._keys = Lane(), Lane()
        self.send({"type": "hello", "device": {"width": WIDTH, "height": HEIGHT}, "version": __version__})
        self.send(self._status())
        self._tasks = [asyncio.create_task(self._write()), asyncio.create_task(self._read())]
        try:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self._close()

    def send(self, message: dict[str, Any]) -> None:
        try:
            self._outbox.put_nowait(json.dumps(message, separators=(",", ":")))
        except asyncio.QueueFull:
            # The viewer does not read: let it reconnect.
            self.end(TRY_AGAIN_LATER)

    def end(self, code: int = POLICY_VIOLATION) -> None:
        """Close the connection; the reader task ends first, and the close follows."""
        self._close_code = code
        self._tasks[1].cancel()

    def error(self, code: str, message: str) -> None:
        if self._errors.take():
            self.send({"type": "error", "code": code, "message": message})

    def _status(self) -> dict[str, Any]:
        return {"type": "status", **self.hub.sup.status()}

    def status_changed(self) -> None:
        """The hub reports a change of the status."""
        self.send(self._status())
        if self.hub.sup.state is not State.RUNNING:
            # The emulator is gone, and its touches and keys with it.
            self.pointers.forget()
            self._held.clear()
            if self._stream is not None and self._control:
                self._control.put(self._android_stopped)

    async def _write(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(self._outbox.get(), STATUS_REFRESH)
            except TimeoutError:
                text = json.dumps(self._status(), separators=(",", ":"))
            await self.ws.send_text(text)

    async def _read(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] != "websocket.receive":
                return
            if not self._rate.take():
                self.error("bad_request", "too many messages")
                self._close_code = POLICY_VIOLATION
                return
            await self._dispatch(message.get("text"))

    async def _dispatch(self, text: str | None) -> None:
        try:
            if text is None or len(text) > MAX_MESSAGE:
                raise bad(f"messages must be JSON text of at most {MAX_MESSAGE} characters")
            try:
                message = json.loads(text)
            except ValueError as e:
                raise bad("the message is not valid JSON") from e
            kind = message.get("type") if isinstance(message, dict) else None
            handler = self._handlers.get(kind) if isinstance(kind, str) else None
            if handler is None:
                raise bad("the message must be an object with a known 'type'")
            await handler(message)
        except Reject as e:
            self.error(e.code, str(e))

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.hub.sessions.discard(self)
        for task in self._tasks:
            task.cancel()
        if self._wheel_task is not None:
            self._wheel_task.cancel()
        tasks = [t for t in (*self._tasks, self._wheel_task) if t is not None]
        await asyncio.gather(*tasks, return_exceptions=True)
        for lane in (self._control, self._keys):
            if lane is not None:
                await lane.close()
        self._drop_stream()
        self.hub.count_viewers()
        await self._let_go()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self.ws.close(self._close_code)

    async def _let_go(self) -> None:
        """Lift this session's touches and release its keys."""
        emulator = self.hub.running_grpc()
        release = self.pointers.release()
        held, self._held = list(self._held.values()), {}
        if emulator is None:
            return
        try:
            if release:
                await emulator.input.send(release)
            for event in held:
                await emulator.send_key(event)
        except (*GRPC_ERRORS, ConnectionError) as e:
            log.info("cannot release the input of a closed session: %s", e)

    def _new_identifier(self) -> int:
        # Only called with the emulator up (see _ready).
        assert self.hub.sup.grpc is not None
        return self.hub.sup.grpc.input.new_identifier()

    # Input

    def _ready(self) -> EmulatorGrpc:
        """The emulator, for input; a session never starts Android. The
        input counts as use."""
        if (emulator := self.hub.running_grpc()) is None:
            raise Reject("not_running", "android is not running")
        self.hub.sup.touch_activity()
        return emulator

    async def _ping(self, message: dict[str, Any]) -> None:
        self.send({"type": "pong", "t": message.get("t")})

    async def _touch(self, message: dict[str, Any]) -> None:
        raw = message.get("touches")
        if not isinstance(raw, list) or len(raw) > MAX_TOUCHES or not all(isinstance(t, dict) for t in raw):
            raise bad(f"'touches' must be a list of up to {MAX_TOUCHES} objects")
        points = [(self._touch_id(t), number(t, "x"), number(t, "y"), number(t, "pressure")) for t in raw]
        emulator = self._ready()
        if event := self.pointers.touches(points):
            await self._send_input(emulator, event)

    @staticmethod
    def _touch_id(touch: dict[str, Any]) -> Hashable:
        value = touch.get("id")
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and len(value) <= 32)):
            raise bad("'id' must be a number or a short text")
        return value

    async def _mouse(self, message: dict[str, Any]) -> None:
        x, y, buttons = number(message, "x"), number(message, "y"), number(message, "buttons")
        emulator = self._ready()
        event, back = self.pointers.mouse(x, y, int(buttons))
        if event:
            await self._send_input(emulator, event)
        if back:
            self._lane(self._keys, functools.partial(self._send_key, input.button_key("BACK", "press")))

    async def _send_input(self, emulator: EmulatorGrpc, event: pb.InputEvent) -> None:
        try:
            await emulator.input.send(event)
        except (*GRPC_ERRORS, ConnectionError) as e:
            log.info("input failed: %s", e)
            self.error("not_running", "android is not running")

    async def _wheel_message(self, message: dict[str, Any]) -> None:
        x, y, dx, dy = (number(message, k) for k in ("x", "y", "dx", "dy"))
        self._ready()
        # Notches that come while a drag runs add up to the next one.
        _, _, sx, sy = self._wheel or (0, 0, 0, 0)
        self._wheel = (x, y, sx + dx, sy + dy)
        if self._wheel_task is None or self._wheel_task.done():
            self._wheel_task = asyncio.create_task(self._scroll())

    async def _scroll(self) -> None:
        while self._wheel is not None:
            x, y, dx, dy = self._wheel
            if (emulator := self.hub.running_grpc()) is None:
                self._wheel = None
                return
            steps = input.wheel(emulator.input.new_identifier(), x, y, dx, dy)
            if not steps:
                # Less than a drag: wait for more notches.
                return
            self._wheel = None
            sent = False
            try:
                await input.play(emulator.input, steps)
                sent = True
            except (*GRPC_ERRORS, ConnectionError):
                self.error("not_running", "android is not running")
                return
            finally:
                if not sent:
                    # A drag that is cut off must not leave the finger down.
                    with contextlib.suppress(*GRPC_ERRORS, ConnectionError):
                        await emulator.input.send(steps[-1].event)

    def _lane(self, lane: Lane | None, job: Job) -> None:
        assert lane
        if not lane.put(job):
            self.error("bad_request", "too many pending messages")
            self.end()

    async def _key(self, message: dict[str, Any]) -> None:
        key = string(message, "key", 32)
        action = choice(message, "action", input.KEY_ACTIONS)
        if not input.valid_key(key):
            raise bad(f"invalid key '{key}'")
        self._ready()
        self._lane(self._keys, functools.partial(self._press, f"key:{key}", key, action, input.browser_key))

    async def _button(self, message: dict[str, Any]) -> None:
        name = choice(message, "name", input.BUTTONS)
        action = choice(message, "action", input.KEY_ACTIONS)
        self._ready()
        self._lane(self._keys, functools.partial(self._press, f"button:{name}", name, action, input.button_key))

    async def _press(self, held: str, name: str, action: str, mapping: Callable[..., pb.KeyboardEvent | str | None]) -> None:
        plan = mapping(name, action, repeat=held in self._held)
        if action == "up":
            self._held.pop(held, None)
        if isinstance(plan, str):
            await self._paste(plan)
        elif plan is not None:
            if plan.eventType == pb.KeyboardEvent.keydown:
                up = pb.KeyboardEvent()
                up.CopyFrom(plan)
                up.eventType = pb.KeyboardEvent.keyup
                self._held[held] = up
            await self._send_key(plan)

    async def _send_key(self, event: pb.KeyboardEvent | None) -> None:
        assert event
        if (emulator := self.hub.running_grpc()) is None:
            self.error("not_running", "android is not running")
            return
        try:
            await emulator.send_key(event)
        except (*GRPC_ERRORS, ConnectionError) as e:
            log.info("key failed: %s", e)
            self.error("not_running", "android is not running")

    async def _text(self, message: dict[str, Any]) -> None:
        text = string(message, "text", MAX_TEXT)
        if not text:
            raise bad("'text' is empty")
        self._ready()
        self._lane(self._keys, functools.partial(self._paste, text))

    async def _paste(self, text: str) -> None:
        # The backend would start Android for a paste; a session never does.
        if self.hub.running_grpc() is None:
            self.error("not_running", "android is not running")
            return
        # The modifiers that the client holds let go for the paste and go down again after it.
        modifiers = [up for up in self._held.values() if up.key in MODIFIERS]
        try:
            for up in modifiers:
                await self._send_key(up)
            await self.hub.backend.text(text)
        except (OperationError, LifecycleError) as e:
            self.error(e.code, str(e))
        finally:
            for up in modifiers:
                down = pb.KeyboardEvent()
                down.CopyFrom(up)
                down.eventType = pb.KeyboardEvent.keydown
                await self._send_key(down)

    # Streams

    async def _stream_message(self, message: dict[str, Any]) -> None:
        action = choice(message, "action", ("start", "stop"))
        self._lane(self._control, self._start_stream if action == "start" else self._stop_stream)

    async def _jsep_message(self, message: dict[str, Any]) -> None:
        data = jsep(message.get("data"))
        self._lane(self._control, functools.partial(self._relay, data))

    async def _relay(self, data: dict[str, Any]) -> None:
        # A candidate can follow the end of its stream.
        if self._stream is not None and data.get("candidate") != "":
            await self._stream.send(data)

    def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
            self.hub.count_viewers()

    async def _stop_stream(self) -> None:
        self._drop_stream()
        self.send({"type": "stream", "state": "ended", "reason": "stopped"})

    async def _android_stopped(self) -> None:
        if self._stream is not None:
            self._drop_stream()
            self.send({"type": "stream", "state": "ended", "reason": "android is not running"})

    async def _start_stream(self) -> None:
        # A start replaces the stream that runs. The old one gets no 'ended':
        # the viewer asked for the change.
        self._drop_stream()
        if (emulator := self.hub.running_grpc()) is None:
            self.error("not_running", "android is not running")
            return
        try:
            servers = self.hub.turn.ice_servers(self.host)
        except ValueError as e:
            self.error("bad_request", str(e))
            return
        if not self.hub.turn.running:
            self.error("unavailable", "the TURN server is not running")
            return
        try:
            stream = self.hub.bridge.create(emulator, self._on_message, self._on_end)
        except StreamLimit as e:
            self.error("stream_limit", str(e))
            return
        self._stream = stream
        self.hub.count_viewers()
        self.hub.sup.touch_activity()
        self.send({"type": "stream", "state": "starting"})
        try:
            await stream.open()
        except GRPC_ERRORS as e:
            self._drop_stream()
            reason = e.details() if isinstance(e, grpc.aio.AioRpcError) else str(e)
            self.send({"type": "stream", "state": "ended", "reason": f"the emulator refused the stream: {reason}"})
            return
        # The emulator's offer follows after this message: the stream starts
        # forwarding when the loop next runs.
        self.send({"type": "stream", "state": "configured", "iceServers": servers, "iceTransportPolicy": "relay"})

    def _on_message(self, data: dict[str, Any]) -> None:
        self.send({"type": "jsep", "data": data})

    def _on_end(self, reason: str) -> None:
        self._stream = None
        self.hub.count_viewers()
        self.send({"type": "stream", "state": "ended", "reason": reason})
