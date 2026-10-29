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
PACKAGE_NAME = "ubpet_settings_test"
T = TypeVar("T")
HomeAssistantError = type("HomeAssistantError", (Exception,), {})


@dataclass(frozen=True, kw_only=True)
class _EntityDescription:
    key: str
    name: str | None = None
    icon: str | None = None
    translation_key: str | None = None
    entity_category: Any = None
    native_min_value: float | None = None
    native_max_value: float | None = None
    native_step: float | None = None
    native_unit_of_measurement: str | None = None
    mode: Any = None


class _CoordinatorEntity(Generic[T]):
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_platforms():
    """Load switch.py and number.py against stand-ins for Home Assistant, removed once loaded."""
    names = types.SimpleNamespace
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.switch": _module(
            "homeassistant.components.switch", SwitchEntity=object, SwitchEntityDescription=_EntityDescription
        ),
        "homeassistant.components.number": _module(
            "homeassistant.components.number",
            NumberEntity=object,
            NumberEntityDescription=_EntityDescription,
            NumberMode=names(SLIDER="slider"),
        ),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.const": _module("homeassistant.const", UnitOfTime=names(MINUTES="min")),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.exceptions": _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module("homeassistant.helpers.device_registry", DeviceInfo=dict),
        "homeassistant.helpers.entity": _module("homeassistant.helpers.entity", EntityCategory=names(CONFIG="config")),
        "homeassistant.helpers.entity_platform": _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity
        ),
        PACKAGE_NAME: _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)]),
        f"{PACKAGE_NAME}.const": _module(f"{PACKAGE_NAME}.const", DOMAIN="ubpet"),
        f"{PACKAGE_NAME}.coordinator": _module(f"{PACKAGE_NAME}.coordinator", UbpetDataUpdateCoordinator=object),
    }
    platforms = {}
    with patch.dict(sys.modules, stubs):
        for name in ("switch", "number"):
            spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", COMPONENT_PATH / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            platforms[name] = module
    return platforms["switch"], platforms["number"]


switch, number = load_platforms()


class FakeClient:
    def __init__(self):
        self.sent = []

    def set_auto_clean_enabled(self, serial, enabled, lazy_time_seconds):
        self.sent.append(("switch", serial, enabled, lazy_time_seconds))

    def set_auto_clean_delay(self, serial, minutes, enabled=True):
        self.sent.append(("delay", serial, minutes, enabled))


def _entity(entity_class, config: dict[str, Any]):
    async def request_refresh():
        pass

    async def run_in_executor(func, *args):
        return func(*args)

    coordinator = types.SimpleNamespace(
        data={"devices": {"SN1": {"config": config}}}, client=FakeClient(), async_request_refresh=request_refresh
    )
    entity = entity_class(coordinator, "entry", "SN1")
    entity.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
    return entity, coordinator.client


class AutoCleanSettingTests(unittest.TestCase):
    def test_auto_clean_switch_keeps_the_delay(self):
        entity, client = _entity(switch.UbpetAutoCleanSwitch, {"lazyTime": 900, "sensorSwitch": 1})

        asyncio.run(entity.async_turn_off())

        self.assertEqual(client.sent, [("switch", "SN1", False, 900)])

    def test_auto_clean_switch_refuses_while_the_delay_is_unknown(self):
        entity, client = _entity(switch.UbpetAutoCleanSwitch, {})

        with self.assertRaises(HomeAssistantError):
            asyncio.run(entity.async_turn_on())
        self.assertEqual(client.sent, [])

    def test_delay_keeps_auto_clean_as_it_is(self):
        entity, client = _entity(number.UbpetAutoCleanDelayNumber, {"lazyTime": 900, "sensorSwitch": 0})

        asyncio.run(entity.async_set_native_value(20))

        self.assertEqual(client.sent, [("delay", "SN1", 20, False)])

    def test_delay_refuses_while_auto_clean_is_unknown(self):
        entity, client = _entity(number.UbpetAutoCleanDelayNumber, {})

        with self.assertRaises(HomeAssistantError):
            asyncio.run(entity.async_set_native_value(20))
        self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()
