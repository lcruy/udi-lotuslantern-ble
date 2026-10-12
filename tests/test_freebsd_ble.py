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
