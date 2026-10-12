# Lotus Lantern (BLEDOM / ELK-BLEDOM) configuration

By default (`transport=native`) the node server uses the eisy's own
Bluetooth adapter — no extra hardware. Strips need to be within Bluetooth
range of the eisy (roughly 10 m indoors). For strips further away, run
**lotus_bridge.py** on a Raspberry Pi near them and set `transport=bridge`.

| Parameter | Meaning |
|---|---|
| `transport` | `native` (eisy Bluetooth, default), `bridge` (Raspberry Pi relay) or `local` (PG3 on Linux) |
| `hci_node` | Native only: the Bluetooth HCI node, normally `ubt0hci` (`ngctl list`) |
| `addr_type` | Native only: `auto`, `public` or `random` — the probe tool reports which one works |
| `bridge_url` | Bridge only: e.g. `http://192.168.1.50:8765` |
| `bridge_token` | Must match the bridge's `--token` / `LOTUS_BRIDGE_TOKEN`. Leave blank if the bridge has none. |
| `scan_seconds` | How long **Discover Strips** listens for (default 8) |
| `auto_add` | `true` adds every strip found; `false` lists them in a notice instead |

## Adding strips

* Press **Discover Strips** on the Lotus Lantern node in the admin console
  (native scanning needs root; if it fails, use the MAC method), or
* add a parameter whose **key is the strip's MAC address** and whose value is
  its name, e.g. `BE:60:65:12:34:56` = `Kitchen Cabinets`.

## Things to know

* **Close the Lotus Lantern phone app.** The controller accepts one Bluetooth
  connection at a time. The node server lets go after 60 s idle so the app can
  connect again.
* The strip never reports its state. The node shows what was last delivered;
  if someone uses the remote or the app, run **Resend State** (or a program)
  to put it back.
* **Link = Error** means the last command did not arrive — check range,
  power, and that the phone app is closed.
