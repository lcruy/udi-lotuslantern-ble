#!/usr/bin/env python3
"""Lotus BLE bridge -- a tiny, generic Bluetooth LE write relay over HTTP.

Why this exists
---------------
The eisy runs FreeBSD. Bleak, the Python BLE library that lotus-led-python
uses, supports Linux (BlueZ), Windows and macOS -- not FreeBSD -- so the eisy
cannot talk to a BLE strip itself. This script runs on any small box that
*does* have a supported Bluetooth stack and sits within radio range of the
strips (a Raspberry Pi is ideal), and the node server on the eisy sends it
ready-made frames over the LAN.

The bridge is deliberately protocol-agnostic: it scans, and it writes bytes to
a GATT characteristic. It knows nothing about LED strips beyond a default
name filter for scans, so the same bridge can relay for other BLE devices.

HTTP API (JSON in, JSON out)
----------------------------
GET  /health                      -> {"ok": true, "version": ..., "connections": n}
GET  /scan?timeout=6&all=0        -> {"devices": [{"name","address","rssi"}...]}
                                     all=1 returns every advertiser, not just
                                     BLEDOM-looking ones.
POST /write                       body: {"address": "BE:60:65:..",
                                          "char": "0000fff3-...",   (optional)
                                          "frames": ["7e0404...", ...],
                                          "response": false}         (optional)
                                  -> {"ok": true, "written": n}
POST /disconnect                  body: {"address": ".."}
GET  /connections                 -> {"connections": [{"address","idle_s"}...]}

If a token is configured (--token or LOTUS_BRIDGE_TOKEN), every request must
carry it in the ``X-Bridge-Token`` header.

Run:   python3 lotus_bridge.py --port 8765 [--token SECRET] [--idle 60]
Needs: bleak  (pip install bleak)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

__version__ = "0.0.4"

log = logging.getLogger("lotus.bridge")

DEFAULT_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
NAME_PATTERNS = (
    "BLEDOM", "BLEDOB", "ELK-BLEDOM", "ELK-BLEDOB", "ELK_BLEDOM",
    "LEDBLE", "ELK-BULB", "MELK",
)
KNOWN_MAC_PREFIX = "BE:60:65"

#: The BLEDOM firmware drops frames above ~20/s.
MIN_FRAME_INTERVAL = 0.05
CONNECT_TIMEOUT = 15.0
FIND_TIMEOUT = 10.0
MAX_FRAMES_PER_REQUEST = 64


def looks_like_bledom(name: str, address: str) -> bool:
    upper = (name or "").upper()
    return any(p in upper for p in NAME_PATTERNS) or address.upper().startswith(KNOWN_MAC_PREFIX)


class _Link:
    """One cached connection."""

    def __init__(self, address: str):
        self.address = address
        self.client = None  # BleakClient
        self.lock = asyncio.Lock()
        self.last_write = 0.0
        self.last_used = time.monotonic()

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected


class BleHub:
    """Owns an asyncio loop in a background thread and every BLE connection.

    All public methods are synchronous and thread-safe, so they can be called
    from an HTTP handler thread or from a PG3 command handler alike.
    """

    def __init__(self, idle_timeout: float = 60.0):
        try:
            import bleak  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on host
            raise RuntimeError(
                "The 'bleak' package is not installed. Run: pip install bleak"
            ) from exc
        self.idle_timeout = idle_timeout
        self._links: Dict[str, _Link] = {}
        self._scan_lock: Optional[asyncio.Lock] = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="blehub", daemon=True)
        self._thread.start()
        self._call(self._init(), timeout=5)

    # ── loop plumbing ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    async def _init(self) -> None:
        self._scan_lock = asyncio.Lock()
        self._loop.create_task(self._reaper())

    async def _reaper(self) -> None:
        """Drop idle connections so the vendor phone app can connect again.

        These controllers accept one connection at a time; holding it forever
        locks the phone out.
        """
        while True:
            await asyncio.sleep(5)
            if self.idle_timeout <= 0:
                continue
            now = time.monotonic()
            for link in list(self._links.values()):
                if link.connected and not link.lock.locked() and now - link.last_used > self.idle_timeout:
                    log.info("idle %.0fs, disconnecting %s", now - link.last_used, link.address)
                    await self._disconnect_link(link)

    # ── public, synchronous API ───────────────────────────────────────────────

    def scan(self, timeout: float = 6.0, all_devices: bool = False) -> List[dict]:
        timeout = max(1.0, min(float(timeout), 30.0))
        return self._call(self._scan(timeout, all_devices), timeout=timeout + 15)

    def write(self, address: str, frames: List[bytes], char: str = DEFAULT_CHAR,
              response: bool = False) -> int:
        budget = CONNECT_TIMEOUT * 2 + FIND_TIMEOUT + len(frames) * 1.0 + 10
        return self._call(self._write(address, frames, char, response), timeout=budget)

    def disconnect(self, address: str) -> None:
        link = self._links.get(address.upper())
        if link:
            self._call(self._disconnect_link(link), timeout=15)

    def connections(self) -> List[dict]:
        now = time.monotonic()
        return [
            {"address": l.address, "connected": l.connected, "idle_s": round(now - l.last_used, 1)}
            for l in self._links.values()
        ]

    def close(self) -> None:
        try:
            self._call(self._close_all(), timeout=20)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── coroutines ────────────────────────────────────────────────────────────

    async def _scan(self, timeout: float, all_devices: bool) -> List[dict]:
        from bleak import BleakScanner

        async with self._scan_lock:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for address, (device, adv) in found.items():
            name = device.name or adv.local_name or ""
            if all_devices or looks_like_bledom(name, address):
                out.append({"name": name, "address": address, "rssi": adv.rssi})
        out.sort(key=lambda d: d["rssi"] if d["rssi"] is not None else -999, reverse=True)
        return out

    async def _connect(self, link: _Link) -> None:
        from bleak import BleakClient, BleakScanner

        await self._disconnect_link(link)
        # Resolving the device first is much more reliable on BlueZ than
        # connecting to a bare address the adapter has not seen recently.
        async with self._scan_lock:
            device = await BleakScanner.find_device_by_address(link.address, timeout=FIND_TIMEOUT)
        if device is None:
            raise RuntimeError(
                f"{link.address} is not advertising. Is it powered, in range, and "
                "not connected to a phone? These strips take one connection at a time."
            )
        client = BleakClient(device, timeout=CONNECT_TIMEOUT)
        await client.connect()
        link.client = client
        log.info("connected to %s", link.address)

    async def _disconnect_link(self, link: _Link) -> None:
        if link.client is not None:
            try:
                await link.client.disconnect()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
            link.client = None

    async def _write(self, address: str, frames: List[bytes], char: str, response: bool) -> int:
        key = address.upper()
        link = self._links.setdefault(key, _Link(address))
        async with link.lock:
            link.last_used = time.monotonic()
            for attempt in (1, 2):
                try:
                    if not link.connected:
                        await self._connect(link)
                    for frame in frames:
                        gap = MIN_FRAME_INTERVAL - (time.monotonic() - link.last_write)
                        if gap > 0:
                            await asyncio.sleep(gap)
                        await link.client.write_gatt_char(char, frame, response=response)
                        link.last_write = time.monotonic()
                    link.last_used = time.monotonic()
                    return len(frames)
                except Exception as exc:  # noqa: BLE001
                    log.warning("write to %s failed (attempt %d): %s", address, attempt, exc)
                    await self._disconnect_link(link)
                    if attempt == 2:
                        raise RuntimeError(f"write to {address} failed: {exc}") from exc
                    await asyncio.sleep(1.0)
        return 0  # unreachable

    async def _close_all(self) -> None:
        for link in list(self._links.values()):
            await self._disconnect_link(link)


# ── HTTP front end ────────────────────────────────────────────────────────────

def parse_frames(raw) -> List[bytes]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("'frames' must be a non-empty list of hex strings")
    if len(raw) > MAX_FRAMES_PER_REQUEST:
        raise ValueError(f"at most {MAX_FRAMES_PER_REQUEST} frames per request")
    frames = []
    for item in raw:
        text = str(item).replace(" ", "").replace(":", "")
        try:
            data = bytes.fromhex(text)
        except ValueError:
            raise ValueError(f"'{item}' is not hex") from None
        if not 1 <= len(data) <= 512:
            raise ValueError("each frame must be 1-512 bytes")
        frames.append(data)
    return frames


def make_handler(hub, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"LotusBridge/{__version__}"

        def log_message(self, fmt, *args):  # route to logging, not stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if token and self.headers.get("X-Bridge-Token", "") != token:
                self._send(401, {"ok": False, "error": "bad or missing X-Bridge-Token"})
                return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("request too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def do_GET(self):  # noqa: N802
            if not self._authorised():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/health":
                    self._send(200, {"ok": True, "version": __version__,
                                     "connections": len(hub.connections())})
                elif url.path == "/scan":
                    devices = hub.scan(float(q.get("timeout", ["6"])[0]),
                                       q.get("all", ["0"])[0] in ("1", "true", "yes"))
                    self._send(200, {"ok": True, "devices": devices})
                elif url.path == "/connections":
                    self._send(200, {"ok": True, "connections": hub.connections()})
                else:
                    self._send(404, {"ok": False, "error": "not found"})
            except Exception as exc:  # noqa: BLE001
                log.warning("GET %s failed: %s", url.path, exc)
                self._send(502, {"ok": False, "error": str(exc)})

        def do_POST(self):  # noqa: N802
            if not self._authorised():
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            try:
                if path == "/write":
                    address = str(body.get("address", "")).strip()
                    if not address:
                        raise ValueError("'address' is required")
                    frames = parse_frames(body.get("frames"))
                    char = str(body.get("char") or DEFAULT_CHAR)
                    n = hub.write(address, frames, char, bool(body.get("response", False)))
                    self._send(200, {"ok": True, "written": n})
                elif path == "/disconnect":
                    hub.disconnect(str(body.get("address", "")))
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"ok": False, "error": "not found"})
            except ValueError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.warning("POST %s failed: %s", path, exc)
                self._send(502, {"ok": False, "error": str(exc)})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Generic BLE write relay for the Lotus Lantern PG3 node server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("LOTUS_BRIDGE_PORT", 8765)))
    ap.add_argument("--token", default=os.environ.get("LOTUS_BRIDGE_TOKEN", ""))
    ap.add_argument("--idle", type=float, default=float(os.environ.get("LOTUS_BRIDGE_IDLE", 60)),
                    help="disconnect a strip after this many idle seconds (0 = stay connected)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    hub = BleHub(idle_timeout=args.idle)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(hub, args.token))
    log.info("Lotus BLE bridge %s listening on %s:%d%s", __version__, args.host, args.port,
             " (token required)" if args.token else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        hub.close()


if __name__ == "__main__":
    main()
