"""Deck-side UDP transport: pairing handshake + input frame streaming.

Owns a single UDP socket to the host. Responsibilities:

* perform the token pairing handshake (PAIR_REQUEST -> PAIR_ACK/NACK);
* send input frames with a monotonic sequence number and millisecond timestamp;
* send periodic heartbeats and watch for host replies to detect a dead link;
* expose lightweight stats (frames sent, last RTT) for the UI.

The transport is *stateless to reconnect*: if the link drops, the daemon side
forgets us after a timeout and we simply re-pair. Nothing here persists across
a Steam/Decky restart, which matches the "stateless reconnect" mitigation in
the design.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import protocol as proto

log = logging.getLogger("deckontrol.transport")

HEARTBEAT_INTERVAL_S = 1.0
PAIR_TIMEOUT_S = 2.0


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class Transport:
    def __init__(self):
        self._sock: socket.socket | None = None
        self._host: tuple[str, int] | None = None
        self._seq = 0
        self._paired = False
        self._frames_sent = 0
        self._last_ack_ms = 0
        self._host_name = ""
        self._lock = asyncio.Lock()

    @property
    def paired(self) -> bool:
        return self._paired

    @property
    def host_name(self) -> str:
        return self._host_name

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    def _ensure_socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
        return self._sock

    async def pair(self, ip: str, port: int, token: str) -> dict:
        """Run the pairing handshake. Returns the host's PAIR_ACK payload.

        Raises TimeoutError if the host never answers, or PermissionError if it
        rejects the token.
        """
        async with self._lock:
            sock = self._ensure_socket()
            self._host = (ip, port)
            loop = asyncio.get_running_loop()
            sock.sendto(proto.encode_control(proto.PKT_PAIR_REQUEST, {"token": token}), self._host)

            deadline = _now_ms() + int(PAIR_TIMEOUT_S * 1000)
            while _now_ms() < deadline:
                try:
                    data = await asyncio.wait_for(loop.sock_recv(sock, proto.MAX_PACKET), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                pkt = proto.parse(data)
                if pkt is None:
                    continue
                if pkt.type == proto.PKT_PAIR_ACK:
                    self._paired = True
                    self._last_ack_ms = _now_ms()
                    self._host_name = (pkt.payload or {}).get("name", ip)
                    log.info("paired with host %s (%s)", self._host_name, ip)
                    return pkt.payload or {}
                if pkt.type == proto.PKT_PAIR_NACK:
                    raise PermissionError((pkt.payload or {}).get("reason", "rejected"))
            raise TimeoutError("host did not respond to pairing request")

    def send_input(self, state: proto.InputState) -> None:
        """Fire-and-forget one input frame. Cheap and non-blocking."""
        if not self._paired or self._sock is None or self._host is None:
            return
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        data = proto.encode_input(state, self._seq, _now_ms())
        try:
            self._sock.sendto(data, self._host)
            self._frames_sent += 1
        except (BlockingIOError, OSError):
            pass  # UDP send buffer full — dropping a frame is fine, the next one is fresh.

    async def heartbeat_loop(self) -> None:
        """Send heartbeats and treat prolonged silence as a dropped link."""
        loop = asyncio.get_running_loop()
        while self._paired and self._sock is not None and self._host is not None:
            try:
                self._sock.sendto(proto.encode_control(proto.PKT_HEARTBEAT, {}), self._host)
            except OSError:
                pass
            try:
                data = await asyncio.wait_for(loop.sock_recv(self._sock, proto.MAX_PACKET), timeout=HEARTBEAT_INTERVAL_S)
                pkt = proto.parse(data)
                if pkt and pkt.type == proto.PKT_HEARTBEAT:
                    self._last_ack_ms = _now_ms()
            except asyncio.TimeoutError:
                pass
            if _now_ms() - self._last_ack_ms > 5000:
                log.warning("host went silent — marking unpaired")
                self._paired = False
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def disconnect(self) -> None:
        if self._sock is not None and self._host is not None and self._paired:
            try:
                self._sock.sendto(proto.encode_control(proto.PKT_DISCONNECT, {}), self._host)
            except OSError:
                pass
        self._paired = False
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._host = None
