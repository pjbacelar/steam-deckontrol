# Architecture

Steam Deckontrol turns a Steam Deck into a **networked controller + dynamic
input surface** for a Steam Machine / SteamOS PC. This document covers the
Phase-1 MVP that lives in this repo and the path beyond it.

## Decision: Decky plugin first

We start as a **Decky plugin**, not a standalone app, because it gives the
fastest path to real usage: native Steam UI integration, auto-start with the
Steam session, and direct access to the Deck input stack. The cost is being
coupled to the Steam client lifecycle and having limited low-level control.

The mitigation — and the most important design rule here — is **input/display
separation**: the input bridge never depends on the Remote Play video stream.
That keeps the door open to extract the Deck-side capture+transport into a
standalone "input daemon" later (Phase 2+) if latency or multi-device scale
demands it, without rewriting the host or the protocol.

## Phase-1 data flow

```
  Steam Deck (Decky plugin)                          Steam Machine
  ┌─────────────────────────────┐                   ┌──────────────────────────┐
  │ input_capture (evdev)        │                   │ daemon (asyncio UDP)     │
  │   └─ live InputState         │                   │   ├─ DiscoveryProtocol   │
  │ main.py stream loop @250Hz   │   UDP 27970/71    │   ├─ ControlProtocol     │
  │   └─ snapshot ──► transport ─┼──────────────────►│   │    pair + seq filter │
  │ discovery (HELLO bcast)      │   binary frames   │   └─ virtual_controller  │
  │ React UI (status/pair/mode)  │                   │        uinput gamepad    │
  └─────────────────────────────┘                   └──────────┬───────────────┘
                                                                ▼
                                                          Steam Input → games
```

Both ends speak the same [wire protocol](./protocol/PROTOCOL.md). The
`protocol.py` module is the single source of truth, copied verbatim into each
runtime (`scripts/sync_protocol.sh`).

## Components

| Path                                  | Lang | Role                                                        |
| ------------------------------------- | ---- | ----------------------------------------------------------- |
| `plugin/src/index.tsx`                | TS   | Decky panel UI: scan, pair, status, input-only toggle       |
| `plugin/main.py`                      | Py   | Backend orchestrator + frontend RPC; the 250 Hz stream loop |
| `plugin/py_modules/input_capture.py`  | Py   | evdev → normalized `InputState`                             |
| `plugin/py_modules/transport.py`      | Py   | UDP client: pairing handshake, frame send, heartbeat        |
| `plugin/py_modules/discovery.py`      | Py   | LAN host discovery via broadcast                            |
| `host/deckontrol_host/daemon.py`      | Py   | UDP server: discovery + pairing + input ingest              |
| `host/deckontrol_host/virtual_controller.py` | Py | uinput virtual gamepad (+ optional mouse)              |
| `protocol/protocol.py`                | Py   | Canonical wire codec                                        |

### Why Python on both ends for the MVP

Decky backends are Python, so capture/transport reuse the protocol module with
zero FFI. The host daemon matches, so one codec serves both. The hot path is a
51-byte `struct.pack`/`unpack` — Python handles 250 Hz of that comfortably. If
profiling later shows the host or a standalone Deck daemon needs sub-frame
determinism, the transport + virtual-controller layers are the natural candidates
to re-implement in Rust behind the same protocol.

## Pairing & trust (Phase 1)

LAN discovery is a broadcast `HELLO`; hosts answer with their name/port. Pairing
presents a shared **token** (generated and printed by the host on first run).
The host only accepts input frames from the single currently-paired peer. This
is deliberately minimal — Phase 1 assumes a trusted home LAN; transport
encryption is a later concern.

## Reliability

- **Stateless reconnect**: nothing persists across a Steam/Decky restart; the
  plugin simply re-pairs. The host forgets a silent client after a timeout.
- **Lossy-tolerant transport**: UDP, newest-frame-wins, no retransmit (see
  PROTOCOL.md). A lost frame costs ≤ 1/250 s of staleness.

## Known on-device work (can't be validated in CI)

1. **Steam Input may grab the Deck controller**, hiding the full evdev surface.
   `input_capture._pick_device()` scores nodes to prefer the raw *Steam Deck*
   node; the exact `ABS_*`/`BTN_*` codes for gyro, trackpads and back buttons
   need confirming with `evtest` on hardware (isolated in clearly-marked tables).
2. **`/dev/uinput` permissions** on immutable SteamOS — see the udev rule in
   `host/packaging/`.
3. **Input-only display suppression** is currently a backend flag mirrored to
   the UI; wiring it to actually minimize/mute the Remote Play surface is a
   frontend SteamClient call to add once tested in a real session.

## Roadmap beyond MVP

- **Phase 2 — game-aware control**: active-game detection, per-game profiles,
  macro engine, richer UI.
- **Phase 3 — secondary screen**: trackpad surface mode, dynamic overlay panels.
- **Phase 4 — advanced**: multi-device, adaptive layouts, latency auto-tuning,
  haptic sync, optional standalone input daemon.
