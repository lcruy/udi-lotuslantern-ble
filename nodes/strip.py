"""One BLEDOM / ELK-BLEDOM ("Lotus Lantern") strip as an ISY/IoX node.

The controller cannot report its own state -- the firmware never answers --
so every driver here is a *shadow*: what we last delivered successfully. The
drivers only change after the transport confirms the write, so the admin
console never shows a colour that did not reach the strip.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

import udi_interface

from lotus_led.protocol import HWMode, Pkt, cct_to_rgb
from transports import TransportError

LOGGER = udi_interface.LOGGER

#: GV4 value -> firmware animation. 0 means "solid colour, no animation".
MODE_BY_INDEX: Dict[int, HWMode] = {m.index + 1: m for m in HWMode}

#: SETPRESET choices -> RGB. Order must match the PRESET nls entries.
PRESETS: List[Tuple[str, Tuple[int, int, int]]] = [
    ("Red", (255, 0, 0)),
    ("Orange", (255, 100, 0)),
    ("Yellow", (255, 200, 0)),
    ("Green", (0, 255, 0)),
    ("Cyan", (0, 255, 255)),
    ("Blue", (0, 0, 255)),
    ("Purple", (160, 0, 255)),
    ("Magenta", (255, 0, 160)),
    ("Pink", (255, 80, 120)),
    ("Warm White", (255, 170, 80)),
    ("White", (255, 255, 255)),
    ("Cool White", (200, 220, 255)),
]

LINK_UNKNOWN, LINK_OK, LINK_ERROR = 0, 1, 2
DIM_STEP = 10


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _query_param(command: dict, pid: str, default=None):
    """Read a named parameter from a PG3 command.

    Multi-parameter commands arrive as ``{'query': {'R.uom100': '255', ...}}``.
    """
    for key, value in (command.get("query") or {}).items():
        if key.split(".", 1)[0] == pid:
            return value
    return default


class _Worker:
    """Delivers jobs to one strip in order, off the PG3 input thread.

    A job has a *kind*. If a newer job of the same kind is already queued when
    an older one comes up, the older one is dropped -- dragging a slider in
    the admin console then sends the final value, not every step on the way.
    """

    def __init__(self, name: str):
        self._jobs: deque = deque()
        self._cv = threading.Condition()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=f"strip-{name}", daemon=True)
        self._thread.start()

    def submit(self, kind: str, fn: Callable[[], None]) -> None:
        with self._cv:
            self._jobs.append((kind, fn))
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._jobs and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                kind, fn = self._jobs.popleft()
                superseded = any(k == kind for k, _ in self._jobs)
            if superseded:
                continue
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad job must not kill the worker
                LOGGER.exception("strip job '%s' failed", kind)


class LotusStrip(udi_interface.Node):
    id = "lotusled"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 51, "name": "Status"},
        {"driver": "GV1", "value": 255, "uom": 100, "name": "Red"},
        {"driver": "GV2", "value": 255, "uom": 100, "name": "Green"},
        {"driver": "GV3", "value": 255, "uom": 100, "name": "Blue"},
        {"driver": "GV4", "value": 0, "uom": 25, "name": "Effect"},
        {"driver": "GV5", "value": 50, "uom": 51, "name": "Effect Speed"},
        {"driver": "GV6", "value": 100, "uom": 51, "name": "Brightness"},
        {"driver": "GV7", "value": 0, "uom": 25, "name": "Link"},
    ]

    def __init__(self, polyglot, primary, address, name, mac: str, controller):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self.mac = mac
        self.controller = controller
        self._lock = threading.Lock()
        # Seed the shadow from the values PG3 restored out of its database.
        self.power = _int(self.getDriver("ST")) > 0
        self.rgb = (_int(self.getDriver("GV1"), 255), _int(self.getDriver("GV2"), 255),
                    _int(self.getDriver("GV3"), 255))
        self.mode = _int(self.getDriver("GV4"))
        self.speed = _int(self.getDriver("GV5"), 50)
        self.level = _int(self.getDriver("GV6"), 100) or 100
        self._worker = _Worker(address)
        self.poly.subscribe(self.poly.START, self.start, address)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        LOGGER.info("strip %s (%s) ready", self.name, self.mac)
        self.setDriver("GV7", LINK_UNKNOWN)

    def stop(self):
        self._worker.stop()

    # ── delivery ──────────────────────────────────────────────────────────────

    def _deliver(self, kind: str, frames: List[bytearray], on_ok: Callable[[], None]) -> None:
        def job():
            transport = self.controller.transport
            if transport is None:
                LOGGER.error("%s: no transport configured -- check the node server settings", self.name)
                self.setDriver("GV7", LINK_ERROR)
                return
            try:
                transport.write(self.mac, [bytes(f) for f in frames])
            except TransportError as exc:
                LOGGER.error("%s (%s): %s", self.name, self.mac, exc)
                self.setDriver("GV7", LINK_ERROR)
                self.controller.strip_failed(self, str(exc))
                return
            with self._lock:
                on_ok()
            self.setDriver("GV7", LINK_OK)
            self.controller.strip_ok(self)
            self._report()

        self._worker.submit(kind, job)

    def _report(self) -> None:
        self.setDriver("ST", self.level if self.power else 0)
        self.setDriver("GV1", self.rgb[0])
        self.setDriver("GV2", self.rgb[1])
        self.setDriver("GV3", self.rgb[2])
        self.setDriver("GV4", self.mode)
        self.setDriver("GV5", self.speed)
        self.setDriver("GV6", self.level)

    def _state_frames(self) -> List[bytearray]:
        """Everything needed to put the strip into our shadow state."""
        frames = [Pkt.power(self.power)]
        if not self.power:
            return frames
        frames.append(Pkt.brightness(self.level))
        if self.mode in MODE_BY_INDEX:
            frames += [Pkt.speed(self.speed), Pkt.hw_mode(MODE_BY_INDEX[self.mode])]
        else:
            frames.append(Pkt.color(*self.rgb))
        return frames

    # ── commands ──────────────────────────────────────────────────────────────

    def cmd_on(self, command=None):
        value = (command or {}).get("value")
        level = _clamp(_int(value), 0, 100) if value not in (None, "") else self.level
        if level == 0:
            return self.cmd_off(command)

        def ok():
            self.power, self.level = True, level

        self._deliver("power", [Pkt.power_on(), Pkt.brightness(level)], ok)

    def cmd_off(self, command=None):
        def ok():
            self.power = False

        self._deliver("power", [Pkt.power_off()], ok)

    def cmd_fast_on(self, command=None):
        self.cmd_on({"value": 100})

    def cmd_brt(self, command=None):
        self.cmd_on({"value": min(100, (self.level if self.power else 0) + DIM_STEP)})

    def cmd_dim(self, command=None):
        current = self.level if self.power else 0
        target = max(0, current - DIM_STEP)
        if target == 0:
            self.cmd_off()
        else:
            self.cmd_on({"value": target})

    def cmd_set_brightness(self, command):
        level = _clamp(_int(command.get("value"), self.level), 1, 100)

        def ok():
            self.level = level

        self._deliver("brightness", [Pkt.brightness(level)], ok)

    def _set_rgb(self, rgb: Tuple[int, int, int]) -> None:
        def ok():
            self.power, self.rgb, self.mode = True, rgb, 0

        self._deliver("color", [Pkt.power_on(), Pkt.color(*rgb)], ok)

    def cmd_set_color(self, command):
        rgb = tuple(_clamp(_int(_query_param(command, p, cur)), 0, 255)
                    for p, cur in zip(("R", "G", "B"), self.rgb))
        self._set_rgb(rgb)  # type: ignore[arg-type]

    def cmd_set_preset(self, command):
        index = _int(command.get("value"), -1)
        if not 0 <= index < len(PRESETS):
            LOGGER.error("%s: unknown preset %s", self.name, command.get("value"))
            return
        self._set_rgb(PRESETS[index][1])

    def cmd_set_ct(self, command):
        kelvin = _clamp(_int(command.get("value"), 4000), 1500, 9000)
        self._set_rgb(cct_to_rgb(kelvin))

    def cmd_set_mode(self, command):
        index = _int(command.get("value"), -1)
        if index == 0:
            return self._set_rgb(self.rgb)
        mode = MODE_BY_INDEX.get(index)
        if mode is None:
            LOGGER.error("%s: unknown effect %s", self.name, command.get("value"))
            return
        speed = self.speed

        def ok():
            self.power, self.mode = True, index

        # The firmware wants speed as its own frame, sent before the mode.
        self._deliver("color", [Pkt.power_on(), Pkt.speed(speed), Pkt.hw_mode(mode)], ok)

    def cmd_set_speed(self, command):
        speed = _clamp(_int(command.get("value"), self.speed), 0, 100)

        def ok():
            self.speed = speed

        self._deliver("speed", [Pkt.speed(speed)], ok)

    def cmd_resend(self, command=None):
        """Push the whole shadow state -- use after the strip lost power."""
        self._deliver("state", self._state_frames(), lambda: None)

    def query(self, command=None):
        self.reportDrivers()

    commands = {
        "DON": cmd_on,
        "DOF": cmd_off,
        "DFON": cmd_fast_on,
        "DFOF": cmd_off,
        "BRT": cmd_brt,
        "DIM": cmd_dim,
        "SETBRT": cmd_set_brightness,
        "SETCOLOR": cmd_set_color,
        "SETPRESET": cmd_set_preset,
        "SETCT": cmd_set_ct,
        "SETMODE": cmd_set_mode,
        "SETSPEED": cmd_set_speed,
        "RESEND": cmd_resend,
        "QUERY": query,
    }
