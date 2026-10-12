"""How frames reach the strips.

A transport is anything with three synchronous methods:

    scan(timeout: float) -> list[dict(name, address, rssi)]
    write(address: str, frames: list[bytes]) -> None   # raises on failure
    close() -> None

``bridge`` -- HTTP to bridge/lotus_bridge.py on a Raspberry Pi (or any
             Linux/Windows/macOS box) near the strips. Use it when the strips
             are out of the eisy's radio range, or if native does not work.
``native`` -- the eisy's / Polisy's own Bluetooth adapter, through FreeBSD's
             kernel Bluetooth stack (see freebsd_ble.py). No extra hardware.
``local``  -- talk BLE directly with bleak. Only works when PG3 itself runs
             on Linux with BlueZ (or on Windows/macOS).
"""

from .base import TransportError


def default_kind() -> str:
    import sys

    return "native" if sys.platform.startswith("freebsd") else "bridge"


def make_transport(kind: str, **options):
    kind = (kind or default_kind()).strip().lower()
    if kind in ("native", "eisy", "freebsd"):
        from .freebsd_ble import FreeBSDBleTransport

        return FreeBSDBleTransport(hci_node=options.get("hci_node", "ubt0hci"),
                                   idle_timeout=float(options.get("idle", 60)),
                                   addr_type=options.get("addr_type", "auto"))
    if kind == "bridge":
        from .bridge import BridgeTransport

        return BridgeTransport(options.get("url", ""), options.get("token", ""),
                               timeout=float(options.get("timeout", 45)))
    if kind == "local":
        from .local import LocalTransport

        return LocalTransport(idle_timeout=float(options.get("idle", 60)))
    raise TransportError(f"unknown transport '{kind}' -- use 'native', 'bridge' or 'local'")


__all__ = ["make_transport", "default_kind", "TransportError"]
