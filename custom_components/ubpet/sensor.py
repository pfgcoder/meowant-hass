from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import UbpetDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class UbpetSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


# Record eventTypes. Cat records: 3 is a visit matched to a cat (21 is a weight the box couldn't
# match). Device records: 31 is a finished auto clean, listed as "Auto cleaned" in the app.
VISIT_EVENT = 3
AUTO_CLEANED_EVENT = 31

# Daily counts. They're totals that restart at local midnight (last_reset), so a dip during the
# day, such as a record falling out of the fetched window, isn't read as a new meter cycle.
TODAY_COUNT_KEYS = {"visits_today", "cleans_today", "cat_visits_today"}

# Each MQTT reply's message bookkeeping (and the live scale reading), which differs on every poll.
# Kept off the MQTT sensors' attributes so an unchanged poll doesn't write a new state row.
MQTT_PER_REPLY_KEYS = frozenset(
    {
        "cid",
        "gid",
        "created_at_ms",
        "from_uid",
        "to_uid",
        "received_phase",
        "received_topic",
        "received_qos",
        "mqtt_service",
        "command_cid",
        "command_gid",
        "command_created_at_ms",
        "protocol_version",
        "content_type",
        "type_url",
        "command_protype",
        "risp_cmd",
        "risp_seq",
        "risp_attr",
        "risp_data_len",
        "risp_body_hex",
        "timestamp",
        "weigh_sensor",
    }
)


DEVICE_SENSORS: tuple[UbpetSensorDescription, ...] = (
    UbpetSensorDescription(
        key="box_status",
        name="Box status",
        icon="mdi:list-status",
        translation_key="box_status",
        value_fn=lambda item: _box_status(item),
    ),
    UbpetSensorDescription(
        key="last_visit",
        name="Last visit",
        icon="mdi:cat",
        translation_key="last_visit",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda item: _milliseconds_to_datetime(_latest_cat_record(item).get("eventTime")),
    ),
    UbpetSensorDescription(
        key="last_used_by",
        name="Last used by",
        icon="mdi:cat",
        translation_key="last_used_by",
        value_fn=lambda item: _latest_visit(item).get("catNickname"),
    ),
    UbpetSensorDescription(
        key="visits_today",
        name="Visits today",
        icon="mdi:counter",
        translation_key="visits_today",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda item: _count_today(_cat_records(item), VISIT_EVENT),
    ),
    UbpetSensorDescription(
        key="cleans_today",
        name="Cleans today",
        icon="mdi:broom",
        translation_key="cleans_today",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda item: _count_today(_device_records(item), AUTO_CLEANED_EVENT),
    ),
    UbpetSensorDescription(
        key="empty_waste_bin_reminder_days",
        name="Empty waste bin reminder days",
        icon="mdi:calendar-week",
        translation_key="empty_waste_bin_reminder_days",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: _empty_waste_bin_reminder_days(item["config"]),
    ),
    UbpetSensorDescription(
        key="box_use_times",
        name="Box use times",
        icon="mdi:counter",
        translation_key="box_use_times",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item["box_use_times"].get("boxUseTimes"),
    ),
    UbpetSensorDescription(
        key="waste_bin_level",
        name="Waste bin level",
        icon="mdi:delete-outline",
        translation_key="waste_bin_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item["box_use_times"].get("percentage"),
    ),
    UbpetSensorDescription(
        key="waste_bin_last_reset",
        name="Waste bin last reset",
        icon="mdi:history",
        translation_key="waste_bin_last_reset",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda item: _milliseconds_to_datetime(item["box_use_times"].get("boxUseTimesResetTime")),
    ),
    UbpetSensorDescription(
        key="box_full_max",
        name="Box full max",
        icon="mdi:delete",
        translation_key="box_full_max",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item["box_use_times"].get("boxFullMax"),
    ),
    UbpetSensorDescription(
        key="box_full_alert",
        name="Box full alert",
        icon="mdi:alert-outline",
        translation_key="box_full_alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item["box_use_times"].get("boxFullAlert"),
    ),
    UbpetSensorDescription(
        key="deodorant_remaining_days",
        name="Deodorant remaining days",
        icon="mdi:air-filter",
        translation_key="deodorant_remaining_days",
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item["deodorant"].get("remainingDays"),
    ),
    UbpetSensorDescription(
        key="auto_clean_delay_current",
        name="Auto clean delay",
        icon="mdi:broom",
        translation_key="auto_clean_delay_current",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: _seconds_to_minutes(item["config"].get("lazyTime")),
    ),
    UbpetSensorDescription(
        key="firmware_version",
        name="Firmware version",
        icon="mdi:chip",
        translation_key="firmware_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: _firmware_version(item["config"]),
    ),
    UbpetSensorDescription(
        key="wifi_name",
        name="Wi-Fi name",
        icon="mdi:wifi",
        translation_key="wifi_name",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: item["config"].get("wifiName"),
    ),
    UbpetSensorDescription(
        key="mqtt_work_mode",
        name="MQTT work mode",
        icon="mdi:robot-vacuum",
        translation_key="mqtt_work_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: _mqtt_state_value(item, "w_mode_app_name"),
    ),
    UbpetSensorDescription(
        key="mqtt_work_state",
        name="MQTT work state",
        icon="mdi:state-machine",
        translation_key="mqtt_work_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: _mqtt_state_value(item, "w_state_app_name"),
    ),
    UbpetSensorDescription(
        key="mqtt_work_cause",
        name="MQTT work cause",
        icon="mdi:alert-circle-outline",
        translation_key="mqtt_work_cause",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda item: _mqtt_state_value(item, "w_cause"),
    ),
    UbpetSensorDescription(
        key="last_rest_update",
        name="Last REST update",
        icon="mdi:cloud-check-outline",
        translation_key="last_rest_update",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Changes on every poll, which floods the logbook and recorder.
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda item: _runtime_state_value(item, "last_rest_update_at"),
    ),
    UbpetSensorDescription(
        key="last_mqtt_update",
        name="Last MQTT update",
        icon="mdi:message-check-outline",
        translation_key="last_mqtt_update",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Changes on every poll, which floods the logbook and recorder.
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda item: _runtime_state_value(item, "last_mqtt_update_at"),
    ),
)

