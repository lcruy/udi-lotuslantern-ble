"""Controller node: settings, discovery and the transport."""

from __future__ import annotations

import re
import threading
from typing import Dict, Optional

import udi_interface

from transports import TransportError, default_kind, make_transport

from .strip import LotusStrip

LOGGER = udi_interface.LOGGER
Custom = udi_interface.Custom

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

BRIDGE_UNSET, BRIDGE_OK, BRIDGE_DOWN, BRIDGE_LOCAL, BRIDGE_NATIVE = 0, 1, 2, 3, 4

DEFAULT_PARAMS = {
    "transport": default_kind(),
    "hci_node": "ubt0hci",
    "addr_type": "auto",
    "bridge_url": "http://192.168.1.50:8765",
    "bridge_token": "",
    "scan_seconds": "8",
    "auto_add": "true",
}


def normalise_mac(mac: str) -> str:
    return mac.strip().upper().replace("-", ":")


def address_for(mac: str) -> str:
    """ISY addresses are <= 14 lowercase chars; a MAC without colons is 12."""
    return re.sub(r"[^0-9a-z]", "", mac.lower())[-12:] or "strip"


class LotusController(udi_interface.Node):
    id = "lotusctl"

    drivers = [
        {"driver": "ST", "value": 1, "uom": 2, "name": "Node Server Online"},
        {"driver": "GV0", "value": 0, "uom": 56, "name": "Strips"},
        {"driver": "GV1", "value": 0, "uom": 25, "name": "Bridge"},
    ]

    def __init__(self, polyglot, primary, address, name):
        super().__init__(polyglot, primary, address, name)
        self.poly = polyglot
        self.transport = None
        self.kind = ""
        self.strips: Dict[str, LotusStrip] = {}
        self._params_ready = False
        self._discover_lock = threading.Lock()

        self.Parameters = Custom(polyglot, "customparams")
        self.Notices = Custom(polyglot, "notices")
        self.Data = Custom(polyglot, "customdata")  # address -> {"mac", "name"}

        self.poly.subscribe(self.poly.START, self.start, address)
        self.poly.subscribe(self.poly.CUSTOMPARAMS, self.parameter_handler)
        self.poly.subscribe(self.poly.CUSTOMDATA, self.data_handler)
        self.poly.subscribe(self.poly.POLL, self.poll)
        self.poly.subscribe(self.poly.STOP, self.stop)
        self.poly.subscribe(self.poly.DISCOVER, self.discover)

        self.poly.ready()
        self.poly.addNode(self, conn_status="ST")

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        LOGGER.info("Lotus Lantern node server starting")
        self.poly.updateProfile()
        self.poly.setCustomParamsDoc()

    def stop(self):
        LOGGER.info("Lotus Lantern node server stopping")
        for strip in self.strips.values():
            strip.stop()
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
        self.setDriver("ST", 0)
        self.poly.stop()

    def data_handler(self, data):
        self.Data.load(data)

    def parameter_handler(self, params):
        self.Parameters.load(params)
        missing = {k: v for k, v in DEFAULT_PARAMS.items() if self.Parameters[k] is None}
        if missing:
            # Writing params re-fires this handler; only do it when something
            # is actually missing, so it cannot loop.
            merged = dict(params or {})
            merged.update(missing)
            self.Parameters.load(merged, True)
            return
        self._apply_params()

    def _apply_params(self):
        self.Notices.clear()
        kind = (self.Parameters["transport"] or default_kind()).strip().lower()
        url = (self.Parameters["bridge_url"] or "").strip()

        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
            self.transport = None

        if kind == "bridge" and (not url or url == DEFAULT_PARAMS["bridge_url"]):
            self.Notices["bridge"] = (
                "Set <b>bridge_url</b> to the address of the Raspberry Pi running "
                "lotus_bridge.py (see the configuration help below)."
            )
            self.setDriver("GV1", BRIDGE_UNSET)
        else:
            try:
                self.transport = make_transport(
                    kind, url=url, token=self.Parameters["bridge_token"] or "",
                    hci_node=self.Parameters["hci_node"] or "ubt0hci",
                    addr_type=self.Parameters["addr_type"] or "auto",
                )
                self.kind = kind
            except TransportError as exc:
                self.Notices["transport"] = str(exc)
                LOGGER.error("transport: %s", exc)
                self.setDriver("GV1", BRIDGE_DOWN)

        self._restore_nodes()
        self._params_ready = True
        self.check_bridge()

    # ── nodes ─────────────────────────────────────────────────────────────────

    def _configured_strips(self) -> Dict[str, str]:
        """MAC -> name, from custom params whose key is a MAC address."""
        out = {}
        for key in list(self.Parameters.keys()):
            if MAC_RE.match(key or ""):
                out[normalise_mac(key)] = (self.Parameters[key] or "").strip()
        return out

    def _restore_nodes(self):
        known: Dict[str, Dict[str, str]] = {}
        # Strips we created on an earlier run.
        for address in list(self.Data.keys()):
            entry = self.Data[address]
            if isinstance(entry, dict) and entry.get("mac"):
                known[normalise_mac(entry["mac"])] = {"name": entry.get("name", "")}
        # Strips the user listed by MAC (these win on naming).
        for mac, name in self._configured_strips().items():
            known.setdefault(mac, {})
            if name:
                known[mac]["name"] = name
        for mac, info in known.items():
            self.add_strip(mac, info.get("name") or "")

    def add_strip(self, mac: str, name: str = "") -> Optional[LotusStrip]:
        mac = normalise_mac(mac)
        address = self.poly.getValidAddress(address_for(mac))
        if address in self.strips:
            return self.strips[address]
        name = self.poly.getValidName(name or f"Lotus {mac[-5:].replace(':', '')}")
        strip = LotusStrip(self.poly, self.address, address, name, mac, self)
        self.poly.addNode(strip)
        self.strips[address] = strip
        if not isinstance(self.Data[address], dict) or self.Data[address].get("mac") != mac:
            self.Data[address] = {"mac": mac, "name": name}
        self.setDriver("GV0", len(self.strips))
        LOGGER.info("added strip %s at %s", name, mac)
        return strip

    def discover(self, command=None):
        if self.transport is None:
            self.Notices["discover"] = "Discovery needs a working transport -- check the settings."
            return
        if not self._discover_lock.acquire(blocking=False):
            LOGGER.info("discovery already running")
            return

        def run():
            try:
                seconds = float(self.Parameters["scan_seconds"] or 8)
                LOGGER.info("scanning for strips for %.0fs", seconds)
                found = self.transport.scan(seconds)
                LOGGER.info("scan found: %s", found)
                auto = (self.Parameters["auto_add"] or "true").lower() in ("1", "true", "yes", "on")
                new = []
                for dev in found:
                    mac = normalise_mac(dev.get("address", ""))
                    if not mac:
                        continue
                    if self.poly.getValidAddress(address_for(mac)) in self.strips:
                        continue
                    if auto:
                        self.add_strip(mac, dev.get("name") or "")
                    new.append(f"{dev.get('name') or '(unnamed)'} {mac} ({dev.get('rssi')} dBm)")
                if "discover" in self.Notices:
                    self.Notices.delete("discover")
                if new and not auto:
                    self.Notices["discover"] = (
                        "Found: " + "; ".join(new)
                        + ". Add them as custom parameters (key = MAC, value = name)."
                    )
                elif not found:
                    self.Notices["discover"] = (
                        "No strips found. Is the strip powered and in range of the bridge, "
                        "and is the Lotus Lantern phone app closed? These controllers accept "
                        "only one connection at a time."
                    )
            except TransportError as exc:
                LOGGER.error("discover: %s", exc)
                self.Notices["discover"] = f"Discovery failed: {exc}"
            finally:
                self._discover_lock.release()

        threading.Thread(target=run, name="discover", daemon=True).start()

    # ── status ────────────────────────────────────────────────────────────────

    def check_bridge(self):
        transport = self.transport
        if transport is None:
            return
        if not hasattr(transport, "health"):
            native = getattr(self, "kind", "") in ("native", "eisy", "freebsd")
            self.setDriver("GV1", BRIDGE_NATIVE if native else BRIDGE_LOCAL)
            return
        try:
            transport.health()
            self.setDriver("GV1", BRIDGE_OK)
            if "bridge_down" in self.Notices:
                self.Notices.delete("bridge_down")
        except TransportError as exc:
            LOGGER.warning("bridge health check failed: %s", exc)
            self.setDriver("GV1", BRIDGE_DOWN)
            self.Notices["bridge_down"] = str(exc)

    def strip_ok(self, strip):
        if f"strip_{strip.address}" in self.Notices:
            self.Notices.delete(f"strip_{strip.address}")

    def strip_failed(self, strip, message: str):
        self.Notices[f"strip_{strip.address}"] = f"{strip.name}: {message}"

    def poll(self, flag):
        if "shortPoll" in flag and self._params_ready:
            self.check_bridge()

    def query(self, command=None):
        self.check_bridge()
        for node in self.poly.getNodes().values():
            node.reportDrivers()

    def cmd_all(self, on: bool):
        for strip in list(self.strips.values()):
            (strip.cmd_on if on else strip.cmd_off)()

    commands = {
        "QUERY": query,
        "DISCOVER": discover,
        "ALLON": lambda self, cmd=None: self.cmd_all(True),
        "ALLOFF": lambda self, cmd=None: self.cmd_all(False),
    }
