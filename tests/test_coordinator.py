from __future__ import annotations

from datetime import timedelta
import importlib.util
from pathlib import Path
import sys
import types
from typing import Generic, TypeVar
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_coordinator_test"
T = TypeVar("T")


class _DataUpdateCoordinator(Generic[T]):
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.data = None

    def async_update_listeners(self):
        pass


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_coordinator_module():
    """Load coordinator.py against stand-ins for Home Assistant and api.py, removed once loaded."""
    package = _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)])
    api_error = type("UbpetApiError", (RuntimeError,), {})
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_DataUpdateCoordinator,
            UpdateFailed=type("UpdateFailed", (Exception,), {}),
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {})
        ),
        PACKAGE_NAME: package,
        f"{PACKAGE_NAME}.api": _module(
            f"{PACKAGE_NAME}.api",
            UbpetApiError=api_error,
            UbpetAuthError=type("UbpetAuthError", (api_error,), {}),
            UbpetClient=object,
        ),
        f"{PACKAGE_NAME}.const": _module(
            f"{PACKAGE_NAME}.const", DEFAULT_SCAN_INTERVAL=timedelta(minutes=3), DOMAIN="ubpet"
        ),
    }
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.coordinator", COMPONENT_PATH / "coordinator.py")
    coordinator = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(sys.modules, stubs):
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

    def test_reply_keeps_the_state_and_files_the_bookkeeping_apart(self):
        reply = _state_reply("717401", "IDLE")
        reply["decoded"].update({"cid": "cid-1", "risp_seq": 9, "new_decoder_field": 1, "w_cause": 90})

        state = self.coordinator._latest_mqtt_state({"target_uid": "717401", "received_messages": [reply]})

        self.assertEqual(
            {key: value for key, value in state.items() if key != "message"},
            {"w_mode_app_name": "IDLE", "w_state_app_name": "PENDING", "w_cause": 90},
        )
        self.assertEqual(state["message"]["cid"], "cid-1")

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


class PreviousRecordsTests(unittest.TestCase):
    def test_records_that_failed_to_load_keep_the_last_ones(self):
        coordinator = load_coordinator_module()
        previous = {"devices": {"SN1": {"cat_records": [{"eventTime": 1}], "device_records": [{"eventTime": 2}]}}}
        data = {"devices": {"SN1": {"cat_records": None, "device_records": []}, "SN2": {"cat_records": None}}}

        coordinator._keep_previous_records(data, previous)

        self.assertEqual(data["devices"]["SN1"]["cat_records"], [{"eventTime": 1}])
        # Loaded but empty stays empty, and a box with nothing earlier stays without records.
        self.assertEqual(data["devices"]["SN1"]["device_records"], [])
        self.assertIsNone(data["devices"]["SN2"]["cat_records"])

    def test_first_update_has_nothing_to_keep(self):
        coordinator = load_coordinator_module()
        data = {"devices": {"SN1": {"cat_records": None}}}

        coordinator._keep_previous_records(data, None)

        self.assertIsNone(data["devices"]["SN1"]["cat_records"])


class GuessedStateTests(unittest.TestCase):
    def setUp(self):
        coordinator_module = load_coordinator_module()
        self.coordinator = coordinator_module.UbpetDataUpdateCoordinator(types.SimpleNamespace(), None)
        self.coordinator._last_rest_update_at = coordinator_module.datetime.now(coordinator_module.UTC)
        self.coordinator.data = {"devices": {"SN1": {}}}

    def state(self):
        return self.coordinator.data["devices"]["SN1"].get("mqtt_state")

    def reply(self, mode):
        self.coordinator.store_mqtt_result(
            "SN1", {"target_uid": "717401", "received_messages": [_state_reply("717401", mode)]}
        )

    def test_undoing_a_guess_before_any_reply_clears_it(self):
        guess = self.coordinator.set_mqtt_work_state("SN1", {"w_state_app_name": "RUNNING"})
        self.assertEqual(self.state()["w_state_app_name"], "RUNNING")

        self.coordinator.restore_mqtt_work_state("SN1", guess)
        self.assertIsNone(self.state())

    def test_undoing_a_guess_goes_back_to_the_last_reply(self):
        self.reply("IDLE")
        guess = self.coordinator.set_mqtt_work_state("SN1", {"w_mode_app_name": "CLEANING"})

        self.coordinator.restore_mqtt_work_state("SN1", guess)
        self.assertEqual(self.state()["w_mode_app_name"], "IDLE")
        self.assertNotIn("guess", self.state())

    def test_a_reply_after_the_guess_is_not_undone(self):
        guess = self.coordinator.set_mqtt_work_state("SN1", {"w_state_app_name": "RUNNING"})
        self.reply("CLEANING")

        self.coordinator.restore_mqtt_work_state("SN1", guess)
        self.assertEqual(self.state()["w_mode_app_name"], "CLEANING")

    def test_two_failed_presses_leave_no_guess_behind(self):
        for order in ((0, 1), (1, 0)):
            self.reply("IDLE")
            guesses = [
                self.coordinator.set_mqtt_work_state("SN1", {"w_mode_app_name": "CLEANING"}),
                self.coordinator.set_mqtt_work_state("SN1", {"w_mode_app_name": "SMOOTHING"}),
            ]

            for index in order:
                self.coordinator.restore_mqtt_work_state("SN1", guesses[index])
            with self.subTest(order=order):
                self.assertEqual(self.state()["w_mode_app_name"], "IDLE")


class RefusedLoginTests(unittest.TestCase):
    def test_refused_login_asks_for_reauth_and_stops_mqtt_polls(self):
        import asyncio

        coordinator_module = load_coordinator_module()
        auth_failed = coordinator_module.ConfigEntryAuthFailed

        def get_dashboard():
            raise coordinator_module.UbpetAuthError("login refused")

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

    def test_polls_stopped_by_a_refused_login_restart_when_login_works_again(self):
        import asyncio

        coordinator_module = load_coordinator_module()

        def refused(serial, service_id):
            raise coordinator_module.UbpetAuthError("login refused")

        async def run_in_executor(func, *args):
            return func(*args)

        def create_task(coro):
            coro.close()
            return types.SimpleNamespace(done=lambda: False, cancel=lambda: None)

        hass = types.SimpleNamespace(async_add_executor_job=run_in_executor, async_create_task=create_task)
        client = types.SimpleNamespace(
            send_mqtt_service=refused,
            get_dashboard=lambda: {"devices": {"SN1": {"device": {}}}, "cats": []},
        )
        coordinator = coordinator_module.UbpetDataUpdateCoordinator(hass, client)
        coordinator.data = {"devices": {"SN1": {}}}
        coordinator._mqtt_state_polls_enabled = True

        asyncio.run(coordinator.async_request_mqtt_state("SN1"))
        self.assertFalse(coordinator._mqtt_state_polls_enabled)

        asyncio.run(coordinator._async_update_data())
        self.assertTrue(coordinator._mqtt_state_polls_enabled)
        self.assertIn("SN1", coordinator._next_mqtt_poll_at_by_serial)


if __name__ == "__main__":
    unittest.main()
