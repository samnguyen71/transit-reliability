"""A stop's departure board: scheduled departures (queries/departures.py) merged with the
live TripUpdates snapshot (live.py). Pure: no database, no files, no clock.

For each scheduled departure, the feed's trip is looked up under (trip_id or one of its
+24 h copies, start_date), and the departure gets one status:

    feed not usable (stale, empty, missing)     scheduled     (realtime says why)
    trip CANCELED                               cancelled
    this stop's update SKIPPED                  skipped
    stop update with a time, trip under way     live          predicted time
    stop update with a time, before trip start  not_started   predicted time
    trip in the feed, only later stops listed   (dropped: the bus has passed)
    trip not in the feed, starts > FEED_LEAD    scheduled     (trips enter 60 min ahead)
    otherwise                                   no_realtime

An update whose stop_id isn't the scheduled stop (a detour renumbering) is ignored.
Then anything whose time (predicted, else scheduled) is more than PAST_GRACE ago is
dropped: the feed keeps a passed stop's frozen time a median 202 s.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from transit_api.live import RtStop, RtTrip, Snapshot

LOOKBACK = timedelta(minutes=60)     # fetch departures scheduled this long ago: a bus this
                                     # late can still be coming (worst seen: 50 min)
EARLY_MARGIN = timedelta(minutes=10)  # and this far past the horizon, in case one's early
PAST_GRACE = timedelta(seconds=60)
FEED_LEAD = timedelta(minutes=55)    # a trip starting later than this isn't in the feed yet


def find_trip(row: dict, snap: Snapshot) -> RtTrip | None:
    for trip_id in row["rt_trip_ids"]:
        trip = snap.trips.get((trip_id, row["start_date"]))
        if trip is not None:
            return trip
    return None


def stop_update(row: dict, trip: RtTrip) -> RtStop | None:
    u = trip.stops.get(row["stop_sequence"])
    if u is not None and u.stop_id is not None and u.stop_id != row["stop_id"]:
        return None
    return u


def classify(row: dict, trip: RtTrip | None, use_realtime: bool,
             now: datetime) -> tuple[str, datetime | None] | None:
    """(status, predicted), or None if the departure is gone."""
    if not use_realtime:
        return "scheduled", None
    if trip is None:
        return ("scheduled" if row["trip_start"] > now + FEED_LEAD else "no_realtime"), None
    if trip.relationship == "CANCELED":
        return "cancelled", None
    u = stop_update(row, trip)
    if u is None:
        if trip.stops and min(trip.stops) > row["stop_sequence"]:
            return None  # only later stops are listed: this one has been served
        return "no_realtime", None
    if u.skipped:
        return "skipped", None
    if u.time is None:
        return "no_realtime", None
    predicted = datetime.fromtimestamp(u.time, now.tzinfo)
    return ("live" if now >= row["trip_start"] else "not_started"), predicted


def merge(rows: list[dict], snap: Snapshot, use_realtime: bool, now: datetime,
          horizon: timedelta) -> list[dict]:
    """The board: Departure dicts whose time is within now - PAST_GRACE .. now + horizon,
    soonest first."""
    out = []
    for row in rows:
        trip = find_trip(row, snap) if use_realtime else None
        verdict = classify(row, trip, use_realtime, now)
        if verdict is None:
            continue
        status, predicted = verdict
        shown = predicted or row["scheduled"]
        if not now - PAST_GRACE <= shown <= now + horizon:
            continue
        out.append({
            "trip_id": row["trip_id"], "service_date": row["service_date"],
            "route": row["route"], "direction_id": row["direction_id"],
            "headsign": row["headsign"], "scheduled": row["scheduled"],
            "predicted": predicted,
            "delay_s": round((predicted - row["scheduled"]).total_seconds())
                       if predicted else None,
            "status": status, "vehicle_id": trip.vehicle_id if trip else None,
            "timepoint": row["timepoint"], "_shown": shown})
    out.sort(key=lambda d: (d["_shown"], d["scheduled"], d["trip_id"]))
    for d in out:
        del d["_shown"]
    return out
