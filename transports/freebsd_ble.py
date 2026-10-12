"""Native Bluetooth LE transport for eisy / Polisy (FreeBSD).

``bleak`` has no FreeBSD backend, but FreeBSD's own Bluetooth stack (netgraph)
already does everything these strips need:

* LE scanning            -- ``hccontrol le_scan`` (FreeBSD 13+)
* LE connections + ATT   -- an L2CAP SOCK_SEQPACKET socket on fixed channel
                            0x0004 (``struct sockaddr_l2cap`` gained
                            ``l2cap_cid`` / ``l2cap_bdaddr_type`` for this)

The only missing piece is a GATT client, and a BLEDOM strip needs a very small
one: find the value handle of characteristic FFF3 once, then send ATT Write
Commands to it. That is implemented here in pure Python, with ``ctypes`` for
the two libc calls Python's socket module cannot make (it has no way to set
the L2CAP CID or LE address type on FreeBSD).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import os
import re
import select
import shutil
import struct
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

from .base import TransportError

# ── FreeBSD constants (sys/sys/socket.h, sys/netgraph/bluetooth/include) ─────

AF_BLUETOOTH = 36
SOCK_SEQPACKET = 5
BLUETOOTH_PROTO_L2CAP = 135
NG_L2CAP_ATT_CID = 0x0004
BDADDR_LE_PUBLIC = 1
BDADDR_LE_RANDOM = 2

# struct sockaddr_l2cap { u_char len; u_char family; u_int16_t psm;
#                         bdaddr_t bdaddr (6, packed); u_int16_t cid;
#                         u_int8_t bdaddr_type; }  -> 13 bytes + 1 pad = 14
_SOCKADDR_FMT = "<BBH6sHBx"
_SOCKADDR_LEN = struct.calcsize(_SOCKADDR_FMT)  # 14

# ── ATT ───────────────────────────────────────────────────────────────────────

ATT_ERROR_RSP = 0x01
ATT_MTU_REQ = 0x02
ATT_MTU_RSP = 0x03
ATT_READ_BY_TYPE_REQ = 0x08
ATT_READ_BY_TYPE_RSP = 0x09
ATT_WRITE_CMD = 0x52
ATT_HANDLE_VALUE_NTF = 0x1B
ATT_HANDLE_VALUE_IND = 0x1D
ATT_HANDLE_VALUE_CFM = 0x1E
ATT_ERR_ATTRIBUTE_NOT_FOUND = 0x0A
GATT_CHARACTERISTIC_DECL = 0x2803

WRITE_UUID16 = 0xFFF3
MIN_FRAME_INTERVAL = 0.05     # the firmware drops frames above ~20/s
CONNECT_TIMEOUT = 12.0
ATT_TIMEOUT = 5.0

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _libc():
    path = ctypes.util.find_library("c")
    lib = ctypes.CDLL(path, use_errno=True)
    lib.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.connect.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    return lib


def mac_to_bdaddr(mac: str) -> bytes:
    """``BE:60:65:12:34:56`` -> little-endian 6 bytes, as bdaddr_t stores it."""
    if not _MAC_RE.match(mac.strip()):
        raise TransportError(f"'{mac}' is not a MAC address (expected AA:BB:CC:DD:EE:FF)")
    return bytes(int(p, 16) for p in reversed(mac.strip().split(":")))


def sockaddr_l2cap_le(mac: str, addr_type: int) -> bytes:
    return struct.pack(_SOCKADDR_FMT, _SOCKADDR_LEN, AF_BLUETOOTH, 0,
                       mac_to_bdaddr(mac), NG_L2CAP_ATT_CID, addr_type)


def uuid16_of(raw: bytes) -> Optional[int]:
    """16-bit UUID from a 2-byte or 16-byte (Bluetooth base) little-endian UUID."""
    if len(raw) == 2:
        return struct.unpack("<H", raw)[0]
    if len(raw) == 16:
        be = raw[::-1]
        if be[:2] == b"\x00\x00" and be[4:] == bytes.fromhex("00001000800000805f9b34fb"):
            return struct.unpack(">H", be[2:4])[0]
    return None


def parse_read_by_type(pdu: bytes) -> List[Tuple[int, int, int, bytes]]:
    """ATT Read By Type Response for characteristic declarations.

    Returns (decl_handle, properties, value_handle, uuid_bytes) per entry.
    """
    if len(pdu) < 2 or pdu[0] != ATT_READ_BY_TYPE_RSP:
        raise ValueError("not a Read By Type Response")
    size = pdu[1]
    if size not in (7, 21):
        raise ValueError(f"unexpected attribute data length {size}")
    out = []
    body = pdu[2:]
    for i in range(0, len(body) - size + 1, size):
        entry = body[i:i + size]
        handle, props, value_handle = struct.unpack("<HBH", entry[:5])
        out.append((handle, props, value_handle, entry[5:]))
    return out


# ── one connection ────────────────────────────────────────────────────────────

class AttLink:
    """An LE ATT channel to one device, as a raw file descriptor."""

    def __init__(self, mac: str, addr_type: int):
        self.mac = mac
        self.addr_type = addr_type
        self.fd: Optional[int] = None
        self.write_handle: Optional[int] = None
        self.last_write = 0.0
        self.last_used = time.monotonic()

    # -- connection ----------------------------------------------------------

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> None:
        libc = _libc()
        fd = libc.socket(AF_BLUETOOTH, SOCK_SEQPACKET, BLUETOOTH_PROTO_L2CAP)
        if fd < 0:
            err = ctypes.get_errno()
            raise TransportError(
                f"cannot open a Bluetooth L2CAP socket ({os.strerror(err)}). Is the "
                "Bluetooth service running on the eisy (service bluetooth status ubt0)?"
            )
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            addr = sockaddr_l2cap_le(self.mac, self.addr_type)
            rc = libc.connect(fd, addr, len(addr))
            if rc != 0:
                err = ctypes.get_errno()
                if err not in (errno.EINPROGRESS, errno.EALREADY):
                    raise TransportError(self._explain(err))
                _, writable, _ = select.select([], [fd], [], timeout)
                if not writable:
                    raise TransportError(
                        f"{self.mac} did not answer within {timeout:.0f}s -- powered, in "
                        "range, and not connected to the phone app?"
                    )
                # SO_ERROR via getsockopt(SOL_SOCKET=0xffff, SO_ERROR=0x1007)
                err_buf = ctypes.c_int(0)
                size = ctypes.c_uint(ctypes.sizeof(err_buf))
                libc.getsockopt(fd, 0xFFFF, 0x1007, ctypes.byref(err_buf), ctypes.byref(size))
                if err_buf.value:
                    raise TransportError(self._explain(err_buf.value))
        except Exception:
            os.close(fd)
            raise
        self.fd = fd
        self.last_used = time.monotonic()

    def _explain(self, err: int) -> str:
        if err == errno.EHOSTUNREACH:
            return ("no Bluetooth adapter is attached to the stack -- start it with "
                    "'service bluetooth start ubt0' (or enable UDI's Bluetooth service)")
        if err in (errno.ETIMEDOUT, errno.ECONNREFUSED):
            return f"{self.mac} refused or timed out ({os.strerror(err)})"
        return f"connect to {self.mac} failed: {os.strerror(err)}"

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    @property
    def connected(self) -> bool:
        return self.fd is not None

    # -- ATT -----------------------------------------------------------------

    def _send(self, pdu: bytes) -> None:
        _, writable, _ = select.select([], [self.fd], [], ATT_TIMEOUT)
        if not writable:
            raise TransportError(f"{self.mac}: send timed out")
        try:
            os.write(self.fd, pdu)
        except OSError as exc:
            raise TransportError(f"{self.mac}: link lost ({exc.strerror})") from exc

    def _request(self, pdu: bytes, expect: int) -> bytes:
        """Send a request and wait for its response, answering peer chatter."""
        self._send(pdu)
        deadline = time.monotonic() + ATT_TIMEOUT
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransportError(f"{self.mac}: no ATT response")
            readable, _, _ = select.select([self.fd], [], [], left)
            if not readable:
                continue
            try:
                data = os.read(self.fd, 512)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise TransportError(f"{self.mac}: link lost ({exc.strerror})") from exc
            if not data:
                raise TransportError(f"{self.mac}: link closed")
            op = data[0]
            if op in (expect, ATT_ERROR_RSP):
                return data
            if op == ATT_MTU_REQ:              # peer asks: keep the default 23
                self._send(struct.pack("<BH", ATT_MTU_RSP, 23))
            elif op == ATT_HANDLE_VALUE_IND:
                self._send(bytes([ATT_HANDLE_VALUE_CFM]))
            # notifications and anything else: ignore

    def discover_write_handle(self) -> int:
        """Walk the characteristic declarations and return FFF3's value handle."""
        start = 0x0001
        while start <= 0xFFFF:
            rsp = self._request(
                struct.pack("<BHHH", ATT_READ_BY_TYPE_REQ, start, 0xFFFF, GATT_CHARACTERISTIC_DECL),
                ATT_READ_BY_TYPE_RSP,
            )
            if rsp[0] == ATT_ERROR_RSP:
                if len(rsp) >= 5 and rsp[4] == ATT_ERR_ATTRIBUTE_NOT_FOUND:
                    break
                raise TransportError(f"{self.mac}: GATT discovery error 0x{rsp[4]:02x}")
            entries = parse_read_by_type(rsp)
            if not entries:
                break
            for handle, _props, value_handle, uuid in entries:
                if uuid16_of(uuid) == WRITE_UUID16:
                    self.write_handle = value_handle
                    return value_handle
            start = entries[-1][0] + 1
        raise TransportError(f"{self.mac} has no FFF3 characteristic -- not a BLEDOM strip?")

    def write_frames(self, frames: List[bytes]) -> None:
        if self.write_handle is None:
            self.discover_write_handle()
        for frame in frames:
            gap = MIN_FRAME_INTERVAL - (time.monotonic() - self.last_write)
            if gap > 0:
                time.sleep(gap)
            self._send(struct.pack("<BH", ATT_WRITE_CMD, self.write_handle) + bytes(frame))
            self.last_write = time.monotonic()
        self.last_used = time.monotonic()


