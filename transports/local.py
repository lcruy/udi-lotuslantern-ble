"""Transport that drives the Bluetooth adapter on this machine with bleak.

Reuses the bridge's BleHub so there is one BLE implementation to maintain.
"""

from __future__ import annotations

import os
import sys
from typing import List

from .base import TransportError

WRITE_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_HERE, "bridge") not in sys.path:
    sys.path.insert(0, os.path.join(_HERE, "bridge"))


class LocalTransport:
    def __init__(self, idle_timeout: float = 60.0):
        if sys.platform.startswith("freebsd"):
            raise TransportError(
                "transport=local uses bleak, which has no FreeBSD backend. On eisy/Polisy "
                "use transport=native (the built-in Bluetooth) or transport=bridge."
            )
        try:
            from lotus_bridge import BleHub
            self._hub = BleHub(idle_timeout=idle_timeout)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"local Bluetooth unavailable: {exc}") from exc

    def scan(self, timeout: float = 6.0) -> List[dict]:
        try:
            return self._hub.scan(timeout)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"scan failed: {exc}") from exc

    def write(self, address: str, frames: List[bytes]) -> None:
        try:
            self._hub.write(address, list(frames), WRITE_CHAR, False)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        self._hub.close()
