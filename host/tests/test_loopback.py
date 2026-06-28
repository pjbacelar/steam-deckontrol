"""End-to-end loopback test: Deck transport -> host daemon -> (fake) controller.

Runs the real control + discovery protocols and the real Deck-side Transport
over localhost UDP, with the uinput device replaced by a recording fake so the
test needs no /dev/uinput and no hardware.
"""

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))                       # host package
sys.path.insert(0, os.path.join(HERE, "..", "..", "plugin", "py_modules"))  # deck modules

from deckontrol_host import protocol as proto         # noqa: E402
from deckontrol_host.daemon import ControlProtocol, DiscoveryProtocol  # noqa: E402
from deckontrol_host.config import HostConfig         # noqa: E402
import transport as deck_transport                    # noqa: E402
import discovery as deck_discovery                    # noqa: E402


class FakeController:
    def __init__(self):
        self.frames = []

    def apply(self, state):
        self.frames.append(state)


async def _start_host(token="secret", port=0, discovery_port=0):
    cfg = HostConfig(name="test-host", token=token)
    controller = FakeController()
    loop = asyncio.get_running_loop()

    control = ControlProtocol(cfg, controller)
    ctrl_transport, _ = await loop.create_datagram_endpoint(
        lambda: control, local_addr=("127.0.0.1", port)
    )
    cfg.port = ctrl_transport.get_extra_info("sockname")[1]

    discovery = DiscoveryProtocol(cfg, control)
    disc_transport, _ = await loop.create_datagram_endpoint(
        lambda: discovery, local_addr=("127.0.0.1", discovery_port)
    )
    cfg.discovery_port = disc_transport.get_extra_info("sockname")[1]

    return cfg, control, controller, (ctrl_transport, disc_transport)


@pytest.mark.asyncio
async def test_pair_and_stream():
    cfg, control, controller, transports = await _start_host(token="hunter2")
    deck = deck_transport.Transport()
    try:
        ack = await deck.pair("127.0.0.1", cfg.port, "hunter2")
        assert ack["name"] == "test-host"
        assert deck.paired

        # Send a few frames with a known button pressed.
        state = proto.InputState(buttons={"a": True, "r4": True}, lx=0.5)
        for _ in range(5):
            deck.send_input(state)
        await asyncio.sleep(0.1)

        assert len(controller.frames) == 5
        assert controller.frames[-1].button("a")
        assert controller.frames[-1].button("r4")
        assert abs(controller.frames[-1].lx - 0.5) < 0.01
    finally:
        deck.disconnect()
        for t in transports:
            t.close()


@pytest.mark.asyncio
async def test_bad_token_rejected():
    cfg, control, controller, transports = await _start_host(token="right")
    deck = deck_transport.Transport()
    try:
        with pytest.raises(PermissionError):
            await deck.pair("127.0.0.1", cfg.port, "wrong")
        assert not deck.paired
        # Frames from an unpaired client must be ignored.
        deck._paired = True  # force a send despite rejection
        deck._host = ("127.0.0.1", cfg.port)
        deck._sock = deck._ensure_socket()
        deck.send_input(proto.InputState(buttons={"a": True}))
        await asyncio.sleep(0.05)
        assert len(controller.frames) == 0
    finally:
        deck.disconnect()
        for t in transports:
            t.close()


@pytest.mark.asyncio
async def test_stale_frames_dropped():
    cfg, control, controller, transports = await _start_host()
    deck = deck_transport.Transport()
    try:
        await deck.pair("127.0.0.1", cfg.port, cfg.token)
        # Send frames straight from the (already-paired) deck socket with
        # hand-picked sequence numbers so we control ordering.
        host_addr = ("127.0.0.1", cfg.port)
        deck._sock.sendto(proto.encode_input(proto.InputState(buttons={"a": True}), 10, 1), host_addr)
        await asyncio.sleep(0.05)
        before = len(controller.frames)
        assert before == 1

        # A lower seq is stale -> ignored.
        deck._sock.sendto(proto.encode_input(proto.InputState(buttons={"b": True}), 5, 2), host_addr)
        await asyncio.sleep(0.05)
        assert len(controller.frames) == before

        # A higher seq advances normally.
        deck._sock.sendto(proto.encode_input(proto.InputState(buttons={"x": True}), 11, 3), host_addr)
        await asyncio.sleep(0.05)
        assert len(controller.frames) == before + 1
        assert controller.frames[-1].button("x")
    finally:
        deck.disconnect()
        for t in transports:
            t.close()
