"""Arrow schemas for parsed GTFS-realtime snapshots.

One Parquet file per feed, table and UTC day: OUT/<feed>/<table>/<YYYY-MM-DD>.parquet,
matching the logger's raw folders one to one.

The tables are faithful to the snapshots: fields are flattened, never interpreted.
- A field the feed didn't set is NULL, never the protobuf default. (An unset
  current_status would otherwise read as IN_TRANSIT_TO, and an unset delay as 0, i.e.
  on time.)
- A time of 0 is NULL too. GTFS-realtime times are POSIX seconds, and DRT sends 0 when
  it has no time.
- Enums are stored as their names.
- Every entity row says which snapshot it came from. (file, entity_index) is unique
  within a feed-day.
"""

import pyarrow as pa

# Parquet has no second-resolution timestamps (pyarrow would store seconds as ms and read
# them back as ms), so every time is milliseconds. Feed times are whole seconds; only
# polls.fetched_at has a fractional part.
TS = pa.timestamp("ms", tz="UTC")


def _f(name: str, type_: pa.DataType, nullable: bool = True) -> pa.Field:
    return pa.field(name, type_, nullable=nullable)


# Where an entity row came from.
_SOURCE = [
    _f("file", pa.string(), False),        # snapshot file name, e.g. 20261105T130002Z.pb.gz
    _f("header_ts", TS),                   # the feed's own timestamp for the snapshot
    _f("entity_index", pa.int32(), False),  # position of the entity in the snapshot
    _f("entity_id", pa.string()),
]

_TRIP = [
    _f("trip_id", pa.string()),
    _f("route_id", pa.string()),
    _f("direction_id", pa.int32()),
    _f("start_date", pa.string()),         # as sent, YYYYMMDD
    _f("start_time", pa.string()),         # as sent, HH:MM:SS
    _f("trip_schedule_relationship", pa.string()),
]

# One row per line of _manifest.jsonl: every poll, good or bad.
POLLS = pa.schema([
    _f("fetched_at", TS, False),
    _f("status", pa.string(), False),
    _f("http_status", pa.int32()),
    _f("bytes", pa.int64()),
    _f("stored_bytes", pa.int64()),
    _f("sha256", pa.string()),
    _f("file", pa.string()),
    _f("feed_timestamp", TS),
    _f("feed_age_s", pa.float64()),
    _f("entities", pa.int32()),
    _f("elapsed_ms", pa.int32()),
    _f("error", pa.string()),
])

# One row per file in the day folder, including ones that couldn't be read.
SNAPSHOTS = pa.schema([
    _f("file", pa.string(), False),
    _f("fetched_at", TS, False),           # from the file name
    _f("status", pa.string(), False),      # ok, invalid (logger's .invalid.gz), unreadable
    _f("error", pa.string()),
    _f("stored_bytes", pa.int64(), False),
    _f("header_ts", TS),
    _f("gtfs_realtime_version", pa.string()),
    _f("incrementality", pa.string()),
    _f("entities", pa.int32()),
    _f("trip_updates", pa.int32()),
    _f("vehicles", pa.int32()),
    _f("alerts", pa.int32()),
])

# One row per TripUpdate entity per snapshot, including ones with no stop updates
# (a CANCELED trip usually has none).
TRIP_UPDATES = pa.schema(_SOURCE + _TRIP + [
    _f("is_deleted", pa.bool_()),
    _f("vehicle_id", pa.string()),
    _f("vehicle_label", pa.string()),
    _f("timestamp", TS),
    _f("delay", pa.int32()),
    _f("stop_time_updates", pa.int32(), False),
])

