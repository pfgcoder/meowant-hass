"""Persistent connection to the vendor IM/MQTT broker.

The boxes push work-state changes, new-record signals and online status to every linked
account. One connection per config entry receives them, and commands go out over the same
connection, so visits reach Home Assistant within seconds and the 3-minute REST update is
only a fallback.

The vendor allows one session per account: any other login, including our own REST token
refresh, kicks this connection. `reconnect_delay` decides how long to wait before taking the
session back.

Ported from bojanrajkovic/upet-hass (37ebe31, e9aa233, ff5a53e, b0edf9b, 0a39720), without its
Cat inside presence logic. Two changes for this fork: a refused password stops the reconnects
(each login tries up to three account types and repeated refusals can lock the account; the
reauth reloads the entry, which starts a new connection), and box state goes through the
coordinator's field allowlist like any other state.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.util.ssl import client_context

from .api import UbpetApiError, UbpetAuthError, find_account_uid
from .const import EVENT_UNKNOWN_MESSAGE
from .mqtt import (
    DEFAULT_MQTT_KEEPALIVE,
    DEFAULT_MQTT_PORT,
    ConfigChanged,
    Ignored,
    Kicked,
    NewRecord,
    OnlineChange,
    StatePush,
    classify_push,
    decode_mqtt_payload_safely,
    generate_gid,
    mqtt_host_from_base_url,
    resolve_service_payload,
)
from .push_logic import reconnect_delay

if TYPE_CHECKING:
    from .coordinator import UbpetDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# The vendor writes the record or new settings just before the push; give REST a moment to serve them.
REFRESH_DELAY_S = 3


class UbpetPush:
    def __init__(self, hass: HomeAssistant, coordinator: UbpetDataUpdateCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._client: paho.Client | None = None
        self._uid_to_serial: dict[str, str] = {}
        self._serial_to_uid: dict[str, str] = {}
        self._account_uid = ""
        self._pub_topic = ""
        self._sub_topic = ""
        self._token_at_connect: str | None = None
        self._connected_at: float | None = None
        self._delay_s = 0.0
        self._cancel_reconnect: Callable[[], None] | None = None
        self._cancel_fetch: Callable[[], None] | None = None
        self._stopped = False
        # Another login took the session; stay off the account until the reconnect.
        self._yielding = False

    @property
    def connected(self) -> bool:
        return self._connected_at is not None

    @property
    def yielding(self) -> bool:
        """True while backing off after another login took the session.

        Any REST login would sign that other session out, so the coordinator
        skips its updates until the reconnect.
        """
        return self._yielding

    async def async_start(self) -> None:
        await self._async_connect()

    async def async_stop(self) -> None:
        self._stopped = True
        for cancel in (self._cancel_reconnect, self._cancel_fetch):
            if cancel is not None:
                cancel()
        self._cancel_reconnect = self._cancel_fetch = None
        await self._async_close()

    async def async_publish(self, serial: str, service_id: str) -> None:
        """Send a work command or state request to one box."""
        self._publish(serial, service_id)

    def _publish(self, serial: str, service_id: str) -> None:
        client, box_uid = self._client, self._serial_to_uid.get(serial)
        if client is None or not self.connected or box_uid is None:
            raise HomeAssistantError("Not connected to the Air Pet cloud right now. Try again in a minute.")
        parts = resolve_service_payload(service_id, seq=None, from_uid=self._account_uid, to_uid=box_uid, gid=generate_gid())
        info = client.publish(self._pub_topic, parts.im_message, qos=0)
        if info.rc != paho.MQTT_ERR_SUCCESS:
            raise HomeAssistantError(f"Could not send {service_id}: {paho.error_string(info.rc)}")
        _LOGGER.info("Sent UPET MQTT service=%s serial=%s", service_id, serial)

    async def _async_connect(self, _now: Any = None) -> None:
        self._cancel_reconnect = None
        self._yielding = False
        if self._stopped:
            return
        try:
            credentials, friends, token = await self._hass.async_add_executor_job(self._fetch_session)
        except UbpetAuthError as err:
            # The password was refused. Retrying would only try it again; the reauth that the
            # next REST update starts reloads the entry, which connects afresh.
            _LOGGER.warning("UPET MQTT login refused, so not reconnecting until the password is fixed: %s", err)
            return
        except (OSError, RuntimeError, UbpetApiError) as err:
            _LOGGER.warning("UPET MQTT login failed: %s", err)
            self._schedule_reconnect(session_s=0, own_login=False)
            return

        devices = (self._coordinator.data or {}).get("devices", {})
        self._uid_to_serial = {
            str(friend["userId"]): friend["userName"]
            for friend in friends
            if isinstance(friend, dict) and friend.get("userId") and friend.get("userName") in devices
        }
        self._serial_to_uid = {serial: uid for uid, serial in self._uid_to_serial.items()}
        try:
            self._account_uid = find_account_uid(credentials, self._coordinator.client.auth)
            self._pub_topic = credentials["pubTopic"]
            self._sub_topic = credentials["subTopic"]
            client = paho.Client(
                CallbackAPIVersion.VERSION2,
                client_id=credentials["clientId"],
                protocol=paho.MQTTv311,
                reconnect_on_failure=False,
            )
            client.username_pw_set(credentials["mqttUserName"], credentials["mqttPassword"])
            client.tls_set_context(client_context())
        except (KeyError, UbpetApiError) as err:
            _LOGGER.warning("UPET MQTT login gave no usable credentials: %s", err)
            self._schedule_reconnect(session_s=0, own_login=False)
            return
        self._token_at_connect = token
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        self._client = client
        host = mqtt_host_from_base_url(self._coordinator.client.base_url)
        client.connect_async(host, DEFAULT_MQTT_PORT, keepalive=DEFAULT_MQTT_KEEPALIVE)
        client.loop_start()

    def _fetch_session(self) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        api = self._coordinator.client
        credentials = api.get_im_credentials()
        friends = api.get_im_friends()
        return credentials, friends, api.auth.token if api.auth else None

    async def _async_close(self) -> None:
        client, self._client = self._client, None
        self._connected_at = None
        if client is not None:
            client.disconnect()
            # loop_stop joins paho's network thread, so it can't run on the event loop.
            await self._hass.async_add_executor_job(client.loop_stop)

    # paho calls these on its network thread; hop to the event loop first.
    def _on_connect(self, client: paho.Client, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_connect, client, reason_code)

    def _on_message(self, client: paho.Client, _userdata: Any, message: paho.MQTTMessage) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_message, client, message.payload)

    def _on_disconnect(self, client: paho.Client, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_disconnect, client, reason_code)

    def _on_connect_fail(self, client: paho.Client, _userdata: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self._handle_disconnect, client, "connect failed")

    @callback
    def _handle_connect(self, client: paho.Client, reason_code: Any) -> None:
        if client is not self._client:
            return
        if reason_code.is_failure:
            _LOGGER.warning("UPET MQTT connect refused: %s", reason_code)
            client.disconnect()
            return
        client.subscribe(self._sub_topic, qos=1)
        self._connected_at = time.monotonic()
        _LOGGER.info("UPET MQTT connected for %s box(es)", len(self._serial_to_uid))
        self._coordinator.async_update_listeners()
        for serial in self._serial_to_uid:
            try:
                # The reply seeds the box's state; from then on the box pushes each change.
                self._publish(serial, "request_state")
            except HomeAssistantError as err:
                _LOGGER.warning("UPET MQTT state request for %s failed: %s", serial, err)
        # Catch up on visits recorded while the connection was down.
        self._hass.async_create_task(self._coordinator.async_request_refresh())

    @callback
    def _handle_message(self, client: paho.Client, payload: bytes) -> None:
        if client is not self._client:
            return
        decoded = decode_mqtt_payload_safely(payload)
        message = classify_push(decoded, self._uid_to_serial)
        _LOGGER.debug("UPET MQTT push: %s cmd=%s", type(message).__name__, decoded.get("risp_cmd"))
        match message:
            case Kicked():
                _LOGGER.info("UPET MQTT session taken over by another login")
                self._yielding = True
                client.disconnect()
            case StatePush(serial=serial, state=state):
                self._coordinator.store_state_push(serial, state)
            case NewRecord() | ConfigChanged():
                self._schedule_refresh()
            case OnlineChange(serial=serial, online=online):
                self._coordinator.set_online(serial, online)
            case Ignored(reason="receipt"):
                pass
            case Ignored(reason=reason):
                # Keep anything unrecognized, such as health alerts, so it can be decoded later.
                self._hass.bus.async_fire(
                    EVENT_UNKNOWN_MESSAGE,
                    {
                        "reason": reason,
                        **{key: decoded.get(key) for key in ("from_uid", "to_uid", "content_type", "type_url", "risp_cmd")},
                        "payload_hex": payload.hex(),
                    },
                )

    @callback
    def _handle_disconnect(self, client: paho.Client, reason_code: Any) -> None:
        if client is not self._client or self._stopped:
            return
        session_s = time.monotonic() - self._connected_at if self._connected_at is not None else 0
        api = self._coordinator.client
        own_login = api.auth is not None and api.auth.token != self._token_at_connect
        self._yielding = self._yielding and not own_login
        _LOGGER.info("UPET MQTT disconnected after %.0fs: %s", session_s, reason_code)
        self._hass.async_create_task(self._async_close())
        self._coordinator.async_update_listeners()
        self._schedule_reconnect(session_s=session_s, own_login=own_login)

    @callback
    def _schedule_reconnect(self, *, session_s: float, own_login: bool) -> None:
        if self._stopped or self._cancel_reconnect is not None:
            return
        self._delay_s = reconnect_delay(self._delay_s, session_s=session_s, own_login=own_login)
        _LOGGER.info("UPET MQTT reconnecting in %.0fs", self._delay_s)
        self._cancel_reconnect = async_call_later(self._hass, self._delay_s, self._async_connect)

    @callback
    def _schedule_refresh(self) -> None:
        if self._cancel_fetch is not None:
            return

        @callback
        def _fetch(_now: Any) -> None:
            self._cancel_fetch = None
            self._hass.async_create_task(self._coordinator.async_request_refresh())

        self._cancel_fetch = async_call_later(self._hass, REFRESH_DELAY_S, _fetch)
