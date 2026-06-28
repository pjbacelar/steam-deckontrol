# Deckontrol Wire Protocol v1

UDP, little-endian. One logical message per datagram. The reference
implementation is [`protocol.py`](./protocol.py) — this document describes what
that code encodes.

## Framing

Every datagram begins with:

```
+--------+--------+-----------------+
| MAGIC  | TYPE   | PAYLOAD ...     |
| "DKC1" | 1 byte |                 |
+--------+--------+-----------------+
```

`MAGIC` lets a listener cheaply reject stray traffic on its port. There are two
payload families.

## Control packets (JSON payload)

Low-frequency messages for discovery, pairing and lifecycle. Payload is a UTF-8
JSON object.

| Type   | Name           | Direction       | Payload                                   |
| ------ | -------------- | --------------- | ----------------------------------------- |
| `0x01` | `HELLO`        | Deck → bcast    | `{version}`                               |
| `0x02` | `HELLO_ACK`    | Host → Deck     | `{name, port, version, busy}`             |
| `0x03` | `PAIR_REQUEST` | Deck → Host     | `{token}`                                 |
| `0x04` | `PAIR_ACK`     | Host → Deck     | `{name, controller, heartbeat_interval_ms}` |
| `0x05` | `PAIR_NACK`    | Host → Deck     | `{reason}`                                |
| `0x20` | `HEARTBEAT`    | both            | `{}`                                      |
| `0x21` | `DISCONNECT`   | both            | `{}`                                      |

## Input frame (`0x10`, binary payload)

The hot path. Fixed-size, allocation-free to decode. After `MAGIC|TYPE`:

| Field         | Type     | Bytes | Notes                                   |
| ------------- | -------- | ----- | --------------------------------------- |
| `seq`         | uint32   | 4     | monotonic; used for stale/reorder drop  |
| `timestamp`   | uint64   | 8     | sender monotonic ms                     |
| `buttons`     | uint32   | 4     | bitmask, see below                      |
| `lx,ly,rx,ry` | int16×4  | 8     | sticks, −32767..32767                   |
| `lt,rt`       | uint8×2  | 2     | triggers, 0..255                        |
| `gyro x,y,z`  | int16×3  | 6     | angular velocity, raw sensor units      |
| `accel x,y,z` | int16×3  | 6     | acceleration, raw sensor units          |
| `lpad x,y`    | int16×2  | 4     | left trackpad, −32767..32767, 0=untouched |
| `rpad x,y`    | int16×2  | 4     | right trackpad                          |

Total: **51 bytes** on the wire.

### Button bitmask

Bit index = position in this list:

```
0  a            6  l2           12 dpad_left     18 l4
1  b            7  r2           13 dpad_right    19 r4
2  x            8  l3           14 start         20 l5
3  y            9  r3           15 select        21 r5
4  l1           10 dpad_up      16 steam         22 ltouch
5  r1           11 dpad_down    17 quick_access  23 rtouch
```

## Reliability model

UDP is unordered and lossy by design — acceptable because input is a
continuously refreshed signal, not a log. The receiver:

- drops datagrams lacking `MAGIC` or with an unknown `TYPE`;
- drops input frames whose `seq` is not strictly newer than the last accepted
  (with wrap tolerance), discarding reordered/duplicate frames;
- treats a paired client as gone after `CLIENT_TIMEOUT_S` of silence.

A dropped frame costs at most `1/STREAM_HZ` seconds of staleness; the next frame
carries the full current state, so no recovery/retransmit is needed.

## Versioning

`PROTOCOL_VERSION` is advertised in `HELLO`/`HELLO_ACK`. Bump it on any layout
change and gate behaviour on the negotiated version.
