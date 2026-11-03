from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    def __init__(self, hass, logger, *, config_entry, **kwargs):
        self.hass = hass
        self.config_entry = config_entry
        self.data = None

    def async_update_listeners(self):
        pass


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


# What each storage key holds, shared by every Store, so a new coordinator reads what the last saved.
STORED: dict[str, dict] = {}


class _Store:
    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return dict(STORED[self.key]) if self.key in STORED else None

    async def async_save(self, data):
        STORED[self.key] = dict(data)


class _Registry:
    """Device and entity registry stand-in: known devices by identifier, entities by unique ID."""

    def __init__(self, devices=None, entities=None):
        self.devices = devices or {}
        self.entities = entities or {}

    def async_get_device(self, *, identifiers):
        return next((self.devices[i] for i in identifiers if i in self.devices), None)

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.entities.get((domain, platform, unique_id))


def load_coordinator_module():
    """Load coordinator.py against stand-ins for Home Assistant and api.py, removed once loaded."""
    package = _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)])
    api_error = type("UbpetApiError", (RuntimeError,), {})
    device_registry = _module(
        "homeassistant.helpers.device_registry", async_get=lambda hass: getattr(hass, "device_registry", _Registry())
    )
    entity_registry = _module(
        "homeassistant.helpers.entity_registry", async_get=lambda hass: getattr(hass, "entity_registry", _Registry())
    )
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.helpers": _module(
            "homeassistant.helpers", device_registry=device_registry, entity_registry=entity_registry
        ),
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.entity_registry": entity_registry,
        "homeassistant.helpers.storage": _module("homeassistant.helpers.storage", Store=_Store),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
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
            f"{PACKAGE_NAME}.const",
            DEFAULT_SCAN_INTERVAL=timedelta(minutes=3),
            DOMAIN="ubpet",
            EVENT_VISIT="ubpet_visit",
            EVENT_BOX="ubpet_box_event",
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


def _entry(create_task=None):
    """A config entry that hands the coroutines of its background tasks to create_task."""
    return types.SimpleNamespace(
        entry_id="entry", async_create_background_task=lambda hass, target, name: create_task(target)
    )


def _loaded_box(**parts) -> dict:
    box = {"device": {}, "config": {}, "box_use_times": {}, "deodorant": {}, "online": {}, "cat_records": [], "device_records": []}
    return {**box, **parts}


class FailedPartsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_coordinator_module()
        self.coordinator = self.module.UbpetDataUpdateCoordinator(types.SimpleNamespace(), _entry(), None)
        self.coordinator.data = {
            "devices": {"SN1": _loaded_box(config={"lazyTime": 900}, cat_records=[{"eventTime": 1}])},
            "cats": [{"catInfoId": 5}],
        }

    def keep(self, box: dict, cats=None) -> dict:
        data = {"devices": {"SN1": box}, "cats": cats}
        self.coordinator._keep_failed_parts(data)
        self.coordinator.data = data
        return data

    def test_parts_that_failed_to_load_keep_their_last_values(self):
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            data = self.keep(_loaded_box(config=None, cat_records=None, device_records=[]))

        self.assertEqual(data["devices"]["SN1"]["config"], {"lazyTime": 900})
        self.assertEqual(data["devices"]["SN1"]["cat_records"], [{"eventTime": 1}])
        self.assertEqual(data["cats"], [{"catInfoId": 5}])
        # Loaded but empty stays empty.
        self.assertEqual(data["devices"]["SN1"]["device_records"], [])

    def test_with_nothing_to_keep_a_failed_part_is_empty(self):
        self.coordinator.data = None
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            data = self.keep(_loaded_box(config=None, cat_records=None))

        self.assertEqual(data["devices"]["SN1"]["config"], {})
        self.assertEqual(data["devices"]["SN1"]["cat_records"], [])
        self.assertEqual(data["cats"], [])

    def test_a_part_failing_for_over_an_hour_goes_empty(self):
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            self.keep(_loaded_box(cat_records=None), cats=[])
        self.coordinator._parts_failing_since[("SN1", "cat_records")] -= timedelta(minutes=61)

        data = self.keep(_loaded_box(cat_records=None), cats=[])

        self.assertEqual(data["devices"]["SN1"]["cat_records"], [])

    def test_a_failing_part_warns_once_then_logs_when_it_loads_again(self):
        with self.assertLogs(self.module._LOGGER, "INFO") as logs:
            self.keep(_loaded_box(cat_records=None), cats=[])
            self.keep(_loaded_box(cat_records=None), cats=[])
            self.keep(_loaded_box(cat_records=[{"eventTime": 2}]), cats=[])

        self.assertEqual(
            [record.levelname for record in logs.records],
            ["WARNING", "INFO"],
        )
        self.assertNotIn(("SN1", "cat_records"), self.coordinator._parts_failing_since)


