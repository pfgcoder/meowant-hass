from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import time
import types
from typing import Any
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_push_test"

# Delays and callbacks handed to async_call_later, in order.
SCHEDULED: list[tuple[float, Any]] = []


def _async_call_later(hass, delay, action):
    SCHEDULED.append((delay, action))
    return lambda: None


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


HomeAssistantError = type("HomeAssistantError", (Exception,), {})


def load_modules():
    """Load push.py (with the real api.py, mqtt.py and push_logic.py) against stand-ins for HA and paho."""
    paho_client = _module(
        "paho.mqtt.client",
        Client=object,
        MQTTMessage=object,
        MQTTv311=4,
        MQTT_ERR_SUCCESS=0,
        error_string=lambda rc: f"error {rc}",
    )
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object, callback=lambda func: func),
        "homeassistant.exceptions": _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.event": _module("homeassistant.helpers.event", async_call_later=_async_call_later),
        "homeassistant.util": _module("homeassistant.util"),
        "homeassistant.util.ssl": _module("homeassistant.util.ssl", client_context=lambda: None),
        "paho": _module("paho"),
        "paho.mqtt": _module("paho.mqtt"),
        "paho.mqtt.client": paho_client,
        "paho.mqtt.enums": _module("paho.mqtt.enums", CallbackAPIVersion=types.SimpleNamespace(VERSION2=2)),
        PACKAGE_NAME: _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)]),
        f"{PACKAGE_NAME}.const": _module(f"{PACKAGE_NAME}.const", EVENT_UNKNOWN_MESSAGE="ubpet_unknown_message"),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.push", COMPONENT_PATH / "push.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module, *(sys.modules[f"{PACKAGE_NAME}.{name}"] for name in ("api", "mqtt", "push_logic"))


push, api, mqtt, push_logic = load_modules()


class FakeClient:
    def __init__(self, rc: int = 0):
        self.rc = rc
        self.published: list[tuple[str, bytes]] = []
        self.disconnects = 0

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))
        return types.SimpleNamespace(rc=self.rc)

    def subscribe(self, topic, qos=0):
        pass

    def disconnect(self):
        self.disconnects += 1

    def loop_stop(self):
        pass


class FakeCoordinator:
    def __init__(self):
        self.data = {"devices": {"SN1": {}}}
        self.client = types.SimpleNamespace(auth=types.SimpleNamespace(token="token-1", user_id=7), base_url="https://apis.example")
        self.states: list[tuple[str, dict]] = []
        self.online: list[tuple[str, bool]] = []
        self.refreshes = 0
        self.listener_updates = 0

    def store_state_push(self, serial, decoded):
        self.states.append((serial, decoded))

    def set_online(self, serial, online):
        self.online.append((serial, online))

    async def async_request_refresh(self):
        self.refreshes += 1

    def async_update_listeners(self):
        self.listener_updates += 1


class FakeHass:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []
        self.tasks: list[Any] = []
        self.bus = types.SimpleNamespace(async_fire=lambda event, data: self.fired.append((event, data)))

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        coro.close()
        self.tasks.append(coro)


def _connected(client: FakeClient | None = None):
    """A UbpetPush connected to box SN1, whose IM user id is 2001."""
    hass, coordinator = FakeHass(), FakeCoordinator()
    connection = push.UbpetPush(hass, coordinator)
    connection._client = client or FakeClient()
    connection._connected_at = time.monotonic()
    connection._uid_to_serial = {"2001": "SN1"}
    connection._serial_to_uid = {"SN1": "2001"}
    connection._account_uid = "7"
    connection._pub_topic = "im/pub"
    connection._token_at_connect = "token-1"
    return connection, hass, coordinator


