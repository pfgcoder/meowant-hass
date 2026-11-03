from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import UbpetApiError, UbpetAuthError, UbpetClient
from .bin_count import cleans_after, recount
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_BOX, EVENT_VISIT
from .mqtt import STATE_FIELD_NAMES
from .visits import new_visits, visit_event_data

if TYPE_CHECKING:
    from .push import UbpetPush

_LOGGER = logging.getLogger(__name__)

# (records key in the dashboard, event to fire, cursor key suffix). Cat visits keep the bare
# serial as their key, as in bojanrajkovic/upet-hass, so its stores would carry over.
RECORD_STREAMS = (("cat_records", EVENT_VISIT, ""), ("device_records", EVENT_BOX, ":box"))

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
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: UbpetClient,
        *,
        update_interval: timedelta = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=True,
        )
        self.client = client
        self._mqtt_state_by_serial: dict[str, dict[str, Any]] = {}
        # The box's last actual reply, which a button press's guess goes back to if the send fails.
        self._mqtt_reply_by_serial: dict[str, dict[str, Any]] = {}
        # The persistent MQTT connection, set by async_setup_entry before the first refresh.
        self.push: UbpetPush | None = None
        self._last_rest_update_at: datetime | None = None
        self._last_mqtt_update_at_by_serial: dict[str, datetime] = {}
        # When each part that's failing to load, keyed (owner, part), first failed.
        self._parts_failing_since: dict[tuple[str, str], datetime] = {}
        # The newest record reported per box and stream. Stored, so a restart neither drops nor
        # replays visits.
        self._record_store: Store[dict[str, int]] = Store(hass, 1, f"{DOMAIN}.{config_entry.entry_id}.visits")
        self._record_cursor: dict[str, int] | None = None
        # Per box, when the waste bin was emptied (epoch ms) and the auto cleans since then, or None
        # while they can't be counted. Stored, so a restart keeps the count.
        self._bin_store: Store[dict[str, dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{config_entry.entry_id}.waste_bin")
        self._bin: dict[str, dict[str, Any]] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        if self.push is not None and self.push.yielding and self.data is not None:
            # Another login holds the account's one session. A REST login now would sign it out;
            # the reconnect fetches what was missed (bojanrajkovic/upet-hass e9aa233).
            return self.data
        started = time.monotonic()
        _LOGGER.info("Starting UPET data update")
        try:
            data = await self.hass.async_add_executor_job(self.client.get_dashboard)
        except UbpetAuthError as err:
            # The stored password was refused. Each retry is up to three logins and could lock the
            # account, so let Home Assistant ask for the password (reauth).
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
        if isinstance(devices, dict):
            # Before the failed parts are filled in. A failed records fetch is still None here,
            # which reports nothing; after the fill-in it could be an empty list, which on the
            # first update would start the cursor at 0 and replay every visit as new.
            await self._fire_new_records(devices)
        self._keep_failed_parts(data)
        return self._with_mqtt_state(data)

    async def _fire_new_records(self, devices: dict[str, Any]) -> None:
        """Fire an event for each record newer than the stored cursor (bojanrajkovic c7feb81, cc6b96b)."""
        if self._record_cursor is None:
            self._record_cursor = await self._record_store.async_load() or {}
        await self._async_load_bin()
        changed = bin_changed = False
        for serial, item in devices.items():
            if not isinstance(item, dict):
                continue
            box_name = (item.get("device") or {}).get("deviceName")
            for records_key, event, suffix in RECORD_STREAMS:
                cursor_key = serial + suffix
                batch = new_visits(item.get(records_key), self._record_cursor.get(cursor_key))
                for record in batch.visits:
                    event_data = visit_event_data(serial, box_name, record)
                    self.hass.bus.async_fire(event, {**event_data, **self._logbook_links(serial, event_data["cat_id"])})
                if batch.last_record_id is not None and batch.last_record_id != self._record_cursor.get(cursor_key):
                    self._record_cursor[cursor_key] = batch.last_record_id
                    changed = True
                if batch.visits:
                    _LOGGER.info("UPET reported %s new %s for %s", len(batch.visits), records_key, serial)
                if records_key == "device_records" and self._count_bin_cleans(serial, item.get(records_key), batch.visits):
                    bin_changed = True
        if changed:
            await self._record_store.async_save(self._record_cursor)
        if bin_changed:
            await self._bin_store.async_save(self._bin)

    async def _async_load_bin(self) -> None:
        if self._bin is None:
            self._bin = await self._bin_store.async_load() or {}

    def _count_bin_cleans(self, serial: str, records: list[dict[str, Any]] | None, new_records: list[dict[str, Any]]) -> bool:
        """Bring the box's cleans since the bin was emptied up to date; True if the count changed.

        The record cursor hands over each record once, so new cleans are added as they come and a
        week of them never needs more records than one fetch holds. A count that couldn't be made
        yet (the records hadn't loaded) is tried again from the whole fetch instead.
        """
        bin_entry = (self._bin or {}).get(serial)
        if bin_entry is None:
            return False
        if bin_entry.get("cleans") is None:
            cleans = recount(records, bin_entry["emptied_at"], self.client.records_per_fetch)
        else:
            cleans = bin_entry["cleans"] + cleans_after(new_records, bin_entry["emptied_at"])
        if cleans == bin_entry.get("cleans"):
            return False
        bin_entry["cleans"] = cleans
        return True

    async def async_set_bin_emptied(self, serial: str, when: datetime) -> None:
        """Count the box's cleans from `when`: Reset waste bin's press, or a time set by hand.

        Counted from scratch in the records already fetched, which the record cursor has passed,
        so the cleans that come after are added once each by the next updates.
        """
        await self._async_load_bin()
        emptied_at = int(when.timestamp() * 1000)
        item = ((self.data or {}).get("devices") or {}).get(serial) or {}
        cleans = recount(item.get("device_records"), emptied_at, self.client.records_per_fetch)
        self._bin[serial] = {"emptied_at": emptied_at, "cleans": cleans}
        await self._bin_store.async_save(self._bin)
        _LOGGER.info("UPET waste bin for %s emptied at %s; %s cleans since", serial, when.isoformat(), cleans)
        self.async_update_listeners()

    def bin_emptied_at(self, serial: str) -> datetime | None:
        bin_entry = (self._bin or {}).get(serial)
        return None if bin_entry is None else datetime.fromtimestamp(bin_entry["emptied_at"] / 1000, UTC)

    def cleans_since_bin_emptied(self, serial: str) -> int | None:
        return ((self._bin or {}).get(serial) or {}).get("cleans")

    def _logbook_links(self, serial: str, cat_id: Any) -> dict[str, str]:
        """Link a record to the cat's device and the box's Last used by sensor.

        Device pages list logbook entries whose event data names the device or one of its
        entities, but the logbook skips continuous entities (anything with a unit or a state
        class). The cat's device puts a visit on the cat's page; the box's Last used by sensor,
        plain text, puts it on the box's page. An unmatched visit names the box's device.
        """
        devices = dr.async_get(self.hass)
        links: dict[str, str] = {}
        cat_device = devices.async_get_device(identifiers={(DOMAIN, f"cat_{cat_id}")}) if cat_id is not None else None
        if device := cat_device or devices.async_get_device(identifiers={(DOMAIN, serial)}):
            links["device_id"] = device.id
        unique_id = f"{self.config_entry.entry_id}_{serial}_last_used_by"
        if entity_id := er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, unique_id):
            links["entity_id"] = entity_id
        return links

    @property
    def every_part_loaded(self) -> bool:
        """True when the last update loaded every part, so what it lacks is really gone."""
        return not self._parts_failing_since

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

    def store_state_push(self, serial: str, decoded: dict[str, Any]) -> bool:
        """Keep a box's state from a push, or from the reply to the state request sent on connect.

        The connection only passes on messages from the box's own IM contact (classify_push).
        """
        state = _state_from_decoded(decoded)
        if state is None:
            return False
        self._mqtt_state_by_serial[serial] = state
        self._mqtt_reply_by_serial[serial] = state
        self._last_mqtt_update_at_by_serial[serial] = datetime.now(UTC)
        self._update_mqtt_data()
        return True

    def set_online(self, serial: str, online: bool) -> None:
        """Apply an online/offline push; the next REST update reports the same value."""
        item = (self.data or {}).get("devices", {}).get(serial)
        if isinstance(item, dict) and isinstance(item.get("online"), dict):
            item["online"]["online"] = online
            self.async_update_listeners()

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

def _state_from_decoded(decoded: dict[str, Any]) -> dict[str, Any] | None:
    """A box's state from a decoded state push or reply, or None if the message carries none."""
    if not isinstance(decoded, dict):
        return None
    if "w_mode_app_name" not in decoded and "w_state_app_name" not in decoded:
        return None
    # Only the box's state fields, so a new field from the decoder can't add per-message noise.
    state = {key: value for key, value in decoded.items() if key in STATE_FIELD_NAMES and value not in (None, "")}
    # The message's bookkeeping, which differs on every message, kept apart from the state.
    state["message"] = {
        key: decoded.get(key) for key in ("risp_cmd", "cid", "gid", "created_at_ms", "from_uid", "to_uid")
    }
    return state
