from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any, Generic, TypeVar
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_binary_sensor_test"
T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    name: str | None = None
    icon: str | None = None
    translation_key: str | None = None
    entity_category: Any = None
    device_class: Any = None


class _CoordinatorEntity(Generic[T]):
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_binary_sensor_module():
    """Load binary_sensor.py against stand-ins for Home Assistant, removed once loaded."""
    names = types.SimpleNamespace
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.binary_sensor": _module(
            "homeassistant.components.binary_sensor",
            BinarySensorDeviceClass=names(PROBLEM="problem", LIGHT="light", UPDATE="update"),
            BinarySensorEntity=object,
            BinarySensorEntityDescription=_EntityDescription,
        ),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module("homeassistant.helpers.device_registry", DeviceInfo=dict),
        "homeassistant.helpers.entity": _module("homeassistant.helpers.entity", EntityCategory=names(DIAGNOSTIC="diagnostic")),
        "homeassistant.helpers.entity_platform": _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity
        ),
        PACKAGE_NAME: _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)]),
        f"{PACKAGE_NAME}.const": _module(f"{PACKAGE_NAME}.const", DOMAIN="ubpet"),
        f"{PACKAGE_NAME}.coordinator": _module(f"{PACKAGE_NAME}.coordinator", UbpetDataUpdateCoordinator=object),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.binary_sensor", COMPONENT_PATH / "binary_sensor.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


binary_sensor = load_binary_sensor_module()

# The SC03's config and the cat_face_upload capability, so every entity is created.
FULL_BOX = {
    "config": {"sensorSwitch": 1, "cameraSwitch": 1, "lightSwitch": 0},
    "capabilities": {"capabilities": [{"code": "cat_face_upload"}]},
    "online": {},
    "deodorant": {},
}


def _sensor(key: str, item: dict[str, Any]):
    coordinator = types.SimpleNamespace(data={"devices": {"SN1": item}})
    description = next(description for description in binary_sensor.BINARY_SENSORS if description.key == key)
    return binary_sensor.UbpetDeviceBinarySensor(coordinator, "entry", "SN1", description)


def _set_up(item: dict[str, Any]) -> set[str]:
    coordinator = types.SimpleNamespace(data={"devices": {"SN1": item}})
    hass = types.SimpleNamespace(data={"ubpet": {"entry": coordinator}})
    added = []
    asyncio.run(binary_sensor.async_setup_entry(hass, types.SimpleNamespace(entry_id="entry"), added.extend))
    return {entity.entity_description.key for entity in added}


class StateSensorTests(unittest.TestCase):
    """Fault and light sensors from the box's pushed state (bojanrajkovic/upet-hass 54e2fde, b96a4bc)."""

    def test_unknown_until_the_box_sends_its_state(self):
        for key in ("cover_open", "part_not_in_place", "motor_stuck", "scale_fault", "light", "firmware_updating"):
            with self.subTest(key=key):
                self.assertIsNone(_sensor(key, {}).is_on)

    def test_a_field_the_box_leaves_out_is_off(self):
        # The SC03's usual state: work mode, state and cause, nothing else.
        item = {"mqtt_state": {"w_mode": 0, "w_state": 0, "w_cause": 90}}
        for key in ("cover_open", "part_not_in_place", "motor_stuck", "scale_fault", "camera_fault", "light", "firmware_updating"):
            with self.subTest(key=key):
                self.assertFalse(_sensor(key, item).is_on)

    def test_each_fault_reads_its_field(self):
        for key, field in (
            ("cover_open", "lid_state"),
            ("part_not_in_place", "box_state"),
            ("scale_fault", "weigh_sensor"),
            ("camera_fault", "camera_state"),
            ("firmware_updating", "update_state"),
        ):
            with self.subTest(key=key):
                self.assertTrue(_sensor(key, {"mqtt_state": {field: 1}}).is_on)
                # Codes nobody has seen yet don't count as the fault.
                self.assertFalse(_sensor(key, {"mqtt_state": {field: 2}}).is_on)

    def test_motor_stuck_gives_the_position(self):
        stuck = _sensor("motor_stuck", {"mqtt_state": {"motor_state": 3}})
        self.assertTrue(stuck.is_on)
        self.assertEqual(stuck.extra_state_attributes, {"position": 3})

        fine = _sensor("motor_stuck", {"mqtt_state": {}})
        self.assertFalse(fine.is_on)
        self.assertEqual(fine.extra_state_attributes, {"position": None})
        self.assertIsNone(_sensor("cover_open", {"mqtt_state": {}}).extra_state_attributes)

    def test_light_is_on_at_any_level(self):
        self.assertTrue(_sensor("light", {"mqtt_state": {"led_state": 2}}).is_on)

    def test_a_field_in_an_unexpected_form_is_unknown(self):
        self.assertIsNone(_sensor("cover_open", {"mqtt_state": {"lid_state": "1"}}).is_on)


class HardwareGatingTests(unittest.TestCase):
    """Only hardware the box has gets entities (bojanrajkovic/upet-hass ad084ff)."""

    def test_a_box_with_a_camera_and_light_gets_everything(self):
        self.assertEqual(_set_up(FULL_BOX), {description.key for description in binary_sensor.BINARY_SENSORS})

    def test_no_camera_or_light_entities_without_them(self):
        item = {**FULL_BOX, "config": {"sensorSwitch": 1, "cameraSwitch": 1}, "capabilities": {"capabilities": []}}

        keys = _set_up(item)

        self.assertFalse(keys & {"camera_switch", "camera_fault", "light"})
        self.assertTrue({"online", "cover_open", "motor_stuck"} <= keys)

    def test_parts_that_failed_to_load_at_startup_keep_the_entities(self):
        item = {**FULL_BOX, "config": {}, "capabilities": None}

        self.assertTrue({"camera_switch", "camera_fault", "light"} <= _set_up(item))


if __name__ == "__main__":
    unittest.main()
