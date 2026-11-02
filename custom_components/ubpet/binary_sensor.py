from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .box_config import has_camera, has_light
from .const import DOMAIN
from .coordinator import UbpetDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class UbpetBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Whether the box has the hardware; None means every box does.
    supported_fn: Callable[[dict[str, Any]], bool] | None = None


def _state(item: dict[str, Any], field: str) -> int | None:
    """A field of the box's latest state, None before the first one.

    The box leaves out fields that are 0, so a missing field reads as 0.
    """
    state = item.get("mqtt_state")
    if not isinstance(state, dict):
        return None
    value = state.get(field, 0)
    return value if isinstance(value, int) else None


def _state_is(field: str, test: Callable[[int], bool]) -> Callable[[dict[str, Any]], bool | None]:
    def value(item: dict[str, Any]) -> bool | None:
        current = _state(item, field)
        return None if current is None else test(current)

    return value


# Fault and light sensors from the box's pushed state, mapped as bojanrajkovic/upet-hass did for the
# MW-SC02 (54e2fde, b96a4bc). Not seen on the SC03 yet, so box_state and motor_state get neutral names:
# on the SC02's app they're the drum, and on a rake box it isn't known yet which part they mean.
STATE_BINARY_SENSORS: tuple[UbpetBinarySensorDescription, ...] = (
    UbpetBinarySensorDescription(
        key="cover_open",
        name="Cover open",
        translation_key="cover_open",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_state_is("lid_state", lambda value: value == 1),
    ),
    UbpetBinarySensorDescription(
        key="part_not_in_place",
        name="Part not in place",
        translation_key="part_not_in_place",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_state_is("box_state", lambda value: value == 1),
    ),
    UbpetBinarySensorDescription(
        key="motor_stuck",
        name="Motor stuck",
        translation_key="motor_stuck",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # The SC02's app names four positions where its drum can stick; see the README.
        value_fn=_state_is("motor_state", lambda value: value in (1, 2, 3, 4)),
        attrs_fn=lambda item: {"position": _state(item, "motor_state") or None},
    ),
    UbpetBinarySensorDescription(
        key="scale_fault",
        name="Scale fault",
        translation_key="scale_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_state_is("weigh_sensor", lambda value: value == 1),
    ),
    UbpetBinarySensorDescription(
        key="camera_fault",
        name="Camera fault",
        translation_key="camera_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_state_is("camera_state", lambda value: value == 1),
        supported_fn=has_camera,
    ),
    UbpetBinarySensorDescription(
        key="light",
        name="Light",
        translation_key="light",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=_state_is("led_state", lambda value: value > 0),
        supported_fn=has_light,
    ),
    UbpetBinarySensorDescription(
        key="firmware_updating",
        name="Firmware updating",
        translation_key="firmware_updating",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_state_is("update_state", lambda value: value == 1),
    ),
)


BINARY_SENSORS: tuple[UbpetBinarySensorDescription, ...] = (
    UbpetBinarySensorDescription(
        key="online",
        name="Online",
        icon="mdi:wifi",
        translation_key="online",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item["online"].get("online"),
    ),
    UbpetBinarySensorDescription(
        key="deodorant_expired",
        name="Deodorant expired",
        icon="mdi:air-filter",
        translation_key="deodorant_expired",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item["deodorant"].get("expired"),
    ),
    UbpetBinarySensorDescription(
        key="sensor_switch",
        name="Sensor enabled",
        icon="mdi:motion-sensor",
        translation_key="sensor_switch",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item["config"].get("sensorSwitch") == 1,
    ),
    UbpetBinarySensorDescription(
        key="camera_switch",
        name="Camera enabled",
        icon="mdi:camera",
        translation_key="camera_switch",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item["config"].get("cameraSwitch") == 1,
        supported_fn=has_camera,
    ),
    *STATE_BINARY_SENSORS,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: UbpetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        UbpetDeviceBinarySensor(coordinator, entry.entry_id, serial, description)
        for serial, item in coordinator.data.get("devices", {}).items()
        for description in BINARY_SENSORS
        if description.supported_fn is None or description.supported_fn(item)
    ]
    async_add_entities(entities)


class UbpetDeviceBinarySensor(CoordinatorEntity[UbpetDataUpdateCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    entity_description: UbpetBinarySensorDescription

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, serial: str, description: UbpetBinarySensorDescription) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{serial}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        item = self.coordinator.data.get("devices", {}).get(self._serial, {})
        device = item.get("device", {})
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer="Meowant",
            name=device.get("deviceName") or self._serial,
            serial_number=self._serial,
        )

    @property
    def is_on(self) -> bool | None:
        item = self.coordinator.data.get("devices", {}).get(self._serial)
        if not item:
            return None
        value = self.entity_description.value_fn(item)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        item = self.coordinator.data.get("devices", {}).get(self._serial)
        if not item or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(item)
