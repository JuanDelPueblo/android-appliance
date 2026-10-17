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


# Browser input


def key_fields(event: pb.KeyboardEvent) -> tuple:
    return (event.key, event.keyCode, pb.KeyboardEvent.KeyEventType.Name(event.eventType))


@pytest.mark.parametrize(
    "key, action, expected",
    [
        ("a", "down", ("a", 0, "keypress")),
        ("a", "press", ("a", 0, "keypress")),
        ("a", "up", None),
        ("Z", "down", ("Z", 0, "keypress")),
        ("~", "down", ("~", 0, "keypress")),
        (" ", "down", (" ", 0, "keypress")),
        ("Enter", "down", ("Enter", 0, "keydown")),
        ("Enter", "up", ("Enter", 0, "keyup")),
        ("Enter", "press", ("Enter", 0, "keypress")),
        ("Backspace", "down", ("Backspace", 0, "keydown")),
        ("ArrowLeft", "press", ("ArrowLeft", 0, "keypress")),
        ("F12", "down", ("F12", 0, "keydown")),
        ("Shift", "down", ("Shift", 0, "keydown")),
        # W3C Home moves the text cursor; the Android button is GoHome.
        ("Home", "press", ("Home", 0, "keypress")),
        ("BrowserHome", "press", ("GoHome", 0, "keypress")),
        ("BrowserBack", "press", ("BrowserBack", 0, "keypress")),
        # Menu and ContextMenu do nothing as W3C names; evdev 139 does.
        ("ContextMenu", "down", ("", 139, "keydown")),
        ("Menu", "up", ("", 139, "keyup")),
        ("Menu", "press", ("", 139, "keypress")),
        ("CapsLock", "down", None),
        ("CapsLock", "up", None),
        ("Dead", "down", None),
        ("Process", "down", None),
        ("Unidentified", "press", None),
        # No key event carries these.
        ("​", "down", None),
    ],
)
def test_browser_keys(key, action, expected):
    event = input.browser_key(key, action)
    assert (event if event is None else key_fields(event)) == expected


def test_browser_keys_outside_ascii_are_pasted():
    assert input.browser_key("é", "down") == "é"
    assert input.browser_key("日", "press") == "日"
    assert input.browser_key("😀", "down") == "😀"
    assert input.browser_key("é", "up") is None


def test_a_held_named_key_is_not_pressed_again_but_a_character_is():
    assert input.browser_key("Enter", "down", repeat=True) is None
    assert input.browser_key("Enter", "up", repeat=True) is not None
    assert input.browser_key("a", "down", repeat=True) is not None


def test_key_names():
    assert all(input.valid_key(k) for k in ("a", "é", "Enter", "ArrowUp", "F12", " "))
    assert not any(input.valid_key(k) for k in ("", "Arrow Up", "x" * 33, "Enter;", "a\n"))


@pytest.mark.parametrize(
    "name, expected",
    [
        ("HOME", ("GoHome", 0)),
        ("BACK", ("GoBack", 0)),
        ("APP_SWITCH", ("AppSwitch", 0)),
        ("POWER", ("Power", 0)),
        ("VOLUME_UP", ("AudioVolumeUp", 0)),
        ("VOLUME_DOWN", ("AudioVolumeDown", 0)),
        ("MENU", ("", 139)),
    ],
)
def test_buttons(name, expected):
    assert name in input.BUTTONS
    for action in ("down", "up", "press"):
        event = input.button_key(name, action)
        assert key_fields(event)[:2] == expected and key_fields(event)[2] == {"down": "keydown", "up": "keyup", "press": "keypress"}[action]
    assert input.button_key(name, "down", repeat=True) is None


def test_contact_pressure_and_clamping():
    c = input.contact(7, -3.4, 5000, 0.5)
    assert (c.x, c.y, c.identifier, c.pressure) == (0, 1919, 7, 512)
    assert input.contact(1, 10, 10, 1).pressure == 1024
    assert input.contact(1, 10, 10, 7).pressure == 1024
    assert input.contact(1, 10, 10, 0.0001).pressure == 1
    assert input.contact(1, 10, 10, 0).pressure == 0


def pointers() -> input.Pointers:
    return input.Pointers(itertools.count(100).__next__)


def points(event: pb.InputEvent | None) -> list[tuple]:
    return [] if event is None else [(t.x, t.y, t.identifier, t.pressure) for t in event.touch_event.touches]


def test_pointer_contacts_get_their_own_identifiers():
    p = pointers()
    assert points(p.touches([("a", 10, 20, 1), (5, 30, 40, 0.5)])) == [(10, 20, 100, 1024), (30, 40, 101, 512)]
    assert points(p.touches([("a", 11, 21, 1)])) == [(11, 21, 100, 1024)]
    # The id 5 and the id "5" are two fingers.
    assert points(p.touches([("5", 1, 1, 1)])) == [(1, 1, 102, 1024)]
    assert points(p.touches([("a", 12, 22, 0), (5, 31, 41, 0)])) == [(12, 22, 100, 0), (31, 41, 101, 0)]
    # A lifted id is free again, as a new contact; a lift of an unknown id is nothing.
    assert points(p.touches([("a", 9, 9, 1)])) == [(9, 9, 103, 1024)]
    assert p.touches([("zz", 1, 1, 0)]) is None
    assert p.touches([]) is None


