"""Gather a service date's evidence from the parsed Parquet tables (realtime.parse).

DuckDB does the heavy part, reducing about 33 million stop updates per service date to the
last fresh listing of each stop. It streams the Parquet and spills to disk past
memory_limit, so this fits on a small VM. The results come back as plain Python values for
realtime.rules.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pyarrow as pa

from realtime.rules import (
    LAST_STOP_SEARCH_S,
    Evidence,
    TuStop,
    clean_reports,
    find_gaps,
    fresh_snapshots,
)


def table_path(parsed: Path, feed: str, table: str, day: str) -> Path:
    return parsed / feed / table / f"{day}.parquet"


def connect(memory_limit: str = "250MB", temp_dir: Path | None = None,
            threads: int | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute("SET preserve_insertion_order = false")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{temp_dir.as_posix()}'")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    return con


def _files(paths: list[Path]) -> list[str]:
    return [p.as_posix() for p in paths]


def snapshots(con, paths: list[Path], stats: Counter) -> list[tuple[str, int]]:
    """The fresh snapshots of one feed, as (file, feed time in s), in fetch order."""
    rows = con.execute("""
        SELECT file, epoch_ms(fetched_at) // 1000, status, epoch_ms(header_ts) // 1000
        FROM read_parquet(?) ORDER BY fetched_at, file
    """, [_files(paths)]).fetchall()
    return fresh_snapshots(rows, stats)


def _register_files(con, name: str, kept: list[tuple[str, int]]) -> None:
    con.register(name, pa.table({"file": [f for f, _ in kept]}))


def trip_updates(con, trip_paths: list[Path], stop_paths: list[Path], start_date: str,
                 alias: dict[str, str], stats: Counter
                 ) -> tuple[dict, set, set, dict]:
    """TripUpdates evidence for trips with this start_date (YYYYMMDD), from the fresh
    snapshots registered as fresh_tu.

    A trip listed twice in one snapshot with different contents is left out of that
    snapshot (counted); identical copies count once.

    Returns (tu_stops, seen, cancelled, trip_info), keyed by canonical trip.
    """
    con.execute("""
        CREATE OR REPLACE TEMP TABLE ent AS
        SELECT t.file, t.entity_index, t.trip_id, t.route_id, t.vehicle_id,
               t.trip_schedule_relationship AS rel, epoch_ms(t.header_ts) // 1000 AS seen_at
        FROM read_parquet(?) t SEMI JOIN fresh_tu f ON t.file = f.file
        WHERE t.start_date = ? AND t.trip_id IS NOT NULL
    """, [_files(trip_paths), start_date])

    con.execute("""
        CREATE OR REPLACE TEMP TABLE dup AS
        SELECT file, trip_id FROM ent GROUP BY ALL HAVING count(*) > 1
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE conflict AS
        WITH content AS (
            SELECT e.file, e.trip_id, e.entity_index,
                   md5(coalesce(e.rel, '') || CAST(list(struct_pack(
                       q := s.stop_sequence, id := s.stop_id, a := s.arrival_time,
                       d := s.departure_time, r := s.schedule_relationship)
                       ORDER BY s.update_index) AS VARCHAR)) AS h
            FROM ent e SEMI JOIN dup USING (file, trip_id)
            LEFT JOIN read_parquet(?) s ON s.file = e.file AND s.entity_index = e.entity_index
            GROUP BY e.file, e.trip_id, e.entity_index, e.rel
        )
        SELECT file, trip_id FROM content GROUP BY ALL HAVING count(DISTINCT h) > 1
    """, [_files(stop_paths)])
    stats["tu trip copies in one snapshot, identical"] += con.execute(
        "SELECT count(*) FROM dup ANTI JOIN conflict USING (file, trip_id)").fetchone()[0]
    stats["tu trip copies in one snapshot, conflicting (left out)"] += con.execute(
        "SELECT count(*) FROM conflict").fetchone()[0]

    con.execute("""
        CREATE OR REPLACE TEMP TABLE keep AS
        SELECT * FROM ent ANTI JOIN conflict USING (file, trip_id)
        QUALIFY row_number() OVER (PARTITION BY file, trip_id ORDER BY entity_index) = 1
    """)

    trip_info: dict = {}
    seen: set = set()
    cancelled: set = set()
    for trip_id, route_id, rels, first, last, vehicle in con.execute("""
        SELECT trip_id, any_value(route_id), list(DISTINCT rel), min(seen_at), max(seen_at),
               arg_max(vehicle_id, seen_at)
        FROM keep GROUP BY trip_id
    """).fetchall():
        key = (alias.get(trip_id, trip_id), start_date)
        seen.add(key)
        rels = [r for r in rels if r is not None]
        if {"CANCELED", "DELETED"} & set(rels):
            cancelled.add(key)
        trip_info[trip_id] = {"route_id": route_id, "relationships": rels,
                              "first_seen": first, "last_seen": last, "vehicle_id": vehicle}

    # The last listing of each stop. arg_max skips rows whose value is NULL, which would
    # let an older time stand in for a last listing without one, so take a whole struct.
    tu_stops: dict = {}
    for trip_id, seq, last in con.execute("""
        SELECT k.trip_id, s.stop_sequence,
               arg_max(struct_pack(seen := k.seen_at,
                                   arr := epoch_ms(s.arrival_time) // 1000,
                                   srel := s.schedule_relationship), k.seen_at)
        FROM read_parquet(?) s JOIN keep k ON s.file = k.file AND s.entity_index = k.entity_index
        GROUP BY k.trip_id, s.stop_sequence
    """, [_files(stop_paths)]).fetchall():
        if seq is None:
            stats["tu stop updates without stop_sequence"] += 1
            continue
        seen_at, time, rel = last["seen"], last["arr"], last["srel"]
        stops = tu_stops.setdefault((alias.get(trip_id, trip_id), start_date), {})
        old = stops.get(seq)
        if old is None or seen_at > old.seen_at:  # both copies of a twin listed: latest wins
            stops[seq] = TuStop(seen_at, time, rel == "SKIPPED")
    for table in ("ent", "dup", "conflict", "keep"):
        con.execute(f"DROP TABLE {table}")
    return tu_stops, seen, cancelled, trip_info


def vehicle_rows(con, paths: list[Path], start_date: str, stats: Counter) -> Iterator[tuple]:
    """VehiclePositions rows from the fresh snapshots registered as fresh_vp, grouped by
    vehicle in fetch order, streamed.

    Only the vehicles that ran a trip with this start_date, and only from their first such
    report to LAST_STOP_SEARCH_S after their last: that is all the rule looks at, and it
    leaves out the next date's service on the second UTC day.
    """
    files = _files(paths)
    stats["vp no timestamp"] += con.execute("""
        SELECT count(*) FROM read_parquet(?) v SEMI JOIN fresh_vp f ON v.file = f.file
        WHERE v.timestamp IS NULL
    """, [files]).fetchone()[0]
    cur = con.execute("""
        WITH v AS (
            SELECT v.*, epoch_ms(v.timestamp) // 1000 AS t
            FROM read_parquet(?) v SEMI JOIN fresh_vp f ON v.file = f.file
            WHERE v.timestamp IS NOT NULL AND v.vehicle_id IS NOT NULL
        ), spans AS (
            SELECT vehicle_id, min(t) AS first_t, max(t) + ? AS last_t
            FROM v WHERE start_date = ? GROUP BY vehicle_id
        )
        SELECT v.vehicle_id, v.t, v.trip_id, v.start_date, v.current_stop_sequence,
               v.current_status = 'STOPPED_AT', v.latitude, v.longitude
        FROM v JOIN spans s ON v.vehicle_id = s.vehicle_id AND v.t BETWEEN s.first_t AND s.last_t
        ORDER BY v.vehicle_id, v.file, v.entity_index
    """, [files, LAST_STOP_SEARCH_S, start_date])
    while rows := cur.fetchmany(20_000):
        yield from rows


def gather(con, parsed: Path, days: list[str], start_date: str,
           gap_window: tuple[int, int], alias: dict[str, str],
           tu_feed: str = "trip_updates", vp_feed: str = "vehicle_positions",
           ) -> tuple[Evidence, dict, Counter]:
    """All evidence for one service date. days: the UTC days to read. gap_window: the span
    the gap lists cover."""
    stats: Counter = Counter()

    def paths(feed: str, table: str) -> list[Path]:
        return [table_path(parsed, feed, table, d) for d in days]

    vp_stats: Counter = Counter()
    tu_stats: Counter = Counter()
    vp_kept = snapshots(con, paths(vp_feed, "snapshots"), vp_stats)
    tu_kept = snapshots(con, paths(tu_feed, "snapshots"), tu_stats)
    stats.update({f"vp {k}": n for k, n in vp_stats.items()})
    stats.update({f"tu {k}": n for k, n in tu_stats.items()})
    _register_files(con, "fresh_vp", vp_kept)
    _register_files(con, "fresh_tu", tu_kept)

    ev = Evidence(
        vp_gaps=find_gaps([t for _, t in vp_kept], *gap_window),
        tu_gaps=find_gaps([t for _, t in tu_kept], *gap_window),
    )
    ev.tu_stops, ev.tu_seen, ev.cancelled, trip_info = trip_updates(
        con, paths(tu_feed, "trip_updates"), paths(tu_feed, "stop_time_updates"),
        start_date, alias, stats)
    rows = vehicle_rows(con, paths(vp_feed, "vehicle_positions"), start_date, stats)
    ev.by_vehicle, ev.by_trip = clean_reports(rows, alias, stats)
    return ev, trip_info, stats
