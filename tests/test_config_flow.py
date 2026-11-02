from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_config_flow_test"


class _ConfigFlow:
    """Just enough of ConfigFlow for the reconfigure step: each result is returned as a dict."""

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}

    def async_update_reload_and_abort(self, entry, **kwargs):
        self.updated = (entry, kwargs)
        return {"type": "abort", "reason": "reconfigure_successful"}

    def _get_reconfigure_entry(self):
        return self.reconfigure_entry

    def _async_current_entries(self, include_ignore=True):
        return self.current_entries


class _Anything:
    """Stands in for any selector class, config or constant: callable, with any attribute."""

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self


class _Selector(types.ModuleType):
    """The form's schema isn't under test, so every selector name is a stand-in."""

    def __getattr__(self, name):
        return _Anything()


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def load_config_flow():
    """Load config_flow.py against stand-ins for Home Assistant and voluptuous, removed once loaded."""
    selector = _Selector("homeassistant.helpers.selector")
    platform = Enum("Platform", {name: name.lower() for name in ("BINARY_SENSOR", "BUTTON", "NUMBER", "SELECT", "SENSOR", "SWITCH", "TIME")})
    stubs = {
        "voluptuous": _module("voluptuous", Schema=lambda fields: fields, Required=lambda key, default=None: key),
        "homeassistant": _module(
            "homeassistant",
            config_entries=_module("homeassistant.config_entries", ConfigFlow=_ConfigFlow, OptionsFlow=object),
        ),
        "homeassistant.const": _module(
            "homeassistant.const", CONF_PASSWORD="password", CONF_USERNAME="username", Platform=platform
        ),
        "homeassistant.core": _module("homeassistant.core", callback=lambda func: func),
        "homeassistant.helpers": _module("homeassistant.helpers", selector=selector),
        "homeassistant.helpers.selector": selector,
        # config_flow.py imports client_from_entry_data from the package's __init__, which
        # the reconfigure step doesn't use.
        PACKAGE_NAME: _module(PACKAGE_NAME, __path__=[str(COMPONENT_PATH)], client_from_entry_data=None),
    }
    stubs["homeassistant.config_entries"] = stubs["homeassistant"].config_entries
    spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.config_flow", COMPONENT_PATH / "config_flow.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(sys.modules, stubs):
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


config_flow = load_config_flow()


class FakeHass:
    def __init__(self, coordinators: dict[str, Any]):
        self.data = {"ubpet": coordinators}
        self.config = types.SimpleNamespace(country="US")

    async def async_add_executor_job(self, func, *args):
        return func(*args)


ENTRY_DATA = {
    "account": "shared@example.com",
    "password": "old-secret",
    "device_id": "device-1",
    "profile": "meowant",
    "country": "US",
    "area_code": "US",
    "something_else": "kept",
}


def _flow(devices_now=("SN1",), other_unique_ids=()):
    entry = types.SimpleNamespace(entry_id="entry", unique_id="app:shared@example.com", data=dict(ENTRY_DATA))
    others = [types.SimpleNamespace(entry_id=f"other{n}", unique_id=uid) for n, uid in enumerate(other_unique_ids)]
    coordinator = types.SimpleNamespace(data={"devices": {serial: {} for serial in devices_now}})
    flow = config_flow.UbpetConfigFlow()
    flow.hass = FakeHass({"entry": coordinator})
    flow.reconfigure_entry = entry
    flow.current_entries = [entry, *others]
    return flow, entry


OWNER = {"country": "US", "username": " Owner@Example.com ", "password": "new-secret"}


def _submit(flow, user_input=OWNER, devices=({"serialNumber": "SN1"},), error: Exception | None = None):
    seen: list[dict] = []

    def validate(data):
        seen.append(data)
        if error is not None:
            raise error
        return list(devices)

    with patch.object(config_flow, "_validate_input", validate):
        result = asyncio.run(flow.async_step_reconfigure(dict(user_input)))
    return result, seen


class ReconfigureTests(unittest.TestCase):
    def test_the_form_starts_from_the_current_account(self):
        flow, _ = _flow()

        result = asyncio.run(flow.async_step_reconfigure())

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["description_placeholders"], {"account": "shared@example.com"})
        self.assertEqual(result["errors"], {})

    def test_an_account_that_sees_the_same_box_replaces_the_old_one(self):
        flow, entry = _flow()

        result, seen = _submit(flow)

        self.assertEqual(result, {"type": "abort", "reason": "reconfigure_successful"})
        updated_entry, changes = flow.updated
        self.assertIs(updated_entry, entry)
        self.assertTrue(changes["unique_id"].endswith(":owner@example.com"))
        data = changes["data"]
        self.assertEqual(data["account"], "Owner@Example.com")
        self.assertEqual(data["password"], "new-secret")
        # Still the same Home Assistant: the device ID stays, and so does anything not about the account.
        self.assertEqual(data["device_id"], "device-1")
        self.assertEqual(seen[0]["device_id"], "device-1")
        self.assertEqual(data["something_else"], "kept")
        self.assertEqual((data["country"], data["area_code"], data["profile"]), ("US", "US", "meowant"))

    def test_an_account_missing_a_box_is_refused_and_nothing_changes(self):
        flow, _ = _flow(devices_now=("SN1", "SN2"))

        result, _ = _submit(flow, devices=({"serialNumber": "SN1"},))

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "different_boxes"})
        self.assertFalse(hasattr(flow, "updated"))

    def test_an_account_another_entry_uses_is_refused(self):
        flow, _ = _flow()
        owner_id = config_flow.account_unique_id(config_flow.get_profile("meowant").app_id, "owner@example.com")
        flow.current_entries.append(types.SimpleNamespace(entry_id="other", unique_id=owner_id))

        result, _ = _submit(flow)

        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})
        self.assertFalse(hasattr(flow, "updated"))

    def test_the_same_account_with_a_new_password_is_fine(self):
        flow, _ = _flow()

        result, _ = _submit(flow, user_input={"country": "US", "username": "shared@example.com", "password": "new"})

        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(flow.updated[1]["data"]["password"], "new")

    def test_a_refused_login_shows_auth_failed(self):
        flow, _ = _flow()

        result, _ = _submit(flow, error=config_flow.UbpetAuthError(200, {"code": 2002}))

        self.assertEqual(result["errors"], {"base": "auth_failed"})
        self.assertFalse(hasattr(flow, "updated"))

    def test_an_unreachable_server_shows_cannot_connect(self):
        flow, _ = _flow()

        result, _ = _submit(flow, error=OSError("timed out"))

        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    def test_a_missing_country_asks_again(self):
        flow, _ = _flow()

        result, seen = _submit(flow, user_input={"country": "", "username": "owner@example.com", "password": "x"})

        self.assertEqual(result["errors"], {"base": "missing_required"})
        self.assertEqual(seen, [])

    def test_with_the_integration_not_loaded_there_are_no_boxes_to_compare(self):
        flow, _ = _flow()
        flow.hass.data["ubpet"] = {}

        result, _ = _submit(flow, devices=())

        self.assertEqual(result["reason"], "reconfigure_successful")


if __name__ == "__main__":
    unittest.main()
