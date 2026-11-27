"""Hand-built TripUpdates snapshots, written where and how the logger writes them:
RAW/trip_updates/<UTC day>/<YYYYMMDDTHHMMSSZ>.pb.gz, gzipped."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2 as pb

TZ = ZoneInfo("America/Toronto")
_TRIP_REL = pb.TripDescriptor.ScheduleRelationship
_STOP_REL = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship


def local(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    """A Toronto wall-clock time, as an aware UTC datetime."""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ).astimezone(UTC)


def stu(seq: int, stop_id: str, at: datetime | None, *, rel: str | None = None,
        arrival: bool = True) -> dict:
    """A stop update. arrival=False puts the time in departure only."""
    return {"seq": seq, "stop_id": stop_id, "at": at, "rel": rel, "arrival": arrival}


def trip(trip_id: str, start_date: str, *stops: dict, rel: str | None = None,
         vehicle: str | None = "V1") -> dict:
    return {"trip_id": trip_id, "start_date": start_date, "stops": stops, "rel": rel,
            "vehicle": vehicle}


def feed_bytes(trips: list[dict], header: datetime | None, deleted: int = 0) -> bytes:
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    if header is not None:
        msg.header.timestamp = int(header.timestamp())
    for i in range(deleted):
        e = msg.entity.add()
        e.id, e.is_deleted = f"deleted{i}", True
        e.trip_update.trip.trip_id = f"deleted{i}"
    for i, t in enumerate(trips):
        e = msg.entity.add()
        e.id = str(i)
        tu = e.trip_update
        tu.trip.trip_id, tu.trip.start_date = t["trip_id"], t["start_date"]
        if t["rel"]:
            tu.trip.schedule_relationship = _TRIP_REL.Value(t["rel"])
        if t["vehicle"]:
            tu.vehicle.id = t["vehicle"]
        for s in t["stops"]:
            u = tu.stop_time_update.add()
            u.stop_sequence, u.stop_id = s["seq"], s["stop_id"]
            if s["at"] is not None:
                (u.arrival if s["arrival"] else u.departure).time = int(s["at"].timestamp())
            if s["rel"]:
                u.schedule_relationship = _STOP_REL.Value(s["rel"])
    return gzip.compress(msg.SerializeToString())


def write_snapshot(raw: Path, fetched: datetime, trips: list[dict], *,
                   header: datetime | None | str = "auto", suffix: str = "",
                   deleted: int = 0) -> Path:
    """A snapshot fetched at `fetched`. The feed's own timestamp is 20 s older by default,
    as DRT's normally is; header=None leaves it unset."""
    if header == "auto":
        header = fetched - timedelta(seconds=20)
    at = fetched.astimezone(UTC)
    path = raw / "trip_updates" / at.strftime("%Y-%m-%d") / \
        f"{at.strftime('%Y%m%dT%H%M%SZ')}{suffix}.pb.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(feed_bytes(trips, header, deleted))
    return path
