from __future__ import annotations

from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
import types
from typing import Generic, TypeVar
import unittest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_coordinator_test"
T = TypeVar("T")


class _DataUpdateCoordinator(Generic[T]):
    pass


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules.setdefault(name, module)
    return sys.modules[name]


def load_coordinator_module():
    _module("homeassistant")
    _module("homeassistant.helpers")
    _module("homeassistant.core").HomeAssistant = object
    update_coordinator = _module("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", _DataUpdateCoordinator)
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", Exception)
    exceptions = _module("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (Exception,), {})
    )
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[PACKAGE_NAME] = package
    api_error = type("UbpetApiError", (RuntimeError,), {})
    sys.modules[f"{PACKAGE_NAME}.api"] = types.SimpleNamespace(
        UbpetApiError=api_error,
        UbpetAuthError=type("UbpetAuthError", (api_error,), {}),
        UbpetClient=object,
    )
    sys.modules[f"{PACKAGE_NAME}.const"] = types.SimpleNamespace(DEFAULT_SCAN_INTERVAL=timedelta(minutes=3), DOMAIN="ubpet")
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.coordinator", COMPONENT_PATH / "coordinator.py")
    coordinator = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = coordinator
    spec.loader.exec_module(coordinator)
    return coordinator


def _state_reply(from_uid: str, mode: str) -> dict:
    return {
        "phase": "command",
        "topic": "im/msg/receive/topic",
        "qos": 0,
        "decoded": {
            "risp_cmd": 1021,
            "from_uid": from_uid,
            "to_uid": "717422",
            "w_mode_app_name": mode,
            "w_state_app_name": "PENDING",
        },
    }


class MqttStateReplyTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = load_coordinator_module()

    def test_reply_from_the_box_sets_the_state(self):
        result = {"target_uid": "717401", "received_messages": [_state_reply("717401", "IDLE")]}

        self.assertEqual(self.coordinator._latest_mqtt_state(result)["w_mode_app_name"], "IDLE")

    def test_reply_from_another_sender_is_ignored(self):
        result = {
            "target_uid": "717401",
            "received_messages": [_state_reply("717401", "IDLE"), _state_reply("999999", "CLEANING")],
        }

        self.assertEqual(self.coordinator._latest_mqtt_state(result)["w_mode_app_name"], "IDLE")
        self.assertIsNone(self.coordinator._latest_mqtt_state({**result, "received_messages": result["received_messages"][1:]}))

    def test_without_a_target_no_reply_is_trusted(self):
        result = {"received_messages": [_state_reply("717401", "IDLE")]}

        self.assertIsNone(self.coordinator._latest_mqtt_state(result))


class RefusedLoginTests(unittest.TestCase):
    def test_refused_login_asks_for_reauth_and_stops_mqtt_polls(self):
        import asyncio

        coordinator_module = load_coordinator_module()
        api = sys.modules[f"{PACKAGE_NAME}.api"]
        auth_failed = sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed

        def get_dashboard():
            raise api.UbpetAuthError("login refused")

        async def run_in_executor(func, *args):
            return func(*args)

        coordinator = object.__new__(coordinator_module.UbpetDataUpdateCoordinator)
        coordinator.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)
        coordinator.client = types.SimpleNamespace(get_dashboard=get_dashboard)
        cancelled = []
        coordinator.cancel_mqtt_state_polls = lambda: cancelled.append(True)

        with self.assertRaises(auth_failed):
            asyncio.run(coordinator._async_update_data())
        self.assertEqual(cancelled, [True])


if __name__ == "__main__":
    unittest.main()