def test_pointer_contacts_are_limited():
    p = pointers()
    assert len(points(p.touches([(i, i, i, 1) for i in range(input.MAX_CONTACTS)]))) == 10
    assert p.touches([(10, 1, 1, 1)]) is None
    assert points(p.touches([(3, 1, 1, 0)])) == [(1, 1, 103, 0)]
    assert len(points(p.touches([(10, 1, 1, 1)]))) == 1


def test_the_release_lifts_every_contact_where_it_was_last():
    p = pointers()
    p.touches([("a", 10, 20, 1), ("b", 30, 40, 1)])
    p.touches([("a", 15, 25, 1)])
    p.mouse(50, 60, 1)
    assert sorted(points(p.release())) == [(15, 25, 100, 0), (30, 40, 101, 0), (50, 60, 102, 0)]
    assert p.release() is None


def test_forget_drops_the_contacts_without_lifting_them():
    p = pointers()
    p.touches([("a", 10, 20, 1)])
    p.forget()
    assert p.release() is None and p.touches([("a", 1, 1, 0)]) is None


def test_the_mouse_is_one_finger():
    p = pointers()
    assert p.mouse(5, 5, 0) == (None, False)
    event, back = p.mouse(100, 200, 1)
    assert points(event) == [(100, 200, 100, 1024)] and not back
    assert points(p.mouse(110, 210, 1)[0]) == [(110, 210, 100, 1024)]
    assert points(p.mouse(110, 210, 0)[0]) == [(110, 210, 100, 0)]
    assert p.mouse(120, 220, 0) == (None, False)
    # The next press is a new finger.
    assert points(p.mouse(1, 1, 1)[0]) == [(1, 1, 101, 1024)]


def test_the_right_button_is_back_once_for_each_press():
    p = pointers()
    assert p.mouse(1, 1, 2) == (None, True)
    assert p.mouse(2, 2, 2) == (None, False)
    assert p.mouse(2, 2, 0) == (None, False)
    assert p.mouse(2, 2, 2) == (None, True)
    # Both buttons: a finger and Back. The middle button is nothing.
    assert p.mouse(2, 2, 0) == (None, False)
    event, back = p.mouse(3, 3, 0b011)
    assert points(event) == [(3, 3, 100, 1024)] and back is True
    event, back = p.mouse(3, 3, 0b100)
    assert points(event) == [(3, 3, 100, 0)] and back is False


def test_a_mouse_and_a_finger_share_the_contact_limit():
    p = pointers()
    p.touches([(i, 1, 1, 1) for i in range(10)])
    assert p.mouse(5, 5, 1) == (None, False)


def test_a_wheel_notch_drags_against_the_wheel():
    steps = input.wheel(9, 540, 960, 0, 120)
    assert [(s.event.touch_event.touches[0].x, s.event.touch_event.touches[0].pressure) for s in steps][0] == (540, 1024)
    ys = [s.event.touch_event.touches[0].y for s in steps]
    assert ys[0] == 960 and ys[-1] == 960 - 168 and ys == sorted(ys, reverse=True)
    assert [s.event.touch_event.touches[0].pressure for s in steps] == [1024] * (len(steps) - 1) + [0]
    # 120 ms of moves at 60 Hz, then the hold of 80 ms before the lift.
    assert abs(sum(s.delay for s in steps[:-1]) - 0.12) < 1e-9 and steps[-1].delay == 0.08
    # Up and to the right with a negative delta; sideways with dx.
    last = input.wheel(9, 500, 900, -120, -120)[-1].event.touch_event.touches[0]
    assert (last.x, last.y) == (500 + 168, 900 + 168)
    assert input.wheel(9, 500, 900, 120, 0)[-1].event.touch_event.touches[0].x == 500 - 168


def test_a_wheel_drag_is_bounded_and_never_a_tap():
    far = input.wheel(1, 540, 1500, 0, 100000)
    assert far[-1].event.touch_event.touches[0].y == 1500 - input.MAX_WHEEL_PX
    assert input.wheel(1, 540, 960, 0, 10) == []
    assert input.wheel(1, 540, 960, 0, 0) == []
    assert input.wheel(1, 540, 960, 3, 4) == []
    assert input.wheel(1, 540, 960, 0, 25)


def test_a_wheel_drag_at_the_edge_starts_further_in():
    steps = input.wheel(1, 540, 10, 0, 120)
    first, last = steps[0].event.touch_event.touches[0], steps[-1].event.touch_event.touches[0]
    assert (first.y, last.y) == (168, 0)
    steps = input.wheel(1, 1070, 960, -120, 0)
    first, last = steps[0].event.touch_event.touches[0], steps[-1].event.touch_event.touches[0]
    assert last.x == 1079 and first.x == 1079 - 168
    # Outside the frame counts as the edge.
    assert input.wheel(1, -500, 99999, 0, -120)[0].event.touch_event.touches[0].y == 1919 - 168
