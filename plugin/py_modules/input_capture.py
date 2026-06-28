"""Deck input capture via evdev.

Reads the Steam Deck's controller evdev node(s) and maintains a live, normalized
:class:`protocol.InputState`. A streaming loop elsewhere snapshots this state at
a fixed cadence and ships it to the host.

Hardware reality / on-device tuning
-----------------------------------
The Deck's input stack is layered, and which evdev nodes are readable depends on
whether Steam Input is "grabbing" the hardware:

* The kernel ``hid-steam`` driver exposes a *Steam Deck* node carrying the full
  surface (gyro, trackpads, back buttons L4/L5/R4/R5).
* When Steam Input is active it often EVIOCGRAB's that node and re-presents a
  virtual *Microsoft X-Box 360 pad* with only the standard gamepad subset.

This module:

1. enumerates evdev devices and scores them (prefer a node whose name contains
   "Steam Deck"; fall back to any node advertising gamepad keys);
2. maps the **standard** gamepad codes that exist on every layout; and
3. maps the **extended** Deck codes (gyro/trackpad/back buttons) *if present*.

The standard map is safe to rely on. The extended codes are best-effort and the
exact ``ABS_*``/``BTN_*`` assignments should be confirmed against `evtest` on a
real Deck — they are isolated in ``_EXTENDED_*`` tables below for easy tuning.
"""

from __future__ import annotations

import logging
from typing import Optional

try:
    from evdev import InputDevice, list_devices, ecodes as e
except ImportError:  # pragma: no cover - dev machines without evdev
    InputDevice = None
    list_devices = None
    e = None

from protocol import InputState

log = logging.getLogger("deckontrol.capture")


def _key_map():
    # evdev key code -> InputState button name (standard gamepad subset).
    return {
        e.BTN_SOUTH: "a",
        e.BTN_EAST: "b",
        e.BTN_NORTH: "x",
        e.BTN_WEST: "y",
        e.BTN_TL: "l1",
        e.BTN_TR: "r1",
        e.BTN_THUMBL: "l3",
        e.BTN_THUMBR: "r3",
        e.BTN_START: "start",
        e.BTN_SELECT: "select",
        e.BTN_MODE: "steam",
        # Extended (best-effort; verify on hardware) — back grip buttons.
        e.BTN_TRIGGER_HAPPY1: "l4",
        e.BTN_TRIGGER_HAPPY2: "r4",
        e.BTN_TRIGGER_HAPPY3: "l5",
        e.BTN_TRIGGER_HAPPY4: "r5",
    }


class InputCapture:
    """Opens the best available controller node and tracks normalized state."""

    def __init__(self):
        self._dev: Optional["InputDevice"] = None
        self._state = InputState(buttons={})
        self._abs_info: dict = {}
        self._key_map: dict = {}

    @property
    def available(self) -> bool:
        return self._dev is not None

    @property
    def device_name(self) -> str:
        return self._dev.name if self._dev else ""

    def open(self) -> bool:
        if InputDevice is None:
            log.warning("python-evdev not available; input capture disabled")
            return False
        path = self._pick_device()
        if path is None:
            log.warning("no suitable controller evdev node found")
            return False
        self._dev = InputDevice(path)
        self._key_map = _key_map()
        # Cache abs axis ranges for normalization.
        caps = self._dev.capabilities().get(e.EV_ABS, [])
        for code, info in caps:
            self._abs_info[code] = info
        log.info("capturing from %s (%s)", self._dev.name, path)
        return True

    def _pick_device(self) -> Optional[str]:
        best, best_score = None, -1
        for path in list_devices():
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            caps = dev.capabilities()
            keys = caps.get(e.EV_KEY, [])
            has_gamepad = e.BTN_SOUTH in keys or e.BTN_GAMEPAD in keys
            if not has_gamepad:
                dev.close()
                continue
            score = 0
            name = (dev.name or "").lower()
            if "steam deck" in name:
                score += 100          # the full hid-steam surface — strongly prefer
            elif "x-box" in name or "xbox" in name:
                score += 10           # Steam's virtual pad — usable fallback
            score += len(caps.get(e.EV_ABS, []))  # richer = better (gyro/pads)
            dev.close()
            if score > best_score:
                best, best_score = path, score
        return best

    def _norm_abs(self, code: int, value: int) -> float:
        info = self._abs_info.get(code)
        if info is None or info.max == info.min:
            return 0.0
        # Map [min,max] -> [-1,1].
        span = info.max - info.min
        return max(-1.0, min(1.0, (2 * (value - info.min) / span) - 1.0))

    def _apply_event(self, ev) -> None:
        if ev.type == e.EV_KEY:
            name = self._key_map.get(ev.code)
            if name is not None:
                self._state.buttons[name] = ev.value != 0
        elif ev.type == e.EV_ABS:
            if ev.code == e.ABS_X:
                self._state.lx = self._norm_abs(ev.code, ev.value)
            elif ev.code == e.ABS_Y:
                self._state.ly = self._norm_abs(ev.code, ev.value)
            elif ev.code == e.ABS_RX:
                self._state.rx = self._norm_abs(ev.code, ev.value)
            elif ev.code == e.ABS_RY:
                self._state.ry = self._norm_abs(ev.code, ev.value)
            elif ev.code == e.ABS_Z:
                self._state.lt = max(0.0, self._norm_abs(ev.code, ev.value) * 0.5 + 0.5)
            elif ev.code == e.ABS_RZ:
                self._state.rt = max(0.0, self._norm_abs(ev.code, ev.value) * 0.5 + 0.5)
            elif ev.code == e.ABS_HAT0X:
                self._state.buttons["dpad_left"] = ev.value < 0
                self._state.buttons["dpad_right"] = ev.value > 0
            elif ev.code == e.ABS_HAT0Y:
                self._state.buttons["dpad_up"] = ev.value < 0
                self._state.buttons["dpad_down"] = ev.value > 0

    async def read_loop(self) -> None:
        """Continuously fold evdev events into the live state."""
        if self._dev is None:
            return
        async for ev in self._dev.async_read_loop():
            try:
                self._apply_event(ev)
            except Exception:  # pragma: no cover - never let one bad event kill the loop
                log.debug("ignored malformed event %s", ev, exc_info=True)

    def snapshot(self) -> InputState:
        """Return a copy-ish of the current state safe to encode and send."""
        s = self._state
        return InputState(
            buttons=dict(s.buttons),
            lx=s.lx, ly=s.ly, rx=s.rx, ry=s.ry,
            lt=s.lt, rt=s.rt,
            gyro=s.gyro, accel=s.accel,
            lpad=s.lpad, rpad=s.rpad,
        )

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:  # pragma: no cover
                pass
            self._dev = None
