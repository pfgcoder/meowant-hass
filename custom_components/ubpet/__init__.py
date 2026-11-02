from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .api import DEFAULT_RECORDS_PER_FETCH, UbpetClient
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
    DEFAULT_APP_ID,
    DEFAULT_APP_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_PRODUCT,
    DOMAIN,
    PLATFORMS,
    base_url_for_country,
)
from .coordinator import UbpetDataUpdateCoordinator
from .profiles import PROFILE_UPET, get_profile
from .push import UbpetPush
from .registry import still_reported

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = UbpetDataUpdateCoordinator(hass, entry, client_from_entry_data(entry.data, entry.options))
    coordinator.push = UbpetPush(hass, coordinator)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_apply_options))

    # The persistent MQTT connection (bojanrajkovic/upet-hass 37ebe31). Started in the background,
    # so a slow or unreachable cloud never holds up setup; until it connects, the REST update
    # still runs and the box's command buttons wait.
    entry.async_create_background_task(hass, coordinator.push.async_start(), "ubpet mqtt push")

    async def _stop_push(_event) -> None:
        await coordinator.push.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_push))
    return True


async def _async_apply_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Use a new records per fetch from the next update on; nothing else needs a reload."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    records_per_fetch = _records_per_fetch(entry.options)
    if coordinator is None or coordinator.client.records_per_fetch == records_per_fetch:
        return
    coordinator.client.records_per_fetch = records_per_fetch
    await coordinator.async_request_refresh()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 1:
        # Made by a newer version of the integration, which this one can't read.
        return False
    if entry.minor_version < 2:
        # Lower-case the account in the unique ID, as new entries have it, so the same account
        # typed with other capitals can't be added again.
        unique_id = _normalised_unique_id(entry.unique_id)
        if unique_id != entry.unique_id and any(
            other.unique_id == unique_id
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
        ):
            _LOGGER.warning("Another Meowant entry is for the same account as %s; leaving its ID as it is", entry.title)
            unique_id = entry.unique_id
        hass.config_entries.async_update_entry(entry, unique_id=unique_id, minor_version=2)
    return True


def _normalised_unique_id(unique_id: str | None) -> str | None:
    """The unique ID (app ID, colon, account) with the account stripped and lower-cased."""
    app_id, separator, account = (unique_id or "").partition(":")
    if not separator:
        return unique_id
    return f"{app_id}:{account.strip().lower()}"


def _records_per_fetch(options: Mapping[str, Any]) -> int:
    return int(options.get(CONF_RECORDS_PER_FETCH, DEFAULT_RECORDS_PER_FETCH))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None and coordinator.push is not None:
            await coordinator.push.async_stop()
    return unload_ok


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry) -> bool:
    """Let a box or cat the account no longer reports be deleted, such as one from before a re-pair.

    Ported from bojanrajkovic/upet-hass (ed43881). Only after an update that loaded every part:
    a cat list that failed to load would otherwise make every cat look gone.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None or not coordinator.last_update_success or not coordinator.every_part_loaded:
        return False
    return not still_reported(device.identifiers, coordinator.data or {})


def client_from_entry_data(data: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> UbpetClient:
    """Build the API client a config entry describes; the reauth step uses it to check a new password."""
    profile_id = data.get(CONF_PROFILE)
    profile = get_profile(profile_id) if profile_id else None
    country = data.get(CONF_COUNTRY, "")
    profile_base_url = (
        base_url_for_country(country)
        if profile and profile.profile_id == PROFILE_UPET and country
        else profile.base_url if profile else DEFAULT_BASE_URL
    )
    app_key = data.get(CONF_APP_KEY) or (profile.app_key if profile else DEFAULT_APP_KEY)
    app_id = data.get(CONF_APP_ID) or (profile.app_id if profile else DEFAULT_APP_ID)
    base_url = data.get(CONF_BASE_URL) or profile_base_url
    product = data.get(CONF_PRODUCT) or (profile.product if profile else DEFAULT_PRODUCT)
    missing = [
        field
        for field, value in (
            (CONF_APP_KEY, app_key),
            (CONF_APP_ID, app_id),
            (CONF_BASE_URL, base_url),
            (CONF_PRODUCT, product),
        )
        if not value
    ]
    if missing:
        _LOGGER.error("UPET setup has missing API field(s): %s", ", ".join(missing))

    return UbpetClient(
        account=data["account"],
        password=data["password"],
        app_key=app_key,
        device_id=data[CONF_DEVICE_ID],
        app_id=app_id,
        base_url=base_url,
        product=product,
        area_code=data.get(CONF_AREA_CODE, country),
        records_per_fetch=_records_per_fetch(options or {}),
    )
