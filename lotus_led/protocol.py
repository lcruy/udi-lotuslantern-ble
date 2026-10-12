"""BLEDOM / ELK-BLEDOM 9-byte BLE packet protocol.

Every command is a fixed 9-byte frame::

    7E <cmd> <sub> <d0> <d1> <d2> <d3> <d4> EF

Provenance
----------
These packets are transcribed from the vendor Android app **Lotus Lantern
v6.5.08** (``wl.smartled``, internally ``com.easylink.colorful``), whose
``classes.dex`` ships unobfuscated -- unlike v6.5.03, which was wrapped in
Qihoo 360 Jiagu. Byte layouts come from
``com.easylink.colorful.service.BluetoothLEService``; the animation table is
the ``R.array.modes`` string array behind the app's mode picker.

This supersedes the earlier hand-reversed guesses, which were wrong in two
ways: the animation opcodes were offset, and the set-mode frame had the wrong
command byte and trailing padding, so the controller silently ignored it.

Keep this module in lockstep with ``lotus-led/src/protocol.rs``; the two are
the same table in two languages, and the tests on each side pin the same bytes.
"""

from __future__ import annotations

import colorsys
from enum import Enum, IntEnum
from typing import Iterable, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

# ── GATT ─────────────────────────────────────────────────────────────────────

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"

#: Advertised as notify-capable, but this firmware never pushes anything to it
#: -- the vendor app does not even subscribe. There is no way to read live
#: state back from the strip.
NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

#: Name fragments seen on this controller family.
DEVICE_NAME_PATTERNS = (
    "BLEDOM", "BLEDOB", "ELK-BLEDOM", "ELK-BLEDOB", "ELK_BLEDOM",
    "LEDBLE", "ELK-BULB", "MELK",
)

#: OUI prefix observed on these controllers. A scan hint, never a filter.
KNOWN_MAC_PREFIX = "BE:60:65"

FRAME_HEAD = 0x7E
FRAME_TAIL = 0xEF

#: The controller drops or stutters above this rate. A hardware property, and
#: the ceiling on every reactive mode's frame rate.
MAX_PACKETS_PER_SEC = 20


# ── Hardware animations ──────────────────────────────────────────────────────

class HWMode(IntEnum):
    """A firmware-resident animation.

    The wire opcode is ``0x80 + index`` where the index is the animation's
    position in the vendor app's picker, so the table is the contiguous range
    ``0x80..0x9C``. The first seven are static colours exposed through the same
    mechanism.
    """

    STATIC_RED = 0x80
    STATIC_BLUE = 0x81
    STATIC_GREEN = 0x82
    STATIC_CYAN = 0x83
    STATIC_YELLOW = 0x84
    STATIC_PURPLE = 0x85
    STATIC_WHITE = 0x86
    JUMP_3_COLOR = 0x87
    JUMP_7_COLOR = 0x88
    CROSS_FADE_3_COLOR = 0x89
    CROSS_FADE_7_COLOR = 0x8A
    FADE_RED = 0x8B
    FADE_GREEN = 0x8C
    FADE_BLUE = 0x8D
    FADE_YELLOW = 0x8E
    FADE_CYAN = 0x8F
    FADE_PURPLE = 0x90
    FADE_WHITE = 0x91
    CROSS_RED_GREEN = 0x92
    CROSS_RED_BLUE = 0x93
    CROSS_GREEN_BLUE = 0x94
    STROBE_7_COLOR = 0x95
    STROBE_RED = 0x96
    STROBE_GREEN = 0x97
    STROBE_BLUE = 0x98
    STROBE_YELLOW = 0x99
    STROBE_CYAN = 0x9A
    STROBE_PURPLE = 0x9B
    STROBE_WHITE = 0x9C

    @property
    def code(self) -> int:
        """Wire opcode."""
        return int(self)

    @property
    def index(self) -> int:
        """Position in the vendor app's list."""
        return int(self) - 0x80

    @property
    def mode_name(self) -> str:
        """Canonical snake_case name used by the CLI, config and GUI."""
        return self.name.lower()

    @property
    def label(self) -> str:
        """Human-readable label, matching the vendor app's wording."""
        return _LABELS[self]


