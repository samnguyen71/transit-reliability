# Observed times: rule v3

How realtime snapshots become an observed time for each scheduled stop visit, and why.
Every output row carries `rule_version`. Changing anything below means a new version
and a rebuild, not results mixed across versions.

- **v3 (2026-11-13):** GPS decides disagreements, and first stops go by position (see
  "Departure from a stop" and "First real run").
- **v2 (2026-11-13):** a blank `timepoint` is a timepoint only in a feed that marks none.
  v1 followed the GTFS default (blank with times = exact), which scored every stop on
  dates using the 2026-10-12 zip. That zip leaves non-timepoints blank, and the next
  marks exactly those stops 0.

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
- **Vehicle reports:** a `timestamp` of 0 means unknown (stored NULL). Such a report
  can't be placed in time, so the rule leaves it out and counts it. A report whose
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
2. **Agreement:** the TU time must be inside the VP departure bracket ±60 s. If it is,
   use the TU time (`source = tu`).
3. **Disagreement: GPS decides** (`disagree = true`). DRT's `current_stop_sequence`
   sometimes stops advancing while the bus drives on, and TU is then right. To check,
   place each of the trip's own reports at the trip's stop nearest its GPS position, and
   build the same strict departure bracket from those positions.
   - TU time inside the GPS bracket ±60 s: use it (`source = tu_gps`).
   - Otherwise, if GPS has the bus already past the stop at the VP bracket's `lo`: VP's
     bracket is stale and TU isn't backed either, so status `lost`.
   - Otherwise use the VP bracket's upper end (`source = vp`), the first time the
     evidence shows the bus had left.
4. **One source only:** TU only gives `source = tu_only` (flagged); VP only gives
   `source = vp`.
5. **Always store** the bracket bounds `lo` and `hi`.

### The first stop of a trip

Neither feed is used for the time. Both move on from the first stop while the bus is
still waiting there. The departure is by position: the bracket runs from the last own
report within **100 m** of the stop to the next one, the first beyond it, and the time is
that upper end (`source = vp_position`). TU's time is stored, and `disagree` is set if it
falls outside this bracket ±60 s.

- A bus never reported within 100 m of the stop is `lost`, even with a TU time.
- So is one whose reports end while it is still at the stop.
- A feed gap gives `no_data`, the same as elsewhere.

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
| `no_data` | a feed gap explains the missing evidence (below) | coverage only |
| `lost` | the trip was seen, but not around this stop: its evidence starts after the stop or ends before it, with no gap to explain that. Also a departure where GPS shows VP's bracket is stale and doesn't back TU's time | coverage only |
| `not_seen` | trip never in realtime while the feed was covered | coverage only |

Coverage statuses are reported, never counted as late or missed. Added trips, or trips
not in the static version used for the date, go to the `unscheduled` table and aren't
scored.

How each visit is decided, in this order:

1. **`skipped`** if the stop's last fresh TripUpdates listing says `SKIPPED`. This wins
   over VehiclePositions, which shows a bus passing a stop it skips the same way as one
   it serves.
2. **`cancelled`** if the trip was `CANCELED` or `DELETED` in a fresh snapshot and never
   ran: no vehicle report and no recorded TU time. A trip that ran anyway is judged on
   its evidence like any other.
3. **`not_seen` or `no_data`** if the trip is in neither feed: `no_data` if its scheduled
   span overlaps a gap in either feed.
4. **Otherwise from the evidence.** A departure bracket that overlaps a VehiclePositions
   gap is `no_data`, with `lo`, `hi` and `tu_time` kept. Where there is no bracket:
   - evidence that stops or starts within 90 s of a VehiclePositions gap is `no_data`
     (the feed cut it short);
   - otherwise it is `lost` (the vehicle did).

   For a last-stop arrival, the same applies to its bracket and to the search window.

Two more columns:

- **`tu_gap`:** set when a bracket overlaps a TripUpdates gap, so the disagreement report
  can leave those departures out. A TripUpdates gap alone never makes a visit `no_data`:
  VehiclePositions carries it.
