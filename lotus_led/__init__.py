"""Vendored subset of lotus-led-python.

Only ``protocol.py`` is copied here, unchanged, from
https://github.com/AlexMelanFromRingo/lotus-led-python
(commit afbd4a01c134602c74962697c2e654a8ac72c2b0, MIT licence -- see LICENSE
in this folder).

``protocol.py`` is pure standard-library Python: it builds the 9-byte BLEDOM
frames and needs no Bluetooth stack. That is what lets this node server run on
an eisy (FreeBSD), where the upstream package's ``bleak`` dependency has no
backend. The actual radio work happens in a transport (see ``transports/``).
"""

from .protocol import (  # noqa: F401
    DEVICE_NAME_PATTERNS,
    KNOWN_MAC_PREFIX,
    MAX_PACKETS_PER_SEC,
    SERVICE_UUID,
    WRITE_UUID,
    HWMode,
    LightChannel,
    Pkt,
    cct_to_rgb,
    hex_frame,
)