_LABELS = {
    HWMode.STATIC_RED: "Static Red",
    HWMode.STATIC_BLUE: "Static Blue",
    HWMode.STATIC_GREEN: "Static Green",
    HWMode.STATIC_CYAN: "Static Cyan",
    HWMode.STATIC_YELLOW: "Static Yellow",
    HWMode.STATIC_PURPLE: "Static Purple",
    HWMode.STATIC_WHITE: "Static White",
    HWMode.JUMP_3_COLOR: "Three Color Jumping Change",
    HWMode.JUMP_7_COLOR: "Seven Color Jumping Change",
    HWMode.CROSS_FADE_3_COLOR: "Three Color Cross Fade",
    HWMode.CROSS_FADE_7_COLOR: "Seven Color Cross Fade",
    HWMode.FADE_RED: "Red Gradual Change",
    HWMode.FADE_GREEN: "Green Gradual Change",
    HWMode.FADE_BLUE: "Blue Gradual Change",
    HWMode.FADE_YELLOW: "Yellow Gradual Change",
    HWMode.FADE_CYAN: "Cyan Gradual Change",
    HWMode.FADE_PURPLE: "Purple Gradual Change",
    HWMode.FADE_WHITE: "White Gradual Change",
    HWMode.CROSS_RED_GREEN: "Red Green Cross Fade",
    HWMode.CROSS_RED_BLUE: "Red Blue Cross Fade",
    HWMode.CROSS_GREEN_BLUE: "Green Blue Cross Fade",
    HWMode.STROBE_7_COLOR: "Seven Color Strobe Flash",
    HWMode.STROBE_RED: "Red Strobe Flash",
    HWMode.STROBE_GREEN: "Green Strobe Flash",
    HWMode.STROBE_BLUE: "Blue Strobe Flash",
    HWMode.STROBE_YELLOW: "Yellow Strobe Flash",
    HWMode.STROBE_CYAN: "Cyan Strobe Flash",
    HWMode.STROBE_PURPLE: "Purple Strobe Flash",
    HWMode.STROBE_WHITE: "White Strobe Flash",
}

#: Names from the older, incorrect tables. Kept so existing configs and scripts
#: keep resolving to the nearest real animation.
_ALIASES = {
    "FADE7COLOR": HWMode.CROSS_FADE_7_COLOR,
    "FADE7": HWMode.CROSS_FADE_7_COLOR,
    "GRADIENT7": HWMode.CROSS_FADE_7_COLOR,
    "FADE3COLOR": HWMode.CROSS_FADE_3_COLOR,
    "FADE3": HWMode.CROSS_FADE_3_COLOR,
    "JUMP7": HWMode.JUMP_7_COLOR,
    "JUMP3": HWMode.JUMP_3_COLOR,
    "STROBE7": HWMode.STROBE_7_COLOR,
}


def hw_mode_from_name(name: str) -> Optional[HWMode]:
    """Resolve a mode name, ignoring case, spaces, dashes and underscores."""
    norm = name.upper().replace("-", "").replace(" ", "").replace("_", "")
    for mode in HWMode:
        if mode.name.replace("_", "") == norm:
            return mode
    return _ALIASES.get(norm)


def hw_mode_from_code(code: int) -> Optional[HWMode]:
    try:
        return HWMode(code)
    except ValueError:
        return None


class LightChannel(IntEnum):
    """Which output a brightness command targets.

    From ``BluetoothUtil.LightMode``. Plain RGB strips only have :attr:`RGB`;
    the rest exist for RGBW / RGBCCT / laser units sharing this firmware. The
    app's own default is ``0xFF``, which every unit accepts.
    """

    ALL = 0
    RGB = 1
    WHITE = 2
    CCT = 3
    LASER = 4
    DEFAULT = 0xFF


class MicEq(IntEnum):
    """Equaliser profile for the controller's built-in microphone."""

    CLASSIC = 0
    SOFT = 1
    DYNAMIC = 2
    DISCO = 3


