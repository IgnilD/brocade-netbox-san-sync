"""
NetBox core (dcim.choices.InterfaceTypeChoices) already ships Fibre
Channel interface types -- no custom_field or choice-set setup needed
on your side. We just need to pick the right slug for a port's
negotiated (or, if not currently negotiated, max-capable) speed.

Slugs below were verified against a live NetBox instance's own
`OPTIONS /api/dcim/interfaces/` response (not just assumed from
memory) -- notably, 8G/16G use the abbreviated "-sfpp" suffix ("SFP+"),
not "-sfpplus". 32G and 64G each have more than one valid connector
variant on current NetBox (e.g. 32gfc-sfp28 vs 32gfc-sfpp); the more
common/modern one is picked as the default here. If your instance
prefers the other variant, or rejects one of these for any other
version-specific reason, `NetBoxSyncClient._safe_interface_type()`
double-checks against your instance's actual OPTIONS response at
runtime and falls back to "other" rather than failing the whole sync.
"""
from __future__ import annotations

from typing import Optional

# (max Gbps this type covers, slug) -- ordered ascending, first match wins
_SPEED_TO_SLUG = [
    (1, "1gfc-sfp"),
    (2, "2gfc-sfp"),
    (4, "4gfc-sfp"),
    (8, "8gfc-sfpp"),
    (16, "16gfc-sfpp"),
    (32, "32gfc-sfp28"),
    (64, "64gfc-qsfpp"),
    (128, "128gfc-qsfp28"),
]

FALLBACK_SLUG = "other"


def slug_for_speed(speed_gbps: Optional[float]) -> str:
    if not speed_gbps:
        return FALLBACK_SLUG
    for max_gbps, slug in _SPEED_TO_SLUG:
        if speed_gbps <= max_gbps:
            return slug
    return _SPEED_TO_SLUG[-1][1]


def kbps_for_speed(speed_gbps: Optional[float]) -> Optional[int]:
    """NetBox Interface.speed is stored in Kbps."""
    if not speed_gbps:
        return None
    return int(speed_gbps * 1_000_000)
