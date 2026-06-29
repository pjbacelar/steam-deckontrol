# steam-deckontrol

A fair attempt at making the Steam Deck an awesome networked controller /
second screen for a Steam Machine.

This repo contains the **Phase-1 MVP**: a low-latency input bridge that streams
every Deck control over the LAN to a host PC, where it appears as a normal
virtual gamepad.

```
Steam Deck (Decky plugin)  ──UDP──►  Steam Machine (host daemon)  ──uinput──►  games
```

## What works today

- **Decky plugin** — captures Deck input (buttons, sticks, triggers, d-pad;
  trackpad/gyro fields are in the wire format) and streams it at 250 Hz.
- **Host daemon** — receives frames and injects a virtual *Steam Deck Controller
  (Network)* gamepad via `uinput`, plus an optional trackpad-driven mouse.
- **LAN discovery + token pairing** — scan for hosts, or pair manually by IP.
- **Input-only mode toggle** — forward input independently of any video stream.
- **Binary UDP protocol** — 51-byte input frames, newest-frame-wins, with a
  round-trip-tested codec ([`protocol/PROTOCOL.md`](protocol/PROTOCOL.md)).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design and the
Decky-vs-standalone decision, and the Phase 2–4 roadmap.

## Layout

```
plugin/    Decky plugin — TS/React UI + Python backend (Deck side)
host/      deckontrol-host daemon — virtual controller injection (PC side)
protocol/  Canonical wire protocol + spec
scripts/   sync_protocol.sh
```

## Quick start

**Host (Steam Machine):**

```bash
pip install --user ./host
# allow uinput without root (see host/README.md), then:
deckontrol-host            # prints the pairing token
```

**Deck:** build the plugin and install it under Decky:

```bash
cd plugin
npm install && npm run build      # outputs dist/index.js
# copy this folder to ~/homebrew/plugins/ (or use the Decky CLI), then open
# the plugin, Scan LAN, pick the host, enter the token, Connect.
```

## Tests

```bash
cd host && python -m pytest        # loopback + full-stack integration
cd plugin && npm run typecheck     # frontend type safety
```

The host test suite includes a **hardware-free full-stack integration test**
(`tests/test_integration.py`): it runs the real discovery handler, pairing
handshake, binary frames over real loopback UDP, daemon stale-frame filtering,
and the actual `VirtualController.apply()` / `InputCapture` mapping using real
`evdev` constants — replacing only the kernel `uinput` device with a recorder.

> **Note:** the one thing no CI/sandbox can exercise is the final kernel
> `uinput` syscall and reading real Deck hardware — those need an actual
> Deck + PC. Everything up to that boundary is tested. Hardware-tuning spots
> (Steam Input grabbing, exact evdev codes) are flagged in `ARCHITECTURE.md`.
