from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"ubpet_{name}_test", COMPONENT_PATH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


visits = _load("visits")
registry = _load("registry")


def _record(record_id: int, **overrides):
    return {
        "recordId": record_id,
        "eventTime": 1792356940000,
        "eventType": 3,
        "catInfoId": 156392,
        "catNickname": "Hilda",
        "weight": 3.3,
        "costTime": 59,
        **overrides,
    }


class NewVisitsTests(unittest.TestCase):
    """Ported from bojanrajkovic/upet-hass tests/test_visits.py."""

    def test_first_poll_seeds_cursor_without_visits(self):
        decision = visits.new_visits([_record(3), _record(2)], last_record_id=None)

        self.assertEqual(decision, visits.VisitBatch(visits=[], last_record_id=3))

    def test_returns_only_newer_records_oldest_first(self):
        # The API returns newest first.
        decision = visits.new_visits([_record(5), _record(4), _record(3)], last_record_id=3)

        self.assertEqual([r["recordId"] for r in decision.visits], [4, 5])
        self.assertEqual(decision.last_record_id, 5)

    def test_no_records_keeps_cursor(self):
        self.assertEqual(visits.new_visits([], last_record_id=7), visits.VisitBatch(visits=[], last_record_id=7))

    def test_empty_first_poll_starts_from_zero(self):
        # A freshly paired box has no records yet; its first record must not be swallowed as history.
        self.assertEqual(visits.new_visits([], last_record_id=None), visits.VisitBatch(visits=[], last_record_id=0))
        self.assertEqual([r["recordId"] for r in visits.new_visits([_record(5)], last_record_id=0).visits], [5])

    def test_failed_fetch_keeps_cursor(self):
        self.assertEqual(visits.new_visits(None, last_record_id=None), visits.VisitBatch(visits=[], last_record_id=None))
        self.assertEqual(visits.new_visits(None, last_record_id=7), visits.VisitBatch(visits=[], last_record_id=7))

    def test_records_without_an_id_are_skipped(self):
        decision = visits.new_visits([_record(5), {"recordId": None}, "not a record"], last_record_id=4)

        self.assertEqual([r["recordId"] for r in decision.visits], [5])


class VisitEventDataTests(unittest.TestCase):
    def test_identified_cat(self):
        self.assertEqual(
            visits.visit_event_data("SN1", "Left Litter Box", _record(9)),
            {
                "serial_number": "SN1",
                "box": "Left Litter Box",
                "record_id": 9,
                "cat_id": 156392,
                "cat": "Hilda",
                "weight_kg": 3.3,
                "duration_s": 59,
                "event_type": 3,
                "time": "2026-10-18T20:55:40+00:00",
            },
        )

    def test_unmatched_visit_has_no_cat_id(self):
        data = visits.visit_event_data("SN1", "Right", _record(9, eventType=21, catInfoId=0, catNickname=None))

        self.assertIsNone(data["cat_id"])
        self.assertIsNone(data["cat"])


class StillReportedTests(unittest.TestCase):
    """Ported from bojanrajkovic/upet-hass tests/test_registry.py."""

    DATA = {"devices": {"SN1": {}}, "cats": [{"catInfoId": 529029}]}

    def test_current_box_and_cat_are_reported(self):
        self.assertTrue(registry.still_reported({("ubpet", "SN1")}, self.DATA))
        self.assertTrue(registry.still_reported({("ubpet", "cat_529029")}, self.DATA))

    def test_cat_from_before_a_re_pair_is_not(self):
        self.assertFalse(registry.still_reported({("ubpet", "cat_156382")}, self.DATA))

    def test_other_domains_are_ignored(self):
        self.assertFalse(registry.still_reported({("other", "SN1")}, self.DATA))


if __name__ == "__main__":
    unittest.main()
