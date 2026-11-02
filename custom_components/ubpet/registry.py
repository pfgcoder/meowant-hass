"""Pure device-registry decisions, free of Home Assistant imports so tests can load them.

Ported from bojanrajkovic/upet-hass (ed43881).
"""

from __future__ import annotations

from typing import Any

DOMAIN = "ubpet"


def still_reported(identifiers: set[tuple[str, str]], data: dict[str, Any]) -> bool:
    """True if the vendor account still reports this box or cat.

    Boxes are identified by serial number and cats by `cat_<catInfoId>`. Re-pairing a box
    or recreating a cat profile gives it a new id, leaving the old device behind.
    """
    current = set(data.get("devices") or {}) | {
        f"cat_{cat.get('catInfoId')}" for cat in data.get("cats") or [] if isinstance(cat, dict)
    }
    return any(domain == DOMAIN and value in current for domain, value in identifiers)