CAT_SENSORS: tuple[UbpetSensorDescription, ...] = (
    UbpetSensorDescription(
        key="cat_weight",
        name="Weight",
        icon="mdi:scale",
        translation_key="cat_weight",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item.get("weight"),
    ),
    UbpetSensorDescription(
        key="cat_visits",
        name="Visits",
        icon="mdi:cat",
        translation_key="cat_visits",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item.get("number"),
    ),
    UbpetSensorDescription(
        key="cat_cost_time",
        name="Usage duration",
        icon="mdi:timer-outline",
        translation_key="cat_cost_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda item: item.get("costTime"),
    ),
    UbpetSensorDescription(
        key="cat_visits_today",
        name="Visits today",
        icon="mdi:counter",
        translation_key="cat_visits_today",
        state_class=SensorStateClass.TOTAL,
        # Needs every box's records, so UbpetCatSensor.native_value counts it.
        value_fn=lambda item: None,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: UbpetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for serial in coordinator.data.get("devices", {}):
        entities.extend(UbpetDeviceSensor(coordinator, entry.entry_id, serial, description) for description in DEVICE_SENSORS)
    for cat in coordinator.data.get("cats", []):
        cat_id = cat.get("catInfoId")
        if cat_id is None:
            continue
        entities.extend(UbpetCatSensor(coordinator, entry.entry_id, str(cat_id), description) for description in CAT_SENSORS)
    async_add_entities(entities)


class UbpetDeviceSensor(CoordinatorEntity[UbpetDataUpdateCoordinator], SensorEntity):
    entity_description: UbpetSensorDescription

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, serial: str, description: UbpetSensorDescription) -> None:
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
    def native_value(self) -> Any:
        if self.entity_description.key == "last_rest_update":
            return self.coordinator.last_rest_update_at
        if self.entity_description.key == "last_mqtt_update":
            return self.coordinator.last_mqtt_update_at(self._serial)
        item = self.coordinator.data.get("devices", {}).get(self._serial)
        if not item:
            return None
        return self.entity_description.value_fn(item)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.key in TODAY_COUNT_KEYS:
            return dt_util.start_of_local_day()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        item = self.coordinator.data.get("devices", {}).get(self._serial)
        if not item:
            return None
        return {
            "serial_number": self._serial,
            **self._extra_status_attributes(item),
        }

    def _extra_status_attributes(self, item: dict[str, Any]) -> dict[str, Any]:
        if self.entity_description.key == "box_status":
            return {
                "device_status_code": item.get("device", {}).get("deviceStatus"),
                "mqtt_work_state": _mqtt_state_value(item, "w_state_app_name"),
            }
        if self.entity_description.key == "last_visit":
            return _last_visit_attributes(item)
        if self.entity_description.key == "last_used_by":
            visit = _latest_visit(item)
            return _visit_summary(visit) if visit else {}
        if self.entity_description.key.startswith("mqtt_"):
            mqtt_state = item.get("mqtt_state")
            if not isinstance(mqtt_state, dict):
                return {}
            return {key: value for key, value in mqtt_state.items() if key not in MQTT_PER_REPLY_KEYS}
        if self.entity_description.key in {
            "last_rest_update",
            "last_mqtt_update",
        }:
            runtime_state = item.get("runtime_state")
            return runtime_state if isinstance(runtime_state, dict) else {}
        return {}


class UbpetCatSensor(CoordinatorEntity[UbpetDataUpdateCoordinator], SensorEntity):
    entity_description: UbpetSensorDescription

    def __init__(self, coordinator: UbpetDataUpdateCoordinator, entry_id: str, cat_id: str, description: UbpetSensorDescription) -> None:
        super().__init__(coordinator)
        self._cat_id = cat_id
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_cat_{cat_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        cat = self._cat_data or {}
        return DeviceInfo(
            identifiers={(DOMAIN, f"cat_{self._cat_id}")},
            manufacturer="Meowant",
            name=cat.get("nickname") or f"Cat {self._cat_id}",
        )

    @property
    def native_value(self) -> Any:
        cat = self._cat_data
        if cat is None:
            return None
        if self.entity_description.key == "cat_visits_today":
            per_box = [_cat_records(item) for item in self.coordinator.data.get("devices", {}).values()]
            if not all(per_box):
                # A box whose records didn't load would make the count drop, so report unknown.
                return None
            return _count_today([record for records in per_box for record in records], VISIT_EVENT, cat)
        return self.entity_description.value_fn(cat)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.key in TODAY_COUNT_KEYS:
            return dt_util.start_of_local_day()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cat = self._cat_data
        if cat is None:
            return None
        return {
            "cat_info_id": cat.get("catInfoId"),
            "nickname": cat.get("nickname"),
            "picture": cat.get("icon"),
        }

    @property
    def _cat_data(self) -> dict[str, Any] | None:
        for cat in self.coordinator.data.get("cats", []):
            if str(cat.get("catInfoId")) == self._cat_id:
                return cat
        return None


def _firmware_version(config: dict[str, Any]) -> str | None:
    versions = sorted(
        {
            part.get("firmwareVersion")
            for part in config.get("boxFunctionVOList") or []
            if part.get("firmwareVersion")
        }
    )
    return ", ".join(versions) if versions else None


def _seconds_to_minutes(value: Any) -> int | float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    minutes = seconds / 60
    return int(minutes) if minutes.is_integer() else minutes


def _milliseconds_to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def _box_status(item: dict[str, Any]) -> str | None:
    mqtt_status = _box_status_from_mqtt(item)
    if mqtt_status is not None:
        return mqtt_status
    device_status = item.get("device", {}).get("deviceStatus")
    if device_status is None:
        return None
    try:
        status = int(device_status)
    except (TypeError, ValueError):
        return str(device_status)
    if status == 1:
        return "Standby"
    if status == 3:
        return "Paused"
    return "Running"


def _box_status_from_mqtt(item: dict[str, Any]) -> str | None:
    state = _mqtt_state_value(item, "w_state_app_name")
    if state == "PENDING":
        return "Standby"
    if state == "RUNNING":
        return "Running"
    if state == "PAUSED":
        return "Paused"
    if state == "BLOCKED":
        # A cat in the box (or something on the scale) is holding the box.
        return "Blocked"
    return None


def _empty_waste_bin_reminder_days(config: dict[str, Any]) -> str | None:
    reminder = _empty_waste_bin_reminder_config(config)
    if reminder is None:
        return None
    try:
        period = int(reminder.get("period") or 0)
    except (TypeError, ValueError):
        return None
    days = [
        name
        for bit, name in (
            (1, "Monday"),
            (2, "Tuesday"),
            (4, "Wednesday"),
            (8, "Thursday"),
            (16, "Friday"),
            (32, "Saturday"),
            (64, "Sunday"),
        )
        if period & bit
    ]
    if len(days) == 7:
        return "Every day"
    return ", ".join(days) if days else "Off"


def _empty_waste_bin_reminder_config(config: dict[str, Any]) -> dict[str, Any] | None:
    for item in config.get("notifyConfigList") or []:
        if isinstance(item, dict) and item.get("type") == 4 and item.get("notifyConfigId") is not None:
            return item
    return None


def _cat_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the box's visit records, newest first."""
    return _records_newest_first(item.get("cat_records"))


def _device_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the box's own records (cleans and the like), newest first."""
    return _records_newest_first(item.get("device_records"))


def _records_newest_first(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    dated = [
        record
        for record in records
        if isinstance(record, dict) and _milliseconds_to_datetime(record.get("eventTime")) is not None
    ]
    return sorted(dated, key=lambda record: _milliseconds_to_datetime(record.get("eventTime")), reverse=True)


def _latest_cat_record(item: dict[str, Any]) -> dict[str, Any]:
    records = _cat_records(item)
    return records[0] if records else {}


def _latest_visit(item: dict[str, Any]) -> dict[str, Any]:
    """Return the newest visit matched to a cat, passing over weights the box couldn't match."""
    return next((record for record in _cat_records(item) if record.get("eventType") == VISIT_EVENT), {})


def _last_visit_attributes(item: dict[str, Any]) -> dict[str, Any]:
    records = _cat_records(item)
    if not records:
        return {}
    return {
        **_visit_summary(records[0]),
        "record_id": records[0].get("recordId"),
        "recent_visits": [_visit_summary(record) for record in records[:5]],
    }


def _count_today(records: list[dict[str, Any]], event_type: int, cat: dict[str, Any] | None = None) -> int | None:
    """Count records of one eventType since local midnight, optionally for one cat."""
    if not records:
        # The box always has some history, so no records means the request failed.
        return None
    midnight = dt_util.start_of_local_day()
    return sum(
        1
        for record in records
        if record.get("eventType") == event_type
        and _milliseconds_to_datetime(record.get("eventTime")) >= midnight
        and (cat is None or _is_cats_record(record, cat))
    )


def _is_cats_record(record: dict[str, Any], cat: dict[str, Any]) -> bool:
    # Match on the cat's ID where the record carries it (the same vendor cloud's records do),
    # else on the nickname. A cat without one matches nothing rather than the whole box.
    if record.get("catInfoId") is not None and cat.get("catInfoId") is not None:
        return str(record["catInfoId"]) == str(cat["catInfoId"])
    nickname = cat.get("nickname")
    return nickname is not None and record.get("catNickname") == nickname


def _visit_summary(record: dict[str, Any]) -> dict[str, Any]:
    # eventTime is epoch milliseconds; the record's timeStr is in the server's China time.
    return {
        "time": dt_util.as_local(_milliseconds_to_datetime(record.get("eventTime"))).isoformat(),
        "cat": record.get("catNickname"),
        "weight_kg": record.get("weight"),
        "duration_s": record.get("costTime"),
        "event_type": record.get("eventType"),
    }


def _mqtt_state_value(item: dict[str, Any], key: str) -> Any:
    mqtt_state = item.get("mqtt_state")
    if not isinstance(mqtt_state, dict):
        return None
    return mqtt_state.get(key)


def _runtime_state_value(item: dict[str, Any], key: str) -> Any:
    runtime_state = item.get("runtime_state")
    if not isinstance(runtime_state, dict):
        return None
    return runtime_state.get(key)