- **The last-stop search start:** the `hi` of the second-to-last stop's departure
  bracket, even when its `lo` is missing (the bus was first seen already past that stop).

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

- **Withdrawn (2026-11-13): "when the sources disagree, VP is right".** This said that in
  97.8% of disagreements the bus's GPS showed it had not reached the stop at VP's last
  "not yet there" report. The test (`feed_profile.where_was_the_bus`) labelled any
  position not between the stop and the next one as "not yet at the stop", including a
  bus far past it. See "First real run" below: the opposite holds. v3 replaced the rule
  that rested on it.
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

## Evidence: gaps (2026-10-12 to 11-10, the first parsed month)

- **Polling is complete:** 2,880 polls a day per feed. Four days lost one poll each
  (logger restarts), and 10-12 lost about 36 minutes on both feeds with no poll lines
  at all.
- **Missing snapshots are nearly all a frozen feed:** the same bytes came back
  (`duplicate`), not an error. There were only a handful of network or HTTP errors in
  the month. A frozen feed still answers while its data goes stale, so gaps are measured
  from the feed's own timestamps, never from poll results.
- **Nightly:** 3 TripUpdates and 4 VehiclePositions duplicates around the 03:00
  changeover. That leaves the longest stretch without a fresh snapshot at 2.0 and
  2.5 minutes, both over the 90 s gap threshold, so it is marked as a gap every night.
- **Longer freezes:** TripUpdates froze for 77 minutes on 10-20 (155 duplicates) and
  6 minutes on 10-18, while VehiclePositions stayed under 3 minutes on both days. The
  feeds fail separately, which is why gaps are kept per feed.
- **Short freezes:** some days have scattered duplicates (VehiclePositions 46 on 10-19
  and 33 on 10-22, TripUpdates 35 on 10-21), but the longest stretch stayed at 2.5 min.

## First real run (2026-11-05 and 2026-10-20, rule v2)

Both dates: 2,532 trips, 17,855 scored visits, 95% `observed`. Two parts of the rule
don't hold, so no on-time numbers from v2 should be reported. Checked on 11-05 by GPS:
the vehicle's position at the report in question, against the trip's own stops.

- **Disagreements: VP's `current_stop_sequence` was stale, not TU early.** At VP's last
  "not yet past" report, the trip's stop nearest the bus was already past the scored
  stop in 929 of 975 scored disagreements (95%), and 6,215 of 6,411 others (97%). The bus
  was a median 4.9 km away. The same test on departures where TU and VP agree puts the bus
  before or at the stop in 97%, so the test itself holds up. Taking VP's `hi` made those
  departures a median 14 min too late.
- **First stops: both feeds move on before the bus does.** 87% of first-stop departures
  are `tu_only` (VP has a `hi` but no `lo`, since nothing comes before stop 1), and TU's
  time matches VP's `hi` to a median 2 s. At that report the bus is a median 28 m from
  the stop, under its own `trip_id`. It first reports more than 100 m away a median 8 min
  later for the 1,055 that looked early. By that movement, 10% leave more than a minute
  early, not 44%.
- **Last-stop arrivals:** 17.5-17.9% `lost`, not the 12% estimated, and skewed by route
  (505: 68%, 502: 40%, 915: 37%).
- **Gaps:** `no_data` is 0.1-0.3% of scored visits, mostly 02:00-03:59 local (the nightly
  changeover). The 77-minute TripUpdates freeze dated 10-20 UTC was 21:52-23:09 local on
  10-19, so it belongs to service date 10-19, which wasn't run.
- **Cleaning counts:** conflicting trip copies 478 (11-05) and 697 (10-20); vehicle
  reports with timestamp 0: 430 and 1.

### What v3 changed (same two dates; figures for 11-05, 10-20 within 1 point)

- **Checks behind the GPS rule**, on all departures, scored or not:
  - Disagreements split into TU earlier than VP's bracket (7,148) and later (219).
  - In the earlier group, GPS has the bus past the stop at `lo` in 99.5%, and TU's time
    falls inside the GPS bracket ±60 s in 99% (6,962 of 7,029).
  - In the later group, GPS shows no stale `lo`, so VP stands.
