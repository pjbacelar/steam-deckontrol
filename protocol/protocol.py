"""Steam Deckontrol wire protocol (canonical reference implementation).

This module is the single source of truth for the UDP wire format spoken
between the Deck-side plugin backend and the host input daemon.

It is duplicated verbatim into both runtimes so each can be packaged
independently:

    plugin/py_modules/protocol.py        (Decky plugin backend)
    host/deckontrol_host/protocol.py     (host daemon)

If you change anything here, run ``scripts/sync_protocol.sh`` to propagate the
change, or copy by hand and keep PROTOCOL_VERSION in step.

Design notes
------------
* Two packet families share one datagram framing:

      MAGIC (4 bytes) | TYPE (1 byte) | PAYLOAD

  - Control packets (discovery / pairing / lifecycle) carry a JSON payload.
    They are rare, so verbosity does not matter and JSON keeps them easy to
    evolve and debug.
  - Input frames carry a fixed-size little-endian binary payload. They are the
    hot path (hundreds per second), so they are compact and allocation-free to
    decode.

* Every packet is self-describing via MAGIC + VERSION so a daemon can reject
  stray traffic on its port without crashing.

* Input frames carry a monotonically increasing ``seq`` and a millisecond
  ``timestamp`` so the receiver can drop reordered/stale frames (UDP makes no
  ordering guarantees) and measure one-way latency once clocks are synced.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Tuple

MAGIC = b"DKC1"
PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Packet types
# ---------------------------------------------------------------------------
PKT_HELLO = 0x01          # Deck -> broadcast : "who is a Deckontrol host?"  (JSON)
PKT_HELLO_ACK = 0x02      # Host -> Deck      : "I am, here is who I am"      (JSON)
PKT_PAIR_REQUEST = 0x03   # Deck -> Host      : request to pair w/ token      (JSON)
PKT_PAIR_ACK = 0x04       # Host -> Deck      : pairing accepted              (JSON)
PKT_PAIR_NACK = 0x05      # Host -> Deck      : pairing rejected (bad token)  (JSON)
PKT_INPUT = 0x10          # Deck -> Host      : one input frame               (binary)
PKT_HEARTBEAT = 0x20      # both ways         : keep-alive / liveness         (JSON)
PKT_DISCONNECT = 0x21     # both ways         : graceful teardown             (JSON)

_JSON_TYPES = {
    PKT_HELLO,
    PKT_HELLO_ACK,
    PKT_PAIR_REQUEST,
    PKT_PAIR_ACK,
    PKT_PAIR_NACK,
    PKT_HEARTBEAT,
    PKT_DISCONNECT,
}

# ---------------------------------------------------------------------------
# Button bitmask layout (uint32). Position == bit index.
# ---------------------------------------------------------------------------
BUTTONS = (
    "a", "b", "x", "y",
    "l1", "r1", "l2", "r2",          # l2/r2 == digital "fully pressed" trigger
    "l3", "r3",                       # stick clicks
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "start", "select", "steam", "quick_access",
    "l4", "r4", "l5", "r5",          # back grip buttons
    "ltouch", "rtouch",              # trackpad touch flags
)
_BIT = {name: i for i, name in enumerate(BUTTONS)}

# ---------------------------------------------------------------------------
# Binary framing
# ---------------------------------------------------------------------------
# Header: magic(4s) | type(B) | seq(uint32) | timestamp_ms(uint64)
_HEADER = struct.Struct("<4sBIQ")

# Input body (all little-endian, whitespace ignored by struct):
#   buttons   uint32
#   lx ly rx ry   int16  (sticks, -32767..32767)
#   lt rt     uint8  (analog triggers, 0..255)
#   gyro  x y z   int16  (angular velocity, raw sensor units)
#   accel x y z   int16  (acceleration, raw sensor units)
#   lpad  x y     int16  (left trackpad position, -32767..32767, 0 when untouched)
#   rpad  x y     int16  (right trackpad position)
_INPUT_BODY = struct.Struct("<I hhhh BB hhh hhh hh hh")

MAX_PACKET = 256  # every packet we emit fits comfortably; bounds receiver buffers.


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _to_i16(norm: float) -> int:
    """Normalized float (-1..1) -> int16."""
    return int(round(_clamp(norm, -1.0, 1.0) * 32767))


def _from_i16(raw: int) -> float:
    return _clamp(raw / 32767.0, -1.0, 1.0)


def _to_u8(norm: float) -> int:
    """Normalized float (0..1) -> uint8."""
    return int(round(_clamp(norm, 0.0, 1.0) * 255))


def _from_u8(raw: int) -> float:
    return _clamp(raw / 255.0, 0.0, 1.0)


@dataclass
class InputState:
    """A normalized snapshot of every Deck control at one instant.

    Sticks and trackpads are in -1..1, triggers in 0..1, buttons are bools.
    Gyro/accel are passed through as raw signed sensor units (consumers that
    care about real-world scale calibrate downstream).
    """

    buttons: dict = field(default_factory=dict)  # name -> bool
    lx: float = 0.0
    ly: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    lt: float = 0.0
    rt: float = 0.0
    gyro: Tuple[int, int, int] = (0, 0, 0)
    accel: Tuple[int, int, int] = (0, 0, 0)
    lpad: Tuple[float, float] = (0.0, 0.0)
    rpad: Tuple[float, float] = (0.0, 0.0)

    def button(self, name: str) -> bool:
        return bool(self.buttons.get(name, False))

    def _buttons_mask(self) -> int:
        mask = 0
        for name, pressed in self.buttons.items():
            bit = _BIT.get(name)
            if bit is not None and pressed:
                mask |= 1 << bit
        return mask

    @classmethod
    def _from_mask(cls, mask: int) -> dict:
        return {name: bool(mask & (1 << bit)) for name, bit in _BIT.items()}


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------
def encode_input(state: InputState, seq: int, timestamp_ms: int) -> bytes:
    header = _HEADER.pack(MAGIC, PKT_INPUT, seq & 0xFFFFFFFF, timestamp_ms & 0xFFFFFFFFFFFFFFFF)
    body = _INPUT_BODY.pack(
        state._buttons_mask(),
        _to_i16(state.lx), _to_i16(state.ly), _to_i16(state.rx), _to_i16(state.ry),
        _to_u8(state.lt), _to_u8(state.rt),
        state.gyro[0], state.gyro[1], state.gyro[2],
        state.accel[0], state.accel[1], state.accel[2],
        _to_i16(state.lpad[0]), _to_i16(state.lpad[1]),
        _to_i16(state.rpad[0]), _to_i16(state.rpad[1]),
    )
    return header + body


def encode_control(pkt_type: int, payload: Optional[dict] = None) -> bytes:
    if pkt_type not in _JSON_TYPES:
        raise ValueError(f"0x{pkt_type:02x} is not a JSON control packet type")
    body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    return MAGIC + bytes([pkt_type]) + body


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
@dataclass
class Packet:
    type: int
    # Exactly one of the following is populated depending on ``type``:
    seq: int = 0
    timestamp_ms: int = 0
    state: Optional[InputState] = None
    payload: Optional[dict] = None


def parse(data: bytes) -> Optional[Packet]:
    """Parse one datagram. Returns None for anything that isn't ours."""
    if len(data) < 5 or data[:4] != MAGIC:
        return None
    pkt_type = data[4]

    if pkt_type in _JSON_TYPES:
        try:
            payload = json.loads(data[5:].decode("utf-8")) if len(data) > 5 else {}
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return Packet(type=pkt_type, payload=payload)

    if pkt_type == PKT_INPUT:
        if len(data) < _HEADER.size + _INPUT_BODY.size:
            return None
        _magic, _type, seq, ts = _HEADER.unpack_from(data, 0)
        (mask, lx, ly, rx, ry, lt, rt,
         gx, gy, gz, ax, ay, az,
         lpx, lpy, rpx, rpy) = _INPUT_BODY.unpack_from(data, _HEADER.size)
        state = InputState(
            buttons=InputState._from_mask(mask),
            lx=_from_i16(lx), ly=_from_i16(ly), rx=_from_i16(rx), ry=_from_i16(ry),
            lt=_from_u8(lt), rt=_from_u8(rt),
            gyro=(gx, gy, gz), accel=(ax, ay, az),
            lpad=(_from_i16(lpx), _from_i16(lpy)),
            rpad=(_from_i16(rpx), _from_i16(rpy)),
        )
        return Packet(type=PKT_INPUT, seq=seq, timestamp_ms=ts, state=state)

    return None


def type_name(pkt_type: int) -> str:
    for name, value in globals().items():
        if name.startswith("PKT_") and value == pkt_type:
            return name
    return f"0x{pkt_type:02x}"
