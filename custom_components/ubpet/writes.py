"""Settings changes shared by the entity platforms."""

from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import UbpetPermissionError


async def async_write_setting(hass: HomeAssistant, setting: Any, func: Callable[..., Any], *args: Any) -> Any:
    """Send a settings change in the executor, with a readable error if the account may not make it."""
    try:
        return await hass.async_add_executor_job(func, *args)
    except UbpetPermissionError as err:
        raise HomeAssistantError(permission_message(setting)) from err


def permission_message(setting: Any) -> str:
    what = f'"{setting}"' if isinstance(setting, str) and setting else "this setting"
    return (
        f"The account Home Assistant uses can't change {what}. The box is shared with that account, "
        "and the vendor only lets the box's owner change this setting. Change it in the Air Pet app "
        "with the owner's account, or reconfigure the integration with it."
    )
