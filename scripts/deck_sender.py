#!/usr/bin/env python3
"""Standalone Deck-side sender — hardware bring-up tool.

Runs the same capture + transport pipeline as the Decky plugin backend, but as
a plain CLI you can launch from a terminal in Desktop Mode. Use this to validate
the real network -> virtual-controller path on actual hardware *before* dealing
with Decky plugin packaging.

Usage (on the Steam Deck, in a venv with evdev installed):

    python scripts/deck_sender.py --discover           # find hosts on the LAN
    python scripts/deck_sender.py --ip 192.168.1.50 --token abcdef0123456789

    # Just list/inspect input devices to see what's readable:
    python scripts/deck_sender.py --list-devices
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "py_modules"))

import discovery  # noqa: E402
from input_capture import InputCapture  # noqa: E402
from transport import Transport  # noqa: E402

STREAM_HZ = 250


def list_devices() -> int:
    try:
        from evdev import InputDevice, list_devices as ld, ecodes as e
    except ImportError:
        print("python-evdev is not installed in this environment.", file=sys.stderr)
        return 1
    paths = ld()
    if not paths:
        print("No /dev/input/event* devices visible (permissions? not in 'input' group?).")
        return 1
    for path in paths:
        try:
            dev = InputDevice(path)
        except OSError as exc:
            print(f"{path}: <cannot open: {exc}>")
            continue
        keys = dev.capabilities().get(e.EV_KEY, [])
        is_pad = e.BTN_SOUTH in keys or e.BTN_GAMEPAD in keys
        print(f"{path}: {dev.name!r}{'  [gamepad]' if is_pad else ''}")
        dev.close()
    return 0


async def run(ip: str, port: int, token: str, discover: bool) -> int:
    if discover:
        print("scanning LAN for hosts...")
        hosts = await discovery.discover()
        if not hosts:
            print("no hosts found. Is deckontrol-host running on the PC?")
            return 1
        for h in hosts:
            print(f"  {h['name']:<20} {h['ip']}:{h['port']}{'  (busy)' if h['busy'] else ''}")
        if not ip:
            ip = hosts[0]["ip"]
            port = hosts[0]["port"]
            print(f"using {ip}:{port}")

    if not ip:
        print("no host IP. Use --ip or --discover.", file=sys.stderr)
        return 1

    capture = InputCapture()
    if not capture.open():
        print("could not open a controller device. Try --list-devices.", file=sys.stderr)
        return 1
    print(f"capturing from: {capture.device_name}")

    transport = Transport()
    try:
        ack = await transport.pair(ip, port, token)
        print(f"paired with host '{ack.get('name', ip)}' — streaming at {STREAM_HZ} Hz (Ctrl-C to stop)")
    except PermissionError:
        print("pairing rejected: bad token", file=sys.stderr)
        return 1
    except TimeoutError:
        print("host did not respond — check IP/port/firewall", file=sys.stderr)
        return 1

    cap_task = asyncio.create_task(capture.read_loop())
    hb_task = asyncio.create_task(transport.heartbeat_loop())
    dt = 1.0 / STREAM_HZ
    frames = 0
    t0 = time.monotonic()
    try:
        while transport.paired:
            transport.send_input(capture.snapshot())
            frames += 1
            if frames % STREAM_HZ == 0:
                rate = frames / (time.monotonic() - t0)
                print(f"\r  {frames} frames sent ({rate:.0f} Hz)", end="", flush=True)
            await asyncio.sleep(dt)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        cap_task.cancel()
        hb_task.cancel()
        transport.disconnect()
        capture.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone Deck-side input sender (hardware bring-up).")
    ap.add_argument("--ip", default="", help="host IP")
    ap.add_argument("--port", type=int, default=27970, help="host control port")
    ap.add_argument("--token", default="", help="pairing token printed by the host")
    ap.add_argument("--discover", action="store_true", help="scan the LAN for hosts first")
    ap.add_argument("--list-devices", action="store_true", help="list readable input devices and exit")
    args = ap.parse_args()

    if args.list_devices:
        return list_devices()
    try:
        return asyncio.run(run(args.ip, args.port, args.token, args.discover))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
