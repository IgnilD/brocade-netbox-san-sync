"""Dotted-decimal netmask -> CIDR prefix length, for ipaddrshow output
(e.g. "255.255.255.0" -> 24)."""
from __future__ import annotations

from typing import Optional


def netmask_to_prefixlen(netmask: Optional[str]) -> Optional[int]:
    if not netmask:
        return None
    try:
        octets = [int(o) for o in netmask.strip().split(".")]
        if len(octets) != 4 or any(not (0 <= o <= 255) for o in octets):
            return None
        bits = "".join(f"{o:08b}" for o in octets)
        if "01" in bits:  # a 0 followed by a 1 means it's not a valid contiguous mask
            return None
        return bits.count("1")
    except (ValueError, AttributeError):
        return None
