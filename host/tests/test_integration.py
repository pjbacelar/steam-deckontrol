"""Full-stack integration tests — real pipeline, hardware-free.

These run the REAL code paths over REAL loopback UDP sockets:

  discovery handler · pairing handshake · binary frame encode/decode ·
  daemon routing + stale-frame filtering · VirtualController.apply() mapping ·
  InputCapture event mapping

Only the kernel ``uinput`` *device* is replaced by a recording fake, because
CI / sandboxes have no input subsystem. We assert on the exact evdev events the
daemon would have written, using the real ``evdev.ecodes`` constants.
"""

import asyncio
import os
import socket
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "plugin", "py_modules"))

evdev = pytest.importorskip("evdev")
from evdev import ecodes as E, AbsInfo  # noqa: E402

import deckontrol_host.virtual_controller as vc  # noqa: E402


class FakeUInput:
    """Records evdev writes instead of opening /dev/uinput."""

    def __init__(self, capabilities=None, name="", **kw):
        self.name = name
        self.events = []
        self.syns = 0

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.syns += 1

    def close(self):
        pass

    def last(self, etype, code):
        for t, c, v in reversed(self.events):
            if t == etype and c == code:
                return v
        return None


@pytest.fixture
def patch_uinput(monkeypatch):
    monkeypatch.setattr(vc, "UInput", FakeUInput)


async def _start_host(token="tok123"):
    from deckontrol_host.daemon import ControlProtocol, DiscoveryProtocol
    from deckontrol_host.config import HostConfig
    from deckontrol_host.virtual_controller import VirtualController

    cfg = HostConfig(name="ci-host", token=token)
    controller = VirtualController(cfg.controller_name, enable_mouse=True)
    controller.open()
    loop = asyncio.get_running_loop()

    control = ControlProtocol(cfg, controller)
    ctrl_t, _ = await loop.create_datagram_endpoint(lambda: control, local_addr=("127.0.0.1", 0))
    cfg.port = ctrl_t.get_extra_info("sockname")[1]

    disc = DiscoveryProtocol(cfg, control)
    disc_t, _ = await loop.create_datagram_endpoint(lambda: disc, local_addr=("127.0.0.1", 0))
    cfg.discovery_port = disc_t.get_extra_info("sockname")[1]
    return cfg, control, controller, (ctrl_t, disc_t)


@pytest.mark.asyncio
async def test_discovery_handler(patch_uinput):
    import deckontrol_host.protocol as proto

    cfg, control, controller, transports = await _start_host()
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(proto.encode_control(proto.PKT_HELLO, {"version": 1}), ("127.0.0.1", cfg.discovery_port))
        data = await asyncio.wait_for(loop.sock_recv(s, 256), timeout=1.0)
        pkt = proto.parse(data)
        assert pkt.type == proto.PKT_HELLO_ACK
        assert pkt.payload["name"] == "ci-host"
        assert pkt.payload["port"] == cfg.port
    finally:
        s.close()
        for t in transports:
            t.close()
        controller.close()


@pytest.mark.asyncio
async def test_button_and_axis_injection(patch_uinput):
    import deckontrol_host.protocol as proto
    import transport as deck_transport

    cfg, control, controller, transports = await _start_host()
    deck = deck_transport.Transport()
    try:
        await deck.pair("127.0.0.1", cfg.port, "tok123")
        pad = controller._pad

        pad.events.clear()
        deck.send_input(proto.InputState(buttons={"a": True, "r4": True, "steam": True}, lx=0.5, ly=-1.0, rt=1.0))
        await asyncio.sleep(0.05)

        assert pad.last(E.EV_KEY, E.BTN_SOUTH) == 1
        assert pad.last(E.EV_KEY, E.BTN_TRIGGER_HAPPY2) == 1   # R4
        assert pad.last(E.EV_KEY, E.BTN_MODE) == 1             # Steam
        assert pad.last(E.EV_KEY, E.BTN_EAST) == 0             # B not pressed
        assert abs(pad.last(E.EV_ABS, E.ABS_X) - 16383) <= 2   # lx 0.5
        assert pad.last(E.EV_ABS, E.ABS_Y) == -32767           # ly -1.0
        assert pad.last(E.EV_ABS, E.ABS_RZ) == 255             # rt 1.0
        assert pad.syns >= 1
    finally:
        deck.disconnect()
        for t in transports:
            t.close()
        controller.close()


@pytest.mark.asyncio
async def test_dpad_hat_and_trackpad_mouse(patch_uinput):
    import deckontrol_host.protocol as proto
    import transport as deck_transport

    cfg, control, controller, transports = await _start_host()
    deck = deck_transport.Transport()
    try:
        await deck.pair("127.0.0.1", cfg.port, "tok123")
        pad, mouse = controller._pad, controller._mouse

        pad.events.clear()
        deck.send_input(proto.InputState(buttons={"dpad_left": True, "dpad_up": True}))
        await asyncio.sleep(0.05)
        assert pad.last(E.EV_ABS, E.ABS_HAT0X) == -1
        assert pad.last(E.EV_ABS, E.ABS_HAT0Y) == -1

        mouse.events.clear()
        deck.send_input(proto.InputState(buttons={"rtouch": True}, rpad=(0.10, 0.10)))
        await asyncio.sleep(0.03)
        deck.send_input(proto.InputState(buttons={"rtouch": True}, rpad=(0.12, 0.12)))
        await asyncio.sleep(0.05)
        assert mouse.last(E.EV_REL, E.REL_X) > 0    # rightward swipe
        assert mouse.last(E.EV_REL, E.REL_Y) < 0    # upward swipe inverts to screen
    finally:
        deck.disconnect()
        for t in transports:
            t.close()
        controller.close()


@pytest.mark.asyncio
async def test_capture_event_mapping():
    """Deck-side: real InputCapture._apply_event folds evdev events into state."""
    import input_capture as deck_capture

    cap = deck_capture.InputCapture()
    cap._key_map = deck_capture._key_map()
    cap._abs_info = {E.ABS_X: AbsInfo(0, -32768, 32767, 0, 0, 0)}

    class Ev:
        def __init__(self, t, c, v):
            self.type, self.code, self.value = t, c, v

    cap._apply_event(Ev(E.EV_KEY, E.BTN_SOUTH, 1))
    cap._apply_event(Ev(E.EV_ABS, E.ABS_X, 16383))
    cap._apply_event(Ev(E.EV_ABS, E.ABS_HAT0X, -1))

    snap = cap.snapshot()
    assert snap.button("a")
    assert abs(snap.lx - 0.5) < 0.01
    assert snap.button("dpad_left") and not snap.button("dpad_right")
