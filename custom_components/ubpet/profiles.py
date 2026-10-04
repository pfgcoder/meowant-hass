from __future__ import annotations

from dataclasses import dataclass

try:
    from . import secrets as _secrets
except ImportError:
    _secrets = None


PROFILE_UPET = "upet_airrobo"
PROFILE_MEOWANT = "meowant"
LEGACY_PROFILE_MEOWANT = "air_pet_meowant"
DEFAULT_PROFILE = PROFILE_MEOWANT


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """Vendor identity used to select and sign against a cloud tenant."""

    profile_id: str
    label: str
    base_url: str
    app_id: str
    app_key: str
    product: str


def _secret(name: str) -> str:
    if _secrets is None:
        return ""
    return str(getattr(_secrets, name, ""))


ACCOUNT_PROFILES = {
    PROFILE_UPET: AccountProfile(
        profile_id=PROFILE_UPET,
        label="UPET / Airrobo",
        base_url=_secret("BASE_URL"),
        app_id=_secret("APP_ID"),
        app_key=_secret("APP_KEY"),
        product=_secret("PRODUCT"),
    ),
    PROFILE_MEOWANT: AccountProfile(
        profile_id=PROFILE_MEOWANT,
        label="Meowant",
        base_url=_secret("MEOWANT_BASE_URL"),
        app_id=_secret("MEOWANT_APP_ID"),
        app_key=_secret("MEOWANT_APP_KEY"),
        product=_secret("MEOWANT_PRODUCT"),
    ),
}


def get_profile(profile_id: str) -> AccountProfile:
    """Return a configured account profile or reject an unknown identifier."""

    if profile_id == LEGACY_PROFILE_MEOWANT:
        profile_id = PROFILE_MEOWANT
    try:
        return ACCOUNT_PROFILES[profile_id]
    except KeyError as err:
        raise ValueError(f"unknown account profile: {profile_id}") from err


def requires_country(profile_id: str) -> bool:
    """Both apps ask for the country chosen when the account was created."""

    get_profile(profile_id)
    return True


def area_code_for(profile_id: str, country: str) -> str:
    """Return the ISO country sent as the login area code."""

    get_profile(profile_id)
    code = country.strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError("country is required")
    return code


def uses_fixed_host(profile_id: str) -> bool:
    """Meowant stays on its own host. UPET picks a host from the country."""

    return get_profile(profile_id).profile_id != PROFILE_UPET
