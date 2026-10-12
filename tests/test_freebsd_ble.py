"""Tests for the native FreeBSD transport's protocol code.

The GATT/ATT exchange runs against a simulated BLEDOM GATT server over a
SEQPACKET socketpair, so everything except the kernel connect() is exercised.
"""

import os
import socket
import struct
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lotus_led.protocol import Pkt  # noqa: E402
from transports.freebsd_ble import (  # noqa: E402
    ATT_WRITE_CMD, AttLink, mac_to_bdaddr, parse_le_scan, parse_read_by_type,
    sockaddr_l2cap_le, uuid16_of, BDADDR_LE_PUBLIC,
)

MAC = "BE:60:65:12:34:56"


def test_sockaddr_layout():
    sa = sockaddr_l2cap_le(MAC, BDADDR_LE_PUBLIC)
    assert len(sa) == 14
    assert sa[0] == 14 and sa[1] == 36          # len, AF_BLUETOOTH
    assert sa[2:4] == b"\x00\x00"                # psm = 0
    assert sa[4:10] == bytes([0x56, 0x34, 0x12, 0x65, 0x60, 0xBE])  # bdaddr LE
    assert sa[10:12] == b"\x04\x00"              # ATT CID
    assert sa[12] == 1                           # LE public
    assert mac_to_bdaddr("00:11:22:33:44:55") == bytes.fromhex("554433221100")


def test_uuid16():
    assert uuid16_of(b"\xf3\xff") == 0xFFF3
    full = bytes.fromhex("0000fff3" "00001000800000805f9b34fb")[::-1]
    assert uuid16_of(full) == 0xFFF3
    assert uuid16_of(bytes(16)) is None


def test_le_cancel_packet():
    from transports.freebsd_ble import hci_command_packet, OGF_LE, OCF_LE_CREATE_CONNECTION_CANCEL
    assert hci_command_packet(OGF_LE, OCF_LE_CREATE_CONNECTION_CANCEL) == bytes([0x01, 0x0E, 0x20, 0x00])
    assert struct.calcsize("BB32s") == 34


def test_connect_timeout_cancels():
    import transports.freebsd_ble as fb
    calls = []
    real = fb.cancel_le_connect
    fb.cancel_le_connect = lambda node: calls.append(node)
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    class FakeLibc:
        def socket(self, *args):
            return os.dup(a.fileno())
        def connect(self, fd, addr, n):
            import ctypes
            ctypes.set_errno(36)  # EINPROGRESS on FreeBSD
            return -1
    real_libc, real_select = fb._libc, fb.select.select
    fb._libc = lambda: FakeLibc()
    fb.select.select = lambda r, w, x, t: ([], [], [])
    real_einprogress = fb.errno.EINPROGRESS
    fb.errno.EINPROGRESS = 36
    try:
        link = fb.AttLink(MAC, BDADDR_LE_PUBLIC, "ubt0hci")
        try:
            link.connect(timeout=0.1)
            assert False, "should time out"
        except fb.TransportError as exc:
            assert "did not answer" in str(exc)
        assert calls == ["ubt0hci"]
    finally:
        fb.cancel_le_connect, fb._libc, fb.select.select = real, real_libc, real_select
        fb.errno.EINPROGRESS = real_einprogress
        a.close(); b.close()


def test_parse_read_by_type():
    pdu = bytes([0x09, 7]) + struct.pack("<HBHH", 0x10, 0x0C, 0x11, 0xFFF3) + struct.pack("<HBHH", 0x12, 0x10, 0x13, 0xFFF4)
    assert parse_read_by_type(pdu) == [(0x10, 0x0C, 0x11, b"\xf3\xff"), (0x12, 0x10, 0x13, b"\xf4\xff")]


SCAN_OUTPUT = """Scan 1
Scan result, num_reports: 1
\tBD_ADDR be:60:65:12:34:56
\tAddress type: Public
\tFlags: ...
\tComplete local name: ELK-BLEDOM
Scan 2
Scan result, num_reports: 1
\tBD_ADDR 11:22:33:44:55:66
\tAddress type: Random
Scan complete
"""


