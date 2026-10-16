"""Key table and input encoding, as verified against Android.

Coordinates are device pixels of the natural 1080x1920 frame. The
emulator does not clamp them (Android drops a touch outside), so clamp
here. A finger lifts only with pressure 0 for its identifier.
"""

import asyncio
import re
from dataclasses import dataclass

from .config import HEIGHT, WIDTH
from .grpc_client import EmulatorGrpc, InputStream, pb

PRESSURE = 1024
TAP_HOLD = 0.05
FRAME = 1 / 60
# Let the clipboard reach the guest before and after the paste key.
PASTE_SETTLE = 0.3
KEY_PASTE = 135
KEY_MENU = 139

EVDEV = pb.KeyboardEvent.Evdev
KEYPRESS = pb.KeyboardEvent.keypress

# Android key names -> KeyboardEvent fields. W3C "Home" moves the text
# cursor, so HOME is GoHome; Menu and ContextMenu do nothing, so MENU is
# evdev 139. CapsLock is missing on purpose: it sticks in the guest.
KEYS: dict[str, dict[str, object]] = {
    "HOME": {"key": "GoHome"},
    "BACK": {"key": "GoBack"},
    "APP_SWITCH": {"key": "AppSwitch"},
    "RECENTS": {"key": "AppSwitch"},
    "POWER": {"key": "Power"},
    "VOLUME_UP": {"key": "AudioVolumeUp"},
    "VOLUME_DOWN": {"key": "AudioVolumeDown"},
    "MENU": {"codeType": EVDEV, "keyCode": KEY_MENU},
    "ENTER": {"key": "Enter"},
    "TAB": {"key": "Tab"},
    "DEL": {"key": "Backspace"},
    "FORWARD_DEL": {"key": "Delete"},
    "ESCAPE": {"key": "Escape"},
    "DPAD_UP": {"key": "ArrowUp"},
    "DPAD_DOWN": {"key": "ArrowDown"},
    "DPAD_LEFT": {"key": "ArrowLeft"},
    "DPAD_RIGHT": {"key": "ArrowRight"},
    "MOVE_HOME": {"key": "Home"},
    "MOVE_END": {"key": "End"},
    "PASTE": {"codeType": EVDEV, "keyCode": KEY_PASTE},
    "SPACE": {"key": " "},
}
KEYS.update({c: {"key": c.lower()} for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
KEYS.update({c: {"key": c} for c in "0123456789"})


def resolve_key(name: str) -> pb.KeyboardEvent | str:
    """Return the gRPC key event of a name, or the argument for
    'adb shell input keyevent' (numbers and names outside the table).

    A bare number is an Android key code, as in v1: "66" is ENTER. Digit
    keys are KEYCODE_0 to KEYCODE_9.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"invalid key name '{name}'")
    upper = name.upper()
    prefixed = upper.startswith("KEYCODE_")
    short = upper.removeprefix("KEYCODE_")
    if short == "CAPS_LOCK":
        raise ValueError("CAPS_LOCK is not supported: it sticks in the guest")
    if short.isdigit() and not prefixed:
        return short
    if fields := KEYS.get(short):
        return pb.KeyboardEvent(eventType=KEYPRESS, **fields)  # type: ignore[arg-type]
    return f"KEYCODE_{short}"


def clamp(x: float, y: float) -> tuple[int, int]:
    return max(0, min(WIDTH - 1, round(x))), max(0, min(HEIGHT - 1, round(y)))


def touch(identifier: int, x: float, y: float, down: bool) -> pb.InputEvent:
    cx, cy = clamp(x, y)
    contact = pb.Touch(x=cx, y=cy, identifier=identifier, pressure=PRESSURE if down else 0)
    return pb.InputEvent(touch_event=pb.TouchEvent(touches=[contact]))


@dataclass(frozen=True)
class Step:
    """Wait `delay` seconds, then send `event`."""

    delay: float
    event: pb.InputEvent


def tap(identifier: int, x: float, y: float) -> list[Step]:
    return [Step(0, touch(identifier, x, y, True)), Step(TAP_HOLD, touch(identifier, x, y, False))]


def long_press(identifier: int, x: float, y: float, duration_ms: int) -> list[Step]:
    return [Step(0, touch(identifier, x, y, True)), Step(duration_ms / 1000, touch(identifier, x, y, False))]


def swipe(identifier: int, x1: float, y1: float, x2: float, y2: float, duration_ms: int) -> list[Step]:
    """A straight move at 60 Hz, lifted at the end point (like 'input swipe')."""
    moves = max(1, round(duration_ms / 1000 / FRAME))
    interval = duration_ms / 1000 / moves
    steps = [Step(0, touch(identifier, x1, y1, True))]
    for i in range(1, moves + 1):
        f = i / moves
        steps.append(Step(interval, touch(identifier, x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, True)))
    steps.append(Step(0, touch(identifier, x2, y2, False)))
    return steps


async def play(stream: InputStream, steps: list[Step]) -> None:
    for step in steps:
        if step.delay:
            await asyncio.sleep(step.delay)
        await stream.send(step.event)


def ascii_keys(text: str) -> list[pb.KeyboardEvent]:
    """Key presses for printable ASCII, newline (Enter) and tab."""
    events = []
    for ch in text:
        if ch == "\n":
            key = "Enter"
        elif ch == "\t":
            key = "Tab"
        elif " " <= ch <= "~":
            key = ch
        else:
            raise ValueError(f"the keys method types only ASCII, not {ch!r}; use the paste method")
        events.append(pb.KeyboardEvent(eventType=KEYPRESS, key=key))
    return events


async def type_keys(grpc: EmulatorGrpc, text: str) -> None:
    for event in ascii_keys(text):
        await grpc.send_key(event)


async def paste(grpc: EmulatorGrpc, text: str, settle: float = PASTE_SETTLE) -> None:
    """Exact text: paste it through the guest clipboard, then restore it.

    Typed keys lose characters and drop non-ASCII; the clipboard does not.
    """
    saved = await grpc.get_clipboard()
    await grpc.set_clipboard(text)
    try:
        await asyncio.sleep(settle)
        await grpc.send_key(pb.KeyboardEvent(codeType=EVDEV, keyCode=KEY_PASTE, eventType=KEYPRESS))
        await asyncio.sleep(settle)
    finally:
        await grpc.set_clipboard(saved)
