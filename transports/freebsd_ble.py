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
import signal
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
BLUETOOTH_PROTO_HCI = 134
BLUETOOTH_PROTO_L2CAP = 135
SOCK_RAW = 3
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
CONNECT_TIMEOUT = 20.0
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


# ── raw HCI commands ─────────────────────────────────────────────────────────

HCI_CMD_PKT = 0x01
OGF_LE = 0x08
OCF_LE_CREATE_CONNECTION_CANCEL = 0x000E


def hci_command_packet(ogf: int, ocf: int, params: bytes = b"") -> bytes:
    opcode = (ogf << 10) | ocf
    return struct.pack("<BHB", HCI_CMD_PKT, opcode, len(params)) + params


def hci_send_command(hci_node: str, ogf: int, ocf: int, params: bytes = b"") -> Optional[str]:
    """Send one HCI command on a raw socket; returns an error string or None.

    hccontrol has no command for some things we need (cancelling a pending
    LE connection), so this speaks to the HCI node directly, the way
    hccontrol itself does: bind + connect a raw socket to the node by name,
    then write the command packet. Needs root. We do not wait for the reply.
    """
    libc = _libc()
    libc.bind.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fd = libc.socket(AF_BLUETOOTH, SOCK_RAW, BLUETOOTH_PROTO_HCI)
    if fd < 0:
        return f"raw HCI socket: {os.strerror(ctypes.get_errno())}"
    try:
        addr = struct.pack("BB32s", 34, AF_BLUETOOTH, hci_node.encode())
        if libc.bind(fd, addr, len(addr)) != 0 or libc.connect(fd, addr, len(addr)) != 0:
            return f"raw HCI socket to {hci_node}: {os.strerror(ctypes.get_errno())}"
        os.write(fd, hci_command_packet(ogf, ocf, params))
        time.sleep(0.2)  # let the controller act before the socket goes away
        return None
    except OSError as exc:
        return f"HCI command failed: {exc.strerror}"
    finally:
        os.close(fd)


