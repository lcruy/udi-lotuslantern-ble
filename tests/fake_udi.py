"""Minimal stand-in for udi_interface, enough to exercise the nodes offline."""

import logging
import re
import sys
import types
from copy import deepcopy

LOGGER = logging.getLogger("fake_udi")


class Custom:
    def __init__(self, poly, kind):
        self._kind = kind
        self._raw = {}
        self.saves = 0

    def load(self, data, save=False):
        self._raw = dict(data or {})
        if save:
            self.saves += 1

    def __getitem__(self, k):
        return self._raw.get(k)

    def __setitem__(self, k, v):
        self._raw[k] = v
        self.saves += 1

    def __contains__(self, k):
        return k in self._raw

    def keys(self):
        return list(self._raw.keys())

    def delete(self, k):
        self._raw.pop(k, None)

    def clear(self):
        self._raw = {}


class Node:
    id = ""
    drivers = []
    commands = {}

    def __init__(self, poly, primary, address, name):
        self.poly, self.primary, self.address, self.name = poly, primary, address, name
        self.drivers = deepcopy(self.drivers)
        for d in poly.db.get(address, []):
            for mine in self.drivers:
                if mine["driver"] == d["driver"]:
                    mine["value"] = d["value"]

    def getDriver(self, drv):
        for d in self.drivers:
            if d["driver"] == drv:
                return d["value"]
        return None

    def setDriver(self, drv, value, report=True, force=False, uom=None, text=None):
        for d in self.drivers:
            if d["driver"] == drv:
                d["value"] = value

    def reportDrivers(self):
        pass

    def runCmd(self, command):
        self.commands[command["cmd"]](self, command)


class Interface:
    START, STOP, POLL, CUSTOMPARAMS, CUSTOMDATA, DISCOVER = (
        "start", "stop", "poll", "customparams", "customdata", "discover")

    def __init__(self, classes=None):
        self.nodes = {}
        self.subs = []
        self.db = {}

    def subscribe(self, topic, fn, address=None):
        self.subs.append((topic, fn, address))

    def ready(self):
        pass

    def addNode(self, node, conn_status=None, rename=False):
        self.nodes[node.address] = node
        return node

    def getNodes(self):
        return self.nodes

    def getValidAddress(self, name):
        return re.sub(r"[<>`~!@#$%^&*(){}[\]?/\\;:\"'\-]+", "", name.lower())[:14]

    def getValidName(self, name):
        return re.sub(r"[<>`~!@#$%^&*(){}[\]?/\\;:\"']+", "", name)

    def updateProfile(self):
        pass

    def setCustomParamsDoc(self):
        pass

    def stop(self):
        pass


def install():
    mod = types.ModuleType("udi_interface")
    mod.LOGGER = LOGGER
    mod.Custom = Custom
    mod.Node = Node
    mod.Interface = Interface
    sys.modules["udi_interface"] = mod
    return mod
