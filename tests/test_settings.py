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

    @property
    def available(self) -> bool:
        return True


# Delays and callbacks handed to async_call_later, in order.
SCHEDULED: list[tuple[float, Any]] = []


def _async_call_later(hass, delay, action):
    SCHEDULED.append((delay, action))
    return lambda: None


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_platforms():
    """Load switch.py, number.py and button.py against stand-ins for Home Assistant, removed once loaded."""
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
            NumberMode=names(SLIDER="slider", BOX="box"),
        ),
        "homeassistant.components.button": _module(
            "homeassistant.components.button", ButtonEntity=object, ButtonEntityDescription=_EntityDescription
        ),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.const": _module("homeassistant.const", UnitOfTime=names(MINUTES="min")),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object, callback=lambda func: func),
        "homeassistant.exceptions": _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module("homeassistant.helpers.device_registry", DeviceInfo=dict),
        "homeassistant.helpers.entity": _module(
            "homeassistant.helpers.entity", EntityCategory=names(CONFIG="config", DIAGNOSTIC="diagnostic")
        ),
        "homeassistant.helpers.entity_platform": _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object),
        "homeassistant.helpers.event": _module("homeassistant.helpers.event", async_call_later=_async_call_later),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=_CoordinatorEntity
        ),
        PACKAGE_NAME: _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)]),
        f"{PACKAGE_NAME}.const": _module(f"{PACKAGE_NAME}.const", DOMAIN="ubpet"),
        f"{PACKAGE_NAME}.coordinator": _module(f"{PACKAGE_NAME}.coordinator", UbpetDataUpdateCoordinator=object),
    }
    platforms = {}
    with patch.dict(sys.modules, stubs):
        for name in ("switch", "number", "button"):
            spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", COMPONENT_PATH / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            platforms[name] = module
    return platforms["switch"], platforms["number"], platforms["button"]


switch, number, button = load_platforms()


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


class RecordingClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, *args))

        return record


class FakeCoordinator:
    def __init__(self, item: dict[str, Any]):
        self.data = {"devices": {"SN1": item}}
        self.client = RecordingClient()
        self.refreshes = 0
        self.listener_updates = 0

    async def async_request_refresh(self):
        self.refreshes += 1

    def async_update_listeners(self):
        self.listener_updates += 1


class FakeHass:
    def __init__(self, coordinator: FakeCoordinator | None = None):
        self.tasks: list[Any] = []
        self.data = {"ubpet": {"entry": coordinator}}

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        self.tasks.append(coro)


def _function_switch(key: str, config: dict[str, Any]):
    description = next(d for d in switch.FUNCTION_SWITCHES if d.key == key)
    coordinator = FakeCoordinator({"config": config})
    entity = switch.UbpetFunctionSwitch(coordinator, "entry", "SN1", description)
    entity.hass = FakeHass(coordinator)
    return entity


class FunctionSwitchTests(unittest.TestCase):
    def setUp(self):
        SCHEDULED.clear()

    def test_child_lock_reads_the_control_board_switch(self):
        self.assertTrue(_function_switch("child_lock", {"controlBoardSwitch": 1}).is_on)
        self.assertFalse(_function_switch("child_lock", {"controlBoardSwitch": 0}).is_on)

    def test_turning_the_child_lock_off_shows_at_once_and_confirms_later(self):
        entity = _function_switch("child_lock", {"controlBoardSwitch": 1})

        asyncio.run(entity.async_turn_off())

        self.assertEqual(entity.coordinator.client.calls, [("set_function_switch", "SN1", 0, False)])
        # The vendor still reports the old value right after the write, so the switch shows
        # the accepted value straight away instead of refreshing now.
        self.assertFalse(entity.is_on)
        self.assertEqual(entity.coordinator.listener_updates, 1)
        self.assertEqual(entity.coordinator.refreshes, 0)
        self.assertEqual([delay for delay, _ in SCHEDULED], [switch.CONFIRM_DELAY_S])

        _, confirm = SCHEDULED[0]
        confirm(None)
        for task in entity.hass.tasks:
            asyncio.run(task)
        self.assertEqual(entity.coordinator.refreshes, 1)

    def test_foreign_object_detection_writes_its_notify_entry(self):
        config = {"notifyConfigList": [{"type": 1, "openSwitch": 0}, {"type": 3, "openSwitch": 0}]}
        entity = _function_switch("foreign_object_detection", config)

        asyncio.run(entity.async_turn_on())

        self.assertEqual(entity.coordinator.client.calls, [("set_function_switch", "SN1", 1, True)])
        self.assertEqual(config["notifyConfigList"], [{"type": 1, "openSwitch": 1}, {"type": 3, "openSwitch": 0}])
        self.assertTrue(entity.is_on)

    def test_only_functions_the_box_reports_get_a_switch(self):
        config = {"lazyTime": 900, "sensorSwitch": 1, "boxFunctionVOList": [{"type": 4}, {"type": 5}]}
        coordinator = FakeCoordinator({"config": config})
        added: list[Any] = []

        asyncio.run(
            switch.async_setup_entry(FakeHass(coordinator), types.SimpleNamespace(entry_id="entry"), added.extend)
        )

        keys = {e.entity_description.key for e in added if isinstance(e, switch.UbpetFunctionSwitch)}
        self.assertEqual(keys, {"child_lock", "shake_mode", "box_full_alert"})


