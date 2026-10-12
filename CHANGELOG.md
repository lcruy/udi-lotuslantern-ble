# Changelog

## 0.0.3 — 2026-10-11

- **Stuck connection attempts are now cancelled.** When a connect timed out,
  the adapter kept trying in the background, so every later attempt failed
  with "Command disallowed". The transport now sends HCI LE Create
  Connection Cancel on timeout and at start-up (raw HCI socket; hccontrol has
  no command for it).
- Connect timeout raised from 12 s to 20 s.
- Probe tool: clears stuck attempts before connecting, `--cancel` to clear
  one on its own, and stops with a "reboot the eisy" message if the HCI node
  is missing.
- Removed advice to run `service bluetooth restart`: it fails on the eisy
  and removes the Bluetooth stack until reboot.

## 0.0.2 — 2026-10-11

Fixes from the first probe run on a real eisy (FreeBSD 14.3, Intel BT 5.2).

- **LE is now switched on before use.** The eisy's adapter starts with
  "LE Host support: 00" and no LE events enabled, so scans heard nothing and
  connects could not complete. The native transport now runs
  `hccontrol write_le_host_support 1 0` and `hccontrol le_enable enable` at
  start-up (and again if they failed). Classic Bluetooth is unaffected.
- **Scan results are no longer lost on timeout.** hccontrol's output is
  line-buffered with stdbuf(1), and the whole process group is stopped when
  the scan time runs out.
- The probe tool shows LE host support before/after, and lists every LE
  device it hears, marking the ones that look like Lotus Lantern strips.
- A notice appears in PG3 if LE could not be switched on.

## 0.0.1 — 2026-10-11

First release, renamed from the working name `udi-lotus-pg3`.

- Controls BLEDOM / ELK-BLEDOM strips (Lotus Lantern app): on/off, dimming,
  RGB colour, 12 presets, white temperature, 29 firmware effects, effect speed.
- `transport=native` (default on eisy): uses the eisy's built-in Bluetooth
  through FreeBSD's kernel stack. Not yet tested on real hardware.
- `transport=bridge`: optional Raspberry Pi BLE relay for strips out of range.
- `tools/eisy_ble_probe.py` to check the eisy's Bluetooth before installing.
