from __future__ import annotations

from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import UbpetApiError, UbpetClient
from .const import (
    CONF_AREA_CODE,
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_BASE_URL,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_PRODUCT,
    CONF_PROFILE,
    DEFAULT_APP_ID,
    DEFAULT_APP_KEY,
    DEFAULT_PRODUCT,
    DOMAIN,
    SUPPORTED_COUNTRIES,
    base_url_for_country,
)
from .profiles import (
    ACCOUNT_PROFILES,
    DEFAULT_PROFILE,
    PROFILE_MEOWANT,
    PROFILE_UPET,
    area_code_for,
    get_profile,
    requires_country,
    uses_fixed_host,
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


def _credential_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    fields: dict[Any, Any] = {
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
    if not DEFAULT_APP_KEY:
        fields[vol.Required(CONF_APP_KEY, default=defaults.get(CONF_APP_KEY, ""))] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    if not DEFAULT_APP_ID:
        fields[vol.Required(CONF_APP_ID, default=defaults.get(CONF_APP_ID, ""))] = str
    if not DEFAULT_PRODUCT:
        fields[vol.Required(CONF_PRODUCT, default=defaults.get(CONF_PRODUCT, ""))] = str
    return fields


def _account_schema(
    defaults: dict[str, Any] | None = None,
    *,
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
            **_credential_fields(defaults),
        }
    )


def _iso_country(value: Any) -> str | None:
    country = value.strip().upper() if isinstance(value, str) else ""
    if len(country) == 2 and country.isalpha():
        return country
    return None


class UbpetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
            schema=_account_schema(user_input, allowed_countries=None, default_country=configured_country),
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
                except UbpetApiError:
                    errors["base"] = "auth_failed"
                except OSError:
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
        app_id = _clean_required_string(connection_data[CONF_APP_ID])
        await self.async_set_unique_id(f"{app_id}:{user_input[CONF_USERNAME]}")
        self._abort_if_unique_id_configured()
        title = profile.label
        if devices:
            title = devices[0].get("deviceName") or devices[0].get("serialNumber") or title
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
                    CONF_APP_ID: app_id,
                    CONF_BASE_URL: _clean_required_string(connection_data[CONF_BASE_URL]),
                    CONF_PRODUCT: _clean_required_string(connection_data[CONF_PRODUCT]),
                }
            )
        return self.async_create_entry(title=title, data=entry_data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UbpetOptionsFlow(config_entry)


class UbpetOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_create_entry(title="", data={})


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
