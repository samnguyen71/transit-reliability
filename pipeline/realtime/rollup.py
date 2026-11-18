"""Reliability rollups: on-time counts by route, stop and hour, per service date.

    python -m realtime.rollup OBSERVED_DIR OUT_DIR [--date YYYY-MM-DD ...] [--force]

Reads realtime.observe's OBSERVED_DIR/visits/<date>.parquet and writes
OUT_DIR/<table>/<date>.parquet for each table in realtime.schemas.ROLLUP_TABLES. No
database: everything needed is in the visits file.

- Scored visits only (OBSERVED_TIMES.md, "What is scored"). On time: a departure from
  EARLY_S early to LATE_S late; an arrival at the last stop no more than LATE_S late.
- Counts add up across rows and dates. To get a route's month, sum its rows.
- A date is skipped if its rollups were made by this ROLLUP_VERSION from the visits file
  as it is now (same rule_version and written_at). --force redoes it.
- Small: a day's visits are about 100,000 rows, so this runs in seconds in under 100 MB.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from realtime.schemas import ROLLUP_TABLES

ROLLUP_VERSION = "2"
EARLY_S = 60    # a departure more than this early is early
LATE_S = 300    # more than this late is late (departures and last-stop arrivals)

KEYS = {
    "by_route": "route_id, direction_id",
    "by_stop": "stop_id, route_id",
    "by_hour": "hour, route_id, direction_id",
}


def visits_path(observed: Path, d: date) -> Path:
    return observed / "visits" / f"{d.isoformat()}.parquet"


def output_paths(out: Path, d: date) -> dict[str, Path]:
    return {name: out / name / f"{d.isoformat()}.parquet" for name in ROLLUP_TABLES}


def observed_dates(observed: Path) -> list[date]:
    return sorted(date.fromisoformat(p.stem) for p in (observed / "visits").glob("*.parquet"))


def _source_meta(observed: Path, d: date) -> dict[bytes, bytes]:
    meta = pq.read_schema(visits_path(observed, d)).metadata or {}
    return {k: v for k, v in meta.items() if k in (b"rule_version", b"written_at")}


def is_done(observed: Path, out: Path, d: date) -> bool:
    paths = list(output_paths(out, d).values())
    if not all(p.exists() for p in paths):
        return False
    want = {b"rollup_version": ROLLUP_VERSION.encode(), **_source_meta(observed, d)}
    for p in paths:
        meta = pq.read_schema(p).metadata or {}
        if any(meta.get(k) != v for k, v in want.items()):
            return False
    return True


def rollup_date(con, observed: Path, d: date, timezone: str) -> dict[str, pa.Table]:
    """The rollup tables for one service date."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE v AS
        SELECT *,
               CASE WHEN event <> 'departure' THEN 'last_stop'
                    WHEN stop_sequence = min(stop_sequence) OVER (PARTITION BY trip_id)
                    THEN 'first_stop' ELSE 'timepoint' END AS kind,
               hour(scheduled AT TIME ZONE ?)::INTEGER AS hour
        FROM read_parquet(?)
    """, [timezone, visits_path(observed, d).as_posix()])
    # kind needs each trip's lowest stop_sequence, so v keeps unscored visits too and
    # the queries below filter on scored.
    counts = f"""
        count(*)::INTEGER AS scheduled,
        count(*) FILTER (status = 'observed')::INTEGER AS observed,
        count(*) FILTER (status = 'observed' AND delay_s <= {LATE_S}
                         AND (kind = 'last_stop' OR delay_s >= -{EARLY_S}))::INTEGER AS on_time,
        count(*) FILTER (status = 'observed' AND kind <> 'last_stop'
                         AND delay_s < -{EARLY_S})::INTEGER AS early,
        count(*) FILTER (status = 'observed' AND delay_s > {LATE_S})::INTEGER AS late,
        count(*) FILTER (status = 'cancelled')::INTEGER AS cancelled,
        count(*) FILTER (status = 'skipped')::INTEGER AS skipped,
        count(*) FILTER (status = 'lost')::INTEGER AS lost,
        count(*) FILTER (status = 'no_data')::INTEGER AS no_data,
        count(*) FILTER (status = 'not_seen')::INTEGER AS not_seen,
        round(quantile_cont(delay_s, 0.5) FILTER (status = 'observed'))::INTEGER
            AS median_delay_s,
        round(quantile_cont(delay_s, 0.9) FILTER (status = 'observed'))::INTEGER
            AS p90_delay_s
    """
    tables = {}
    for name, keys in KEYS.items():
        t = con.execute(f"""
            SELECT ?::DATE AS service_date, {keys}, kind, {counts}
            FROM v WHERE scored
            GROUP BY {keys}, kind ORDER BY {keys}, kind
        """, [d]).to_arrow_table()
        tables[name] = t.select(ROLLUP_TABLES[name].names).cast(ROLLUP_TABLES[name])
    con.execute("DROP TABLE v")
    return tables


def write_outputs(out: Path, d: date, tables: dict[str, pa.Table],
                  meta: dict[bytes, bytes]) -> None:
    """Write every table to a temp file first, then rename them all."""
    paths = output_paths(out, d)
    tmps = {}
    try:
        for name, table in tables.items():
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            tmps[name] = paths[name].with_name(paths[name].name + ".tmp")
            pq.write_table(table.replace_schema_metadata(meta), tmps[name], compression="zstd")
    except BaseException:
        for tmp in tmps.values():
            tmp.unlink(missing_ok=True)
        raise
    for name, tmp in tmps.items():
        os.replace(tmp, paths[name])


def summary(d: date, tables: dict[str, pa.Table]) -> str:
    t = tables["by_hour"]  # every scored visit is in exactly one hour row
    n = {c: sum(t.column(c).to_pylist()) for c in ("scheduled", "observed", "on_time", "late")}
    share = f"{n['on_time'] / n['observed']:.1%}" if n["observed"] else "n/a"
    rows = ", ".join(f"{name} {tables[name].num_rows}" for name in ROLLUP_TABLES)
    return (f"{d}: on time {share} of {n['observed']:,} observed "
            f"({n['scheduled']:,} scored, {n['late']:,} late); rows: {rows}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m realtime.rollup", description=__doc__.split("\n")[0])
    ap.add_argument("observed", type=Path, help="realtime.observe's output folder")
    ap.add_argument("out", type=Path, help="where to write <table>/<date>.parquet")
    ap.add_argument("--date", action="append", type=date.fromisoformat,
                    help="only this service date (repeatable)")
    ap.add_argument("--force", action="store_true", help="redo dates already done")
    ap.add_argument("--timezone", default="America/Toronto",
                    help="the agency's timezone, for by_hour (default America/Toronto)")
    args = ap.parse_args(argv)

    con = duckdb.connect()
    con.execute("SET memory_limit = '100MB'")
    con.execute("SET threads = 1")
    failed = 0
    for d in args.date or observed_dates(args.observed):
        if not visits_path(args.observed, d).exists():
            print(f"{d}: no visits file, observe it first", file=sys.stderr)
            failed += 1
            continue
        if not args.force and is_done(args.observed, args.out, d):
            continue
        try:
            tables = rollup_date(con, args.observed, d, args.timezone)
            meta = {b"rollup_version": ROLLUP_VERSION.encode(),
                    **_source_meta(args.observed, d)}
            write_outputs(args.out, d, tables, meta)
        except Exception as e:  # noqa: BLE001 - report the date and go on to the next
            print(f"{d}: failed, nothing written: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(summary(d, tables))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
