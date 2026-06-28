"""Virtual gamepad injection for the host daemon.

Creates a uinput device that presents as a standard gamepad so SteamOS / Steam
Input picks it up as a normal controller. Incoming :class:`InputState` frames
are translated into evdev events on this device.

We deliberately model an Xbox-360-style layout (BTN_SOUTH/EAST/.., ABS_X/Y,
ABS_RX/RY, ABS_Z/RZ triggers, ABS_HAT0X/Y dpad) because that is the most
universally understood gamepad shape on Linux and Steam maps it cleanly.

Deck-specific surfaces that have no gamepad equivalent (gyro, trackpads, the
back grip buttons) are handled here too:

* The right trackpad optionally drives the system mouse (relative motion) so
  "trackpad as mouse" works out of the box. This lives on a *second* uinput
  device (a mouse) because mixing pointer + gamepad capabilities on one node
  confuses some consumers.
* Back buttons L4/R4/L5/R5 are surfaced as extra gamepad buttons (BTN_TRIGGER_
  HAPPY1..4) so profiles can bind them later.

Requires python-evdev and write access to /dev/uinput (root, or a udev rule).
"""

from __future__ import annotations

import logging
from typing import Optional

try:
    from evdev import UInput, ecodes as e, AbsInfo
except ImportError:  # pragma: no cover - import guard for dev machines without evdev
    UInput = None
    e = None
    AbsInfo = None

from .protocol import InputState

log = logging.getLogger("deckontrol.vcontroller")

# int16 stick range matches the wire format, so no rescaling on the hot path.
_AXIS_MIN, _AXIS_MAX = -32767, 32767
_TRIGGER_MAX = 255

# Map our button names -> evdev key codes for the virtual gamepad.
def _button_map():
    return {
        "a": e.BTN_SOUTH,
        "b": e.BTN_EAST,
        "x": e.BTN_NORTH,
        "y": e.BTN_WEST,
        "l1": e.BTN_TL,
        "r1": e.BTN_TR,
        "l3": e.BTN_THUMBL,
        "r3": e.BTN_THUMBR,
        "start": e.BTN_START,
        "select": e.BTN_SELECT,
        "steam": e.BTN_MODE,
        # Back grip buttons -> spare gamepad buttons for later profile binding.
        "l4": e.BTN_TRIGGER_HAPPY1,
        "r4": e.BTN_TRIGGER_HAPPY2,
        "l5": e.BTN_TRIGGER_HAPPY3,
        "r5": e.BTN_TRIGGER_HAPPY4,
    }


class VirtualController:
    """Owns the uinput device(s) and applies InputState frames to them."""

    def __init__(self, name: str = "Steam Deck Controller (Network)", enable_mouse: bool = True):
        if UInput is None:
            raise RuntimeError(
                "python-evdev is not installed. Install it (pip install evdev) "
                "and ensure /dev/uinput is writable."
            )
        self._name = name
        self._enable_mouse = enable_mouse
        self._pad: Optional[UInput] = None
        self._mouse: Optional[UInput] = None
        self._btn = _button_map()
        # Track previous trackpad sample so we can emit *relative* mouse motion.
        self._last_rpad = None

    def open(self) -> None:
        axis = AbsInfo(value=0, min=_AXIS_MIN, max=_AXIS_MAX, fuzz=0, flat=16, resolution=0)
        trig = AbsInfo(value=0, min=0, max=_TRIGGER_MAX, fuzz=0, flat=0, resolution=0)
        hat = AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)

        capabilities = {
            e.EV_KEY: list(set(self._btn.values())),
            e.EV_ABS: [
                (e.ABS_X, axis), (e.ABS_Y, axis),
                (e.ABS_RX, axis), (e.ABS_RY, axis),
                (e.ABS_Z, trig), (e.ABS_RZ, trig),
                (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat),
            ],
        }
        # vendor/product chosen to look like a generic XInput pad.
        self._pad = UInput(capabilities, name=self._name, vendor=0x28de, product=0x11ff, version=0x0001)
        log.info("virtual gamepad ready: %s", self._name)

        if self._enable_mouse:
            mouse_caps = {
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
            }
            self._mouse = UInput(mouse_caps, name=f"{self._name} Pointer")
            log.info("virtual pointer ready")

    def close(self) -> None:
        for dev in (self._pad, self._mouse):
            if dev is not None:
                try:
                    dev.close()
                except Exception:  # pragma: no cover - best effort teardown
                    pass
        self._pad = None
        self._mouse = None

    def __enter__(self) -> "VirtualController":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- hot path -----------------------------------------------------------
    def apply(self, state: InputState) -> None:
        """Translate one normalized frame into evdev events and sync."""
        if self._pad is None:
            raise RuntimeError("VirtualController.open() must be called first")

        pad = self._pad

        # Buttons
        for name, code in self._btn.items():
            pad.write(e.EV_KEY, code, 1 if state.button(name) else 0)

        # Sticks (already normalized; rescale to int16 axis range)
        pad.write(e.EV_ABS, e.ABS_X, int(state.lx * _AXIS_MAX))
        pad.write(e.EV_ABS, e.ABS_Y, int(state.ly * _AXIS_MAX))
        pad.write(e.EV_ABS, e.ABS_RX, int(state.rx * _AXIS_MAX))
        pad.write(e.EV_ABS, e.ABS_RY, int(state.ry * _AXIS_MAX))

        # Analog triggers
        pad.write(e.EV_ABS, e.ABS_Z, int(state.lt * _TRIGGER_MAX))
        pad.write(e.EV_ABS, e.ABS_RZ, int(state.rt * _TRIGGER_MAX))

        # D-pad as a hat (-1/0/1 on each axis)
        hat_x = (1 if state.button("dpad_right") else 0) - (1 if state.button("dpad_left") else 0)
        hat_y = (1 if state.button("dpad_down") else 0) - (1 if state.button("dpad_up") else 0)
        pad.write(e.EV_ABS, e.ABS_HAT0X, hat_x)
        pad.write(e.EV_ABS, e.ABS_HAT0Y, hat_y)

        pad.syn()

        if self._mouse is not None:
            self._apply_mouse(state)

    def _apply_mouse(self, state: InputState) -> None:
        """Right trackpad -> relative mouse motion while touched."""
        touching = state.button("rtouch")
        if touching:
            if self._last_rpad is not None:
                dx = (state.rpad[0] - self._last_rpad[0]) * 1000.0
                dy = (state.rpad[1] - self._last_rpad[1]) * 1000.0
                if int(dx) or int(dy):
                    # Screen Y grows downward; trackpad Y grows upward -> invert.
                    self._mouse.write(e.EV_REL, e.REL_X, int(dx))
                    self._mouse.write(e.EV_REL, e.REL_Y, int(-dy))
                    self._mouse.syn()
            self._last_rpad = state.rpad
        else:
            self._last_rpad = None
