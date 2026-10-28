from __future__ import annotations

from typing import Any


def empty_waste_bin_reminder_config(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the box's empty-waste-bin reminder (notification type 4) from its allConfig, if set up."""
    for item in config.get("notifyConfigList") or []:
        if isinstance(item, dict) and item.get("type") == 4 and item.get("notifyConfigId") is not None:
            return item
    return None
