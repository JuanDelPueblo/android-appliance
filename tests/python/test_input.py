"""input.py and the gRPC client: key table, touch encoding, text, pacing."""

import asyncio
import itertools

import grpc
import pytest
from google.protobuf import empty_pb2

from appliance import input
from appliance.grpc_client import KEY_GAP, EmulatorGrpc, pb, pb_grpc


class Recorder(pb_grpc.EmulatorControllerServicer):
    """An in-process emulator that records what it gets."""

    def __init__(self, stream_limit: int | None = None):
        self.events: list[tuple[float, str, object]] = []
        self.clipboard = "saved"
        self.streams = 0
        self.stream_limit = stream_limit

    def _add(self, kind: str, value: object) -> None:
        self.events.append((asyncio.get_running_loop().time(), kind, value))

    async def getClipboard(self, request, context):
        self._add("get", None)
        return pb.ClipData(text=self.clipboard)

    async def setClipboard(self, request, context):
        self.clipboard = request.text
        self._add("set", request.text)
        return empty_pb2.Empty()

    async def sendKey(self, request, context):
        self._add("key", request.key or request.keyCode)
        return empty_pb2.Empty()

    async def streamInputEvent(self, request_iterator, context):
        self.streams += 1
        count = 0
        async for event in request_iterator:
            touch = event.touch_event.touches[0]
            self._add("touch", (touch.identifier, touch.x, touch.y, touch.pressure))
            count += 1
            # Ending the call early breaks the client's stream.
            if self.stream_limit and count >= self.stream_limit:
                break
        return empty_pb2.Empty()


async def serve(servicer: Recorder) -> tuple[grpc.aio.Server, EmulatorGrpc]:
    server = grpc.aio.server()
    pb_grpc.add_EmulatorControllerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, EmulatorGrpc(port)


def fields(event: pb.KeyboardEvent) -> dict:
    return {"key": event.key, "codeType": event.codeType, "keyCode": event.keyCode, "eventType": event.eventType}


@pytest.mark.parametrize(
    "name, key",
    [
        ("HOME", "GoHome"),
        ("home", "GoHome"),
        ("KEYCODE_HOME", "GoHome"),
        ("BACK", "GoBack"),
        ("APP_SWITCH", "AppSwitch"),
        ("RECENTS", "AppSwitch"),
        ("POWER", "Power"),
        ("VOLUME_UP", "AudioVolumeUp"),
        ("VOLUME_DOWN", "AudioVolumeDown"),
        ("ENTER", "Enter"),
        ("TAB", "Tab"),
        ("DEL", "Backspace"),
        ("FORWARD_DEL", "Delete"),
        ("ESCAPE", "Escape"),
        ("DPAD_UP", "ArrowUp"),
        ("DPAD_DOWN", "ArrowDown"),
        ("DPAD_LEFT", "ArrowLeft"),
        ("DPAD_RIGHT", "ArrowRight"),
        ("MOVE_HOME", "Home"),
        ("MOVE_END", "End"),
        ("SPACE", " "),
        ("A", "a"),
        ("KEYCODE_Z", "z"),
        ("KEYCODE_5", "5"),
    ],
)
def test_named_keys(name, key):
    event = input.resolve_key(name)
    assert fields(event) == {"key": key, "codeType": pb.KeyboardEvent.Usb, "keyCode": 0, "eventType": pb.KeyboardEvent.keypress}


@pytest.mark.parametrize("name, code", [("MENU", 139), ("KEYCODE_MENU", 139), ("PASTE", 135)])
def test_evdev_keys(name, code):
    event = input.resolve_key(name)
    assert (event.key, event.codeType, event.keyCode) == ("", pb.KeyboardEvent.Evdev, code)


def test_adb_fallback_and_refusals():
    # A bare number is an Android key code (v1: "key 66" is ENTER).
    assert input.resolve_key("66") == "66"
    assert input.resolve_key("3") == "3"
    assert input.resolve_key("camera") == "KEYCODE_CAMERA"
    assert input.resolve_key("KEYCODE_CALL") == "KEYCODE_CALL"
    for name in ("CAPS_LOCK", "KEYCODE_CAPS_LOCK", "HOME; reboot", "", "BACK BACK"):
        with pytest.raises(ValueError):
            input.resolve_key(name)


def test_clamp():
    assert input.clamp(-5, -5) == (0, 0)
    assert input.clamp(2000, 3000) == (1079, 1919)
    assert input.clamp(1080, 1920) == (1079, 1919)
    assert input.clamp(540.4, 959.6) == (540, 960)


def touches(steps: list[input.Step]) -> list[tuple]:
    out = []
    for step in steps:
        t = step.event.touch_event.touches
        assert len(t) == 1
        out.append((step.delay, t[0].identifier, t[0].x, t[0].y, t[0].pressure))
    return out


def test_tap_and_long_press():
    assert touches(input.tap(7, 100, 200)) == [(0, 7, 100, 200, 1024), (input.TAP_HOLD, 7, 100, 200, 0)]
    assert touches(input.long_press(8, 5000, -1, 800)) == [(0, 8, 1079, 0, 1024), (0.8, 8, 1079, 0, 0)]