# ── scanning via hccontrol ────────────────────────────────────────────────────

def _hccontrol_cmd() -> List[str]:
    exe = shutil.which("hccontrol") or "/usr/sbin/hccontrol"
    if os.geteuid() == 0:
        return [exe]
    sudo = shutil.which("sudo")
    return [sudo, "-n", exe] if sudo else [exe]


def parse_le_scan(text: str) -> List[dict]:
    """Parse ``hccontrol le_scan -v`` output into name/address records."""
    found: Dict[str, dict] = {}
    current: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"BD_ADDR\s+(\S+)", line)
        if m:
            addr = m.group(1).upper()
            current = found.setdefault(addr, {"name": "", "address": addr, "rssi": None, "addr_type": ""})
            continue
        if current is None:
            continue
        m = re.match(r"Address type:\s*(.+)", line)
        if m:
            current["addr_type"] = m.group(1).strip().lower()
            continue
        m = re.match(r"(Complete|Shortened) local name:\s*(.*)", line)
        if m and m.group(2).strip():
            current["name"] = m.group(2).strip()
    return list(found.values())


# ── the transport ─────────────────────────────────────────────────────────────

class FreeBSDBleTransport:
    """Talks to the strips through the eisy's own Bluetooth adapter."""

    def __init__(self, hci_node: str = "ubt0hci", idle_timeout: float = 60.0, addr_type: str = "auto"):
        self.hci_node = hci_node or "ubt0hci"
        self.idle_timeout = idle_timeout
        self.addr_type = (addr_type or "auto").lower()
        self._links: Dict[str, AttLink] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._reaper, name="ble-reaper", daemon=True).start()

    # -- scan ----------------------------------------------------------------

    def scan(self, timeout: float = 8.0) -> List[dict]:
        base = _hccontrol_cmd() + ["-n", self.hci_node]
        # le_scan counts HCI events rather than seconds; ask for plenty and
        # bound it with a wall-clock timeout.
        try:
            proc = subprocess.run(base + ["le_scan", "-v", "-n", "200"], capture_output=True,
                                  text=True, timeout=max(3.0, float(timeout)))
            out = proc.stdout
            if proc.returncode != 0 and not out:
                raise TransportError(
                    "hccontrol le_scan failed: " + (proc.stderr.strip() or f"exit {proc.returncode}")
                    + ". Scanning needs root; add the strip by MAC address instead "
                    "(custom parameter key = MAC, value = name)."
                )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        except FileNotFoundError as exc:
            raise TransportError("hccontrol not found on this system") from exc
        finally:
            # A killed le_scan leaves the controller scanning; switch it off.
            subprocess.run(base + ["le_set_scan_enable", "disable"], capture_output=True, timeout=10)
        devices = parse_le_scan(out)
        from lotus_led.protocol import DEVICE_NAME_PATTERNS, KNOWN_MAC_PREFIX

        def is_strip(d):
            name = d["name"].upper()
            return any(p in name for p in DEVICE_NAME_PATTERNS) or d["address"].startswith(KNOWN_MAC_PREFIX)

        return [d for d in devices if is_strip(d)]

    # -- write ---------------------------------------------------------------

    def _types_to_try(self) -> List[int]:
        if self.addr_type == "public":
            return [BDADDR_LE_PUBLIC]
        if self.addr_type == "random":
            return [BDADDR_LE_RANDOM]
        return [BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM]

    def _lock_for(self, mac: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(mac, threading.Lock())

    def write(self, address: str, frames: List[bytes]) -> None:
        mac = address.strip().upper()
        with self._lock_for(mac):
            last_error: Optional[Exception] = None
            for attempt in (1, 2):
                link = self._links.get(mac)
                try:
                    if link is None or not link.connected:
                        link = self._open(mac, link)
                    link.write_frames(frames)
                    return
                except TransportError as exc:
                    last_error = exc
                    if link is not None:
                        link.close()
                    if attempt == 1:
                        time.sleep(1.0)
            raise TransportError(str(last_error))

    def _open(self, mac: str, previous: Optional[AttLink]) -> AttLink:
        # Reuse the address type and handle that worked before.
        types = [previous.addr_type] if previous and previous.write_handle else self._types_to_try()
        errors = []
        for addr_type in types:
            link = AttLink(mac, addr_type)
            try:
                link.connect()
                if previous and previous.write_handle and previous.addr_type == addr_type:
                    link.write_handle = previous.write_handle
                self._links[mac] = link
                return link
            except TransportError as exc:
                link.close()
                errors.append(str(exc))
        raise TransportError(errors[-1] if errors else f"cannot connect to {mac}")

    def _reaper(self) -> None:
        """Drop idle links so the Lotus Lantern phone app can connect again."""
        while not self._stop.wait(5):
            if self.idle_timeout <= 0:
                continue
            now = time.monotonic()
            for mac, link in list(self._links.items()):
                lock = self._lock_for(mac)
                if link.connected and now - link.last_used > self.idle_timeout and lock.acquire(False):
                    try:
                        link.close()
                    finally:
                        lock.release()

    def close(self) -> None:
        self._stop.set()
        for link in self._links.values():
            link.close()
