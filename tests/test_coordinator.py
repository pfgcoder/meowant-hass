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
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[PACKAGE_NAME] = package
    sys.modules[f"{PACKAGE_NAME}.api"] = types.SimpleNamespace(UbpetApiError=Exception, UbpetClient=object)
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


if __name__ == "__main__":
    unittest.main()
