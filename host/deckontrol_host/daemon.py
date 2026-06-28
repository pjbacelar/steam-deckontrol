"""The host input daemon.

Listens on two UDP sockets:

* the discovery port, answering HELLO broadcasts so the Deck can find this host
  on the LAN without anyone typing an IP address; and
* the main control/input port, handling token pairing and then translating the
  input-frame stream onto a virtual controller.

State machine per client is intentionally tiny: a single "currently paired
peer" identified by address. A new valid pairing supersedes the old one (you
only drive one host from one Deck at a time in Phase 1). Stale/forged frames
are ignored — only datagrams from the paired peer reach the virtual device.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import protocol as proto
from .config import HostConfig, CLIENT_TIMEOUT_S, STALE_FRAME_WINDOW
from .virtual_controller import VirtualController

log = logging.getLogger("deckontrol.daemon")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class _Client:
    def __init__(self, addr):
        self.addr = addr
        self.last_seq = -1
        self.last_seen_ms = _now_ms()
        self.frames = 0


class ControlProtocol(asyncio.DatagramProtocol):
    """Handles the main port: pairing + input frames."""

    def __init__(self, cfg: HostConfig, controller: VirtualController):
        self.cfg = cfg
        self.controller = controller
        self.client: _Client | None = None
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport
        log.info("control channel listening on udp/%d", self.cfg.port)

    def datagram_received(self, data, addr):
        pkt = proto.parse(data)
        if pkt is None:
            return

        if pkt.type == proto.PKT_PAIR_REQUEST:
            self._handle_pair(pkt, addr)
        elif pkt.type == proto.PKT_INPUT:
            self._handle_input(pkt, addr)
        elif pkt.type == proto.PKT_HEARTBEAT:
            if self._is_paired(addr):
                self.client.last_seen_ms = _now_ms()
                self._send(proto.PKT_HEARTBEAT, {}, addr)
        elif pkt.type == proto.PKT_DISCONNECT:
            if self._is_paired(addr):
                log.info("client %s disconnected", addr)
                self.client = None

    # -- handlers -----------------------------------------------------------
    def _handle_pair(self, pkt, addr):
        token = (pkt.payload or {}).get("token", "")
        if token != self.cfg.token:
            log.warning("rejected pairing from %s (bad token)", addr)
            self._send(proto.PKT_PAIR_NACK, {"reason": "bad_token"}, addr)
            return
        self.client = _Client(addr)
        log.info("paired with %s", addr)
        self._send(proto.PKT_PAIR_ACK, {
            "name": self.cfg.name,
            "controller": self.cfg.controller_name,
            "heartbeat_interval_ms": 1000,
        }, addr)

    def _handle_input(self, pkt, addr):
        if not self._is_paired(addr):
            return  # ignore frames from anyone we haven't paired with
        client = self.client
        client.last_seen_ms = _now_ms()
        # Drop stale / reordered frames. seq is monotonic; allow wrap.
        if client.last_seq >= 0:
            delta = (pkt.seq - client.last_seq) & 0xFFFFFFFF
            if delta == 0 or delta > STALE_FRAME_WINDOW:
                return
        client.last_seq = pkt.seq
        client.frames += 1
        try:
            self.controller.apply(pkt.state)
        except Exception as exc:  # pragma: no cover - device write failures
            log.error("failed to apply input frame: %s", exc)

    # -- helpers ------------------------------------------------------------
    def _is_paired(self, addr) -> bool:
        return self.client is not None and self.client.addr == addr

    def _send(self, pkt_type, payload, addr):
        if self.transport is not None:
            self.transport.sendto(proto.encode_control(pkt_type, payload), addr)

    def check_timeout(self):
        if self.client and _now_ms() - self.client.last_seen_ms > CLIENT_TIMEOUT_S * 1000:
            log.info("client %s timed out", self.client.addr)
            self.client = None


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Handles the discovery port: answers HELLO broadcasts."""

    def __init__(self, cfg: HostConfig, control: ControlProtocol):
        self.cfg = cfg
        self.control = control
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport
        log.info("discovery channel listening on udp/%d", self.cfg.discovery_port)

    def datagram_received(self, data, addr):
        pkt = proto.parse(data)
        if pkt is None or pkt.type != proto.PKT_HELLO:
            return
        log.debug("discovery probe from %s", addr)
        reply = proto.encode_control(proto.PKT_HELLO_ACK, {
            "name": self.cfg.name,
            "port": self.cfg.port,
            "version": proto.PROTOCOL_VERSION,
            "busy": self.control.client is not None,
        })
        self.transport.sendto(reply, addr)


async def run(cfg: HostConfig) -> None:
    loop = asyncio.get_running_loop()
    with VirtualController(cfg.controller_name, enable_mouse=cfg.enable_mouse) as controller:
        control = ControlProtocol(cfg, controller)
        await loop.create_datagram_endpoint(lambda: control, local_addr=("0.0.0.0", cfg.port))

        discovery = DiscoveryProtocol(cfg, control)
        await loop.create_datagram_endpoint(
            lambda: discovery, local_addr=("0.0.0.0", cfg.discovery_port), allow_broadcast=True
        )

        log.info("deckontrol host '%s' ready — pairing token: %s", cfg.name, cfg.token)

        # Liveness sweep: reap timed-out clients.
        try:
            while True:
                await asyncio.sleep(1.0)
                control.check_timeout()
        except asyncio.CancelledError:
            pass