def _visit(record_id: int, cat_id: int = 5, cat: str | None = "Tiny", event_type: int = 3) -> dict:
    return {
        "recordId": record_id,
        "eventTime": 1793600000000,
        "eventType": event_type,
        "catInfoId": cat_id,
        "catNickname": cat,
        "weight": 6.7,
        "costTime": 58,
    }


class RecordEventTests(unittest.TestCase):
    """Ported from bojanrajkovic/upet-hass (c7feb81, cc6b96b, 4f74dcb, 167c42b)."""

    def setUp(self):
        STORED.clear()
        self.module = load_coordinator_module()
        self.fired: list[tuple[str, dict]] = []
        self.hass = types.SimpleNamespace(
            bus=types.SimpleNamespace(async_fire=lambda event, data: self.fired.append((event, data))),
            device_registry=_Registry(
                devices={("ubpet", "cat_5"): types.SimpleNamespace(id="tiny-device"), ("ubpet", "SN1"): types.SimpleNamespace(id="box-device")}
            ),
            entity_registry=_Registry(
                entities={("sensor", "ubpet", "entry_SN1_last_used_by"): "sensor.meowant_sc03_litter_box_last_used_by"}
            ),
        )

    def coordinator(self):
        return self.module.UbpetDataUpdateCoordinator(self.hass, _entry(), None)

    def fire(self, coordinator, **parts):
        import asyncio

        asyncio.run(coordinator._fire_new_records({"SN1": _loaded_box(device={"deviceName": "Litter Box"}, **parts)}))

    def test_the_first_update_starts_the_cursor_without_replaying_history(self):
        self.fire(self.coordinator(), cat_records=[_visit(11), _visit(10)], device_records=[_visit(20, cat_id=0, cat=None, event_type=31)])

        self.assertEqual(self.fired, [])
        self.assertEqual(STORED["ubpet.entry.visits"], {"SN1": 11, "SN1:box": 20})

    def test_a_new_visit_fires_once_linked_to_the_cat_and_the_box(self):
        coordinator = self.coordinator()
        self.fire(coordinator, cat_records=[_visit(10)])

        self.fire(coordinator, cat_records=[_visit(11), _visit(10)])
        self.fire(coordinator, cat_records=[_visit(11), _visit(10)])

        self.assertEqual(len(self.fired), 1)
        event, data = self.fired[0]
        self.assertEqual(event, "ubpet_visit")
        self.assertEqual((data["record_id"], data["cat"], data["box"], data["weight_kg"]), (11, "Tiny", "Litter Box", 6.7))
        self.assertEqual(data["device_id"], "tiny-device")
        self.assertEqual(data["entity_id"], "sensor.meowant_sc03_litter_box_last_used_by")

    def test_an_unmatched_visit_links_to_the_box(self):
        coordinator = self.coordinator()
        self.fire(coordinator, cat_records=[_visit(10)])

        self.fire(coordinator, cat_records=[_visit(11, cat_id=0, cat=None, event_type=21), _visit(10)])

        data = self.fired[0][1]
        self.assertIsNone(data["cat_id"])
        self.assertEqual(data["device_id"], "box-device")

    def test_a_finished_clean_fires_a_box_event(self):
        coordinator = self.coordinator()
        self.fire(coordinator, device_records=[_visit(20, cat_id=0, cat=None, event_type=31)])

        self.fire(coordinator, device_records=[_visit(21, cat_id=0, cat=None, event_type=31), _visit(20, cat_id=0, cat=None, event_type=31)])

        self.assertEqual([(event, data["record_id"]) for event, data in self.fired], [("ubpet_box_event", 21)])

    def test_a_failed_first_fetch_neither_starts_the_cursor_nor_replays_later(self):
        coordinator = self.coordinator()
        self.fire(coordinator, cat_records=None)
        self.assertNotIn("SN1", STORED.get("ubpet.entry.visits", {}))

        self.fire(coordinator, cat_records=[_visit(11), _visit(10)])

        self.assertEqual(self.fired, [])
        self.assertEqual(STORED["ubpet.entry.visits"]["SN1"], 11)

    def test_a_restart_neither_drops_nor_replays_visits(self):
        self.fire(self.coordinator(), cat_records=[_visit(10)])

        restarted = self.coordinator()
        self.fire(restarted, cat_records=[_visit(12), _visit(11), _visit(10)])

        self.assertEqual([data["record_id"] for _, data in self.fired], [11, 12])

    def test_the_update_fires_records_before_filling_in_failed_parts(self):
        # An empty kept-over list would otherwise start a fresh cursor at 0 and replay everything.
        import asyncio

        coordinator = self.coordinator()
        coordinator.client = types.SimpleNamespace(
            get_dashboard=lambda: {"devices": {"SN1": _loaded_box(cat_records=None)}, "cats": []}
        )
        self.hass.async_add_executor_job = lambda func, *args: asyncio.sleep(0, result=func(*args))
        with self.assertLogs(self.module._LOGGER, "WARNING"):
            asyncio.run(coordinator._async_update_data())

        self.assertNotIn("SN1", STORED.get("ubpet.entry.visits", {}))