class MessageTests(unittest.TestCase):
    """How each push is handled (bojanrajkovic/upet-hass 37ebe31, e9aa233, b0edf9b, 0a39720)."""

    def setUp(self):
        SCHEDULED.clear()
        self.connection, self.hass, self.coordinator = _connected()

    def receive(self, decoded: dict, client=None):
        with patch.object(push, "decode_mqtt_payload_safely", return_value=decoded):
            self.connection._handle_message(client or self.connection._client, b"\x01\x02")

    def test_a_state_push_from_the_box_is_stored(self):
        decoded = {"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_STATE_PUSH, "w_state_app_name": "RUNNING"}

        self.receive(decoded)

        self.assertEqual(self.coordinator.states, [("SN1", decoded)])

    def test_a_new_record_fetches_once_a_few_seconds_later(self):
        self.receive({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_RECORD_PUSH})
        self.receive({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_CONFIG_REQUEST})

        self.assertEqual([delay for delay, _ in SCHEDULED], [push.REFRESH_DELAY_S])
        SCHEDULED[0][1](None)
        self.assertEqual(len(self.hass.tasks), 1)

    def test_an_online_push_updates_the_box(self):
        self.receive({"type_url": mqtt.ANY_ONLINE_STATUS_TYPE_URL, "online_status": "status/offline", "status_uid": "2001"})

        self.assertEqual(self.coordinator.online, [("SN1", False)])

    def test_another_login_taking_the_session_makes_it_yield(self):
        self.receive({"type_url": mqtt.ANY_FORCE_OFFLINE_TYPE_URL})

        self.assertTrue(self.connection.yielding)
        self.assertEqual(self.connection._client.disconnects, 1)

    def test_a_message_nobody_decodes_is_kept_as_an_event(self):
        self.receive({"from_uid": "2001", "risp_cmd": 0x1234, "content_type": "custom"})

        self.assertEqual(len(self.hass.fired), 1)
        event, data = self.hass.fired[0]
        self.assertEqual((event, data["reason"], data["payload_hex"]), ("ubpet_unknown_message", "unhandled message", "0102"))

    def test_receipts_are_ignored(self):
        self.receive({"from_uid": "2001", "content_type": "receipt"})

        self.assertEqual((self.hass.fired, self.coordinator.states), ([], []))

    def test_a_state_from_another_sender_is_not_stored(self):
        self.receive({"from_uid": "9999", "risp_cmd": mqtt.RISP_CMD_STATE_PUSH, "w_state_app_name": "RUNNING"})

        self.assertEqual(self.coordinator.states, [])

    def test_messages_from_a_replaced_client_are_ignored(self):
        self.receive({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_STATE_PUSH, "w_state_app_name": "RUNNING"}, client=FakeClient())

        self.assertEqual(self.coordinator.states, [])


class SendTests(unittest.TestCase):
    def test_a_command_goes_to_the_box_on_the_publish_topic(self):
        connection, _hass, _coordinator = _connected()

        asyncio.run(connection.async_publish("SN1", "start_clean_up"))

        topic, payload = connection._client.published[0]
        self.assertEqual(topic, "im/pub")
        self.assertIn(b"2001", payload)

    def test_no_command_while_disconnected(self):
        connection, _hass, _coordinator = _connected()
        connection._connected_at = None

        with self.assertRaises(HomeAssistantError):
            asyncio.run(connection.async_publish("SN1", "start_clean_up"))

    def test_a_refused_publish_is_an_error(self):
        connection, _hass, _coordinator = _connected(FakeClient(rc=4))

        with self.assertRaises(HomeAssistantError):
            asyncio.run(connection.async_publish("SN1", "start_clean_up"))


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        SCHEDULED.clear()

    def test_a_refused_password_stops_reconnecting(self):
        # Each login tries up to three account types, and repeated refusals can lock the account.
        connection = push.UbpetPush(FakeHass(), FakeCoordinator())
        with patch.object(connection, "_fetch_session", side_effect=api.UbpetAuthError(200, {"code": 2014})):
            with self.assertLogs(push._LOGGER, "WARNING"):
                asyncio.run(connection._async_connect())

        self.assertEqual(SCHEDULED, [])

    def test_a_login_that_fails_otherwise_retries_in_a_minute(self):
        connection = push.UbpetPush(FakeHass(), FakeCoordinator())
        with patch.object(connection, "_fetch_session", side_effect=OSError("timed out")):
            with self.assertLogs(push._LOGGER, "WARNING"):
                asyncio.run(connection._async_connect())

        self.assertEqual([delay for delay, _ in SCHEDULED], [60])

    def test_our_own_token_refresh_reconnects_at_once(self):
        connection, _hass, coordinator = _connected()
        coordinator.client.auth.token = "token-2"

        with self.assertLogs(push._LOGGER, "INFO"):
            connection._handle_disconnect(connection._client, "session taken")

        self.assertEqual([delay for delay, _ in SCHEDULED], [5])

    def test_after_another_login_took_the_session_it_backs_off_and_keeps_yielding(self):
        connection, _hass, _coordinator = _connected()
        connection._yielding = True

        with self.assertLogs(push._LOGGER, "INFO"):
            connection._handle_disconnect(connection._client, "kicked")

        self.assertEqual([delay for delay, _ in SCHEDULED], [60])
        self.assertTrue(connection.yielding)

    def test_nothing_reconnects_after_stopping(self):
        connection, _hass, _coordinator = _connected()
        client = connection._client
        asyncio.run(connection.async_stop())

        connection._handle_disconnect(client, "closed")

        self.assertEqual(SCHEDULED, [])


class ReconnectDelayTests(unittest.TestCase):
    """Ported from bojanrajkovic/upet-hass tests/test_push_logic.py."""

    def delay(self, previous_s, *, session_s=0, own_login=False):
        return push_logic.reconnect_delay(previous_s, session_s=session_s, own_login=own_login)

    def test_first_reconnect_waits_a_minute(self):
        self.assertEqual(self.delay(0), 60)

    def test_repeated_short_sessions_back_off_to_thirty_minutes(self):
        self.assertEqual(self.delay(60), 120)
        self.assertEqual(self.delay(1200), 1800)
        self.assertEqual(self.delay(1800), 1800)

    def test_a_long_session_starts_the_backoff_over(self):
        self.assertEqual(self.delay(1800, session_s=601), 60)

    def test_our_own_login_reconnects_in_seconds(self):
        self.assertEqual(self.delay(1800, own_login=True), 5)


if __name__ == "__main__":
    unittest.main()
