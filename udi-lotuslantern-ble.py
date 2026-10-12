#!/usr/bin/env python3
"""udi-lotuslantern-ble: Lotus Lantern (BLEDOM / ELK-BLEDOM) node server for Polyglot v3 on eisy / Polisy."""

import sys

import udi_interface

from nodes import LotusController, LotusStrip

LOGGER = udi_interface.LOGGER
VERSION = "0.0.3"

if __name__ == "__main__":
    polyglot = None
    try:
        polyglot = udi_interface.Interface([LotusController, LotusStrip])
        polyglot.start(VERSION)
        LotusController(polyglot, "controller", "controller", "Lotus Lantern")
        polyglot.runForever()
    except (KeyboardInterrupt, SystemExit):
        LOGGER.warning("Received interrupt or exit...")
        if polyglot is not None:
            polyglot.stop()
    except Exception as err:  # noqa: BLE001
        LOGGER.error("Exception: %s", err, exc_info=True)
    sys.exit(0)
