# Changelog

## 0.0.1 — 2026-10-11

First release, renamed from the working name `udi-lotus-pg3`.

- Controls BLEDOM / ELK-BLEDOM strips (Lotus Lantern app): on/off, dimming,
  RGB colour, 12 presets, white temperature, 29 firmware effects, effect speed.
- `transport=native` (default on eisy): uses the eisy's built-in Bluetooth
  through FreeBSD's kernel stack. Not yet tested on real hardware.
- `transport=bridge`: optional Raspberry Pi BLE relay for strips out of range.
- `tools/eisy_ble_probe.py` to check the eisy's Bluetooth before installing.
