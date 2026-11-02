"""Logbook lines for litter box visits. Ported from bojanrajkovic/upet-hass (c7feb81, 167c42b)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_VISIT


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe_visit(event: Event) -> dict[str, Any]:
        data = event.data
        entry = {
            "name": data.get("cat") or "Unidentified cat",
            "message": f"used {data.get('box') or 'the litter box'} ({data.get('weight_kg')} kg, {data.get('duration_s')} s)",
        }
        # Links the entry to the box's Last used by sensor, so the visit shows on the box's page too.
        if data.get("entity_id"):
            entry["entity_id"] = data["entity_id"]
        return entry

    async_describe_event(DOMAIN, EVENT_VISIT, describe_visit)
