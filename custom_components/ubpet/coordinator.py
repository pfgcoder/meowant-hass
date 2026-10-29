from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import UbpetApiError, UbpetAuthError, UbpetClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .mqtt import STATE_FIELD_NAMES

_LOGGER = logging.getLogger(__name__)

# A state request now ends as soon as the box replies (about a second instead of a fixed 5 s
# listen), so wait 5 s between polls while running to keep the old pace of one every ~6 s.
MQTT_STATE_RUNNING_POLL_SECONDS = 5
MQTT_STATE_PAUSED_POLL_SECONDS = 10
MQTT_STATE_PENDING_POLL_SECONDS = 60
MQTT_STATE_POLL_HEARTBEAT_SECONDS = 1
# The parts get_dashboard reads besides each box's device entry, per box and for the account, with
# what each reads as empty. A part whose call failed comes back as None: the update shows its last
# values for up to KEEP_FAILED_PART_MINUTES, then (or with nothing to keep) an empty one, which the
# sensors show as unknown, so a lasting failure shows.
BOX_PARTS = {
    "config": dict,
    "box_use_times": dict,
    "deodorant": dict,
    "online": dict,
    "cat_records": list,
    "device_records": list,
}
ACCOUNT_PARTS = {"cats": list}
KEEP_FAILED_PART_MINUTES = 60


class UbpetDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, client: UbpetClient, *, update_interval: timedelta = DEFAULT_SCAN_INTERVAL) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=True,
        )
        self.client = client
        self._mqtt_state_by_serial: dict[str, dict[str, Any]] = {}
        # The box's last actual reply, which a button press's guess goes back to if the send fails.
        self._mqtt_reply_by_serial: dict[str, dict[str, Any]] = {}
        self._mqtt_state_poll_task: asyncio.Task[None] | None = None
        # Boxes with a state request still running. A box's next one waits for it to end.
        self._mqtt_state_requests_in_progress: set[str] = set()
        self._mqtt_state_polls_enabled = False
        # Set when a refused login stopped the polls, so the next update that logs in restarts them.
        self._mqtt_state_polls_paused_for_auth = False
        # Set for good when the entry unloads or Home Assistant stops.
        self._mqtt_state_polls_cancelled = False
        self._last_rest_update_at: datetime | None = None
        self._last_mqtt_update_at_by_serial: dict[str, datetime] = {}
        self._next_mqtt_poll_at_by_serial: dict[str, datetime] = {}
        # When each part that's failing to load, keyed (owner, part), first failed.
        self._parts_failing_since: dict[tuple[str, str], datetime] = {}
        # Boxes whose MQTT state polls are failing, logged at debug level after the first warning.
        self._mqtt_polls_failing: set[str] = set()

    async def _async_update_data(self) -> dict[str, Any]:
        started = time.monotonic()
        _LOGGER.info("Starting UPET data update")
        try:
            data = await self.hass.async_add_executor_job(self.client.get_dashboard)
        except UbpetAuthError as err:
            # The stored password was refused. Each retry is up to three logins and could lock the
            # account, so stop polling and let Home Assistant ask for the password (reauth).
            self._pause_mqtt_state_polls_for_auth()
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OSError, RuntimeError, UbpetApiError) as err:
            # Home Assistant logs the error when updates start failing and again when they recover;
            # a traceback on every failed update would flood the log through an outage.
            _LOGGER.debug("UPET data update failed", exc_info=True)
            raise UpdateFailed(str(err)) from err

        devices = data.get("devices", {})
        cats = data.get("cats", [])
        _LOGGER.info(
            "Finished UPET data update in %.2fs: devices=%s cats=%s",
            time.monotonic() - started,
            len(devices) if isinstance(devices, dict) else 0,
            len(cats) if isinstance(cats, list) else 0,
        )
        self._last_rest_update_at = datetime.now(UTC)
        self._keep_failed_parts(data)
        data = self._with_mqtt_state(data)
        if self._mqtt_state_polls_paused_for_auth:
            _LOGGER.info("UPET login works again; restarting MQTT state polls")
            self._mqtt_state_polls_paused_for_auth = False
            self._mqtt_state_polls_enabled = True
        if self._mqtt_state_polls_enabled:
            self._ensure_mqtt_state_poll_task()
            self._ensure_mqtt_state_polls(data)
        return data

    def _keep_failed_parts(self, data: dict[str, Any]) -> None:
        """Fill in the parts that failed to load, so one failed call doesn't drop sensors to unknown
        and back (re-firing automations), and log when a part starts and stops failing."""
        now = datetime.now(UTC)
        previous = self.data or {}
        previous_devices = previous.get("devices") or {}
        owners = [("the account", data, previous, ACCOUNT_PARTS)]
        devices = data.get("devices")
        if isinstance(devices, dict):
            owners += [
                (serial, item, previous_devices.get(serial) or {}, BOX_PARTS)
                for serial, item in devices.items()
                if isinstance(item, dict)
            ]
        for owner, item, before, parts in owners:
            for part, empty in parts.items():
                if item.get(part) is not None:
                    if self._parts_failing_since.pop((owner, part), None) is not None:
                        _LOGGER.info("UPET %s for %s loads again", part, owner)
                    continue
                since = self._parts_failing_since.get((owner, part))
                if since is None:
                    since = self._parts_failing_since[(owner, part)] = now
                    _LOGGER.warning(
                        "UPET couldn't load %s for %s; showing the last values for up to %s minutes",
                        part,
                        owner,
                        KEEP_FAILED_PART_MINUTES,
                    )
                kept = before.get(part)
                expired = now - since > timedelta(minutes=KEEP_FAILED_PART_MINUTES)
                item[part] = empty() if kept is None or expired else kept

    def store_mqtt_result(self, serial: str, result: dict[str, Any] | object) -> bool:
        state = _latest_mqtt_state(result)
        if state is None:
            return False
        self._mqtt_state_by_serial[serial] = state
        self._mqtt_reply_by_serial[serial] = state
        self._last_mqtt_update_at_by_serial[serial] = datetime.now(UTC)
        self._update_mqtt_data()
        return True

    @property
    def last_rest_update_at(self) -> datetime | None:
        return self._last_rest_update_at

    def last_mqtt_update_at(self, serial: str) -> datetime | None:
        return self._last_mqtt_update_at_by_serial.get(serial)

    def set_mqtt_work_state(self, serial: str, state: dict[str, Any]) -> dict[str, Any]:
        """Show a guessed state until the box replies; returns the guess."""
        mqtt_state = deepcopy(self._mqtt_state_by_serial.get(serial) or {})
        mqtt_state.pop("message", None)
        mqtt_state.update({key: value for key, value in state.items() if key in STATE_FIELD_NAMES})
        mqtt_state["guess"] = {"mqtt_service": state.get("mqtt_service"), "at": time.time()}
        self._mqtt_state_by_serial[serial] = mqtt_state
        self._update_mqtt_data()
        return mqtt_state

    def restore_mqtt_work_state(self, serial: str, guess: dict[str, Any]) -> None:
        """Undo set_mqtt_work_state after a failed send, going back to the box's last reply.

        A reply or a newer guess that replaced this one stays. Restoring the last reply rather than
        whatever the guess replaced keeps one failed press from bringing back another's guess.
        """
        if self._mqtt_state_by_serial.get(serial) is not guess:
            return
        if (reply := self._mqtt_reply_by_serial.get(serial)) is None:
            self._mqtt_state_by_serial.pop(serial, None)
        else:
            self._mqtt_state_by_serial[serial] = reply
        self._update_mqtt_data()

    def _update_mqtt_data(self) -> None:
        self.data = self._with_mqtt_state(self.data or {})
        self.async_update_listeners()

    def start_mqtt_state_poll(self, serial: str, *, fast: bool = False) -> None:
        if not self._mqtt_state_polls_enabled:
            # Polls start 30 s after setup and stop after a refused login. A press before they start
            # still wants the box's real state at once; after a refused login, asking would only try
            # the password again.
            if fast and not self._mqtt_state_polls_paused_for_auth:
                self.hass.async_create_task(self.async_request_mqtt_state(serial))
            return
        _LOGGER.info("Starting UPET MQTT state polling for %s fast=%s", serial, fast)
        self._schedule_mqtt_state_poll(serial, 0 if fast else self._mqtt_state_poll_delay(serial))
        self._ensure_mqtt_state_poll_task()

    def enable_mqtt_state_polls(self) -> None:
        if self._mqtt_state_polls_cancelled:
            return
        self._mqtt_state_polls_enabled = True
        self._ensure_mqtt_state_poll_task()
        self._ensure_mqtt_state_polls(self.data or {})

    def _pause_mqtt_state_polls_for_auth(self) -> None:
        if self._mqtt_state_polls_cancelled:
            # A poll or update still running when the entry unloaded mustn't set up a restart.
            return
        self._stop_mqtt_state_poll_loop()
        self._mqtt_state_polls_paused_for_auth = True

    def cancel_mqtt_state_polls(self) -> None:
        """Stop the polls for good: the entry is unloading or Home Assistant is stopping."""
        self._mqtt_state_polls_cancelled = True
        self._mqtt_state_polls_paused_for_auth = False
        self._stop_mqtt_state_poll_loop()

    def _stop_mqtt_state_poll_loop(self) -> None:
        self._mqtt_state_polls_enabled = False
        if self._mqtt_state_poll_task is not None:
            self._mqtt_state_poll_task.cancel()
            self._mqtt_state_poll_task = None
        self._next_mqtt_poll_at_by_serial.clear()

    def _ensure_mqtt_state_poll_task(self) -> None:
        if self._mqtt_state_poll_task is None or self._mqtt_state_poll_task.done():
            if self._mqtt_state_poll_task is not None and self._mqtt_state_poll_task.done():
                try:
                    self._mqtt_state_poll_task.result()
                except asyncio.CancelledError:
                    _LOGGER.warning("UPET MQTT state poll loop was cancelled; restarting")
                except Exception:
                    _LOGGER.exception("UPET MQTT state poll loop died; restarting")
            self._mqtt_state_poll_task = self.hass.async_create_task(self._mqtt_state_poll_loop())

    async def _mqtt_state_poll_loop(self) -> None:
        _LOGGER.info("Starting UPET MQTT state poll loop")
        try:
            while self._mqtt_state_polls_enabled:
                try:
                    self._run_due_mqtt_state_polls()
                except Exception:
                    _LOGGER.exception("Unexpected UPET MQTT state poll loop failure")
                await asyncio.sleep(MQTT_STATE_POLL_HEARTBEAT_SECONDS)
        except asyncio.CancelledError:
            raise
        finally:
            _LOGGER.info("Stopped UPET MQTT state poll loop")

    async def async_request_mqtt_state(self, serial: str) -> None:
        if serial in self._mqtt_state_requests_in_progress:
            _LOGGER.debug("UPET MQTT state request already in progress for %s", serial)
            return
        self._next_mqtt_poll_at_by_serial.pop(serial, None)
        self._mqtt_state_requests_in_progress.add(serial)
        _LOGGER.info("Requesting UPET MQTT state for %s", serial)
        try:
            # No timeout of our own: Home Assistant would stop waiting, but the send's thread would
            # carry on, and the polls after it would queue for the client's MQTT lock, each holding
            # one of Home Assistant's shared worker threads. Every network step of a send has its
            # own timeout, so it ends anyway.
            result = await self.hass.async_add_executor_job(self.client.send_mqtt_service, serial, "request_state")
            if isinstance(result, dict):
                result.setdefault("service_id", "request_state")
            updated = self.store_mqtt_result(serial, result)
            if serial in self._mqtt_polls_failing:
                self._mqtt_polls_failing.discard(serial)
                _LOGGER.info("UPET MQTT state polls work again for %s", serial)
            item = self.data.get("devices", {}).get(serial) if self.data else None
            _LOGGER.info(
                "Finished UPET MQTT state request for %s updated=%s state=%s mode=%s received=%s",
                serial,
                updated,
                _mqtt_work_state(item),
                _mqtt_work_mode(item),
                result.get("received_count") if isinstance(result, dict) else None,
            )
        except UbpetAuthError as err:
            # Stop polling with a refused password. The next data update either logs in, which
            # restarts the polls, or starts the reauth.
            _LOGGER.warning("UPET MQTT state poll stopped for %s: login refused: %s", serial, err)
            self._pause_mqtt_state_polls_for_auth()
        except (OSError, RuntimeError, UbpetApiError) as err:
            self._mqtt_poll_failed(serial, str(err))
        except Exception:
            _LOGGER.exception("Unexpected UPET MQTT state poll failure for %s", serial)
        finally:
            self._mqtt_state_requests_in_progress.discard(serial)
            self._schedule_next_mqtt_state_poll(serial)

    def _mqtt_poll_failed(self, serial: str, reason: str) -> None:
        # While the box or the cloud can't be reached every poll fails, once a minute or more, so
        # only the first one is a warning.
        if serial in self._mqtt_polls_failing:
            _LOGGER.debug("UPET MQTT state poll failed again for %s: %s", serial, reason)
            return
        self._mqtt_polls_failing.add(serial)
        _LOGGER.warning(
            "UPET MQTT state poll failed for %s: %s; logging further failures at debug level until a poll works",
            serial,
            reason,
        )

    def _with_mqtt_state(self, data: dict[str, Any]) -> dict[str, Any]:
        devices = data.get("devices")
        if not isinstance(devices, dict):
            return data
        # Copy only the levels that change: the records and config underneath are shared, not
        # deep-copied on every MQTT update.
        merged_devices: dict[str, Any] = {}
        for serial, item in devices.items():
            if not isinstance(item, dict):
                merged_devices[serial] = item
                continue
            item = dict(item)
            if state := self._mqtt_state_by_serial.get(serial):
                item["mqtt_state"] = deepcopy(state)
            else:
                # Also clears a guess that was undone before the box ever replied.
                item.pop("mqtt_state", None)
            merged_devices[serial] = item
        return {**data, "devices": merged_devices}

    def _schedule_next_mqtt_state_poll(self, serial: str) -> None:
        self._schedule_mqtt_state_poll(serial, self._mqtt_state_poll_delay(serial))

    def _schedule_mqtt_state_poll(self, serial: str, delay: int) -> None:
        _LOGGER.info("Scheduling UPET MQTT state poll for %s in %ss", serial, delay)
        self._next_mqtt_poll_at_by_serial[serial] = datetime.now(UTC) + timedelta(seconds=delay)

    def _ensure_mqtt_state_polls(self, data: dict[str, Any]) -> None:
        devices = data.get("devices")
        if not isinstance(devices, dict):
            return
        for serial in devices:
            self._next_mqtt_poll_at_by_serial.setdefault(serial, datetime.now(UTC))

    def _run_due_mqtt_state_polls(self) -> None:
        devices = self.data.get("devices", {}) if self.data else {}
        if not isinstance(devices, dict):
            return
        now = datetime.now(UTC)
        for serial in devices:
            if serial in self._mqtt_state_requests_in_progress:
                continue
            due_at = self._next_mqtt_poll_at_by_serial.get(serial)
            if due_at is not None and due_at > now:
                continue
            _LOGGER.info("Running due UPET MQTT state poll for %s", serial)
            self.hass.async_create_task(self.async_request_mqtt_state(serial))

    def _mqtt_state_poll_delay(self, serial: str) -> int:
        state = _mqtt_work_state(self.data.get("devices", {}).get(serial) if self.data else None)
        if state == "RUNNING":
            return MQTT_STATE_RUNNING_POLL_SECONDS
        if state == "PAUSED":
            return MQTT_STATE_PAUSED_POLL_SECONDS
        return MQTT_STATE_PENDING_POLL_SECONDS


