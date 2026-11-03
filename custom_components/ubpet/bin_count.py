"""Cleans since the waste bin was emptied, kept free of Home Assistant imports so tests can load them.

The box's own waste-bin counter (Box use times) stays at 0 on the SC03, so the integration keeps a
count of its own: the box's finished auto cleans after the time the bin was emptied. Every clean
rakes waste into the bin, so the count shows how full it's getting.
"""

from __future__ import annotations

from typing import Any

# A device record for a finished auto clean, listed as "Auto cleaned" in the app. Cleans started
# from the app or Home Assistant leave no such record.
AUTO_CLEANED_EVENT = 31


def cleans_after(records: list[dict[str, Any]] | None, emptied_at_ms: int) -> int:
    """Count the finished auto cleans among `records` that happened after the bin was emptied.

    By the record's own time, so a clean the box uploads late after an outage counts only if it
    happened after the empty.
    """
    return sum(
        1
        for record in records or []
        if isinstance(record, dict)
        and record.get("eventType") == AUTO_CLEANED_EVENT
        and isinstance(record.get("eventTime"), int)
        and record["eventTime"] > emptied_at_ms
    )


def recount(records: list[dict[str, Any]] | None, emptied_at_ms: int, page_size: int) -> int | None:
    """Count from scratch in the fetched device records, or None when they can't show every clean.

    None when the records didn't load (a box always has some), or when the fetch came back full
    and its oldest record is still after the empty, so earlier cleans may be beyond it.
    """
    if not records:
        return None
    times = [
        record["eventTime"]
        for record in records
        if isinstance(record, dict) and isinstance(record.get("eventTime"), int)
    ]
    if not times:
        return None
    if len(records) >= page_size and min(times) > emptied_at_ms:
        return None
    return cleans_after(records, emptied_at_ms)
