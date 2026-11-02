from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import client_from_entry_data
from .api import DEFAULT_RECORDS_PER_FETCH, UbpetApiError, UbpetAuthError, UbpetClient
from .const import (
    CONF_AREA_CODE,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PRODUCT,
    CONF_PROFILE,
    CONF_RECORDS_PER_FETCH,
    DOMAIN,
    SUPPORTED_COUNTRIES,
    base_url_for_country,
)
from .profiles import (
    ACCOUNT_PROFILES,
    AccountProfile,
    DEFAULT_PROFILE,
    PROFILE_MEOWANT,
    PROFILE_UPET,
    account_unique_id,
    area_code_for,
    get_profile,
    missing_boxes,
    requires_country,
    uses_fixed_host,
)

# Entry data that belongs to the account. Reconfigure replaces these and keeps anything else.
_ACCOUNT_KEYS = frozenset(
    {
        "account",
        "password",
        CONF_COUNTRY,
        CONF_AREA_CODE,
        CONF_APP_KEY,
        CONF_APP_ID,
        CONF_BASE_URL,
        CONF_PRODUCT,
        CONF_DEVICE_ID,
        CONF_PROFILE,
    }
)


def _profile_schema(default: str | None = None) -> vol.Schema:
    ordered = (PROFILE_MEOWANT, PROFILE_UPET)
    return vol.Schema(
        {
            vol.Required(CONF_PROFILE, default=default or DEFAULT_PROFILE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=profile_id, label=ACCOUNT_PROFILES[profile_id].label)
                        for profile_id in ordered
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _credential_fields(defaults: dict[str, Any], profile: AccountProfile) -> dict[Any, Any]:
    fields: dict[Any, Any] = {
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
    # Ask only for what the chosen app's bundled values lack.
    if not profile.app_key:
        fields[vol.Required(CONF_APP_KEY, default=defaults.get(CONF_APP_KEY, ""))] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    if not profile.app_id:
        fields[vol.Required(CONF_APP_ID, default=defaults.get(CONF_APP_ID, ""))] = str
    if not profile.product:
        fields[vol.Required(CONF_PRODUCT, default=defaults.get(CONF_PRODUCT, ""))] = str
    return fields


def _account_schema(
    defaults: dict[str, Any] | None = None,
    *,
    profile: AccountProfile,
    allowed_countries: tuple[str, ...] | None,
    default_country: str | None = None,
) -> vol.Schema:
    defaults = defaults or {}
    selected_country = _iso_country(defaults.get(CONF_COUNTRY, default_country))
    if allowed_countries is not None and selected_country not in allowed_countries:
        selected_country = None
    country_field = (
        vol.Required(CONF_COUNTRY, default=selected_country)
        if selected_country
        else vol.Required(CONF_COUNTRY)
    )
    selector_config = (
        selector.CountrySelectorConfig()
        if allowed_countries is None
        else selector.CountrySelectorConfig(countries=list(allowed_countries))
    )
    return vol.Schema(
        {
            country_field: selector.CountrySelector(selector_config),
            **_credential_fields(defaults, profile),
        }
    )


def _iso_country(value: Any) -> str | None:
    country = value.strip().upper() if isinstance(value, str) else ""
    if len(country) == 2 and country.isalpha():
        return country
    return None


class UbpetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    # 1.2: the account in the unique ID is lower-cased (async_migrate_entry).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._profile_id: str | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                profile = get_profile(_clean_required_string(user_input.get(CONF_PROFILE)))
            except ValueError:
                errors["base"] = "missing_required"
            else:
                self._profile_id = profile.profile_id
                if uses_fixed_host(profile.profile_id):
                    return await self.async_step_meowant()
                return await self.async_step_upet()

        return self.async_show_form(
            step_id="user",
            data_schema=_profile_schema(),
            errors=errors,
        )

    async def async_step_meowant(self, user_input: dict[str, Any] | None = None):
        if self._profile_id is None:
            return await self.async_step_user()
        configured_country = getattr(getattr(self.hass, "config", None), "country", None)
        country = ""
        if user_input is not None:
            country = _iso_country(user_input.get(CONF_COUNTRY)) or ""
        return await self._async_account_step(
            step_id="meowant",
            schema=_account_schema(
                user_input,
                profile=get_profile(self._profile_id),
                allowed_countries=None,
                default_country=configured_country,
            ),
            user_input=user_input,
            country=country,
        )

    async def async_step_upet(self, user_input: dict[str, Any] | None = None):
        if self._profile_id is None:
            return await self.async_step_user()
        configured_country = getattr(getattr(self.hass, "config", None), "country", None)
        country = ""
        if user_input is not None:
            country = _iso_country(user_input.get(CONF_COUNTRY)) or ""
            if country not in SUPPORTED_COUNTRIES:
                country = ""
        return await self._async_account_step(
            step_id="upet",
            schema=_account_schema(
                user_input,
                profile=get_profile(self._profile_id),
                allowed_countries=SUPPORTED_COUNTRIES,
                default_country=configured_country,
            ),
            user_input=user_input,
            country=country,
        )

    async def _async_account_step(
        self,
        *,
        step_id: str,
        schema: vol.Schema,
        user_input: dict[str, Any] | None,
        country: str,
    ):
        errors: dict[str, str] = {}
        profile = get_profile(self._profile_id or "")
        if user_input is not None:
            if requires_country(profile.profile_id) and not country:
                errors["base"] = "missing_required"
            else:
                try:
                    connection_data = _connection_data(profile, user_input, country)
                    devices = await self.hass.async_add_executor_job(_validate_input, connection_data)
                except ValueError:
                    errors["base"] = "missing_required"
                except UbpetAuthError:
                    errors["base"] = "auth_failed"
                except (OSError, UbpetApiError):
                    # Includes a login that failed on the network or a busy server, as in reauth.
                    errors["base"] = "cannot_connect"
                except Exception:
                    errors["base"] = "unknown"
                else:
                    return await self._async_create_account_entry(profile, user_input, connection_data, devices, country)

        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
        )

    async def _async_create_account_entry(
        self,
        profile,
        user_input: dict[str, Any],
        connection_data: dict[str, Any],
        devices: list[dict[str, Any]],
        country: str,
    ):
        await self.async_set_unique_id(
            account_unique_id(
                _clean_required_string(connection_data[CONF_APP_ID]),
                _clean_required_string(user_input[CONF_USERNAME]),
            )
        )
        self._abort_if_unique_id_configured()
        title = profile.label
        if devices:
            title = devices[0].get("deviceName") or devices[0].get("serialNumber") or title
        return self.async_create_entry(title=title, data=_entry_data(profile, user_input, connection_data, country))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Ask for the account's current password after the stored one was refused."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                password = _clean_required_string(user_input.get(CONF_PASSWORD))
                client = client_from_entry_data({**entry.data, "password": password})
                await self.hass.async_add_executor_job(client.login)
            except ValueError:
                errors["base"] = "missing_required"
            except UbpetAuthError:
                errors["base"] = "auth_failed"
            except (OSError, UbpetApiError):
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self._async_save_and_reload(
                    entry, reason="reauth_successful", data={**entry.data, "password": password}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={"account": entry.data.get("account", "")},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Log in with another account, or a new password, without removing the integration.

        The entry, its entities and their history stay, so the new account has to see the same
        boxes. Not every setting changes from an account the box is shared with (the child lock
        is the owner's), so this is how to move between the owner's account and a shared one.
        """
        entry = self._get_reconfigure_entry()
        profile = get_profile(entry.data.get(CONF_PROFILE) or DEFAULT_PROFILE)
        fixed_host = uses_fixed_host(profile.profile_id)
        errors: dict[str, str] = {}
        if user_input is not None:
            country = _iso_country(user_input.get(CONF_COUNTRY)) or ""
            if not fixed_host and country not in SUPPORTED_COUNTRIES:
                country = ""
            if requires_country(profile.profile_id) and not country:
                errors["base"] = "missing_required"
            else:
                try:
                    connection_data = {
                        **_connection_data(profile, user_input, country),
                        # Still the same Home Assistant, so keep its device ID.
                        CONF_DEVICE_ID: entry.data.get(CONF_DEVICE_ID) or uuid.uuid4().hex,
                    }
                    devices = await self.hass.async_add_executor_job(_validate_input, connection_data)
                    unique_id = account_unique_id(
                        _clean_required_string(connection_data[CONF_APP_ID]),
                        _clean_required_string(user_input[CONF_USERNAME]),
                    )
                except ValueError:
                    errors["base"] = "missing_required"
                except UbpetAuthError:
                    errors["base"] = "auth_failed"
                except (OSError, UbpetApiError):
                    errors["base"] = "cannot_connect"
                except Exception:
                    errors["base"] = "unknown"
                else:
                    if any(
                        other.entry_id != entry.entry_id and other.unique_id == unique_id
                        for other in self._async_current_entries(include_ignore=False)
                    ):
                        return self.async_abort(reason="already_configured")
                    if missing_boxes(_current_serials(self.hass, entry), devices):
                        # Their entities would go unavailable, so keep the account that sees them.
                        errors["base"] = "different_boxes"
                    else:
                        kept = {key: value for key, value in entry.data.items() if key not in _ACCOUNT_KEYS}
                        return self._async_save_and_reload(
                            entry,
                            reason="reconfigure_successful",
                            unique_id=unique_id,
                            data={**kept, **_entry_data(profile, user_input, connection_data, country)},
                        )

        defaults = user_input or {
            CONF_USERNAME: entry.data.get("account", ""),
            CONF_COUNTRY: entry.data.get(CONF_COUNTRY),
            **{key: entry.data[key] for key in (CONF_APP_KEY, CONF_APP_ID, CONF_PRODUCT) if key in entry.data},
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_account_schema(
                defaults,
                profile=profile,
                allowed_countries=None if fixed_host else SUPPORTED_COUNTRIES,
                default_country=entry.data.get(CONF_COUNTRY),
            ),
            description_placeholders={"account": entry.data.get("account", "")},
            errors=errors,
        )

    def _async_save_and_reload(self, entry, *, reason: str, **changes: Any):
        """Save the new login and reload the entry so it logs in with it.

        async_update_reload_and_abort leaves the reload to an entry's update listener from
        HA 2026.12, and this entry's listener only applies options without a reload. So save
        the change, then schedule the reload here. The reload runs even if nothing changed,
        as after a refused login the entry has to try again.
        """
        self.hass.config_entries.async_update_entry(entry, **changes)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UbpetOptionsFlow()


class UbpetOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_RECORDS_PER_FETCH: int(user_input[CONF_RECORDS_PER_FETCH])},
            )
        records_per_fetch = self.config_entry.options.get(CONF_RECORDS_PER_FETCH, DEFAULT_RECORDS_PER_FETCH)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RECORDS_PER_FETCH, default=records_per_fetch): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=10, max=500, step=10, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
        )


def _connection_data(profile, user_input: dict[str, Any], country: str) -> dict[str, Any]:
    return {
        **user_input,
        CONF_COUNTRY: country,
        CONF_APP_KEY: profile.app_key or user_input.get(CONF_APP_KEY),
        CONF_APP_ID: profile.app_id or user_input.get(CONF_APP_ID),
        CONF_AREA_CODE: area_code_for(profile.profile_id, country),
        CONF_BASE_URL: (
            profile.base_url or user_input.get(CONF_BASE_URL)
            if uses_fixed_host(profile.profile_id)
            else base_url_for_country(country)
        ),
        CONF_PRODUCT: profile.product or user_input.get(CONF_PRODUCT),
        CONF_DEVICE_ID: uuid.uuid4().hex,
    }


def _entry_data(profile, user_input: dict[str, Any], connection_data: dict[str, Any], country: str) -> dict[str, Any]:
    entry_data = {
        "account": _clean_required_string(user_input[CONF_USERNAME]),
        "password": _clean_required_string(user_input[CONF_PASSWORD]),
        CONF_DEVICE_ID: connection_data[CONF_DEVICE_ID],
        CONF_PROFILE: profile.profile_id,
    }
    if country:
        entry_data[CONF_COUNTRY] = country
        entry_data[CONF_AREA_CODE] = country
    if not all((profile.app_key, profile.app_id, profile.base_url, profile.product)):
        entry_data.update(
            {
                CONF_APP_KEY: _clean_required_string(connection_data[CONF_APP_KEY]),
                CONF_APP_ID: _clean_required_string(connection_data[CONF_APP_ID]),
                CONF_BASE_URL: _clean_required_string(connection_data[CONF_BASE_URL]),
                CONF_PRODUCT: _clean_required_string(connection_data[CONF_PRODUCT]),
            }
        )
    return entry_data


def _current_serials(hass, entry) -> set[str]:
    """The boxes the entry has now; none when it isn't loaded, so there's nothing to compare."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    data = getattr(coordinator, "data", None)
    devices = data.get("devices") if isinstance(data, dict) else None
    return set(devices) if isinstance(devices, dict) else set()


def _validate_input(data: dict[str, Any]) -> list[dict[str, Any]]:
    account = _clean_required_string(data.get(CONF_USERNAME))
    password = _clean_required_string(data.get(CONF_PASSWORD))
    app_key = _clean_required_string(data.get(CONF_APP_KEY))
    app_id = _clean_required_string(data.get(CONF_APP_ID))
    base_url = _clean_required_string(data.get(CONF_BASE_URL))
    product = _clean_required_string(data.get(CONF_PRODUCT))
    client = UbpetClient(
        account=account,
        password=password,
        app_key=app_key,
        device_id=data.get(CONF_DEVICE_ID, uuid.uuid4().hex),
        app_id=app_id,
        base_url=base_url,
        product=product,
        area_code=_clean_optional_string(data.get(CONF_AREA_CODE)),
    )
    client.login()
    return client.get_devices()


def _clean_required_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required value is empty")
    return value.strip()


def _clean_optional_string(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""
