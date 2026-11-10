# Known exceptions in DRT's feeds

The loader stores the static GTFS exactly as published. Where the feed says something
that shouldn't be scored as written, the exception is recorded separately and applied
downstream, so the raw rows stay checkable against the file.

## overnight_twin_24h: duplicated night trips

**What the static feed has.** Timetable 2026-10 (effective 2026-10-27) has 26 trips in
`Overnight` (N1 13, N2 11, 901 2). Each is an exact copy of another trip in the same
service, with every time 24:00:00 later: same route, direction and stops, but a
different `trip_id` and `block_id`. One example is `7718__491905…` at 01:15 and
`388__491005…` at 25:15. Expanded as written, every night bus is scheduled twice.

**What realtime does.** Checked against `trip_updates` for the nights of
2026-10-28 to 2026-11-10 with `night_copies.py`:

- DRT's realtime switches operating day at about 03:00 local. Before then it reports a
  run under the late copy's `trip_id`. From about 03:10–03:20 it reports it under the
  early copy's `trip_id`. A run that crosses 03:00 changes `trip_id` partway through.
- `start_date` is always the local calendar date of the run. For the late copies the
  spec says it should be the previous day, so the spec reading is 24 h off.
- The 01:15–01:55 early copies and the 28:02+ late copies never appear: they would only
  be used on the side of 03:00 where they don't run.
- On the fall-back night (2026-11-01), 25:15 and 26:15 ran at 01:15 and 02:15 **EST**
  by the clock. That matches the early copy on service date 2026-11-01 under the spec's
  noon-minus-12 h rule, not the late copy on 2026-10-31.

**How it's handled.**

- `gtfs.mark_overnight_twins(feed_id)` runs in the loader's transaction. It records each
  late copy in `gtfs.trip_alias` with `canonical_trip_id` = its early twin. The rule is
  strict: everything must match except times exactly +86400 s. On timetable 2026-10 it
  finds exactly the 26 pairs.
- `gtfs.scheduled_stop_visits` leaves aliased trips out. That's 1,391 visits a day, so a
  weekday date has **97,996** scheduled visits (not 99,387) and a Sat/Sun date
  **64,539** (not 65,930):

  ```
  python -m gtfsdb.load --expect-weekday 97996 --expect-weekend 64539 <zip>
  ```

- Realtime matching (Phase 2) should score a trip as
  `gtfs.canonical_trip_id(feed_id, rt.trip_id)` on `rt.start_date` unchanged.

**Consequences for scoring.**

- From about 03:00 to 03:15 local, night routes have no trip IDs in realtime. Visits
  scheduled in that window should count as "no data", not "missed".

**Still to check.**

- Vehicle IDs across the 03:00 handover in `vehicle_positions`: one bus, two IDs?
- Spring forward (2027-03-14).
- Each new timetable version: rerun `night_copies.py` and confirm the alias count
  the loader prints.
