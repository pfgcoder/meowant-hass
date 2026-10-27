from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {
    # Login and session
    "account",
    "app_key",
    "authorization",
    "password",
    "refreshToken",
    "token",
    "userEmail",
    "userImage",
    "userName",
    "userPhone",
    # The user, the phone, the box and its network
    "device_id",
    "deviceId",
    "from_uid",
    "serialNumber",
    "to_uid",
    "user_id",
    "wifiName",
    # The cats
    "catNickname",
    "icon",
    "nickname",
    "weight",
}

# Each box's visit and clean history: a timeline of every cat's litter box use.
OMITTED_DEVICE_KEYS = {"cat_records", "device_records"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}
    # Serial numbers are the keys of the devices dict, which redaction by key doesn't reach.
    devices = {
        f"device_{number}": {key: value for key, value in item.items() if key not in OMITTED_DEVICE_KEYS}
        for number, item in enumerate(data.get("devices", {}).values(), start=1)
    }
    return {
        "entry": async_redact_data(
            {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            TO_REDACT,
        ),
        "last_update_success": coordinator.last_update_success,
        "data": async_redact_data({**data, "devices": devices}, TO_REDACT),
    }
