from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from typing import Generic, TypeVar
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_init_test"
T = TypeVar("T")


class _DataUpdateCoordinator(Generic[T]):
    pass


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_init_module():
    """Load the integration's __init__.py against stand-ins for Home Assistant, removed once loaded."""
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.const": _module(
            "homeassistant.const",
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
            Platform=types.SimpleNamespace(
                SENSOR="sensor", BINARY_SENSOR="binary_sensor", NUMBER="number", SWITCH="switch",
                TIME="time", SELECT="select", BUTTON="button",
            ),
        ),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object, callback=lambda func: func),
        "homeassistant.exceptions": _module("homeassistant.exceptions", ConfigEntryAuthFailed=Exception),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.event": _module("homeassistant.helpers.event", async_call_later=None),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_DataUpdateCoordinator,
            UpdateFailed=Exception,
        ),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME, COMPONENT_PATH / "__init__.py", submodule_search_locations=[str(COMPONENT_PATH)]
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


integration = load_init_module()


class FakeConfigEntries:
    def __init__(self, entries):
        self.entries = entries

    def async_entries(self, domain):
        return self.entries

    def async_update_entry(self, entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)


def _entry(entry_id: str, unique_id: str, minor_version: int = 1):
    return types.SimpleNamespace(
        entry_id=entry_id, unique_id=unique_id, version=1, minor_version=minor_version, title="Litter Box"
    )


class UniqueIdMigrationTests(unittest.TestCase):
    def migrate(self, entry, *others):
        hass = types.SimpleNamespace(config_entries=FakeConfigEntries([entry, *others]))
        return asyncio.run(integration.async_migrate_entry(hass, entry))

    def test_account_is_lower_cased(self):
        entry = _entry("a", "app-id:User@Example.com ")

        self.assertTrue(self.migrate(entry))
        self.assertEqual(entry.unique_id, "app-id:user@example.com")
        self.assertEqual(entry.minor_version, 2)

    def test_an_entry_already_holding_the_new_id_keeps_this_one_as_it_is(self):
        entry = _entry("a", "app-id:User@Example.com")
        other = _entry("b", "app-id:user@example.com", minor_version=2)

        with self.assertLogs(integration._LOGGER, "WARNING"):
            self.assertTrue(self.migrate(entry, other))
        self.assertEqual(entry.unique_id, "app-id:User@Example.com")
        self.assertEqual(entry.minor_version, 2)

    def test_a_newer_major_version_is_refused(self):
        entry = _entry("a", "app-id:user@example.com")
        entry.version = 2

        self.assertFalse(self.migrate(entry))


class RecordsPerFetchTests(unittest.TestCase):
    def test_client_uses_the_option_or_the_default(self):
        data = {"account": "user@example.com", "password": "secret", "device_id": "phone", "profile": "meowant"}

        self.assertEqual(integration.client_from_entry_data(data).records_per_fetch, 50)
        self.assertEqual(integration.client_from_entry_data(data, {"records_per_fetch": 120}).records_per_fetch, 120)

    def test_new_option_applies_from_the_next_update(self):
        refreshed = []

        async def request_refresh():
            refreshed.append(True)

        coordinator = types.SimpleNamespace(
            client=types.SimpleNamespace(records_per_fetch=50), async_request_refresh=request_refresh
        )
        hass = types.SimpleNamespace(data={"ubpet": {"a": coordinator}})
        entry = types.SimpleNamespace(entry_id="a", options={"records_per_fetch": 100})

        asyncio.run(integration._async_apply_options(hass, entry))
        asyncio.run(integration._async_apply_options(hass, entry))

        self.assertEqual(coordinator.client.records_per_fetch, 100)
        self.assertEqual(refreshed, [True])


if __name__ == "__main__":
    unittest.main()
