from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
spec = importlib.util.spec_from_file_location("ubpet_box_config_test", COMPONENT_PATH / "box_config.py")
box_config = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(box_config)

REMINDER = {"type": 4, "notifyConfigId": 7, "period": 127}


class HardwareTests(unittest.TestCase):
    """Ported from bojanrajkovic/upet-hass tests/test_registry.py (ad084ff), keeping entities whenever a part didn't load."""

    def test_camera_follows_the_cat_face_capability(self):
        self.assertTrue(box_config.has_camera({"capabilities": {"capabilities": [{"code": "cat_face_upload", "enabled": True}]}}))
        self.assertFalse(box_config.has_camera({"capabilities": {"capabilities": [{"code": "something_else"}]}}))
        self.assertFalse(box_config.has_camera({"capabilities": {"capabilities": []}}))

    def test_capabilities_that_failed_or_look_different_keep_the_camera(self):
        for capabilities in (None, {}, {"capabilities": None}, {"capabilities": "cat_face_upload"}, ["cat_face_upload"]):
            with self.subTest(capabilities=capabilities):
                self.assertTrue(box_config.has_camera({"capabilities": capabilities}))
        self.assertTrue(box_config.has_camera({}))

    def test_light_needs_a_light_switch_in_a_loaded_config(self):
        self.assertTrue(box_config.has_light({"config": {"lightSwitch": 0}}))
        self.assertFalse(box_config.has_light({"config": {"sensorSwitch": 1}}))
        # A config that failed to load at startup (empty, or not there) keeps it.
        self.assertTrue(box_config.has_light({"config": {}}))
        self.assertTrue(box_config.has_light({"config": None}))

    def test_reminder_needs_a_reminder_in_a_loaded_config(self):
        self.assertTrue(box_config.has_waste_bin_reminder({"config": {"notifyConfigList": [REMINDER]}}))
        # Boxes without the reminder still list a type-4 entry, but with no notifyConfigId.
        self.assertFalse(box_config.has_waste_bin_reminder({"config": {"notifyConfigList": [{"type": 4}]}}))
        self.assertFalse(box_config.has_waste_bin_reminder({"config": {"notifyConfigList": None, "sensorSwitch": 1}}))
        self.assertTrue(box_config.has_waste_bin_reminder({"config": {}}))
        self.assertTrue(box_config.has_waste_bin_reminder({}))


if __name__ == "__main__":
    unittest.main()
