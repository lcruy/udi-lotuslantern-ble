"""Offline tests: run with  python3 -m pytest tests  (or python3 tests/test_nodeserver.py)."""

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_udi  # noqa: E402

fake_udi.install()

from lotus_led.protocol import HWMode, Pkt  # noqa: E402
from nodes import LotusController  # noqa: E402
from nodes.strip import MODE_BY_INDEX, PRESETS  # noqa: E402
from transports import TransportError  # noqa: E402
from transports.bridge import BridgeTransport  # noqa: E402
import lotus_bridge  # noqa: E402

MAC = "BE:60:65:12:34:56"


class FakeTransport:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    def write(self, address, frames):
        if self.fail:
            raise TransportError("strip unreachable")
        self.writes.append((address, [bytes(f).hex() for f in frames]))

    def scan(self, timeout):
        return [{"name": "ELK-BLEDOM", "address": "be:60:65:aa:bb:cc", "rssi": -60}]

    def health(self):
        return {"ok": True}

    def close(self):
        pass


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def make_controller(params=None, transport=None):
    poly = fake_udi.Interface()
    ctl = LotusController(poly, "controller", "controller", "Lotus Lantern")
    from nodes.controller import DEFAULT_PARAMS
    p = dict(DEFAULT_PARAMS)
    p.update({"transport": "bridge", "bridge_url": "http://10.0.0.2:8765", "bridge_token": "",
         "scan_seconds": "1", "auto_add": "true", MAC: "Kitchen"})
    p.update(params or {})
    import transports
    orig = transports.make_transport
    import nodes.controller as c
    c.make_transport = lambda kind, **kw: transport or FakeTransport()
    try:
        ctl.parameter_handler(p)
    finally:
        c.make_transport = orig
    return poly, ctl


# ── protocol frames (pinned against the upstream docstrings) ─────────────────

def test_frames():
    assert Pkt.power_on().hex() == "7e0404010001ff00ef"
    assert Pkt.color(255, 136, 0).hex() == "7e070503ff880010ef"
    assert Pkt.brightness(70).hex() == "7e040146ffffff00ef"
    assert Pkt.hw_mode(HWMode.JUMP_7_COLOR).hex() == "7e05038803ffff00ef"
    assert len(MODE_BY_INDEX) == 29 and MODE_BY_INDEX[1] is HWMode.STATIC_RED
    assert MODE_BY_INDEX[29] is HWMode.STROBE_WHITE


# ── controller ────────────────────────────────────────────────────────────────

def test_strip_created_from_params():
    poly, ctl = make_controller()
    assert "be6065123456" in poly.nodes
    strip = poly.nodes["be6065123456"]
    assert strip.mac == MAC and strip.name == "Kitchen"
    assert ctl.getDriver("GV0") == 1
    assert ctl.Data["be6065123456"]["mac"] == MAC


def test_missing_defaults_are_filled_once():
    poly = fake_udi.Interface()
    ctl = LotusController(poly, "controller", "controller", "x")
    ctl.parameter_handler({})
    assert ctl.Parameters["transport"] == "bridge"  # not FreeBSD here; "native" on eisy
    assert ctl.Parameters.saves == 1
    assert ctl.transport is None  # returned early, waits for the re-fired event


def test_unset_bridge_url_raises_notice():
    poly = fake_udi.Interface()
    ctl = LotusController(poly, "controller", "controller", "x")
    from nodes.controller import DEFAULT_PARAMS
    ctl.parameter_handler(dict(DEFAULT_PARAMS, transport="bridge"))
    assert "bridge" in ctl.Notices and ctl.transport is None


def test_discover_auto_adds():
    poly, ctl = make_controller()
    ctl.discover()
    assert wait_for(lambda: "be6065aabbcc" in poly.nodes)
    assert ctl.getDriver("GV0") == 2


# ── strip commands ────────────────────────────────────────────────────────────

def test_on_with_level_and_off():
    t = FakeTransport()
    poly, ctl = make_controller(transport=t)
    s = poly.nodes["be6065123456"]
    s.runCmd({"cmd": "DON", "value": "40"})
    assert wait_for(lambda: s.getDriver("ST") == 40)
    assert t.writes[-1] == (MAC, [Pkt.power_on().hex(), Pkt.brightness(40).hex()])
    assert s.getDriver("GV7") == 1
    s.runCmd({"cmd": "DOF"})
    assert wait_for(lambda: s.getDriver("ST") == 0)
    assert s.getDriver("GV6") == 40  # remembered level
    s.runCmd({"cmd": "DON"})
    assert wait_for(lambda: s.getDriver("ST") == 40)


def test_set_color_query_params():
    t = FakeTransport()
    poly, ctl = make_controller(transport=t)
    s = poly.nodes["be6065123456"]
    s.runCmd({"cmd": "SETCOLOR", "query": {"R.uom100": "10", "G.uom100": "20", "B.uom100": "300"}})
    assert wait_for(lambda: s.getDriver("GV1") == 10)
    assert (s.getDriver("GV2"), s.getDriver("GV3"), s.getDriver("GV4")) == (20, 255, 0)
    assert t.writes[-1][1][-1] == Pkt.color(10, 20, 255).hex()