# Category selector in byte 4 of the ``7E 05 03 ...`` frame family.
_CAT_LIGHT_MODE = 0x03
_CAT_MIC_EQ = 0x04
_CAT_LASER_MODE = 0x08


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# ── Packets ──────────────────────────────────────────────────────────────────

class Pkt:
    """Builders for every frame the firmware understands.

    Each returns a 9-byte :class:`bytearray` ready for a write to ``FFF3``.
    """

    # -- power ---------------------------------------------------------------

    @staticmethod
    def power(on: bool) -> bytearray:
        """``7E 04 04 <on> 00 <on> FF 00 EF``

        Byte 3 is a channel mask; the vendor app writes ``0x01`` for a normal
        unit. ``0xF0`` -- used by older reverse-engineered docs -- also works.
        """
        v = 0x01 if on else 0x00
        return bytearray([FRAME_HEAD, 0x04, 0x04, v, 0x00, v, 0xFF, 0x00, FRAME_TAIL])

    @staticmethod
    def power_on() -> bytearray:
        return Pkt.power(True)

    @staticmethod
    def power_off() -> bytearray:
        return Pkt.power(False)

    @staticmethod
    def power_channel(mask: int, channel: LightChannel, on: bool) -> bytearray:
        """``7E 04 04 <mask> <channel> <on> FF 00 EF`` for multi-channel units."""
        return bytearray([FRAME_HEAD, 0x04, 0x04, mask & 0xFF, int(channel),
                          1 if on else 0, 0xFF, 0x00, FRAME_TAIL])

    # -- colour --------------------------------------------------------------

    @staticmethod
    def color(r: int, g: int, b: int) -> bytearray:
        """``7E 07 05 03 R G B 10 EF``"""
        return bytearray([FRAME_HEAD, 0x07, 0x05, 0x03,
                          r & 0xFF, g & 0xFF, b & 0xFF, 0x10, FRAME_TAIL])

    @staticmethod
    def color_streaming(r: int, g: int, b: int) -> bytearray:
        """Colour frame with trailer ``0x20``.

        The vendor app uses this while streaming music-reactive colour; it
        tells the firmware frames are arriving rapidly so it does not re-latch
        animation state on every write.
        """
        return bytearray([FRAME_HEAD, 0x07, 0x05, 0x03,
                          r & 0xFF, g & 0xFF, b & 0xFF, 0x20, FRAME_TAIL])

    @staticmethod
    def cct(warm: int, cold: int) -> bytearray:
        """``7E 06 05 02 <warm> <cold> FF 08 EF`` -- CCT-capable units only."""
        return bytearray([FRAME_HEAD, 0x06, 0x05, 0x02,
                          _clamp(warm, 0, 255), _clamp(cold, 0, 255),
                          0xFF, 0x08, FRAME_TAIL])

    @staticmethod
    def single_color(index: int) -> bytearray:
        """``7E 05 05 01 <index> FF FF 08 EF`` -- firmware preset colour."""
        return bytearray([FRAME_HEAD, 0x05, 0x05, 0x01, index & 0xFF,
                          0xFF, 0xFF, 0x08, FRAME_TAIL])

    # -- levels --------------------------------------------------------------

    @staticmethod
    def brightness(level: int, channel: LightChannel = LightChannel.DEFAULT) -> bytearray:
        """``7E 04 01 <level> <channel> FF FF 00 EF``, level 0-100."""
        return bytearray([FRAME_HEAD, 0x04, 0x01, _clamp(level, 0, 100),
                          int(channel), 0xFF, 0xFF, 0x00, FRAME_TAIL])

    @staticmethod
    def speed(level: int) -> bytearray:
        """``7E 04 02 <speed> FF FF FF 00 EF``, 0-100."""
        return bytearray([FRAME_HEAD, 0x04, 0x02, _clamp(level, 0, 100),
                          0xFF, 0xFF, 0xFF, 0x00, FRAME_TAIL])

    # -- animations ----------------------------------------------------------

    @staticmethod
    def hw_mode(mode: HWMode) -> bytearray:
        """``7E 05 03 <opcode> 03 FF FF 00 EF``

        Speed is *not* carried here -- send :meth:`speed` separately.
        """
        return bytearray([FRAME_HEAD, 0x05, 0x03, int(mode), _CAT_LIGHT_MODE,
                          0xFF, 0xFF, 0x00, FRAME_TAIL])

    # -- on-board microphone -------------------------------------------------

    @staticmethod
    def mic_enabled(on: bool) -> bytearray:
        """``7E 04 07 <on> FF FF FF 00 EF``"""
        return bytearray([FRAME_HEAD, 0x04, 0x07, 1 if on else 0,
                          0xFF, 0xFF, 0xFF, 0x00, FRAME_TAIL])

    @staticmethod
    def mic_sensitivity(sens: int) -> bytearray:
        """``7E 04 06 <sens> FF FF FF 00 EF``, 0-100.

        The command byte is ``0x06``; older docs claimed ``0x05``, which is
        actually part of the colour/laser family.
        """
        return bytearray([FRAME_HEAD, 0x04, 0x06, _clamp(sens, 0, 100),
                          0xFF, 0xFF, 0xFF, 0x00, FRAME_TAIL])

    @staticmethod
    def mic_eq(eq: MicEq) -> bytearray:
        """``7E 05 03 <0x80+eq> 04 FF FF 00 EF``"""
        return bytearray([FRAME_HEAD, 0x05, 0x03, 0x80 + int(eq), _CAT_MIC_EQ,
                          0xFF, 0xFF, 0x00, FRAME_TAIL])

    # -- wiring --------------------------------------------------------------

    @staticmethod
    def pin_order(pin1: int, pin2: int, pin3: int) -> bytearray:
        """``7E 06 81 <pin1> <pin2> <pin3> FF 00 EF``

        Argument *n* is the colour pin *n* drives -- ``1`` red, ``2`` green,
        ``3`` blue -- and the factory default is ``(1, 2, 3)``. The controller
        keeps it in flash, so it outlives both the program and the power.

        The direction is easy to invert: this says "pin 1 is red", not "red
        comes out of pin 1". Both readings agree on the factory order and on
        any swap of two colours, and disagree on the two rotations -- so
        getting it backwards looks correct until it meets a strip wired in a
        cycle. ``PinSequenceActivity`` in the vendor app settles it: one
        indicator per pin, stored as ``rgbTypeInColumn[column] = colour``.
        """
        return bytearray([FRAME_HEAD, 0x06, 0x81, _clamp(pin1, 1, 3), _clamp(pin2, 1, 3),
                          _clamp(pin3, 1, 3), 0xFF, 0x00, FRAME_TAIL])

    # -- laser-projector units ----------------------------------------------

    @staticmethod
    def laser(value: int) -> bytearray:
        return bytearray([FRAME_HEAD, 0x05, 0x05, 0x01, value & 0xFF,
                          0xFF, 0xFF, 0x10, FRAME_TAIL])

    @staticmethod
    def laser_mode(mode: int) -> bytearray:
        return bytearray([FRAME_HEAD, 0x05, 0x03, mode & 0xFF, _CAT_LASER_MODE,
                          0xFF, 0xFF, 0xFF, FRAME_TAIL])

    @staticmethod
    def laser_speed(speed: int) -> bytearray:
        return bytearray([FRAME_HEAD, 0x04, 0x02, _clamp(speed, 0, 100), 0x04,
                          0xFF, 0xFF, 0xFF, FRAME_TAIL])

    # -- on-device clock & timers -------------------------------------------

    @staticmethod
    def system_time(hour: int, minute: int, second: int, weekdays: int = 0x7F) -> bytearray:
        """``7E 07 83 <h> <m> <s> <weekday-mask> FF EF``"""
        return bytearray([FRAME_HEAD, 0x07, 0x83, hour & 0xFF, minute & 0xFF,
                          second & 0xFF, weekdays & 0xFF, 0xFF, FRAME_TAIL])

    @staticmethod
    def timer(hour: int, minute: int, second: int, on: bool, weekdays: int = 0x7F) -> bytearray:
        """``7E 08 82 <h> <m> <s> <mode> <weekday-mask> EF``

        Programs an on/off entry into the controller's own clock, so it keeps
        working with the PC switched off.
        """
        return bytearray([FRAME_HEAD, 0x08, 0x82, hour & 0xFF, minute & 0xFF,
                          second & 0xFF, 1 if on else 0, weekdays & 0xFF, FRAME_TAIL])

    @staticmethod
    def countdown(seconds: int, mode: int = 1) -> bytearray:
        """``7E 07 76 <t0> <t1> <t2> <mode> FF EF``"""
        s = max(0, int(seconds))
        return bytearray([FRAME_HEAD, 0x07, 0x76, s & 0xFF, (s >> 8) & 0xFF,
                          (s >> 16) & 0xFF, mode & 0xFF, 0xFF, FRAME_TAIL])

    # -- escape hatch --------------------------------------------------------

    @staticmethod
    def raw(data: Sequence[int]) -> bytearray:
        """Build a custom frame from exactly nine bytes."""
        values = list(data)
        if len(values) != 9:
            raise ValueError(f"a frame is exactly 9 bytes, got {len(values)}")
        return bytearray(v & 0xFF for v in values)