def cancel_le_connect(hci_node: str) -> Optional[str]:
    """Tell the controller to stop trying to make an LE connection.

    Without this, a connect that times out on our side keeps running in the
    controller, and every later attempt -- ours or hccontrol's -- fails with
    "Command disallowed". Harmless when nothing is pending.
    """
    if os.geteuid() == 0:
        return hci_send_command(hci_node, OGF_LE, OCF_LE_CREATE_CONNECTION_CANCEL)
    # Not root (PG3 runs node servers as admin): use the root-owned helper
    # that tools/install_privileges.sh installs and allows through sudo.
    if not os.path.exists(CANCEL_HELPER):
        return f"cannot cancel a pending LE connection without root -- {PRIVILEGE_HINT}"
    try:
        p = subprocess.run(_privileged([CANCEL_HELPER, hci_node]), capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cancel helper failed: {exc}"
    if p.returncode != 0:
        return f"cancel helper failed: {(p.stdout + p.stderr).strip()} -- {PRIVILEGE_HINT}"
    return None


# ── one connection ────────────────────────────────────────────────────────────

class AttLink:
    """An LE ATT channel to one device, as a raw file descriptor."""

    def __init__(self, mac: str, addr_type: int, hci_node: str = "ubt0hci"):
        self.mac = mac
        self.addr_type = addr_type
        self.hci_node = hci_node
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
                "Bluetooth stack up on the eisy ('ngctl list' should show ubt0hci)? If not, reboot the eisy."
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
                    cancel_le_connect(self.hci_node)
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
            return ("no Bluetooth adapter is attached to the stack -- restore it with "
                    "a reboot of the eisy (not 'service bluetooth restart', which fails on the eisy)")
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

HCCONTROL = "/usr/sbin/hccontrol"
#: Root-owned copy of tools/hci_cancel.py, put there by tools/install_privileges.sh.
CANCEL_HELPER = "/usr/local/libexec/lotuslantern-hci-cancel"
PRIVILEGE_HINT = ("run 'sudo sh tools/install_privileges.sh' once on the eisy "
                  "so the node server may use hccontrol")


def _sudo() -> Optional[str]:
    """Find sudo even under PG3's minimal PATH (FreeBSD keeps it in /usr/local/bin)."""
    for candidate in (shutil.which("sudo"), "/usr/local/bin/sudo", "/usr/bin/sudo"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _privileged(cmd: List[str]) -> List[str]:
    """Prefix a command with non-interactive sudo unless we are already root."""
    if os.geteuid() == 0:
        return cmd
    sudo = _sudo()
    return [sudo, "-n"] + cmd if sudo else cmd


def _hccontrol_cmd() -> List[str]:
    exe = HCCONTROL if os.path.exists(HCCONTROL) else (shutil.which("hccontrol") or HCCONTROL)
    return _privileged([exe])


def _run_scan(cmd: List[str], seconds: float) -> Tuple[str, str]:
    """Run hccontrol le_scan for `seconds`, keeping everything it printed.

    hccontrol block-buffers stdout into a pipe, so a scan we cut short lost
    its results. Giving it a pseudo-terminal makes stdio line-buffered. (The
    stdbuf trick doesn't survive sudo, which strips LD_PRELOAD, and allowing
    stdbuf through sudo would be as good as full root.)
    """
    import pty

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=subprocess.PIPE,
                                start_new_session=True)
    except FileNotFoundError as exc:
        os.close(master)
        os.close(slave)
        raise TransportError("hccontrol not found on this system") from exc
    os.close(slave)
    chunks = []
    deadline = time.monotonic() + seconds
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([master], [], [], min(left, 0.5))
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:          # EIO: hccontrol exited, pty closed
                    break
                if not data:
                    break
                chunks.append(data)
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            FreeBSDBleTransport._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                FreeBSDBleTransport._signal_group(proc, signal.SIGKILL)
                proc.wait()
        # drain anything printed while stopping
        while True:
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                break
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        os.close(master)
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    return b"".join(chunks).decode(errors="replace").replace("\r\n", "\n"), err


def enable_le(hci_node: str) -> List[str]:
    """Switch LE on in the host controller; returns any problems found.

    Out of the box the eisy's adapter reports "LE Host support: 00" and its
    event masks leave out LE events, so advertising reports and LE connection
    events never reach the stack -- scans come back empty and connects hang.
    These two settings are lost on adapter reset or reboot, so this runs every
    time the transport starts. Both are additive: classic Bluetooth (UDI's
    audio service) keeps working.
    """
    base = _hccontrol_cmd() + ["-n", hci_node]
    problems = []
    for args in (["write_le_host_support", "1", "0"], ["le_enable", "enable"]):
        try:
            p = subprocess.run(base + args, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{args[0]}: {exc}")
            continue
        text = (p.stdout + p.stderr).strip()
        if p.returncode != 0 or "Status:" in text or "ermission" in text:
            if "password is required" in text or "not permitted" in text or "ermission" in text:
                text += " -- " + PRIVILEGE_HINT
            problems.append(f"{args[0]}: {text or 'exit ' + str(p.returncode)}")
    return problems


def le_host_supported(hci_node: str) -> Optional[bool]:
    try:
        p = subprocess.run(_hccontrol_cmd() + ["-n", hci_node, "read_le_host_support"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"LE Host support:\s*(\S+)", p.stdout)
    if not m:
        return None
    return m.group(1) not in ("00", "0", "0x00")


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
        self.le_problems = enable_le(self.hci_node)
        cancel_le_connect(self.hci_node)  # clear any attempt left from a previous run
        threading.Thread(target=self._reaper, name="ble-reaper", daemon=True).start()

    # -- scan ----------------------------------------------------------------

    def scan(self, timeout: float = 8.0, all_devices: bool = False) -> List[dict]:
        if self.le_problems:
            # Try again: the adapter may have been reset since we started.
            self.le_problems = enable_le(self.hci_node)
        base = _hccontrol_cmd() + ["-n", self.hci_node]
        # le_scan counts HCI events rather than seconds; ask for plenty and
        # stop it ourselves after `timeout` seconds.
        out, err = _run_scan(base + ["le_scan", "-v", "-n", "1000"], max(3.0, float(timeout)))
        # A stopped le_scan leaves the controller scanning; switch it off.
        subprocess.run(base + ["le_set_scan_enable", "disable"], capture_output=True, timeout=10)
        if not out.strip() and err.strip():
            raise TransportError(
                "hccontrol le_scan failed: " + err.strip()
                + ". Scanning needs root; add the strip by MAC address instead "
                "(custom parameter key = MAC, value = name)."
            )
        devices = parse_le_scan(out or "")
        if all_devices:
            return devices
        from lotus_led.protocol import DEVICE_NAME_PATTERNS, KNOWN_MAC_PREFIX

        def is_strip(d):
            name = d["name"].upper()
            return any(p in name for p in DEVICE_NAME_PATTERNS) or d["address"].startswith(KNOWN_MAC_PREFIX)

        return [d for d in devices if is_strip(d)]

    @staticmethod
    def _signal_group(proc, sig) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

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
        if self.le_problems:
            self.le_problems = enable_le(self.hci_node)
        # Reuse the address type and handle that worked before.
        types = [previous.addr_type] if previous and previous.write_handle else self._types_to_try()
        errors = []
        for addr_type in types:
            link = AttLink(mac, addr_type, self.hci_node)
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
