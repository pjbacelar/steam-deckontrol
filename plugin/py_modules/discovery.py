"""LAN discovery (Deck side).

Broadcasts a HELLO on the discovery port and collects HELLO_ACK replies for a
short window. Returns the hosts found so the UI can present a pick list instead
of forcing the user to type an IP.

Falls back gracefully: if broadcast is blocked on the network, the user can
still pair manually with an IP + token.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import protocol as proto

log = logging.getLogger("deckontrol.discovery")

DEFAULT_DISCOVERY_PORT = 27971
DEFAULT_CONTROL_PORT = 27970


async def discover(discovery_port: int = DEFAULT_DISCOVERY_PORT, timeout_s: float = 1.5) -> list[dict]:
    """Return a list of {name, ip, port, busy, version} for hosts on the LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()

    found: dict[str, dict] = {}
    try:
        probe = proto.encode_control(proto.PKT_HELLO, {"version": proto.PROTOCOL_VERSION})
        sock.sendto(probe, ("255.255.255.255", discovery_port))

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, proto.MAX_PACKET), timeout=remaining)
            except asyncio.TimeoutError:
                break
            pkt = proto.parse(data)
            if pkt is None or pkt.type != proto.PKT_HELLO_ACK:
                continue
            payload = pkt.payload or {}
            ip = addr[0]
            found[ip] = {
                "name": payload.get("name", ip),
                "ip": ip,
                "port": payload.get("port", DEFAULT_CONTROL_PORT),
                "busy": bool(payload.get("busy", False)),
                "version": payload.get("version", 0),
            }
    finally:
        sock.close()

    return list(found.values())