EMPTIED_MS = 1793600000000
EMPTIED = datetime.fromtimestamp(EMPTIED_MS / 1000, UTC)


def _clean(record_id: int, after_s: int, event_type: int = 31) -> dict:
    """A device record `after_s` seconds after the bin was emptied (negative for before)."""
    return {"recordId": record_id, "eventTime": EMPTIED_MS + after_s * 1000, "eventType": event_type}


class WasteBinCountTests(unittest.TestCase):
    """Cleans since bin emptied: counted by the integration, since the box's own counter stays at 0."""

    def setUp(self):
        import asyncio

        self.asyncio = asyncio
        STORED.clear()
        self.module = load_coordinator_module()
        self.hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_fire=lambda event, data: None))

    def coordinator(self, page_size: int = 50):
        coordinator = self.module.UbpetDataUpdateCoordinator(self.hass, _entry(), types.SimpleNamespace(records_per_fetch=page_size))
        coordinator.data = {"devices": {}}
        return coordinator

    def update(self, coordinator, records, serial: str = "SN1") -> None:
        """One update with these device records, newest first, as get_dashboard returns them."""
        box = _loaded_box(device_records=records)
        self.asyncio.run(coordinator._fire_new_records({serial: box}))
        coordinator.data["devices"][serial] = box

    def empty(self, coordinator, when=EMPTIED, serial: str = "SN1") -> None:
        self.asyncio.run(coordinator.async_set_bin_emptied(serial, when))

    def test_unknown_until_the_bin_is_first_marked_emptied(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(2, 60), _clean(1, -60)])

        self.assertIsNone(coordinator.cleans_since_bin_emptied("SN1"))
        self.assertIsNone(coordinator.bin_emptied_at("SN1"))

    def test_marking_it_emptied_after_the_fact_counts_the_cleans_already_fetched(self):
        # The forgotten press: set to last night's time, the records already show the cleans since.
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(4, 300), _clean(3, 200, event_type=12), _clean(2, 100), _clean(1, -100)])

        self.empty(coordinator)

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 2)
        self.assertEqual(coordinator.bin_emptied_at("SN1"), EMPTIED)
        self.assertEqual(STORED["ubpet.entry.waste_bin"], {"SN1": {"emptied_at": EMPTIED_MS, "cleans": 2}})

    def test_each_new_clean_adds_one_once(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(1, -100)])
        self.empty(coordinator)

        self.update(coordinator, [_clean(3, 200), _clean(2, 100, event_type=12), _clean(1, -100)])
        self.update(coordinator, [_clean(3, 200), _clean(2, 100, event_type=12), _clean(1, -100)])

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 1)

    def test_a_clean_uploaded_late_counts_by_when_it_happened(self):
        # After an outage the box uploads what it stored: a clean from before the empty doesn't count.
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(1, -100)])
        self.empty(coordinator)

        self.update(coordinator, [_clean(3, 50), _clean(2, -50), _clean(1, -100)])

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 1)

    def test_more_cleans_than_one_fetch_holds_are_all_counted(self):
        coordinator = self.coordinator(page_size=3)
        self.update(coordinator, [_clean(1, -100)])
        self.empty(coordinator)

        records = {1: _clean(1, -100), **{record_id: _clean(record_id, record_id * 10) for record_id in range(2, 9)}}
        for newest in range(2, 9):
            # Each update fetches only the newest three.
            self.update(coordinator, [records[record_id] for record_id in range(newest, 0, -1)][:3])

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 7)

    def test_a_restart_keeps_the_count_without_counting_twice(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(2, 100), _clean(1, -100)])
        self.empty(coordinator)

        restarted = self.coordinator()
        self.update(restarted, [_clean(3, 200), _clean(2, 100), _clean(1, -100)])

        self.assertEqual(restarted.cleans_since_bin_emptied("SN1"), 2)
        self.assertEqual(restarted.bin_emptied_at("SN1"), EMPTIED)

    def test_a_time_older_than_the_fetched_records_is_unknown(self):
        coordinator = self.coordinator(page_size=2)
        self.update(coordinator, [_clean(2, 200), _clean(1, 100)])

        self.empty(coordinator)
        self.update(coordinator, [_clean(3, 300), _clean(2, 200)])

        self.assertIsNone(coordinator.cleans_since_bin_emptied("SN1"))

    def test_records_that_had_not_loaded_are_counted_at_the_next_update(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(1, -100)])
        coordinator.data["devices"]["SN1"]["device_records"] = []
        self.empty(coordinator)
        self.assertIsNone(coordinator.cleans_since_bin_emptied("SN1"))

        self.update(coordinator, [_clean(2, 100), _clean(1, -100)])

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 1)

    def test_a_new_time_counts_again_from_it(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(3, 300), _clean(2, 100), _clean(1, -100)])
        self.empty(coordinator)
        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 2)

        self.empty(coordinator, EMPTIED + timedelta(seconds=200))

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 1)

    def test_each_box_counts_on_its_own(self):
        coordinator = self.coordinator()
        self.update(coordinator, [_clean(1, -100)], serial="SN1")
        self.update(coordinator, [_clean(11, -100)], serial="SN2")
        self.empty(coordinator, serial="SN1")

        self.update(coordinator, [_clean(2, 100), _clean(1, -100)], serial="SN1")
        self.update(coordinator, [_clean(12, 100), _clean(11, -100)], serial="SN2")

        self.assertEqual(coordinator.cleans_since_bin_emptied("SN1"), 1)
        self.assertIsNone(coordinator.cleans_since_bin_emptied("SN2"))


