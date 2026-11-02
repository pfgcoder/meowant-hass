from __future__ import annotations

from typing import Any


def empty_waste_bin_reminder_config(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return the box's empty-waste-bin reminder (notification type 4) from its allConfig, if set up."""
    for item in config.get("notifyConfigList") or []:
        if isinstance(item, dict) and item.get("type") == 4 and item.get("notifyConfigId") is not None:
            return item
    return None


# Which hardware a box has, to create entities only for that (bojanrajkovic/upet-hass ad084ff).
# Each keeps the entity when the part it reads didn't load, so an outage at startup never hides one.


def has_camera(item: dict[str, Any]) -> bool:
    """True unless the box's capabilities say it has no camera.

    The app treats the `cat_face_upload` capability as camera-backed cat recognition.
    """
    capabilities = item.get("capabilities")
    listed = capabilities.get("capabilities") if isinstance(capabilities, dict) else None
    if not isinstance(listed, list):
        return True
    return any(isinstance(capability, dict) and capability.get("code") == "cat_face_upload" for capability in listed)


def has_light(item: dict[str, Any]) -> bool:
    """True unless the box's config loaded without a lightSwitch, which boxes without a light leave out."""
    config = item.get("config")
    if not isinstance(config, dict) or not config:
        return True
    return config.get("lightSwitch") is not None


def has_waste_bin_reminder(item: dict[str, Any]) -> bool:
    """True unless the box's config loaded without an empty-waste-bin reminder."""
    config = item.get("config")
    if not isinstance(config, dict) or not config:
        return True
    return empty_waste_bin_reminder_config(config) is not None