def hex_frame(data: Iterable[int]) -> str:
    """Format a frame the way the protocol notes and the CLI print it."""
    return " ".join(f"{b:02X}" for b in data)


# ── Colour utilities ─────────────────────────────────────────────────────────

def hsv_to_rgb(h: float, s: float, v: float) -> RGB:
    """HSV (0.0-1.0) to RGB (0-255)."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0.0), 1.0), min(max(v, 0.0), 1.0))
    return round(r * 255), round(g * 255), round(b * 255)


def rgb_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """RGB (0-255) to HSV (0.0-1.0)."""
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)


def lerp_color(a: RGB, b: RGB, t: float) -> RGB:
    """Linear blend; ``t`` is clamped to 0.0-1.0."""
    t = min(max(t, 0.0), 1.0)
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def scale_color(c: RGB, percent: float) -> RGB:
    """Dim a colour without touching the strip's brightness register."""
    k = min(max(percent, 0.0), 100.0) / 100.0
    return tuple(round(v * k) for v in c)  # type: ignore[return-value]


def cct_to_rgb(kelvin: int) -> RGB:
    """Colour temperature to RGB, Tanner Helland's approximation."""
    import math

    temp = min(max(kelvin, 1000), 12000) / 100.0
    if temp <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        r = 329.698727446 * ((temp - 60) ** -0.1332047592)
        g = 288.1221695283 * ((temp - 60) ** -0.0755148492)
    if temp >= 66:
        b = 255.0
    elif temp <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(temp - 10) - 305.0447927307
    clamp = lambda x: int(min(max(x, 0.0), 255.0))
    return clamp(r), clamp(g), clamp(b)


def parse_color(value) -> RGB:
    """Accept ``(r, g, b)``, ``[r, g, b]``, ``"#RRGGBB"``, ``"RRGGBB"`` or ``"#RGB"``.

    Raises :class:`ValueError` with a message naming what was wrong, because
    this is the one place users type a colour by hand.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise ValueError(f"a colour needs three components, got {len(value)}")
        out = []
        for v in value:
            iv = int(v)
            if not 0 <= iv <= 255:
                raise ValueError(f"colour components run 0-255, got {iv}")
            out.append(iv)
        return out[0], out[1], out[2]

    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
            return tuple(int(c * 2, 16) for c in text)  # type: ignore[return-value]
        if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text):
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        parts = value.replace(",", " ").split()
        if len(parts) == 3:
            return parse_color(parts)
        raise ValueError(f"'{value}' is not a colour -- try '#FF8800' or '255 136 0'")

    raise ValueError(f"cannot read a colour from {type(value).__name__}")
