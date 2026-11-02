from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UbpetDataUpdateCoordinator
from .writes import async_write_setting

AUTO_CLEAN_DELAY = NumberEntityDescription(
    key="auto_clean_delay",
    name="Auto clean delay",
    icon="mdi:broom",
    translation_key="auto_clean_delay",
    native_min_value=1,
    native_max_value=60,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    entity_category=EntityCategory.CONFIG,
    mode=NumberMode.SLIDER,
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: UbpetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    for serial in coordinator.data.get("devices", {}):
        entities.append(UbpetAutoCleanDelayNumber(coordinator, entry.entry_id, serial))
        entities.append(UbpetBoxFullAlertNumber(coordinator, entry.entry_id, serial))
    async_add_entities(entities)


class UbpetAutoCleanDelayNumber(CoordinatorEntity[UbpetDataUpdateCoordinator], NumberEntity):
    _attr_has_entity_name = True
    entity_description = AUTO_CLEAN_DELAY

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{entry_id}_{serial}_auto_clean_delay"

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
    def native_value(self) -> int | float | None:
        item = self.coordinator.data.get("devices", {}).get(self._serial)
        if not item:
            return None
        return _seconds_to_minutes(item.get("config", {}).get("lazyTime"))

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(round(value))
        item = self.coordinator.data.get("devices", {}).get(self._serial) or {}
        auto_clean = item.get("config", {}).get("sensorSwitch")
        if auto_clean is None:
            # The box takes the delay and the switch as one setting, so a guess could turn auto
            # clean on or off.
            raise HomeAssistantError(
                "Whether auto clean is on isn't known right now, so the delay can't be set without "
                "changing it. Try again after the next update."
            )
        await async_write_setting(
            self.hass,
            self.entity_description.name,
            self.coordinator.client.set_auto_clean_delay,
            self._serial,
            minutes,
            auto_clean == 1,
        )
        await self.coordinator.async_request_refresh()


BOX_FULL_ALERT = NumberEntityDescription(
    key="box_full_alert",
    name="Waste bin full after",
    icon="mdi:delete-alert-outline",
    translation_key="box_full_alert",
    native_min_value=1,
    native_step=1,
    native_unit_of_measurement="uses",
    entity_category=EntityCategory.CONFIG,
    mode=NumberMode.BOX,
)


class UbpetBoxFullAlertNumber(CoordinatorEntity[UbpetDataUpdateCoordinator], NumberEntity):
    _attr_has_entity_name = True
    entity_description = BOX_FULL_ALERT

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{entry_id}_{serial}_box_full_alert_threshold"

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
    def _use_times(self) -> dict[str, Any]:
        return (self.coordinator.data.get("devices", {}).get(self._serial) or {}).get("box_use_times", {})

    @property
    def native_value(self) -> int | None:
        return self._use_times.get("boxFullAlert")

    @property
    def native_max_value(self) -> float:
        # The box reports how many uses its waste bin holds at most.
        return float(self._use_times.get("boxFullMax") or 30)

    async def async_set_native_value(self, value: float) -> None:
        await async_write_setting(self.hass, self.entity_description.name, self.coordinator.client.set_box_full_alert, self._serial, int(round(value)))
        await self.coordinator.async_request_refresh()


def _seconds_to_minutes(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    minutes = seconds / 60
    return int(minutes) if minutes.is_integer() else minutes
