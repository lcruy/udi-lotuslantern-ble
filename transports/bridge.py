"""Transport that relays frames through bridge/lotus_bridge.py over HTTP."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import List

from .base import TransportError

WRITE_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"


class BridgeTransport:
    def __init__(self, url: str, token: str = "", timeout: float = 45.0):
        url = (url or "").strip().rstrip("/")
        if not url:
            raise TransportError("bridge_url is not set -- e.g. http://192.168.1.50:8765")
        if "://" not in url:
            url = "http://" + url
        self.url = url
        self.token = token or ""
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict = None, timeout: float = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Bridge-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error") or exc.reason
            except Exception:  # noqa: BLE001
                detail = exc.reason
            raise TransportError(f"bridge: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(f"cannot reach bridge at {self.url}: {reason}") from exc
        except ValueError as exc:
            raise TransportError(f"bridge sent a bad reply: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/health", timeout=10)

    def scan(self, timeout: float = 6.0) -> List[dict]:
        reply = self._request("GET", f"/scan?timeout={float(timeout):g}", timeout=timeout + 20)
        return list(reply.get("devices", []))

    def write(self, address: str, frames: List[bytes]) -> None:
        self._request("POST", "/write", {
            "address": address,
            "char": WRITE_CHAR,
            "frames": [bytes(f).hex() for f in frames],
            "response": False,
        })

    def close(self) -> None:
        pass