def test_parse_le_scan():
    d = parse_le_scan(SCAN_OUTPUT)
    assert {x["address"] for x in d} == {"BE:60:65:12:34:56", "11:22:33:44:55:66"}
    strip = [x for x in d if x["address"].startswith("BE")][0]
    assert strip["name"] == "ELK-BLEDOM" and strip["addr_type"] == "public"


def _fake_gatt_server(sock, writes):
    """Answers Read By Type in two pages, sends noise, records Write Commands."""
    chars = [(0x02, 0x0A, 0x03, 0x2A00), (0x05, 0x10, 0x06, 0xFFF4), (0x08, 0x0C, 0x09, 0xFFF3)]
    sock.send(bytes([0x1B, 0x06, 0x00, 0xAA]))  # unsolicited notification
    while True:
        try:
            pdu = sock.recv(512)
        except OSError:
            return
        if not pdu:
            return
        if pdu[0] == 0x08:
            start = struct.unpack("<H", pdu[1:3])[0]
            page = [c for c in chars if c[0] >= start][:2]
            if not page:
                sock.send(bytes([0x01, 0x08]) + struct.pack("<H", start) + b"\x0a")
            else:
                sock.send(bytes([0x09, 7]) + b"".join(struct.pack("<HBHH", *c) for c in page))
        elif pdu[0] == ATT_WRITE_CMD:
            writes.append(pdu)


def test_att_discovery_and_write():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    writes = []
    t = threading.Thread(target=_fake_gatt_server, args=(b, writes), daemon=True)
    t.start()
    link = AttLink(MAC, BDADDR_LE_PUBLIC)
    link.fd = a.detach()
    link.write_frames([Pkt.power_on(), Pkt.color(1, 2, 3)])
    assert link.write_handle == 0x09
    deadline = __import__("time").time() + 2
    while len(writes) < 2 and __import__("time").time() < deadline:
        pass
    assert writes == [b"\x52\x09\x00" + bytes(Pkt.power_on()), b"\x52\x09\x00" + bytes(Pkt.color(1, 2, 3))]
    link.close()
    b.close()


def test_enable_le_and_timed_out_scan_keeps_results(tmp_path=None):
    """Fake hccontrol: le_scan prints one strip then hangs, like the real one."""
    import tempfile
    import time as _t
    import transports.freebsd_ble as fb

    d = tempfile.mkdtemp()
    log = os.path.join(d, "calls.log")
    script = os.path.join(d, "hccontrol")
    with open(script, "w") as f:
        f.write(f"""#!/bin/sh
printf "%s\\n" "$*" >> {log}
case "$*" in
  *le_scan*) printf 'Scan 1\\n\\tBD_ADDR be:60:65:aa:bb:cc \\n\\tAddress type: Public\\n\\tComplete local name: ELK-BLEDOM\\n'; sleep 30;;
  *read_le_host_support*) echo "LE Host support: 01";;
  *le_enable*) echo "LE enabled";;
esac
""")
    os.chmod(script, 0o755)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = d + os.pathsep + old_path
    real_euid = os.geteuid
    os.geteuid = lambda: 0
    real_cancel = fb.cancel_le_connect
    fb.cancel_le_connect = lambda node: None
    try:
        t = fb.FreeBSDBleTransport(hci_node="ubt0hci")
        assert t.le_problems == []
        start = _t.time()
        found = t.scan(2)
        assert _t.time() - start < 10
        assert [x["address"] for x in found] == ["BE:60:65:AA:BB:CC"]
        assert fb.le_host_supported("ubt0hci") is True
        calls = open(log).read()
        assert "write_le_host_support 1 0" in calls and "le_enable enable" in calls
        assert "le_set_scan_enable disable" in calls
        t.close()
    finally:
        os.environ["PATH"] = old_path
        os.geteuid = real_euid
        fb.cancel_le_connect = real_cancel


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                fails += 1
                print("FAIL", name, repr(exc))
    sys.exit(1 if fails else 0)
