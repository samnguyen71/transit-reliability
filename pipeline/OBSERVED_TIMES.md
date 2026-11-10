# Observed times: rule v1

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
stop's time. Status alone isn't enough either: `STOPPED_AT` the last stop under the
trip's own `trip_id` shows up for only 39.4% of trips, because the bus often switches
to its next `trip_id` (and a different `stop_id`) as it pulls in.

The arrival is the **first report by the trip's vehicle that is either**:

- `STOPPED_AT` the last stop on this trip, or
- within **100 m** of the last stop's coordinates, under any `trip_id`.

Two limits apply:

- **Start:** only reports after the bus has passed the second-to-last stop on this
  trip (strict, as above). This keeps loop routes, and routes that pass near their own
  terminal, from matching an earlier pass.
- **End:** no later than **10 minutes** after the trip's last report under its own
  `trip_id`.

`source = vp_stopped` or `vp_position`. The bracket runs from that vehicle's previous
report to this one. Without such a report, the status is `lost`.

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
- **At a stop by position:** within 100 m of its coordinates.
- **Last-stop search:** up to 10 min after the trip's last own report.
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

- **When a bus passes without stopping and the sources disagree, VP is right.** Against
  the whole bracket, 83.1% agree. In the 15.6% where TU is more than 60 s before VP's last
  "not yet there" report, the bus's GPS position at that report shows it had not
  reached the stop in 97.8% of cases (7,745); VP lagged in only 1.4%. DRT's TripUpdates
  sometimes marks a stop it doesn't serve as passed early. Hence step 2 of the
  departure rule: on disagreement, VP's bracket wins.
- **TU's departure field is not used.** Among passed stops it differs from the arrival
  field 32,148 times. Even then it's a departure, but a less accurate one: at stopped
  stops it matches the VP departure within 60 s for 86.9% (non-timepoints) and 75.2%
  (timepoints), against 97.9% and 98.8% for the arrival field.
- **Last-stop arrivals need position.** Under the trip's own `trip_id`:
  - 39.4% of trips show `STOPPED_AT` at the last stop;
  - 41.9% head to it but never report stopped there;
  - 17.4% stop reporting before it.

  Following the vehicle to that `stop_id` and `STOPPED_AT` under any `trip_id`
  recovered only 10 of about 1,580. Following it by GPS (within 100 m) recovered
  1,269 (80%), 676 of them under another `trip_id`. That's about 88% of trips with an
  observed last-stop arrival, and about 12% `lost`.

  Those reports came a median 21 s after the trip's last own report (p95 176 s). The
  run searched from 10 min before to 30 min after, without the start limit. The
  limits above will lower the count slightly.

## To watch

- **Last-stop limits:** how much the start and end limits change the 88%, and whether
  `lost` last stops cluster on some routes (a skewed sample would bias the arrival
  score).
- **The weekly disagreement report:** record its numbers here with the rule version.
- **Each new timetable version** (next expected around 2026-11-30): rerun
  `feed_profile.py` with the new static zip and check these numbers still hold.
