from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
spec = importlib.util.spec_from_file_location("ubpet_bin_count_test", COMPONENT_PATH / "bin_count.py")
bin_count = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bin_count)

EMPTIED = 1_000_000


def _record(event_time: int, event_type: int = 31) -> dict:
    return {"recordId": event_time, "eventTime": event_time, "eventType": event_type}


class CleansAfterTests(unittest.TestCase):
    def test_counts_finished_auto_cleans_after_the_empty(self):
        records = [_record(EMPTIED + 3), _record(EMPTIED + 2, event_type=12), _record(EMPTIED + 1), _record(EMPTIED), _record(EMPTIED - 1)]

        self.assertEqual(bin_count.cleans_after(records, EMPTIED), 2)

    def test_odd_records_are_skipped(self):
        records = [None, "x", {"eventType": 31}, {"eventType": 31, "eventTime": "later"}, _record(EMPTIED + 1)]

        self.assertEqual(bin_count.cleans_after(records, EMPTIED), 1)
        self.assertEqual(bin_count.cleans_after(None, EMPTIED), 0)


class RecountTests(unittest.TestCase):
    def test_counts_when_the_fetch_reaches_back_past_the_empty(self):
        records = [_record(EMPTIED + 2), _record(EMPTIED + 1), _record(EMPTIED - 5)]

        self.assertEqual(bin_count.recount(records, EMPTIED, page_size=3), 2)

    def test_unknown_when_a_full_fetch_starts_after_the_empty(self):
        # Earlier cleans may be beyond the newest `page_size` records.
        records = [_record(EMPTIED + 3), _record(EMPTIED + 2), _record(EMPTIED + 1)]

        self.assertIsNone(bin_count.recount(records, EMPTIED, page_size=3))

    def test_a_short_fetch_is_the_box_whole_history(self):
        records = [_record(EMPTIED + 2), _record(EMPTIED + 1)]

        self.assertEqual(bin_count.recount(records, EMPTIED, page_size=50), 2)

    def test_unknown_when_the_records_did_not_load(self):
        for records in (None, [], [{"eventType": 31}]):
            with self.subTest(records=records):
                self.assertIsNone(bin_count.recount(records, EMPTIED, page_size=50))


if __name__ == "__main__":
    unittest.main()