class GuessedStateTests(unittest.TestCase):
    def setUp(self):
        coordinator_module = load_coordinator_module()
        self.coordinator = coordinator_module.UbpetDataUpdateCoordinator(types.SimpleNamespace(), _entry(), None)
        self.coordinator.data = {"devices": {"SN1": {}}}

    def state(self):
        return self.coordinator.data["devices"]["SN1"].get("mqtt_state")

    def reply(self, mode):
        self.coordinator.store_state_push("SN1", _state_reply("717401", mode)["decoded"])

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


class PushedStateTests(unittest.TestCase):
    """State and online status from the persistent MQTT connection (bojanrajkovic/upet-hass 37ebe31)."""

    def setUp(self):
        self.module = load_coordinator_module()
        self.updates = []
        self.coordinator = self.module.UbpetDataUpdateCoordinator(types.SimpleNamespace(), _entry(), None)
        self.coordinator.async_update_listeners = lambda: self.updates.append(True)
        self.coordinator.data = {"devices": {"SN1": {"online": {"online": True}}}}

    def test_a_push_keeps_the_state_and_files_the_bookkeeping_apart(self):
        decoded = _state_reply("717401", "IDLE")["decoded"]
        decoded.update({"cid": "cid-1", "risp_seq": 9, "new_decoder_field": 1, "w_cause": 90, "risp_cmd": 1024})

        self.assertTrue(self.coordinator.store_state_push("SN1", decoded))

        state = self.coordinator.data["devices"]["SN1"]["mqtt_state"]
        self.assertEqual(
            {key: value for key, value in state.items() if key != "message"},
            {"w_mode_app_name": "IDLE", "w_state_app_name": "PENDING", "w_cause": 90},
        )
        self.assertEqual((state["message"]["cid"], state["message"]["risp_cmd"]), ("cid-1", 1024))
        self.assertIsNotNone(self.coordinator.last_mqtt_update_at("SN1"))

    def test_a_message_without_a_state_changes_nothing(self):
        self.assertFalse(self.coordinator.store_state_push("SN1", {"risp_cmd": 1024, "from_uid": "717401"}))
        self.assertNotIn("mqtt_state", self.coordinator.data["devices"]["SN1"])

    def test_an_online_push_updates_the_box(self):
        self.coordinator.set_online("SN1", False)

        self.assertFalse(self.coordinator.data["devices"]["SN1"]["online"]["online"])
        self.assertEqual(self.updates, [True])

    def test_an_online_push_for_an_unknown_box_is_ignored(self):
        self.coordinator.set_online("SN9", False)

        self.assertEqual(self.updates, [])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        import asyncio

        self.asyncio = asyncio
        self.module = load_coordinator_module()
        self.calls = []

        async def run_in_executor(func, *args):
            return func(*args)

        self.hass = types.SimpleNamespace(async_add_executor_job=run_in_executor)

    def coordinator(self, get_dashboard):
        def dashboard():
            self.calls.append("dashboard")
            return get_dashboard()

        return self.module.UbpetDataUpdateCoordinator(self.hass, _entry(), types.SimpleNamespace(get_dashboard=dashboard))

    def test_a_refused_login_asks_for_reauth(self):
        def refused():
            raise self.module.UbpetAuthError("login refused")

        with self.assertRaises(self.module.ConfigEntryAuthFailed):
            self.asyncio.run(self.coordinator(refused)._async_update_data())

    def test_no_rest_update_while_another_login_holds_the_session(self):
        # A REST login now would sign the other session out (bojanrajkovic/upet-hass e9aa233).
        coordinator = self.coordinator(lambda: {"devices": {}, "cats": []})
        coordinator.data = {"devices": {"SN1": {}}}
        coordinator.push = types.SimpleNamespace(yielding=True)

        self.assertEqual(self.asyncio.run(coordinator._async_update_data()), {"devices": {"SN1": {}}})
        self.assertEqual(self.calls, [])

    def test_the_first_update_runs_even_while_yielding(self):
        coordinator = self.coordinator(lambda: {"devices": {}, "cats": []})
        coordinator.push = types.SimpleNamespace(yielding=True)

        self.asyncio.run(coordinator._async_update_data())

        self.assertEqual(self.calls, ["dashboard"])


if __name__ == "__main__":
    unittest.main()