def _latest_mqtt_state(result: dict[str, Any] | object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    messages = result.get("received_messages")
    if not isinstance(messages, list):
        return None
    # The request went to the box's IM contact, so only its replies count; anything else arriving
    # on the topic during the listen window could set a false status.
    target_uid = result.get("target_uid")

    latest: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        decoded = message.get("decoded")
        if not isinstance(decoded, dict):
            continue
        if decoded.get("risp_cmd") != 1021:
            continue
        if target_uid is None or str(decoded.get("from_uid")) != str(target_uid):
            continue
        if "w_mode_app_name" not in decoded and "w_state_app_name" not in decoded:
            continue
        # Only the box's state fields, so a new field from the decoder can't add per-reply noise.
        latest = {key: value for key, value in decoded.items() if key in STATE_FIELD_NAMES and value not in (None, "")}
        # The message's bookkeeping, which differs on every reply, kept apart from the state.
        latest["message"] = {
            "cid": decoded.get("cid"),
            "gid": decoded.get("gid"),
            "created_at_ms": decoded.get("created_at_ms"),
            "from_uid": decoded.get("from_uid"),
            "to_uid": decoded.get("to_uid"),
            "received_phase": message.get("phase"),
            "received_topic": message.get("topic"),
            "received_qos": message.get("qos"),
            "mqtt_service": result.get("service_id"),
            "command_cid": result.get("cid"),
            "command_gid": result.get("gid"),
            "command_created_at_ms": result.get("created_at_ms"),
        }
    return latest


def _mqtt_work_state(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    mqtt_state = item.get("mqtt_state")
    if not isinstance(mqtt_state, dict):
        return None
    state = mqtt_state.get("w_state_app_name")
    return state if isinstance(state, str) else None


def _mqtt_work_mode(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    mqtt_state = item.get("mqtt_state")
    if not isinstance(mqtt_state, dict):
        return None
    mode = mqtt_state.get("w_mode_app_name")
    return mode if isinstance(mode, str) else None
