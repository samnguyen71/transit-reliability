"""Observed times for each scheduled stop visit, by rule v1 (OBSERVED_TIMES.md).

    python -m realtime.observe PARSED_DIR OUT_DIR [--date YYYY-MM-DD ...] [--force]

DATABASE_URL names the PostgreSQL holding the static feeds (gtfsdb.load). For each
service date this writes OUT_DIR/<table>/<date>.parquet for visits, gaps and unscheduled
(realtime.schemas.OBSERVED_TABLES).

- A service date's trips run into the next day, so it needs the parsed UTC days D and D+1
  of both feeds. Without --date, every date with both days parsed is done.
- The static feed is the newest one fetched before the end of the date that runs service
  on it, unless --static-feed says otherwise.
- A date is skipped if its output was written by this rule version, from the same static
  feed, after its parsed input. --force redoes it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from realtime import evidence
from realtime.rules import RULE_VERSION, Evidence, Stop, Trip, Visit, observe_trip
from realtime.schemas import OBSERVED_TABLES

TU_TABLES = ("snapshots", "trip_updates", "stop_time_updates")
VP_TABLES = ("snapshots", "vehicle_positions")


@dataclass
class Schedule:
    feed_id: int
    trips: list[Trip]
    alias: dict[str, str]  # trip_id -> canonical trip_id


# Schedule (PostgreSQL) -----------------------------------------------------------------

def pick_static_feed(conn, service_date: date) -> int | None:
    row = conn.execute("""
        SELECT f.feed_id FROM gtfs.feed f
        WHERE f.fetched_at < ((%(d)s::date + 1)::timestamp AT TIME ZONE f.timezone)
          AND EXISTS (SELECT 1 FROM gtfs.active_services(f.feed_id, %(d)s::date))
        ORDER BY f.fetched_at DESC, f.feed_id DESC LIMIT 1
    """, {"d": service_date}).fetchone()
    return row[0] if row else None


def load_schedule(conn, service_date: date, feed_id: int) -> Schedule:
    rows = conn.execute("""
        SELECT v.trip_id, v.route_id, v.direction_id, v.stop_sequence, v.stop_id,
               extract(epoch FROM v.arrival_at)::bigint,
               extract(epoch FROM v.departure_at)::bigint,
               st.timepoint, s.stop_lat, s.stop_lon
        FROM gtfs.scheduled_stop_visits(%(f)s, %(d)s) v
        JOIN gtfs.stop_times st
          ON st.feed_id = %(f)s AND st.trip_id = v.trip_id AND st.stop_sequence = v.stop_sequence
        JOIN gtfs.stops s ON s.feed_id = %(f)s AND s.stop_id = v.stop_id
        ORDER BY v.trip_id, v.stop_sequence
    """, {"f": feed_id, "d": service_date}).fetchall()
    alias = dict(conn.execute(
        "SELECT trip_id, canonical_trip_id FROM gtfs.trip_alias WHERE feed_id = %s",
        (feed_id,)).fetchall())
    return Schedule(feed_id, build_trips(rows, service_date), alias)


def build_trips(rows: list[tuple], service_date: date) -> list[Trip]:
    """rows: (trip_id, route_id, direction_id, stop_sequence, stop_id, arrival, departure,
    timepoint, lat, lon), ordered by trip and stop_sequence, times in POSIX seconds.

    A departure is scored at a timepoint (GTFS: timepoint 1, or blank with times given)
    and at the trip's first stop, if it has a scheduled time.
    """
    start_date = service_date.strftime("%Y%m%d")
    trips: list[Trip] = []
    current = None
    for trip_id, route_id, direction_id, seq, stop_id, arr, dep, tp, lat, lon in rows:
        if current is None or current.key[0] != trip_id:
            current = Trip((trip_id, start_date), route_id, direction_id, [])
            trips.append(current)
        first = not current.stops
        exact = tp == 1 or (tp is None and dep is not None)
        current.stops.append(Stop(seq, stop_id, arr, dep, dep is not None and (exact or first),
                                  lat, lon))
    return trips


# Running a date ------------------------------------------------------------------------

def input_paths(parsed: Path, service_date: date, feeds: tuple[str, str]) -> list[Path]:
    """The parsed files a service date reads: UTC days D and D+1 of both feeds."""
    days = [service_date.isoformat(), (service_date + timedelta(days=1)).isoformat()]
    tu_feed, vp_feed = feeds
    return [evidence.table_path(parsed, feed, table, day)
            for feed, tables in ((tu_feed, TU_TABLES), (vp_feed, VP_TABLES))
            for table in tables for day in days]


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def observe_date(con, parsed: Path, service_date: date, schedule: Schedule,
                 tu_feed: str = "trip_updates", vp_feed: str = "vehicle_positions",
                 ) -> tuple[dict[str, pa.Table], Counter]:
    """Observe every scheduled visit of one service date. Returns the output tables."""
    missing = [p for p in input_paths(parsed, service_date, (tu_feed, vp_feed)) if not p.exists()]
    if missing:
        raise FileNotFoundError(f"not parsed yet: {', '.join(str(p) for p in missing[:3])}"
                                + (f" and {len(missing) - 3} more" if len(missing) > 3 else ""))
    days = [service_date.isoformat(), (service_date + timedelta(days=1)).isoformat()]
    start_date = service_date.strftime("%Y%m%d")
    gap_window = (_epoch(service_date), _epoch(service_date + timedelta(days=2)))

    ev, trip_info, stats = evidence.gather(con, parsed, days, start_date, gap_window,
                                           schedule.alias, tu_feed, vp_feed)
    stats["trips"] = len(schedule.trips)
    scheduled = {trip.key for trip in schedule.trips}
    unscheduled = _unscheduled(ev, trip_info, scheduled, start_date)
    tables = {
        "visits": _visits_table(service_date, schedule.trips, ev),
        "gaps": _gaps_table([(tu_feed, g) for g in ev.tu_gaps] + [(vp_feed, g) for g in ev.vp_gaps]),
        "unscheduled": _unscheduled_table(unscheduled),
    }
    return tables, stats


def _ms(t: int | None) -> int | None:
    return t * 1000 if t is not None else None


def _visit_row(service_date: date, trip: Trip, v: Visit) -> tuple:
    """A visits row, in schema order."""
    delay = v.time - v.scheduled if v.time is not None and v.scheduled is not None else None
    return (service_date, trip.key[0], trip.route_id, trip.direction_id, v.seq, v.stop_id,
            v.event, v.scored, _ms(v.scheduled), v.status, _ms(v.time), delay, v.source,
            _ms(v.lo), _ms(v.hi), _ms(v.tu_time), v.disagree, v.tu_gap, v.vehicle_id)


def _visits_table(service_date: date, trips: list[Trip], ev: Evidence,
                  batch_rows: int = 50_000) -> pa.Table:
    """Run the rule trip by trip, turning rows into Arrow batches as they come."""
    schema = OBSERVED_TABLES["visits"]
    batches, rows = [], []

    def flush():
        if rows:
            cols = zip(*rows, strict=True)
            batches.append(pa.record_batch(
                [pa.array(c, type=f.type) for c, f in zip(cols, schema, strict=True)],
                schema=schema))
            rows.clear()

    for trip in trips:
        rows.extend(_visit_row(service_date, trip, v) for v in observe_trip(trip, ev))
        if len(rows) >= batch_rows:
            flush()
    flush()
    return pa.Table.from_batches(batches, schema=schema)


def _gaps_table(gaps: list[tuple[str, tuple[int, int]]]) -> pa.Table:
    return pa.table({
        "feed": [f for f, _ in gaps],
        "start": [_ms(a) for _, (a, _b) in gaps],
        "end": [_ms(b) for _, (_a, b) in gaps],
        "seconds": [b - a for _, (a, b) in gaps],
    }, schema=OBSERVED_TABLES["gaps"])


def _unscheduled(ev: Evidence, trip_info: dict, scheduled: set, start_date: str) -> list[dict]:
    keys = {k for k in ev.tu_seen | set(ev.by_trip) if k[1] == start_date} - scheduled
    out = []
    for key in sorted(keys):
        info = trip_info.get(key[0], {})
        reports = ev.by_trip.get(key, [])
        seen = [t for t in (info.get("first_seen"), info.get("last_seen")) if t is not None]
        seen += [r.t for r in reports[:1] + reports[-1:]]
        out.append({
            "trip_id": key[0], "start_date": key[1], "route_id": info.get("route_id"),
            "relationships": ",".join(info.get("relationships", [])) or None,
            "in_tu": key in ev.tu_seen, "in_vp": bool(reports),
            "first_seen": _ms(min(seen)) if seen else None,
            "last_seen": _ms(max(seen)) if seen else None,
            "vehicle_id": reports[-1].vehicle_id if reports else info.get("vehicle_id"),
        })
    return out


def _unscheduled_table(rows: list[dict]) -> pa.Table:
    schema = OBSERVED_TABLES["unscheduled"]
    return pa.table({f.name: [r[f.name] for r in rows] for f in schema}, schema=schema)


# Output --------------------------------------------------------------------------------

def output_paths(out: Path, service_date: date) -> dict[str, Path]:
    return {t: out / t / f"{service_date.isoformat()}.parquet" for t in OBSERVED_TABLES}


def write_outputs(out: Path, service_date: date, tables: dict[str, pa.Table],
                  meta: dict[str, str]) -> None:
    """Write every table to a temp file first, then rename them all."""
    paths = output_paths(out, service_date)
    tmps = {}
    try:
        for name, table in tables.items():
            path = paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmps[name] = path.with_name(path.name + ".tmp")
            pq.write_table(table.replace_schema_metadata(meta), tmps[name], compression="zstd")
    except BaseException:
        for tmp in tmps.values():
            tmp.unlink(missing_ok=True)
        raise
    for name, tmp in tmps.items():
        os.replace(tmp, paths[name])


def is_done(out: Path, parsed: Path, service_date: date, feed_id: int,
            feeds: tuple[str, str]) -> bool:
    paths = list(output_paths(out, service_date).values())
    if not all(p.exists() for p in paths):
        return False
    metas = [pq.read_schema(p).metadata or {} for p in paths]
    if any(m.get(b"rule_version") != RULE_VERSION.encode()
           or m.get(b"static_feed_id") != str(feed_id).encode() for m in metas):
        return False
    if len({m.get(b"written_at") for m in metas}) != 1:
        return False
    inputs = [p.stat().st_mtime for p in input_paths(parsed, service_date, feeds) if p.exists()]
    return min(p.stat().st_mtime for p in paths) >= max(inputs, default=0)


def parsed_dates(parsed: Path, feeds: tuple[str, str]) -> list[date]:
    """Service dates whose UTC days D and D+1 are parsed for both feeds."""
    days = None
    for feed in feeds:
        found = {p.stem for p in (parsed / feed / "snapshots").glob("*.parquet")}
        days = found if days is None else days & found
    out = []
    for day in sorted(days or ()):
        d = date.fromisoformat(day)
        if (d + timedelta(days=1)).isoformat() in days:
            out.append(d)
    return out


def summary(service_date: date, tables: dict[str, pa.Table], stats: Counter, feed_id: int,
            seconds: float) -> str:
    v = tables["visits"].to_pydict()
    scored = [i for i, s in enumerate(v["scored"]) if s]
    status = Counter(v["status"][i] for i in scored)
    n = len(scored) or 1
    statuses = ", ".join(f"{k} {c / n:.1%}" for k, c in status.most_common())
    sources = Counter(v["source"][i] for i in scored if v["source"][i])
    both = [i for i in range(len(v["status"])) if v["source"][i] in ("tu", "vp")
            and v["tu_time"][i] is not None]
    disagree = sum(1 for i in both if v["disagree"][i])
    parts = [
        f"{stats['trips']:,} trips, {len(scored):,} scored visits: {statuses}",
        "sources " + ", ".join(f"{k} {c:,}" for k, c in sources.most_common()),
        f"TU and VP disagree at {disagree:,} of {len(both):,} departures"
        + (f" ({disagree / len(both):.1%})" if both else ""),
        f"{tables['gaps'].num_rows} gaps, {tables['unscheduled'].num_rows} unscheduled trips",
        f"static feed {feed_id}",
    ]
    return f"{service_date}: " + "; ".join(parts) + f" in {seconds:.0f}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Observed times per scheduled stop visit.")
    ap.add_argument("parsed", type=Path, help="realtime.parse's output folder")
    ap.add_argument("out", type=Path, help="where to write <table>/<date>.parquet")
    ap.add_argument("--date", action="append", type=date.fromisoformat,
                    help="only this service date, YYYY-MM-DD (repeatable)")
    ap.add_argument("--force", action="store_true", help="redo dates already done")
    ap.add_argument("--static-feed", type=int, help="gtfs.feed id to use instead of the default")
    ap.add_argument("--tu-feed", default="trip_updates")
    ap.add_argument("--vp-feed", default="vehicle_positions")
    ap.add_argument("--memory-limit", default="250MB", help="for DuckDB (default 250MB)")
    args = ap.parse_args(argv)

    import psycopg  # only needed here, so the rest imports without a database driver

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    feeds = (args.tu_feed, args.vp_feed)
    failures = 0
    # autocommit: these are read-only queries, and one failing must not leave the
    # connection in an aborted transaction that fails every later date too.
    with psycopg.connect(url, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('gtfs.feed')").fetchone()[0] is None:
            print("No gtfs schema in this database. Run sql/01_schema.sql and "
                  "sql/02_functions.sql against it, then load the static feeds "
                  "(python -m gtfsdb.load ZIP ...).", file=sys.stderr)
            return 2
        con = evidence.connect(args.memory_limit, args.out / "_tmp")
        for d in args.date or parsed_dates(args.parsed, feeds):
            feed_id = args.static_feed or pick_static_feed(conn, d)
            if feed_id is None:
                print(f"{d}: no loaded static feed runs service on this date", file=sys.stderr)
                failures += 1
                continue
            if not args.force and is_done(args.out, args.parsed, d, feed_id, feeds):
                continue
            started = time.monotonic()
            try:
                schedule = load_schedule(conn, d, feed_id)
                tables, stats = observe_date(con, args.parsed, d, schedule, *feeds)
                meta = {"rule_version": RULE_VERSION, "static_feed_id": str(feed_id),
                        "service_date": d.isoformat(),
                        "written_at": datetime.now(UTC).isoformat(),
                        "stats": json.dumps(dict(stats))}
                write_outputs(args.out, d, tables, meta)
            except Exception as e:  # noqa: BLE001 - report the date and go on to the next
                print(f"{d}: failed, nothing written: {type(e).__name__}: {e}", file=sys.stderr)
                failures += 1
                continue
            print(summary(d, tables, stats, feed_id, time.monotonic() - started))
            notes = {k: n for k, n in stats.items() if k != "trips" and n}
            if notes:
                print("    " + ", ".join(f"{k} {n:,}" for k, n in sorted(notes.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