def test_mode_speed_preset_ct():
    t = FakeTransport()
    poly, ctl = make_controller(transport=t)
    s = poly.nodes["be6065123456"]
    s.runCmd({"cmd": "SETSPEED", "value": "80"})
    assert wait_for(lambda: s.getDriver("GV5") == 80)
    s.runCmd({"cmd": "SETMODE", "value": "9"})  # JUMP_7_COLOR (index 8 + 1)
    assert wait_for(lambda: s.getDriver("GV4") == 9)
    assert t.writes[-1][1][1:] == [Pkt.speed(80).hex(), Pkt.hw_mode(HWMode.JUMP_7_COLOR).hex()]
    s.runCmd({"cmd": "SETPRESET", "value": "5"})
    assert wait_for(lambda: s.getDriver("GV3") == PRESETS[5][1][2] and s.getDriver("GV4") == 0)
    s.runCmd({"cmd": "SETCT", "value": "2700"})
    assert wait_for(lambda: s.getDriver("GV1") == 255 and s.getDriver("GV3") < 200)


def test_failure_sets_link_error_and_keeps_state():
    t = FakeTransport(fail=True)
    poly, ctl = make_controller(transport=t)
    s = poly.nodes["be6065123456"]
    s.runCmd({"cmd": "DON", "value": "50"})
    assert wait_for(lambda: s.getDriver("GV7") == 2)
    assert s.getDriver("ST") == 0
    assert "strip_be6065123456" in ctl.Notices


def test_resend_after_restart_uses_db_state():
    poly = fake_udi.Interface()
    poly.db["be6065123456"] = [
        {"driver": "ST", "value": "60"}, {"driver": "GV1", "value": "1"},
        {"driver": "GV2", "value": "2"}, {"driver": "GV3", "value": "3"},
        {"driver": "GV4", "value": "0"}, {"driver": "GV6", "value": "60"},
    ]
    t = FakeTransport()
    import nodes.controller as c
    orig = c.make_transport
    c.make_transport = lambda kind, **kw: t
    ctl = LotusController(poly, "controller", "controller", "x")
    from nodes.controller import DEFAULT_PARAMS
    params = dict(DEFAULT_PARAMS)
    params.update({"transport": "bridge", "bridge_url": "http://h:1", MAC: "K"})
    ctl.parameter_handler(params)
    c.make_transport = orig
    s = poly.nodes["be6065123456"]
    s.runCmd({"cmd": "RESEND"})
    assert wait_for(lambda: t.writes)
    assert t.writes[-1][1] == [Pkt.power_on().hex(), Pkt.brightness(60).hex(), Pkt.color(1, 2, 3).hex()]


# ── bridge HTTP round trip with a fake hub ───────────────────────────────────

class FakeHub:
    def __init__(self):
        self.writes = []

    def scan(self, timeout, all_devices):
        return [{"name": "ELK-BLEDOM", "address": MAC, "rssi": -50}]

    def write(self, address, frames, char, response):
        if address == "bad":
            raise RuntimeError("not advertising")
        self.writes.append((address, frames, char))
        return len(frames)

    def connections(self):
        return []

    def disconnect(self, address):
        pass


def test_bridge_round_trip():
    hub = FakeHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), lotus_bridge.make_handler(hub, "tok"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        bt = BridgeTransport(url, "tok", timeout=5)
        assert bt.health()["ok"]
        assert bt.scan(1)[0]["address"] == MAC
        bt.write(MAC, [Pkt.power_on(), Pkt.color(1, 2, 3)])
        assert hub.writes[-1][1] == [bytes(Pkt.power_on()), bytes(Pkt.color(1, 2, 3))]
        assert hub.writes[-1][2] == lotus_bridge.DEFAULT_CHAR
        try:
            BridgeTransport(url, "wrong", timeout=5).health()
            assert False, "token not enforced"
        except TransportError as exc:
            assert "token" in str(exc).lower()
        try:
            bt.write("bad", [Pkt.power_on()])
            assert False
        except TransportError as exc:
            assert "not advertising" in str(exc)
        req = urllib.request.Request(url + "/write", data=json.dumps({"address": MAC, "frames": ["zz"]}).encode(),
                                     method="POST", headers={"X-Bridge-Token": "tok"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()


def test_unreachable_bridge_message():
    try:
        BridgeTransport("http://127.0.0.1:1", timeout=2).health()
        assert False
    except TransportError as exc:
        assert "cannot reach bridge" in str(exc)


def test_native_is_default_on_freebsd():
    import transports
    real = sys.platform
    try:
        sys.platform = "freebsd14"
        assert transports.default_kind() == "native"
    finally:
        sys.platform = real


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if failures else 0)

