"""The service dates a reliability answer covers: ?start=&end=&day_type=, resolved against
the rollups that exist."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException

from transit_api.rollups import DayType, RollupStore, Selection, Snapshot

DEFAULT_DAYS = 28  # four whole weeks, so every weekday counts the same


@dataclass
class WindowQuery:
    """start, end: service dates, inclusive. Default: the 28 days ending at the newest
    rollup. day_type: weekday or weekend; default every day."""
    start: date | None = None
    end: date | None = None
    day_type: DayType | None = None


WindowDep = Annotated[WindowQuery, Depends()]


def resolve(q: WindowQuery, store: RollupStore, snap: Snapshot, table: str,
            today: date) -> tuple[dict, Selection]:
    """The Window fields for the answer, and the files to read."""
    end = q.end or snap.latest(table) or today
    start = q.start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise HTTPException(422, "start is after end")
    sel = store.select(snap, table, start, end, q.day_type)
    return {"start": start, "end": end, "day_type": q.day_type, "dates": len(sel.dates),
            "excluded": [{"service_date": d, "coverage": c} for d, c in sel.excluded]}, sel


def group(rows: list[dict], key: str) -> dict:
    """{row[key]: [the rows, without key]}, in row order."""
    out: dict = {}
    for r in rows:
        out.setdefault(r.pop(key), []).append(r)
    return out