def test_swipe():
    steps = touches(input.swipe(3, 100, 1500, 100, 300, 300))
    assert steps[0] == (0, 3, 100, 1500, 1024)
    # 300 ms at 60 Hz: 18 moves, then the lift at the end point.
    assert len(steps) == 1 + 18 + 1
    assert steps[-2][1:] == (3, 100, 300, 1024)
    assert steps[-1] == (0, 3, 100, 300, 0)
    ys = [s[3] for s in steps[1:-1]]
    assert ys == sorted(ys, reverse=True)
    assert sum(s[0] for s in steps) == pytest.approx(0.3)
    assert all(s[1] == 3 for s in steps)


def test_ascii_keys():
    events = input.ascii_keys("Hi !\n\t~")
    assert [e.key for e in events] == ["H", "i", " ", "!", "Enter", "Tab", "~"]
    assert all(e.eventType == pb.KeyboardEvent.keypress for e in events)
    for text in ("Grüße", "日本", "😀"):
        with pytest.raises(ValueError, match="paste"):
            input.ascii_keys(text)


async def test_paste_restores_the_clipboard():
    servicer = Recorder()
    server, stub = await serve(servicer)
    try:
        await input.paste(stub, "Grüße 日本", settle=0.05)
    finally:
        await stub.close()
        await server.stop(0)
    kinds = [(kind, value) for _, kind, value in servicer.events]
    assert kinds == [("get", None), ("set", "Grüße 日本"), ("key", 135), ("set", "saved")]
    times = [t for t, _, _ in servicer.events]
    assert times[2] - times[1] >= 0.05 and times[3] - times[2] >= 0.05


async def test_paste_restores_the_clipboard_on_failure():
    servicer = Recorder()
    server, stub = await serve(servicer)

    async def broken(event):
        raise ConnectionError("gone")

    stub.send_key = broken
    try:
        with pytest.raises(ConnectionError):
            await input.paste(stub, "secret", settle=0)
    finally:
        await stub.close()
        await server.stop(0)
    assert [v for _, kind, v in servicer.events if kind == "set"] == ["secret", "saved"]


async def test_keys_are_paced():
    servicer = Recorder()
    server, stub = await serve(servicer)
    try:
        # Two senders at once share the pacing.
        await asyncio.gather(input.type_keys(stub, "abcdefghij"), input.type_keys(stub, "0123456789"))
    finally:
        await stub.close()
        await server.stop(0)
    times = [t for t, kind, _ in servicer.events if kind == "key"]
    assert len(times) == 20
    assert min(b - a for a, b in itertools.pairwise(times)) >= KEY_GAP * 0.9


async def test_input_stream_is_shared_and_recreated():
    servicer = Recorder(stream_limit=3)
    server, stub = await serve(servicer)
    try:
        ident = stub.input.new_identifier()
        assert stub.input.new_identifier() != ident
        for i in range(8):
            await stub.input.send(input.touch(ident, i, i, True))
            # Give the server time to end the stream after its limit.
            await asyncio.sleep(0.02)
    finally:
        await stub.close()
        await server.stop(0)
    received = [v for _, kind, v in servicer.events if kind == "touch"]
    assert servicer.streams >= 2
    # Every event arrives, in order, after the stream broke.
    assert [x for _, x, _, _ in received] == list(range(8))


async def test_play_sends_a_tap_in_order():
    servicer = Recorder()
    server, stub = await serve(servicer)
    try:
        await input.play(stub.input, input.tap(5, 10, 20))
        await asyncio.sleep(0.05)
    finally:
        await stub.close()
        await server.stop(0)
    assert [v for _, kind, v in servicer.events if kind == "touch"] == [(5, 10, 20, 1024), (5, 10, 20, 0)]
    assert servicer.streams == 1


class TokenRecorder(Recorder):
    """Remembers the metadata of every call."""

    def __init__(self):
        super().__init__()
        self.authorization: list[str | None] = []

    async def getClipboard(self, request, context):
        self.authorization.append(dict(context.invocation_metadata()).get("authorization"))
        return await super().getClipboard(request, context)

    async def sendKey(self, request, context):
        self.authorization.append(dict(context.invocation_metadata()).get("authorization"))
        return await super().sendKey(request, context)

    async def streamInputEvent(self, request_iterator, context):
        self.authorization.append(dict(context.invocation_metadata()).get("authorization"))
        return await super().streamInputEvent(request_iterator, context)


async def test_every_call_carries_the_emulator_token():
    servicer = TokenRecorder()
    server = grpc.aio.server()
    pb_grpc.add_EmulatorControllerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    stub = EmulatorGrpc(port, "t0k/en+==")
    try:
        await stub.get_clipboard()
        await stub.send_key(pb.KeyboardEvent(key="a"))
        await input.play(stub.input, input.tap(1, 5, 5))
        await asyncio.sleep(0.05)
    finally:
        await stub.close()
        await server.stop(0)
    assert servicer.authorization == ["Bearer t0k/en+=="] * 3
    # Without a token no header goes out.
    servicer = TokenRecorder()
    server, stub = await serve(servicer)
    try:
        await stub.get_clipboard()
    finally:
        await stub.close()
        await server.stop(0)
    assert servicer.authorization == [None]


async def test_closed_input_stream_refuses_sends():
    """After the emulator is torn down, a gesture that is still playing
    gets a ConnectionError, not an AttributeError or a UsageError."""
    servicer = Recorder()
    server, stub = await serve(servicer)
    try:
        await stub.input.send(input.touch(1, 5, 5, True))
        await stub.close()
        with pytest.raises(ConnectionError, match="closed"):
            await stub.input.send(input.touch(1, 6, 6, True))
        with pytest.raises(grpc.aio.BaseError):
            await stub.get_clipboard()
    finally:
        await server.stop(0)
