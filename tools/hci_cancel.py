#!/usr/local/bin/python3
"""Cancel a pending Bluetooth LE connection attempt on a FreeBSD HCI node.

Usage:  lotuslantern-hci-cancel [hci_node]      (default ubt0hci)

tools/install_privileges.sh copies this file to
/usr/local/libexec/lotuslantern-hci-cancel, owned by root and not writable by
anyone else, and allows the node server's user to run that copy -- and only
that copy -- through sudo. It is deliberately standalone (standard library
only) so the root-owned copy never imports code from the node server's
folder, which the node server's own user can write to.

It sends HCI "LE Create Connection Cancel" (OGF 0x08, OCF 0x000E). hccontrol
has no command for it. Without it, a connection attempt that times out keeps
running in the controller and every later one fails with "Command
disallowed". Harmless when nothing is pending.
"""

import ctypes
import ctypes.util
import os
import re
import struct
import sys
import time

AF_BLUETOOTH = 36
SOCK_RAW = 3
BLUETOOTH_PROTO_HCI = 134


def main() -> int:
    node = sys.argv[1] if len(sys.argv) > 1 else "ubt0hci"
    if not re.fullmatch(r"[A-Za-z0-9_]{1,31}", node):
        print(f"invalid HCI node name: {node!r}", file=sys.stderr)
        return 2
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    libc.bind.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    libc.connect.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fd = libc.socket(AF_BLUETOOTH, SOCK_RAW, BLUETOOTH_PROTO_HCI)
    if fd < 0:
        print(f"raw HCI socket: {os.strerror(ctypes.get_errno())}", file=sys.stderr)
        return 1
    try:
        addr = struct.pack("BB32s", 34, AF_BLUETOOTH, node.encode())
        if libc.bind(fd, addr, len(addr)) != 0 or libc.connect(fd, addr, len(addr)) != 0:
            print(f"raw HCI socket to {node}: {os.strerror(ctypes.get_errno())}", file=sys.stderr)
            return 1
        opcode = (0x08 << 10) | 0x000E
        os.write(fd, struct.pack("<BHB", 0x01, opcode, 0))
        time.sleep(0.2)
        return 0
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
