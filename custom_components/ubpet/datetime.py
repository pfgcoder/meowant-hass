"""When each box's waste bin was last emptied, which Cleans since bin emptied counts from."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity, DateTimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import UbpetDataUpdateCoordinator

WASTE_BIN_EMPTIED = DateTimeEntityDescription(
    key="waste_bin_emptied",
    name="Waste bin emptied",
    icon="mdi:delete-empty",
    translation_key="waste_bin_emptied",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: UbpetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        UbpetWasteBinEmptied(coordinator, entry.entry_id, serial) for serial in coordinator.data.get("devices", {})
    )


class UbpetWasteBinEmptied(CoordinatorEntity[UbpetDataUpdateCoordinator], DateTimeEntity):
    """Set to now by Reset waste bin, or set by hand when the press was forgotten."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, serial: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self.entity_description = WASTE_BIN_EMPTIED
        self._attr_unique_id = f"{entry_id}_{serial}_{WASTE_BIN_EMPTIED.key}"

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
    def native_value(self) -> datetime | None:
        # Unknown until the bin is first marked emptied.
        return self.coordinator.bin_emptied_at(self._serial)

    async def async_set_value(self, value: datetime) -> None:
        if value > dt_util.utcnow():
            raise HomeAssistantError("The waste bin can't have been emptied in the future. Pick a time that has passed.")
        await self.coordinator.async_set_bin_emptied(self._serial, value)
