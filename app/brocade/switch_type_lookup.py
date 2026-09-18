"""
Maps a switchshow `switchType` value to a human-readable Brocade
product name -- this is the field that actually identifies the
hardware model; the part number from `chassisshow` is a useful
cross-check/fallback but switchType is the canonical source (Broadcom's
own support docs use it to look up product names too).

Table compiled from Broadcom's official TechDocs "Switch Type" reference
(docs.broadcom.com / techdocs.broadcom.com, Fabric OS Administration
Guide, "Performing Basic Configuration Tasks") and the equivalent NetApp
support KB mirroring the same table ("What is the basis on which
Brocade switch models are determined"). switchType is reported as
`<model>.<hw-revision>` (e.g. "66.1", "162.5") -- the revision suffix
distinguishes hardware sub-revisions (flash chip changes, lifetime
-warranty SKUs, etc.) that don't change the product name, so lookups
here are done on the integer part only.

This list covers the switches likely to actually be encountered (older
fixed-port switches through current-generation G-series and directors).
It is not exhaustive -- Brocade has shipped many embedded/blade variants
over 25+ years. Unrecognized switchTypes fall back to the raw part
number from chassisshow, so an unmapped switch still gets a sensible
(if less pretty) device type rather than failing.
"""
from __future__ import annotations

from typing import Optional

# switchType (integer part) -> Brocade product name
_SWITCH_TYPE_TO_MODEL: dict[int, str] = {
    1: "Brocade 1000",
    2: "Brocade 2800",
    3: "Brocade 2400",
    6: "Brocade 2800",
    7: "Brocade 2000",
    9: "Brocade 3800",
    10: "Brocade 12000",
    58: "Brocade 5000",
    62: "Brocade DCX",
    64: "Brocade 5300",
    66: "Brocade 5100",
    67: "Brocade Encryption Switch",
    69: "Brocade 5410",
    71: "Brocade 300",
    72: "Brocade 5480",
    73: "Brocade 5470",
    74: "Brocade 8000",
    75: "Brocade M5424",
    77: "Brocade DCX-4S",
    87: "Brocade 5460",
    90: "Brocade 8470",
    92: "Brocade VA-40FC",
    95: "Brocade VDX 6720-24",
    96: "Brocade VDX 6730-32",
    97: "Brocade VDX 6720-60",
    98: "Brocade VDX 6730-76",
    108: "Dell M8428-k FCoE Embedded Switch",
    109: "Brocade 6510",
    116: "Brocade VDX 6710",
    117: "Brocade 6547",
    118: "Brocade 6505",
    120: "Brocade DCX 8510-8",
    121: "Brocade DCX 8510-4",
    133: "Brocade 6520",
    148: "Brocade 7840",
    162: "Brocade G620",
    165: "Brocade X6-4",
    166: "Brocade X6-8",
    170: "Brocade G610",
    173: "Brocade G630",
    178: "Brocade 7810",
    179: "Brocade X7-4",
    180: "Brocade X7-8",
    181: "Brocade G720",
    183: "Brocade G620",
    184: "Brocade G630",
    189: "Brocade G730",
}


def model_for_switch_type(switch_type: Optional[str]) -> Optional[str]:
    """switch_type is the raw switchshow value, e.g. "66.1" or "162.5"."""
    if not switch_type:
        return None
    try:
        major = int(float(switch_type))
    except ValueError:
        return None
    return _SWITCH_TYPE_TO_MODEL.get(major)