class BoxFullAlertNumberTests(unittest.TestCase):
    def _number(self, use_times: dict[str, Any]):
        coordinator = FakeCoordinator({"box_use_times": use_times})
        entity = number.UbpetBoxFullAlertNumber(coordinator, "entry", "SN1")
        entity.hass = FakeHass(coordinator)
        return entity

    def test_reads_the_threshold_and_caps_it_at_the_bin_capacity(self):
        entity = self._number({"boxFullAlert": 20, "boxFullMax": 30})
        self.assertEqual(entity.native_value, 20)
        self.assertEqual(entity.native_max_value, 30.0)

    def test_setting_the_threshold_sends_it_and_refreshes(self):
        entity = self._number({"boxFullAlert": 20, "boxFullMax": 30})

        asyncio.run(entity.async_set_native_value(25.0))

        self.assertEqual(entity.coordinator.client.calls, [("set_box_full_alert", "SN1", 25)])
        self.assertEqual(entity.coordinator.refreshes, 1)


def _button(item: dict[str, Any], service_id: str):
    description = next(d for d in button.MQTT_BUTTONS if d.service_id == service_id)
    entity = button.UbpetMqttCommandButton(FakeCoordinator(item), "entry", "SN1", description)
    entity.hass = FakeHass(entity.coordinator)
    return entity


def _work(mode: str, state: str) -> dict[str, Any]:
    return {"mqtt_state": {"w_mode_app_name": mode, "w_state_app_name": state}}


class LowerRakeAvailabilityTests(unittest.TestCase):
    def test_lower_is_available_while_the_raised_rake_is_paused(self):
        self.assertTrue(_button(_work("RAKING_UP", "PAUSED"), "start_drop").available)

    def test_lower_is_available_while_the_raised_rake_is_blocked(self):
        # 11-01: with the rake up and hands and litter on the scale, the box reported
        # BLOCKED for 42 minutes and Lower stayed greyed out; the app could still lower it.
        self.assertTrue(_button(_work("RAKING_UP", "BLOCKED"), "start_drop").available)

    def test_lower_waits_while_the_rake_is_still_rising(self):
        self.assertFalse(_button(_work("RAKING_UP", "RUNNING"), "start_drop").available)

    def test_lower_is_off_when_the_rake_is_down(self):
        self.assertFalse(_button(_work("IDLE", "PENDING"), "start_drop").available)
        self.assertFalse(_button(_work("CLEANING", "BLOCKED"), "start_drop").available)


class ResetDeodorantButtonTests(unittest.TestCase):
    def test_reset_deodorant_is_available_without_mqtt_state(self):
        self.assertTrue(_button({}, button.RESET_DEODORANT_SERVICE).available)

    def test_reset_deodorant_posts_and_refreshes(self):
        entity = _button({}, button.RESET_DEODORANT_SERVICE)

        asyncio.run(entity.async_press())

        self.assertEqual(entity.coordinator.client.calls, [("reset_deodorant", "SN1")])
        self.assertEqual(entity.coordinator.refreshes, 1)


if __name__ == "__main__":
    unittest.main()