- **Mid-route scored departures:** `tu` 84.0%, `tu_gps` 6.8%, `vp` 8.8%, `tu_only` 0.5%.
  - 111 disagreements are now `lost`: the bus was already a median 9 km past the stop at
    `lo`, and VP's `hi` came a median 24 min after GPS showed it past.
  - The remaining `vp` departures without a TU time (1,063) do agree with GPS: VP's `hi`
    is a median 48 s before the bus is first more than 100 m past the stop by GPS. They
    really were late, by a median 10 min.
- **First stops:** 94.1% `observed`, 4.7% `lost`, against 0.9% `lost` under v2 when TU
  filled in. On time 71.5%, early 4.2%, late 24.3%; v2 had 33.7%, 44.0%, 22.3%.
- **Mid-route timepoints are left as they are.** Where TU and VP agree, the bus is a
  median 16 m from the stop at VP's `hi`, and first reports more than 100 m away a median
  30 s after TU's time (one poll; p90 91 s). That's dwell, not the first-stop problem.
- **On time, all scored observed visits:** 64.8% (11-05) and 64.7% (10-20).
  - By kind on 11-05: first stops 71.5%, mid-route timepoints 62.4%, last-stop
    arrivals 71.3%.
  - Late is the main failure: 29.5% of mid-route timepoint departures are more than
    5 min late.

## Running it

From `pipeline/`, with `DATABASE_URL` set and the static feeds loaded:

    python -m realtime.observe ../data/parsed ../data/observed [--date YYYY-MM-DD] [--force]

- **Inputs:** a service date reads the parsed UTC days D and D+1 of both feeds.
- **Which dates:** without `--date`, every date with both days parsed.
- **Which static feed:** the newest one that runs service on the date and was fetched
  before the date began (local midnight). DRT's zips are fetched around 15:10 local, so
  a new one first applies the next day. A feed fetched during the date is used only if
  no earlier one covers it.
- **Outputs:** `visits`, `gaps` and `unscheduled`, one file each per date (schema in
  `realtime/schemas.py`).
- **Metadata:** each file records `rule_version`, the static `feed_id` used, and the
  cleaning counts (`stats`).
- **Reruns:** a date is redone only when the rule version, the static feed or its parsed
  input changes, or with `--force`.
- **Memory:** DuckDB runs on `--threads` (default 2) within `--memory-limit` (default
  250MB). Each thread buffers its own Parquet reads: with one per core (22 here), 250MB
  fails at once. A date takes about 10 s and peaks around 330 MB for the whole process.
  Don't run it on the logging VM (1 GB): it froze the VM for hours on 2026-11-12.

The code:
- the rule is in `realtime/rules.py` (no I/O; `tests/test_rules.py` has one test per
  edge case);
- reading the Parquet is in `realtime/evidence.py`;
- the driver is `realtime/observe.py`.

## To watch

- **Last-stop limits:** how much the start and end limits change the 88%, and whether
  `lost` last stops cluster on some routes (a skewed sample would bias the arrival
  score).
- **The weekly disagreement report:** record its numbers here with the rule version.
- **Service date 10-19:** whether its 77-minute TripUpdates freeze shows up as `vp`
  sources rather than `no_data`. It needs parsed day 10-19 as well as 10-20.
- **First stops `lost` (4.7%):** whether they cluster at terminals whose stop coordinates
  are more than 100 m from where buses wait.
- **Loop routes:** where a trip passes near one of its own stops twice, the nearest stop
  can be the wrong one, which sends a disagreement to `vp` or `lost`. Check those by
  route.
- **Mid-route early departures (8%):** TU's time comes a median 30 s before the bus is
  first more than 100 m away. That's within a poll, but at −60 s it decides some
  departures.
- **Each new timetable version** (next expected around 2026-11-30): rerun
  `feed_profile.py` with the new static zip and check these numbers still hold.
