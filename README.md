# udi-lotuslantern-ble — Lotus Lantern node server for eisy (PG3x)

Version 0.0.3

Control **BLEDOM / ELK-BLEDOM** Bluetooth LED strips — the ones set up with the
**Lotus Lantern** phone app — from an eisy or Polisy: on/off, dimming, RGB
colour, colour presets, white temperature, all 29 firmware effects and effect
speed, usable in IoX programs and scenes.

Frame layouts come from [lotus-led-python](https://github.com/AlexMelanFromRingo/lotus-led-python)
(MIT); its `protocol.py` is vendored unchanged in `lotus_led/`.

## How it fits together

Two ways to reach the strips — pick one with the `transport` parameter:

```
 transport=native (default on eisy)
 ┌───────────────────────────────┐   BLE (ATT write to FFF3)   ┌────────────┐
 │ eisy: node server             │ ──────────────────────────► │ ELK-BLEDOM │
 │  + FreeBSD kernel BT stack    │                             └────────────┘
 └───────────────────────────────┘

 transport=bridge (strips out of the eisy's range)
 ┌──────────────┐  HTTP/LAN  ┌──────────────────────────┐  BLE  ┌────────────┐
 │ eisy         │ ─────────► │ Raspberry Pi:            │ ────► │ ELK-BLEDOM │
 │ node server  │            │ bridge/lotus_bridge.py   │       └────────────┘
 └──────────────┘            └──────────────────────────┘
```

**Native (eisy Bluetooth).** `bleak`, the library lotus-led-python is built
on, has no FreeBSD backend, but FreeBSD's own Bluetooth stack already does LE
scanning (`hccontrol le_scan`) and LE connections on the ATT channel through
L2CAP sockets. `transports/freebsd_ble.py` adds the one missing piece: a tiny
GATT client that finds the strip's FFF3 characteristic and writes frames to
it. No extra hardware, no extra packages.

> **Status:** written against the FreeBSD source and tested against a
> simulated strip, but not yet on a real eisy. Run the probe below first.

**Bridge.** A small, generic HTTP-to-BLE relay for a Raspberry Pi (or any
Linux / Windows / macOS machine) placed near strips that are out of the
eisy's radio range. It knows nothing about LED strips beyond a scan filter.

If you run PG3 on Linux with Bluetooth instead of an eisy, set
`transport=local`.

## 0. Test the eisy's Bluetooth first

SSH into the eisy, copy this folder there, close the Lotus Lantern phone app,
then:

```bash
cd udi-lotuslantern-ble
sudo python3 tools/eisy_ble_probe.py                          # adapter info + LE scan
sudo python3 tools/eisy_ble_probe.py --mac BE:60:65:xx:xx:xx --blink
```

`--blink` cycles the strip red, green, blue, then off. If it ends with
**SUCCESS**, use `transport=native` and the `addr_type` it reports. If not,
send the output back — or use the bridge.

If the adapter isn't found, check `ngctl list` for the HCI node name
(normally `ubt0hci`; set `hci_node` if it differs). If no `ubt0hci` node is
listed, **reboot the eisy**. Do not run `service bluetooth restart`: the eisy
sets Bluetooth up its own way at boot, and FreeBSD's standard script fails on
it and removes the stack.

If a connection attempt ever gets stuck ("Command disallowed" from
`hccontrol le_connect`), clear it with
`sudo python3 tools/eisy_ble_probe.py --cancel`.

Discovery from the admin console runs `hccontrol le_scan`, which needs root.
If it can't run from PG3, add strips by MAC instead (see below).

## 1. (Optional) Set up the bridge (Raspberry Pi example)

Any Pi with built-in Bluetooth (3B+, 4, 5, Zero 2 W) running Raspberry Pi OS:

```bash
mkdir -p ~/lotus-bridge && cd ~/lotus-bridge
# copy bridge/lotus_bridge.py here, then:
python3 -m venv venv
venv/bin/pip install bleak
venv/bin/python lotus_bridge.py --port 8765 --token change-me -v
```

Check it from another machine:

```bash
curl -H 'X-Bridge-Token: change-me' 'http://PI-IP:8765/scan?timeout=8'
```

To run it at boot, edit and install `bridge/lotus-bridge.service` (instructions
at the top of the file).

Options: `--idle 60` disconnects a strip after 60 idle seconds so the phone
app can still connect (0 keeps the link open — fastest, but locks the app
out). `--token` / `LOTUS_BRIDGE_TOKEN` requires an `X-Bridge-Token` header.

Windows or macOS also work: `pip install bleak`, then run the same script.

## 2. Install the node server on the eisy

Until it is published in the PG3 store, install it as a local node server:

1. Copy this folder to the eisy (e.g. `/home/admin/udi-lotuslantern-ble`), or push it
   to a GitHub repo.
2. Install it in a free slot with PG3x's local / development install option
   (see the PG3x documentation on the UDI wiki for the current menu path).
3. Open the node server's **Configuration** page. With `transport=native`
   there is nothing else to set (`hci_node`, `addr_type` only if the probe
   said so). With `transport=bridge`, set `bridge_url` and `bridge_token`.
4. Press **Discover Strips** on the *Lotus Lantern* node in the admin console,
   or add a parameter `BE:60:65:xx:xx:xx` = `Strip name`.

Close the Lotus Lantern phone app first — the controllers accept one Bluetooth
connection at a time.

## Nodes

**Lotus Lantern** (controller) — Online, Strips (count), Bridge
Radio (eisy Bluetooth / bridge Online / Unreachable / Not configured / Local). Commands: Discover Strips,
All On, All Off, Query.

**Lotus LED Strip** — one per strip.

| Status | Meaning |
|---|---|
| Status | 0–100 % (0 when off) — behaves like a dimmer in scenes |
| Red / Green / Blue | Last solid colour |
| Effect | Solid Color, or one of the 29 firmware effects |
| Effect Speed | 0–100 % |
| Brightness | Level used by a plain *On* |
| Link | OK / Error for the last command |

Commands: On (optional level), Off, Fast On/Off, Brighten, Dim, Set
Brightness, Set Color (R, G, B), Set Color Preset, Set White Temperature
(1500–9000 K), Set Effect, Set Effect Speed, Resend State, Query.

**State is a shadow.** The strip firmware never reports anything back, so the
node shows what it last delivered successfully. A change made from the IR
remote or the phone app is invisible; *Resend State* puts the strip back to
what the node shows (handy in a program after a power cut).

## Tests

```bash
python3 tests/test_nodeserver.py
```

```bash
python3 tests/test_freebsd_ble.py
```

Both run offline: the first against a stand-in for `udi_interface`, a fake
transport and an HTTP round trip through the bridge; the second runs the
native GATT client against a simulated BLEDOM GATT server.

## Other open-source software for these strips

* [lotus-led-python](https://github.com/AlexMelanFromRingo/lotus-led-python)
  and its Rust twin `lotus-led` — desktop control, audio sync, ambilight.
* Home Assistant: the `elkbledom` custom integration (HACS).
* Homebridge / HomeKit: `@cagdaskemik/homekit-bledom`.
