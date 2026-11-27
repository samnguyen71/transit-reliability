"""Live TripUpdates: the logger's newest snapshot, parsed once and indexed by trip.

The logger (logger/feedlogger/collector.py) moves each new snapshot into
RAW_DIR/trip_updates/<UTC day>/<YYYYMMDDTHHMMSSZ>[_n].pb.gz with os.replace, so the
newest file is always whole; downloads in progress sit in trip_updates/_tmp/. A poll that
returns the same bytes as the last one isn't saved, so the newest file can be older than
the 30 s poll interval while the feed is current. That's why freshness is judged by the
feed's own header timestamp (status()), never by the file's age.

DRT's TripUpdates, as measured on 2026-11-05 (OBSERVED_TIMES.md has the scoring side):
- Absolute times only. arrival.time holds the departure, and departure.time is a worse
  copy of it, so a stop's time is arrival.time, or departure.time if that's all there is.
- A trip enters the feed 60 min before its first departure. A passed stop stays a median
  202 s (p90 583 s) after its time, frozen, so callers drop past times themselves.
- About 227 trips and 7,500 stop updates per snapshot, 64 KiB gzipped: parsing and
  indexing take under 10 ms, so a snapshot is parsed once per file, and nothing else is
  cached.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from google.transit import gtfs_realtime_pb2 as pb

log = logging.getLogger(__name__)

FEED = "trip_updates"
SUFFIX = ".pb.gz"
DAY_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")  # not _tmp, which sorts after every day

RealtimeState = Literal["ok", "stale", "empty", "missing"]

_TRIP_REL = pb.TripDescriptor.ScheduleRelationship
_STOP_REL = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship


@dataclass(frozen=True)
class RtStop:
    stop_id: str | None
    time: int | None  # POSIX seconds; None for NO_DATA, or an update with no time
    skipped: bool


@dataclass(frozen=True)
class RtTrip:
    relationship: str | None  # CANCELED, ADDED, ...; None for SCHEDULED or unset
    vehicle_id: str | None
    stops: dict[int, RtStop]  # by stop_sequence


@dataclass(frozen=True)
class Snapshot:
    """One TripUpdates snapshot. trips is keyed by (trip_id, start_date) exactly as the
    feed sends them: DRT's start_date is the local date the run starts, and before 03:00
    the overnight runs come under their +24 h copy's trip_id."""
    file: str | None = None
    feed_ts: datetime | None = None
    entities: int = 0
    trips: dict[tuple[str, str], RtTrip] = field(default_factory=dict)


def parse(data: bytes, name: str) -> Snapshot:
    """A snapshot from a .pb.gz file's bytes."""
    msg = pb.FeedMessage()
    msg.ParseFromString(gzip.decompress(data))
    trips: dict[tuple[str, str], RtTrip] = {}
    for e in msg.entity:
        if e.is_deleted or not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        if not tu.trip.trip_id:
            continue
        rel = None
        if tu.trip.HasField("schedule_relationship") and \
                tu.trip.schedule_relationship != _TRIP_REL.SCHEDULED:
            rel = _TRIP_REL.Name(tu.trip.schedule_relationship)
        stops = {}
        for u in tu.stop_time_update:
            if not u.HasField("stop_sequence"):
                continue  # DRT always sends one; without it there's nothing safe to match
            t = (u.arrival.time if u.HasField("arrival") and u.arrival.time else
                 u.departure.time if u.HasField("departure") and u.departure.time else None)
            stops[u.stop_sequence] = RtStop(
                stop_id=u.stop_id or None,
                time=None if u.schedule_relationship == _STOP_REL.NO_DATA else t,
                skipped=u.schedule_relationship == _STOP_REL.SKIPPED)
        vehicle = tu.vehicle.id if tu.HasField("vehicle") and tu.vehicle.id else None
        trips[(tu.trip.trip_id, tu.trip.start_date)] = RtTrip(rel, vehicle, stops)
    ts = msg.header.timestamp
    return Snapshot(file=name, feed_ts=datetime.fromtimestamp(ts, UTC) if ts else None,
                    entities=len(msg.entity), trips=trips)


def newest(folder: Path) -> Path | None:
    """The newest snapshot in folder (RAW_DIR/trip_updates): the last *.pb.gz by name in
    the newest day folder that has one. Just after 00:00 UTC the new day's folder is
    missing or empty, so the day before is looked at too."""
    try:
        days = sorted((e.name for e in os.scandir(folder)
                       if e.is_dir() and DAY_DIR.fullmatch(e.name)), reverse=True)
    except FileNotFoundError:
        return None
    for day in days[:2]:
        try:
            names = [e.name for e in os.scandir(folder / day)
                     if e.name.endswith(SUFFIX) and e.is_file()]
        except FileNotFoundError:
            continue
        if names:
            # Same-second files get _1, _2: "...Z.pb.gz" < "...Z_1.pb.gz", as wanted.
            return folder / day / max(names)
    return None


class LiveFeed:
    """The newest TripUpdates snapshot. snapshot() looks for a newer file at most every
    rescan_s seconds and parses a file only once. Safe to call from many threads."""

    def __init__(self, raw_dir: Path, *, rescan_s: float = 5,
                 clock: Callable[[], float] = time.monotonic):
        self.folder = raw_dir / FEED
        self.rescan_s, self.clock = rescan_s, clock
        self.parses = 0  # files parsed so far (tests check a file is parsed once)
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._scanned = float("-inf")

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = self.clock()
            if now - self._scanned >= self.rescan_s:
                self._scanned = now
                path = newest(self.folder)
                if path is None:
                    self._snap = Snapshot()
                elif path.name != self._snap.file:
                    try:
                        self._snap = parse(path.read_bytes(), path.name)
                        self.parses += 1
                    except Exception:  # keep the last good one; it goes stale on its own
                        log.exception("couldn't read %s", path)
            return self._snap


def status(snap: Snapshot, now: datetime, stale_s: float) -> dict:
    """The RealtimeStatus fields: ok only if the snapshot is fresh and has entities."""
    age = round((now - snap.feed_ts).total_seconds()) if snap.feed_ts else None
    if snap.file is None:
        state: RealtimeState = "missing"
    elif age is None or age > stale_s:
        state = "stale"
    elif snap.entities == 0:
        state = "empty"  # DRT serves valid empty feeds around its 03:00 changeover
    else:
        state = "ok"
    return {"status": state, "file": snap.file, "feed_timestamp": snap.feed_ts,
            "age_s": age, "trips": len(snap.trips)}
