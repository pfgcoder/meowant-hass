from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

from .profiles import UPET_BASE_URL

DOMAIN = "ubpet"
DEFAULT_SCAN_INTERVAL = timedelta(minutes=3)

try:
    from .secrets import (
        APP_ID as DEFAULT_APP_ID,
        APP_KEY as DEFAULT_APP_KEY,
        PRODUCT as DEFAULT_PRODUCT,
    )
except ImportError:
    DEFAULT_APP_ID = ""
    DEFAULT_APP_KEY = ""
    DEFAULT_PRODUCT = ""

CONF_APP_KEY = "app_key"
CONF_APP_ID = "app_id"
CONF_AREA_CODE = "area_code"
CONF_COUNTRY = "country"
CONF_DEVICE_ID = "device_id"
CONF_BASE_URL = "base_url"
CONF_PRODUCT = "product"
CONF_PROFILE = "profile"
# Option: how many of each box's latest visit and clean records each update fetches.
CONF_RECORDS_PER_FETCH = "records_per_fetch"

# Plain here and in profiles.py rather than in the secrets bundle, so a change to where logins go
# shows in a diff. The EU host is UPET's, defined once in profiles.py.
EU_BASE_URL = UPET_BASE_URL
RUSSIA_BASE_URL = "https://apis-ru.airrobo-home.com"
DEFAULT_BASE_URL = EU_BASE_URL

# Countries returned by the UPET 2.1.14 region map for the European app.
SUPPORTED_COUNTRIES = (
    "AL", "AD", "AT", "BY", "BH", "BE", "BA", "BG", "HR", "CY", "CZ",
    "DK", "EE", "FO", "FI", "FR", "GE", "DE", "GI", "GR", "HU", "IS",
    "IE", "IT", "KW", "XK", "LV", "LI", "LT", "LU", "MT", "MD", "MC",
    "ME", "NL", "MK", "NO", "PT", "PL", "QA", "RO", "RU", "SE", "CH",
    "SA", "SM", "RS", "SK", "SI", "ES", "TR", "UA", "GB", "AE", "VA",
)


def base_url_for_country(country: str) -> str:
    """Return the vendor API endpoint used by the mobile app for a country."""
    return RUSSIA_BASE_URL if country == "RU" else EU_BASE_URL


PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.TIME,
    Platform.SELECT,
    Platform.BUTTON,
]
