"""Scheduled departures at a stop, for the live board (board.py merges in the feed)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import psycopg
from psycopg.rows import dict_row

from transit_api.queries.routes import ROUTE_COLS, marks_timepoints

ROUTE_KEYS = ("route_id", "short_name", "long_name", "color")


def scheduled(conn: psycopg.Connection, stop_id: str, days: list[date],
              lo: datetime, hi: datetime) -> list[dict]:
    """Departures from stop_id scheduled lo..hi, on any of the service dates days, each
    from the static feed covering that date (around a timetable change, yesterday's
    after-midnight trips come from the old one). Not the trip's last stop, where nobody
    boards, nor a stop with pickup_type 1.

    Each row also has what realtime matching needs:
    - start_date: the local date of the trip's first scheduled time, which is what DRT
      sends. The same rule as observe.load_schedule (rule v4): the service date, or the
      next day for a trip that starts at 24:00 or later.
    - rt_trip_ids: the trip_id, then any +24 h copies of it (gtfs.trip_alias): before
      03:00 DRT reports the overnight runs under the copy's trip_id.
    - trip_start: the trip's first scheduled time.
    """
    # The stop's visits first, MATERIALIZED: left to itself the planner drove the query
    # from gtfs.routes and expanded each day's schedule once per route row: 1.5 s at
    # Oshawa Centre Terminal, against about 30 ms this way (2026-11-27, 70 rows).
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(f"""
            WITH v AS MATERIALIZED (
                SELECT f.feed_id, f.timezone, f.marks_timepoints, v.service_date, v.trip_id,
                       v.stop_sequence, v.stop_id, v.departure_at
                FROM unnest(%(days)s::date[]) AS d(day)
                CROSS JOIN LATERAL (
                    SELECT fe.feed_id, fe.timezone,
                           {marks_timepoints("fe.feed_id")} AS marks_timepoints
                    FROM gtfs.feed fe WHERE fe.feed_id = gtfs.feed_for(d.day)
                ) f
                CROSS JOIN LATERAL gtfs.scheduled_stop_visits(f.feed_id, d.day) v
                WHERE v.stop_id = %(stop)s
                  AND v.departure_at BETWEEN %(lo)s AND %(hi)s
            )
            SELECT v.service_date, v.trip_id, v.stop_sequence, v.stop_id,
                   v.departure_at AS scheduled,
                   {ROUTE_COLS}, t.direction_id, t.trip_headsign AS headsign,
                   coalesce(st.timepoint = 1, NOT v.marks_timepoints) AS timepoint,
                   gtfs.service_time(v.service_date, fd.first_secs, v.timezone) AS trip_start,
                   to_char(gtfs.service_time(v.service_date, fd.first_secs, v.timezone)
                           AT TIME ZONE v.timezone, 'YYYYMMDD') AS start_date,
                   ARRAY[v.trip_id] || coalesce(
                       (SELECT array_agg(a.trip_id ORDER BY a.trip_id) FROM gtfs.trip_alias a
                        WHERE a.feed_id = v.feed_id AND a.canonical_trip_id = v.trip_id),
                       '{{}}') AS rt_trip_ids
            FROM v
            JOIN gtfs.trips t ON t.feed_id = v.feed_id AND t.trip_id = v.trip_id
            JOIN gtfs.routes r ON r.feed_id = v.feed_id AND r.route_id = t.route_id
            JOIN gtfs.stop_times st
              ON st.feed_id = v.feed_id AND st.trip_id = v.trip_id
             AND st.stop_sequence = v.stop_sequence
            CROSS JOIN LATERAL (
                SELECT min(coalesce(x.departure_secs, x.arrival_secs)) AS first_secs,
                       max(x.stop_sequence) AS last_seq
                FROM gtfs.stop_times x
                WHERE x.feed_id = v.feed_id AND x.trip_id = v.trip_id
            ) fd
            WHERE v.stop_sequence < fd.last_seq
              AND coalesce(st.pickup_type, 0) <> 1
            ORDER BY v.departure_at, v.trip_id
        """, {"days": days, "stop": stop_id, "lo": lo, "hi": hi}).fetchall()
    for r in rows:  # psycopg gives the session's time zone; the API answers in UTC
        r["scheduled"] = r["scheduled"].astimezone(UTC)
        r["trip_start"] = r["trip_start"].astimezone(UTC)
    # "route" first: its pops must run before **r copies the row.
    return [{"route": {k: r.pop(k) for k in ROUTE_KEYS}, **r} for r in rows]
