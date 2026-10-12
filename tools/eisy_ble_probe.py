#!/usr/bin/env python3
"""Check that the eisy's built-in Bluetooth can drive a Lotus Lantern strip.

Run on the eisy over SSH, from the node server folder:

    sudo python3 tools/eisy_ble_probe.py                 # adapter + LE scan
    sudo python3 tools/eisy_ble_probe.py --mac BE:60:65:12:34:56 --blink

Close the Lotus Lantern phone app first (one connection at a time). Nothing
here changes any configuration; --blink turns the strip red, green, blue,
then off.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lotus_led.protocol import Pkt  # noqa: E402
from transports import TransportError  # noqa: E402
from transports.freebsd_ble import (  # noqa: E402
    AttLink, FreeBSDBleTransport, BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM, cancel_le_connect,
    le_host_supported,
)


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return (p.stdout + p.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"(failed: {exc})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="ubt0hci", help="HCI node (see 'ngctl list')")
    ap.add_argument("--mac", help="strip MAC to connect to")
    ap.add_argument("--blink", action="store_true", help="cycle colours to prove writes work")
    ap.add_argument("--scan-seconds", type=float, default=15)
    ap.add_argument("--cancel", action="store_true",
                    help="only cancel a stuck LE connection attempt, then exit")
    args = ap.parse_args()

    print("== System")
    print(run(["uname", "-a"]))
    print("running as root" if os.geteuid() == 0 else "NOT root -- scanning will probably fail; use sudo")

    print("\n== Bluetooth netgraph nodes")
    nodes = run(["ngctl", "list"]) if shutil.which("ngctl") else "ngctl not found"
    print(nodes)
    if f"Name: {args.node} " not in nodes:
        print(f"\nThe HCI node '{args.node}' is missing, so the Bluetooth stack is not set up."
              "\nReboot the eisy to restore it (don't use 'service bluetooth restart' -- the eisy"
              "\nsets Bluetooth up its own way at boot), then run this again.")
        return

    if args.cancel:
        err = cancel_le_connect(args.node)
        print("\nCancelled any pending LE connection." if not err else f"\nCancel failed: {err}")
        return

    hcc = shutil.which("hccontrol") or "/usr/sbin/hccontrol"
    print(f"\n== Adapter {args.node}")
    print(run([hcc, "-n", args.node, "read_local_version_information"]))
    print(run([hcc, "-n", args.node, "read_le_host_support"]))
    print(run([hcc, "-n", args.node, "le_read_local_supported_features"]))

    print("\n== Enabling LE on the host (write_le_host_support 1 0, le_enable enable)")
    print(f"  before: LE host support = {le_host_supported(args.node)}")
    transport = FreeBSDBleTransport(hci_node=args.node)
    for problem in transport.le_problems:
        print(f"  problem: {problem}")
    print(f"  after:  LE host support = {le_host_supported(args.node)}")

    print(f"\n== LE scan ({args.scan_seconds:.0f}s, every device heard)")
    try:
        found = transport.scan(args.scan_seconds, all_devices=True)
        from lotus_led.protocol import DEVICE_NAME_PATTERNS, KNOWN_MAC_PREFIX
        for d in sorted(found, key=lambda x: x["name"] == ""):
            strip = any(p in d["name"].upper() for p in DEVICE_NAME_PATTERNS) or d["address"].startswith(KNOWN_MAC_PREFIX)
            print(f"  {'*' if strip else ' '} {d['name'] or '(no name)':22} {d['address']}  ({d['addr_type'] or '?'})")
        print(f"  {len(found)} device(s) heard; * = looks like a Lotus Lantern / BLEDOM strip")
        if not found:
            print("  Nothing at all was heard -- send this output back.")
    except TransportError as exc:
        print(f"  scan failed: {exc}")

    if not args.mac:
        print("\nRe-run with --mac <address> --blink to test control.")
        return

    print(f"\n== Connect to {args.mac}")
    err = cancel_le_connect(args.node)  # clear anything left over from earlier attempts
    print("  cleared any stuck connection attempt" if not err else f"  could not clear old attempts: {err}")
    for name, kind in (("public", BDADDR_LE_PUBLIC), ("random", BDADDR_LE_RANDOM)):
        link = AttLink(args.mac.upper(), kind, args.node)
        try:
            link.connect()
            handle = link.discover_write_handle()
            print(f"  connected ({name} address), FFF3 value handle = 0x{handle:04x}")
            if args.blink:
                for label, frames in (("on", [Pkt.power_on(), Pkt.brightness(100)]),
                                      ("red", [Pkt.color(255, 0, 0)]), ("green", [Pkt.color(0, 255, 0)]),
                                      ("blue", [Pkt.color(0, 0, 255)]), ("off", [Pkt.power_off()])):
                    print(f"  -> {label}")
                    link.write_frames(frames)
                    time.sleep(1.2)
            print(f"\nSUCCESS. Use transport=native, addr_type={name} in the node server.")
            return
        except TransportError as exc:
            print(f"  {name} address: {exc}")
        finally:
            link.close()
    print("\nCould not control the strip natively. Send this whole output back for diagnosis, "
          "or use transport=bridge with a Raspberry Pi.")


if __name__ == "__main__":
    main()
