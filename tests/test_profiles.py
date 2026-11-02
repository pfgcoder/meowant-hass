from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PACKAGE_NAME = "ubpet_profiles_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(COMPONENT_PATH)]

spec = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.profiles",
    COMPONENT_PATH / "profiles.py",
)
profiles = importlib.util.module_from_spec(spec)
assert spec.loader is not None
# The package is only needed while profiles.py loads (for its import of secrets.py).
with patch.dict(sys.modules, {PACKAGE_NAME: package}):
    sys.modules[spec.name] = profiles
    spec.loader.exec_module(profiles)


class AccountProfileTests(unittest.TestCase):
    def test_bundled_profiles_have_distinct_app_ids_and_keys(self):
        upet = profiles.get_profile(profiles.PROFILE_UPET)
        meowant = profiles.get_profile(profiles.PROFILE_MEOWANT)

        self.assertEqual(upet.app_id, "970010026")
        self.assertEqual(meowant.app_id, "970010016")
        self.assertEqual(meowant.base_url, "https://apis.airrobo-home.com")
        self.assertEqual(upet.base_url, "https://apis-eu.airrobo-home.com")
        self.assertEqual(meowant.product, "97001")
        self.assertEqual(len(meowant.app_key), 32)
        self.assertNotEqual(meowant.app_key, upet.app_key)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown account profile"):
            profiles.get_profile("not-a-profile")

    def test_legacy_meowant_profile_id_is_supported(self):
        legacy = profiles.get_profile(profiles.LEGACY_PROFILE_MEOWANT)

        self.assertEqual(legacy.profile_id, profiles.PROFILE_MEOWANT)

    def test_meowant_sends_the_phone_app_country_on_its_own_host(self):
        self.assertTrue(profiles.requires_country(profiles.PROFILE_MEOWANT))
        self.assertEqual(profiles.area_code_for(profiles.PROFILE_MEOWANT, "us"), "US")
        self.assertTrue(profiles.uses_fixed_host(profiles.PROFILE_MEOWANT))
        self.assertFalse(profiles.uses_fixed_host(profiles.PROFILE_UPET))
        self.assertEqual(profiles.DEFAULT_PROFILE, profiles.PROFILE_MEOWANT)


class AccountChangeTests(unittest.TestCase):
    def test_the_same_account_typed_differently_has_the_same_unique_id(self):
        self.assertEqual(
            profiles.account_unique_id(" app1 ", " Someone@Example.com "),
            profiles.account_unique_id("app1", "someone@example.com"),
        )

    def test_another_account_has_another_unique_id(self):
        self.assertNotEqual(
            profiles.account_unique_id("app1", "owner@example.com"),
            profiles.account_unique_id("app1", "shared@example.com"),
        )

    def test_an_account_that_sees_every_box_misses_none(self):
        devices = [{"serialNumber": "SN1"}, {"serialNumber": "SN2"}]

        self.assertEqual(profiles.missing_boxes({"SN1"}, devices), [])

    def test_boxes_the_new_account_doesnt_see_are_listed(self):
        devices = [{"serialNumber": "SN2"}, "not a device"]

        self.assertEqual(profiles.missing_boxes({"SN3", "SN1", "SN2"}, devices), ["SN1", "SN3"])

    def test_nothing_to_compare_misses_nothing(self):
        self.assertEqual(profiles.missing_boxes(set(), []), [])


if __name__ == "__main__":
    unittest.main()
