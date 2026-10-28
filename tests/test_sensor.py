from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any, Generic, TypeVar
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_sensor_test"
MIDNIGHT = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)
T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    name: str | None = None
    icon: str | None = None
    translation_key: str | None = None
    device_class: Any = None
    state_class: Any = None
    native_unit_of_measurement: str | None = None
    entity_category: Any = None
    entity_registry_enabled_default: bool = True


class _CoordinatorEntity(Generic[T]):
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_sensor_module():
    """Load sensor.py against stand-ins for Home Assistant, removed again once it's loaded."""
    package = _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)])
    names = types.SimpleNamespace
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.sensor": _module(
            "homeassistant.components.sensor",
            SensorDeviceClass=names(TIMESTAMP="timestamp", WEIGHT="weight", DURATION="duration"),
            SensorEntity=object,
            SensorEntityDescription=_EntityDescription,
            SensorStateClass=names(TOTAL="total", MEASUREMENT="measurement"),
        ),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.const": _module(
            "homeassistant.const",
            PERCENTAGE="%",
            UnitOfMass=names(KILOGRAMS="kg"),
            UnitOfTime=names(MINUTES="min", SECONDS="s"),
        ),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity": _module("homeassistant.helpers.entity", EntityCategory=names(DIAGNOSTIC="diagnostic")),
        "homeassistant.helpers.device_registry": _module("homeassistant.helpers.device_registry", DeviceInfo=dict),
        "homeassistant.helpers.entity_platform": _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity
        ),
        "homeassistant.util": _module(
            "homeassistant.util",
            dt=names(start_of_local_day=lambda: MIDNIGHT, as_local=lambda value: value),
        ),
        PACKAGE_NAME: package,
        f"{PACKAGE_NAME}.const": _module(f"{PACKAGE_NAME}.const", DOMAIN="ubpet"),
        f"{PACKAGE_NAME}.coordinator": _module(f"{PACKAGE_NAME}.coordinator", UbpetDataUpdateCoordinator=object),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.sensor", COMPONENT_PATH / "sensor.py")
        sensor = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = sensor
        spec.loader.exec_module(sensor)
    return sensor


sensor = load_sensor_module()


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _visit(cat: str, when: datetime, *, event_type: int = 3, cat_id: int | None = None) -> dict[str, Any]:
    record = {"catNickname": cat, "eventTime": _ms(when), "eventType": event_type, "weight": 4.0, "costTime": 40}
    if cat_id is not None:
        record["catInfoId"] = cat_id
    return record


def _device_sensor(key: str, item: dict[str, Any]):
    coordinator = types.SimpleNamespace(data={"devices": {"SN1": item}, "cats": []})
    description = next(description for description in sensor.DEVICE_SENSORS if description.key == key)
    return sensor.UbpetDeviceSensor(coordinator, "entry", "SN1", description)


class LastUsedByTests(unittest.TestCase):
    def test_names_the_cat_on_the_newest_matched_visit(self):
        item = {
            "cat_records": [
                _visit("Freya", MIDNIGHT + timedelta(hours=1)),
                # A weight the box couldn't match is newer, but isn't a visit by a named cat.
                _visit(None, MIDNIGHT + timedelta(hours=2), event_type=21),
            ]
        }
        entity = _device_sensor("last_used_by", item)

        self.assertEqual(entity.native_value, "Freya")
        self.assertEqual(entity.extra_state_attributes["cat"], "Freya")

    def test_keeps_the_last_cat_when_the_records_hold_no_matched_visit(self):
        item = {"cat_records": [_visit("Tiny", MIDNIGHT + timedelta(hours=1))]}
        entity = _device_sensor("last_used_by", item)
        self.assertEqual(entity.native_value, "Tiny")

        for records in ([_visit(None, MIDNIGHT + timedelta(hours=3), event_type=21)], [], None):
            item["cat_records"] = records
            with self.subTest(records=records):
                self.assertEqual(entity.native_value, "Tiny")
                self.assertEqual(entity.extra_state_attributes["cat"], "Tiny")

    def test_unknown_until_any_matched_visit(self):
        entity = _device_sensor("last_used_by", {"cat_records": []})

        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)


class TodayCountTests(unittest.TestCase):
    def test_counts_visits_since_local_midnight(self):
        item = {
            "cat_records": [
                _visit("Milo", MIDNIGHT - timedelta(minutes=1)),
                _visit("Milo", MIDNIGHT + timedelta(minutes=1)),
                _visit(None, MIDNIGHT + timedelta(minutes=2), event_type=21),
                _visit("Freya", MIDNIGHT + timedelta(hours=5)),
            ]
        }

        self.assertEqual(_device_sensor("visits_today", item).native_value, 2)

    def test_no_records_is_unknown_rather_than_zero(self):
        self.assertIsNone(_device_sensor("visits_today", {"cat_records": []}).native_value)

    def test_daily_counts_reset_at_local_midnight(self):
        self.assertEqual(_device_sensor("visits_today", {}).last_reset, MIDNIGHT)
        self.assertEqual(_device_sensor("cleans_today", {}).last_reset, MIDNIGHT)
        self.assertIsNone(_device_sensor("box_use_times", {}).last_reset)

    def test_cat_records_match_on_the_cat_id_then_the_nickname(self):
        tiny = {"catInfoId": 532227, "nickname": "Tiny"}

        self.assertTrue(sensor._is_cats_record(_visit("Tiny", MIDNIGHT, cat_id=532227), tiny))
        # The ID wins over a nickname that happens to match.
        self.assertFalse(sensor._is_cats_record(_visit("Tiny", MIDNIGHT, cat_id=1), tiny))
        self.assertTrue(sensor._is_cats_record(_visit("Tiny", MIDNIGHT), tiny))
        # A cat without a nickname matches nothing rather than every unnamed record.
        self.assertFalse(sensor._is_cats_record(_visit(None, MIDNIGHT), {"catInfoId": None, "nickname": None}))


class AttributeTests(unittest.TestCase):
    def test_mqtt_sensors_show_only_state_fields_that_hold_still(self):
        item = {
            "mqtt_state": {
                "w_mode": 0,
                "w_state_app_name": "PENDING",
                "w_cause": 90,
                "timestamp": 1793034925,
                "weigh_sensor": 4321,
                "message": {"cid": "cid-1"},
                "guess": {"at": 1.0},
            }
        }

        attributes = _device_sensor("mqtt_work_state", item).extra_state_attributes

        self.assertEqual(attributes, {"w_mode": 0, "w_state_app_name": "PENDING", "w_cause": 90})

    def test_timestamp_sensors_and_plain_sensors_carry_no_attributes(self):
        item = {"runtime_state": {"last_rest_update_at": MIDNIGHT}, "box_use_times": {"boxUseTimes": 3}}

        for key in ("last_rest_update", "last_mqtt_update", "box_use_times"):
            with self.subTest(key=key):
                self.assertIsNone(_device_sensor(key, item).extra_state_attributes)

    def test_cat_picture_stays_out_of_the_history_database(self):
        self.assertIn("picture", sensor.UbpetCatSensor._unrecorded_attributes)


if __name__ == "__main__":
    unittest.main()