# One row per StopTimeUpdate. Joins to trip_updates on (file, entity_index).
STOP_TIME_UPDATES = pa.schema(_SOURCE + [
    _f("update_index", pa.int32(), False),  # position within the trip's stop_time_update list
    _f("trip_id", pa.string()),
    _f("start_date", pa.string()),
    _f("stop_sequence", pa.int32()),
    _f("stop_id", pa.string()),
    _f("arrival_time", TS),
    _f("arrival_delay", pa.int32()),
    _f("arrival_uncertainty", pa.int32()),
    _f("departure_time", TS),
    _f("departure_delay", pa.int32()),
    _f("departure_uncertainty", pa.int32()),
    _f("schedule_relationship", pa.string()),
])

# One row per VehiclePosition entity per snapshot.
VEHICLE_POSITIONS = pa.schema(_SOURCE + _TRIP + [
    _f("is_deleted", pa.bool_()),
    _f("vehicle_id", pa.string()),
    _f("vehicle_label", pa.string()),
    _f("latitude", pa.float64()),
    _f("longitude", pa.float64()),
    _f("bearing", pa.float32()),
    _f("speed", pa.float32()),
    _f("current_stop_sequence", pa.int32()),
    _f("stop_id", pa.string()),
    _f("current_status", pa.string()),
    _f("timestamp", TS),
    _f("congestion_level", pa.string()),
    _f("occupancy_status", pa.string()),
])

TABLES: dict[str, pa.Schema] = {
    "polls": POLLS,
    "snapshots": SNAPSHOTS,
    "trip_updates": TRIP_UPDATES,
    "stop_time_updates": STOP_TIME_UPDATES,
    "vehicle_positions": VEHICLE_POSITIONS,
}


# Observed times (realtime.observe), one file per service date: OUT/<table>/<date>.parquet.
# See OBSERVED_TIMES.md for what each column means.

# One row per scheduled stop visit: a departure, or the arrival at the trip's last stop.
VISITS = pa.schema([
    _f("service_date", pa.date32(), False),
    _f("trip_id", pa.string(), False),     # canonical (gtfs.canonical_trip_id)
    _f("route_id", pa.string()),
    _f("direction_id", pa.int32()),
    _f("stop_sequence", pa.int32(), False),
    _f("stop_id", pa.string(), False),
    _f("event", pa.string(), False),       # departure, or arrival at the last stop
    _f("scored", pa.bool_(), False),       # timepoint departure or last-stop arrival
    _f("scheduled", TS),
    _f("status", pa.string(), False),      # observed, cancelled, skipped, no_data, lost, not_seen
    _f("observed", TS),
    _f("delay_s", pa.int32()),             # observed - scheduled
    _f("source", pa.string()),             # tu, vp, tu_only, vp_stopped, vp_position
    _f("lo", TS),                          # the VP bracket (lo, hi]
    _f("hi", TS),
    _f("tu_time", TS),                     # TU's recorded time, kept even when not used
    _f("disagree", pa.bool_()),            # TU time outside the bracket ±60 s
    _f("tu_gap", pa.bool_()),              # bracket overlaps a TripUpdates gap
    _f("vehicle_id", pa.string()),
])

# The feed gaps the statuses were judged against.
GAPS = pa.schema([
    _f("feed", pa.string(), False),
    _f("start", TS, False),                # last fresh feed time before the gap
    _f("end", TS, False),                  # first fresh feed time after it
    _f("seconds", pa.int32(), False),
])

# Trips in realtime with this start_date that aren't in the schedule (ADDED, or unknown).
UNSCHEDULED = pa.schema([
    _f("trip_id", pa.string(), False),
    _f("start_date", pa.string(), False),
    _f("route_id", pa.string()),
    _f("relationships", pa.string()),      # TU trip schedule_relationships seen, comma-separated
    _f("in_tu", pa.bool_(), False),
    _f("in_vp", pa.bool_(), False),
    _f("first_seen", TS),
    _f("last_seen", TS),
    _f("vehicle_id", pa.string()),
])

OBSERVED_TABLES: dict[str, pa.Schema] = {
    "visits": VISITS,
    "gaps": GAPS,
    "unscheduled": UNSCHEDULED,
}
