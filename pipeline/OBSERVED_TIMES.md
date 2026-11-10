# Observed times: rule v1 (draft)

How realtime snapshots become an observed time for each scheduled stop visit, and why.
Every output row carries `rule_version`. Changing anything below means a new version
and a rebuild, not results mixed across versions.

## What is scored

The usual transit standard:

- **Departure from each timepoint** (`stop_times.timepoint = 1`, including a trip's
  first stop). On time = from 1 minute early to 5 minutes late against the scheduled
  departure. Leaving early is the failure riders feel most.
- **Arrival at the last stop.** On time = no more than 5 minutes late; early counts as
  on time.

The window (−1 / +5 min) is the common default. Check it against DRT's published
definition before reporting numbers as comparable to theirs.

## Trip instance

Everything is keyed by `(gtfs.canonical_trip_id(feed_id, trip_id), start_date)`, with
realtime's `start_date` used unchanged (see KNOWN_EXCEPTIONS.md). This happens before
any comparison with the schedule.

## Cleaning, before any rule

- **Snapshots:** skip one whose feed timestamp is not newer than the last one kept
  (the logger has already dropped byte-identical downloads).
- **Duplicate trips:** if a trip appears twice in one snapshot with different contents,
  skip that trip in that snapshot and count it.
- **Vehicle reports:** a `timestamp` of 0 means unknown (store NULL). A report whose
  timestamp isn't newer than that vehicle's last one is not new evidence.
- **Times:** use the feed's own timestamps (header, vehicle), never the fetch time.

## Vehicle evidence (VehiclePositions), strict

A report shows the bus **reached** stop *s* if it is `STOPPED_AT` *s* or its
`current_stop_sequence` is past *s*. It only counts if both of these hold:

1. **No later report on the trip goes back before *s*.**
2. **The bus was seen before *s* on this trip, or the report is `STOPPED_AT` *s*.**

Reason: DRT attaches the next `trip_id` while a bus is still on, or laying over from,
its previous trip, with that trip's stop_sequence. Without this check, 13% of stops
look reached more than 5 minutes early; with it, 2.2%.

From the reports that count:

- **Arrival bracket:** (last report before reaching *s*, first report reaching *s*].
- **Departure bracket:** (last report at or before *s*, first report past *s* that
  never goes back to *s* or earlier].

## Departure from a stop

1. **TU time:** `arrival.time` from the last fresh TripUpdates snapshot that listed the
   stop. Accept it only if it was already in the past at that snapshot, i.e. recorded,
   not predicted.
2. **Agreement:** the TU time must be inside the VP departure bracket ±60 s.
   - If it is, use the TU time (`source = tu`).
   - If it isn't, use the bracket's upper end (`source = vp`, `disagree = true`). That's
     the first time the evidence shows the bus had left.
3. **One source only:** TU only gives `source = tu_only` (flagged); VP only gives
   `source = vp`.
4. **Always store** the bracket bounds `lo` and `hi`.

## Arrival at the last stop

TripUpdates can't be used here: trips leave the feed a median 196 s before their last
stop's time. Use the first strict `STOPPED_AT` report at the last stop
(`source = vp`, bracket kept). Without one, the status is `lost`.

## Status of each scheduled visit

Each visit gets exactly one status:

| Status | Meaning | Counts toward |
|---|---|---|
| `observed` | time, lo/hi and source known | on-time score |
| `cancelled` | trip `CANCELED`/`DELETED` and never seen running | service delivered (failure) |
| `skipped` | stop update `SKIPPED` | service delivered (failure) |
| `no_data` | the bracket overlaps a feed gap | coverage only |
| `lost` | evidence ends before the stop (vehicle stopped reporting, trip vanished) | coverage only |
| `not_seen` | trip never in realtime while the feed was covered | coverage only |

Coverage statuses are reported, never counted as late or missed. Added trips, or trips
not in any loaded static version, go to `unscheduled_trips` and aren't scored.

## Thresholds

- **Agreement tolerance:** 60 s (two polls).
- **Gap:** more than 90 s without a fresh snapshot.
- **Feed frozen:** feed timestamp more than 120 s old.

## Evidence (2026-11-05, one weekday, `feed_profile.py`)

- **Polling:** 2,877 polls, all saved except 3 duplicates. The only gap was about
  2 minutes at 08:00Z, the 03:00-local operating-day changeover, and it happens nightly.
- **Feed lag:** snapshots are about 20 s old when fetched. Vehicle timestamps are a
  median 15 s older than the snapshot.
- **TripUpdates contents:** 100% of stop updates have an absolute arrival time; there are
  no delay-only updates and no uncertainty values. Passed stops drop out about a minute
  after their time (median 57 s). The few that stay listed are frozen: 329 of 54,986
  changed.
- **TU's time is when the bus left.** At stops where the bus was `STOPPED_AT`, it
  matches the VP departure within 60 s for 97.9% (non-timepoints, n=24,571) and 98.8%
  (timepoints, n=3,720). Against the VP arrival, about 2/3 of those are 1–5 minutes late.
  For buses that passed without stopping, 80.9% match within 60 s.
- **`arrival == departure`** in 98.9% of stop updates that carry both times.
- **Cancelled, added and skipped:** 2,458 cancelled-trip rows, 508 added-trip rows and
  186,422 skipped stop updates (1.1%) in the day.
- **Vehicle data quirks:** 430 vehicle reports had timestamp 0; 925 stop_sequence
  backwards jumps; one vehicle still in the feed 4.5 h after its last report.

- **TU's departure field is not used.** Among passed stops it differs from the arrival
  field 32,148 times. Even then it's a departure, but a less accurate one: at stopped
  stops it matches the VP departure within 60 s for 86.9% (non-timepoints) and 75.2%
  (timepoints), against 97.9% and 98.8% for the arrival field.

## Open (check before v1 is final)

- **Last-stop arrivals.** Under the trip's own `trip_id`, only 39.4% of trips show
  `STOPPED_AT` at their last stop. 41.9% head to it but never report stopped there, and
  17.4% stop reporting before it. Likely cause: the bus switches to its next `trip_id`
  as it pulls in. `feed_profile.py` now follows the vehicle to the last stop's
  `stop_id` under any `trip_id`, within 30 min. If that recovers most of them, the
  last-stop rule follows the vehicle rather than the trip.
- **Passed without stopping, TU early.** Checked against the whole bracket, 83.1% agree
  and 15.6% of TU times are more than 60 s before VP's last "not yet there" report.
  `feed_profile.py` now uses the bus's GPS position at that report to tell whether VP's
  stop_sequence lagged (TU right) or TU recorded the stop early (VP right). That decides
  which source wins when they disagree.
- **Re-measure each new timetable version,** and when the disagreement report exists,
  record its weekly numbers here.
