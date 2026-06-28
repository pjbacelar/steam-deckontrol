"""Steam Deckontrol — Decky plugin backend.

Orchestrates the Deck side of the input bridge:

    [evdev capture] --snapshot--> [streaming loop @ N Hz] --UDP--> [host daemon]

Exposes async methods to the React frontend (via @decky/api `call`/`callable`):

    discover_hosts()          -> [{name, ip, port, busy, version}, ...]
    connect(ip, port, token)  -> {ok, name} | {ok: False, error}
    disconnect()              -> None
    get_status()              -> {connected, host, fps, frames, capturing, device, input_only}
    set_input_only_mode(bool) -> None

The "input-only mode" flag is mirrored to the frontend, which is responsible for
the actual display suppression (minimizing the Remote Play surface / muting),
because those are Steam-client UI actions only reachable from the frontend.
"""

from __future__ import annotations

import asyncio
import time

import decky

# py_modules/ is on sys.path inside Decky.
from input_capture import InputCapture
from transport import Transport
import discovery

# Streaming cadence. 250 Hz keeps added latency well under a frame while staying
# light on Wi-Fi (51-byte frames -> ~100 kbit/s).
STREAM_HZ = 250
_STREAM_DT = 1.0 / STREAM_HZ


class Plugin:
    async def _main(self):
        self._capture: InputCapture | None = None
        self._transport: Transport | None = None
        self._stream_task: asyncio.Task | None = None
        self._capture_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._input_only = False
        self._fps = 0.0
        decky.logger.info("Deckontrol backend loaded")

    async def _unload(self):
        await self.disconnect()
        decky.logger.info("Deckontrol backend unloaded")

    # -- frontend API -------------------------------------------------------
    async def discover_hosts(self) -> list:
        try:
            return await discovery.discover()
        except Exception as exc:  # pragma: no cover
            decky.logger.error("discovery failed: %s", exc)
            return []

    async def connect(self, ip: str, port: int, token: str) -> dict:
        await self.disconnect()  # clean slate

        self._capture = InputCapture()
        if not self._capture.open():
            self._capture = None
            return {"ok": False, "error": "no_controller"}

        self._transport = Transport()
        try:
            ack = await self._transport.pair(ip, int(port), token)
        except PermissionError:
            await self.disconnect()
            return {"ok": False, "error": "bad_token"}
        except TimeoutError:
            await self.disconnect()
            return {"ok": False, "error": "no_host"}

        self._capture_task = asyncio.create_task(self._capture.read_loop())
        self._stream_task = asyncio.create_task(self._stream_loop())
        self._heartbeat_task = asyncio.create_task(self._transport.heartbeat_loop())
        decky.logger.info("connected to host %s", ack.get("name", ip))
        return {"ok": True, "name": self._transport.host_name}

    async def disconnect(self) -> None:
        for task in (self._stream_task, self._capture_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        self._stream_task = self._capture_task = self._heartbeat_task = None
        if self._transport is not None:
            self._transport.disconnect()
            self._transport = None
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        self._fps = 0.0

    async def get_status(self) -> dict:
        return {
            "connected": bool(self._transport and self._transport.paired),
            "host": self._transport.host_name if self._transport else "",
            "fps": round(self._fps, 1),
            "frames": self._transport.frames_sent if self._transport else 0,
            "capturing": bool(self._capture and self._capture.available),
            "device": self._capture.device_name if self._capture else "",
            "input_only": self._input_only,
        }

    async def set_input_only_mode(self, enabled: bool) -> None:
        self._input_only = bool(enabled)
        decky.logger.info("input-only mode: %s", self._input_only)

    # -- internals ----------------------------------------------------------
    async def _stream_loop(self) -> None:
        """Snapshot the live input state and ship it at a fixed cadence."""
        assert self._capture and self._transport
        next_tick = time.monotonic()
        window_start = next_tick
        window_frames = 0
        try:
            while True:
                state = self._capture.snapshot()
                self._transport.send_input(state)
                window_frames += 1

                now = time.monotonic()
                if now - window_start >= 1.0:
                    self._fps = window_frames / (now - window_start)
                    window_start, window_frames = now, 0
                    if not self._transport.paired:
                        decky.logger.warning("link lost; stopping stream")
                        break

                next_tick += _STREAM_DT
                sleep = next_tick - time.monotonic()
                if sleep > 0:
                    await asyncio.sleep(sleep)
                else:
                    next_tick = time.monotonic()  # we fell behind; resync
        except asyncio.CancelledError:
            pass
