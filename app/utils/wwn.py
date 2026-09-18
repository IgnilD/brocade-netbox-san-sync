"""WWN string normalization -- both NetBox core and the SAN plugin
expect colon-separated uppercase hex, e.g. 50:0A:09:81:88:AB:CD:EF."""
from __future__ import annotations

import re

_HEX_ONLY = re.compile(r"[^0-9A-Fa-f]")


def normalize_wwn(raw: str | None) -> str | None:
    if not raw:
        return None
    hexdigits = _HEX_ONLY.sub("", raw).upper()
    if len(hexdigits) != 16:
        return raw.strip().upper() or None  # leave as-is, let NetBox's own validation catch it
    return ":".join(hexdigits[i:i + 2] for i in range(0, 16, 2))
