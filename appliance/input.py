"""Key table and input encoding, as verified against Android.

Coordinates are device pixels of the natural 1080x1920 frame. The
emulator does not clamp them (Android drops a touch outside), so clamp
here. A finger lifts only with pressure 0 for its identifier.
"""

import asyncio
import math
import re
from collections.abc import Callable, Hashable
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


# Browser input

# The emulator's touch screen has ten slots.
MAX_CONTACTS = 10
# Android ignores injectWheel, so a wheel notch becomes a short touch drag:
# about 168 px per 120 units of delta, in 120 ms, held 80 ms before the
# lift so that Android does not fling.
WHEEL_PX = 168 / 120
WHEEL_MOVE = 0.12
WHEEL_HOLD = 0.08
MAX_WHEEL_PX = 1000
MIN_WHEEL_PX = 30
# Not forwarded: CapsLock sticks in the guest, and the others stand for a
# composition that arrives as text.
DROPPED_KEYS = frozenset({"CapsLock", "Dead", "Process", "Unidentified"})
# Browser key names that Android reads differently (see KEYS).
KEY_ALIASES = {"ContextMenu": "MENU", "Menu": "MENU", "BrowserHome": "HOME"}
BUTTONS = ("HOME", "BACK", "APP_SWITCH", "POWER", "VOLUME_UP", "VOLUME_DOWN", "MENU")
KEY_ACTIONS = {"down": pb.KeyboardEvent.keydown, "up": pb.KeyboardEvent.keyup, "press": KEYPRESS}
KEY_NAME = re.compile(r"[A-Za-z0-9]{1,32}")


def valid_key(key: str) -> bool:
    """Whether `key` can be a W3C KeyboardEvent.key: one character or a name."""
    return len(key) == 1 or bool(KEY_NAME.fullmatch(key))


def browser_key(key: str, action: str, repeat: bool = False) -> pb.KeyboardEvent | str | None:
    """Map a W3C KeyboardEvent.key (see valid_key) to what the emulator takes.

    Returns a key event, text to paste (a printable character outside
    ASCII, which no key event carries), or None for a key that is dropped.
    `repeat` says that the key is held already: Android repeats a held
    named key itself, but a printable character is one key press for each
    event, repeats included.
    """
    if key in DROPPED_KEYS:
        return None
    if len(key) == 1:
        if action == "up":
            return None
        if " " <= key <= "~":
            return pb.KeyboardEvent(eventType=KEYPRESS, key=key)
        return key if key.isprintable() else None
    if action == "down" and repeat:
        return None
    fields = KEYS[KEY_ALIASES[key]] if key in KEY_ALIASES else {"key": key}
    return pb.KeyboardEvent(eventType=KEY_ACTIONS[action], **fields)  # type: ignore[arg-type]


def button_key(name: str, action: str, repeat: bool = False) -> pb.KeyboardEvent | None:
    """A hardware button (one of BUTTONS) as a key event; down and up
    allow holds (a 1 s POWER hold opens the power menu)."""
    if action == "down" and repeat:
        return None
    return pb.KeyboardEvent(eventType=KEY_ACTIONS[action], **KEYS[name])  # type: ignore[arg-type]


def contact(identifier: int, x: float, y: float, pressure: float) -> pb.Touch:
    """A touch point; `pressure` is 0 (a lift) or 0 < p <= 1 like PointerEvent.pressure."""
    cx, cy = clamp(x, y)
    return pb.Touch(x=cx, y=cy, identifier=identifier, pressure=max(1, round(min(pressure, 1) * PRESSURE)) if pressure > 0 else 0)


def _fit(start: float, move: float, size: int) -> float:
    """Shift `start` so that start + move stays inside the frame."""
    return min(max(start, -move), size - 1 - move) if abs(move) < size else start


def wheel(identifier: int, x: float, y: float, dx: float, dy: float) -> list[Step]:
    """A wheel notch as a touch drag, or nothing when the delta is too
    small: a drag shorter than Android's touch slop is a tap. The wheel
    moves the content, so the finger moves the opposite way. A drag near
    the edge starts further in, so that it keeps its length."""
    mx = max(-MAX_WHEEL_PX, min(MAX_WHEEL_PX, -dx * WHEEL_PX))
    my = max(-MAX_WHEEL_PX, min(MAX_WHEEL_PX, -dy * WHEEL_PX))
    if math.hypot(mx, my) < MIN_WHEEL_PX:
        return []
    x, y = clamp(x, y)
    x, y = _fit(x, mx, WIDTH), _fit(y, my, HEIGHT)
    moves = max(1, round(WHEEL_MOVE / FRAME))
    steps = [Step(0, touch(identifier, x, y, True))]
    for i in range(1, moves + 1):
        steps.append(Step(WHEEL_MOVE / moves, touch(identifier, x + mx * i / moves, y + my * i / moves, True)))
    steps.append(Step(WHEEL_HOLD, touch(identifier, x + mx, y + my, False)))
    return steps


class Pointers:
    """The fingers and the mouse of one browser session.

    The browser numbers its touches itself, and two sessions can use the
    same numbers, so every contact gets an emulator identifier of its own.
    The mouse is one contact: the left button is a finger.
    """

    def __init__(self, new_identifier: Callable[[], int]):
        self._new_identifier = new_identifier
        # key -> (emulator identifier, last x, last y)
        self._contacts: dict[Hashable, tuple[int, float, float]] = {}
        self._right = False

    def _point(self, key: Hashable, x: float, y: float, pressure: float) -> pb.Touch | None:
        """The touch point for a contact that is pressed (`pressure` > 0) or lifted."""
        known = self._contacts.get(key)
        if pressure <= 0:
            if known is None:
                return None
            del self._contacts[key]
            return contact(known[0], x, y, 0)
        if known is None:
            if len(self._contacts) >= MAX_CONTACTS:
                return None
            known = (self._new_identifier(), x, y)
        self._contacts[key] = (known[0], x, y)
        return contact(known[0], x, y, pressure)

    def touches(self, points: list[tuple[Hashable, float, float, float]]) -> pb.InputEvent | None:
        """One event for the (id, x, y, pressure) points of a browser message."""
        touches = [t for id_, x, y, pressure in points if (t := self._point(("touch", id_), x, y, pressure))]
        return pb.InputEvent(touch_event=pb.TouchEvent(touches=touches)) if touches else None

    def mouse(self, x: float, y: float, buttons: int) -> tuple[pb.InputEvent | None, bool]:
        """Handle a mouse message: the touch event of the left button, and
        whether the right button was just pressed (that is Back). Moving
        with no button pressed does nothing."""
        event = self.touches([("mouse", x, y, 1 if buttons & 1 else 0)])
        back = bool(buttons & 2) and not self._right
        self._right = bool(buttons & 2)
        return event, back

    def release(self) -> pb.InputEvent | None:
        """Lift every contact where it was last seen."""
        touches = [contact(identifier, x, y, 0) for identifier, x, y in self._contacts.values()]
        self.forget()
        return pb.InputEvent(touch_event=pb.TouchEvent(touches=touches)) if touches else None

    def forget(self) -> None:
        """Drop the contacts without lifting them (the emulator is gone)."""
        self._contacts.clear()
        self._right = False
