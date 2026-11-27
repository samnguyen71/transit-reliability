"""A scratch Postgres with the pipeline's schema and a tiny hand-built static GTFS, for the
static endpoints. Skipped without DATABASE_URL, as in pipeline/tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

SQL_DIR = Path(__file__).resolve().parents[2] / "pipeline" / "sql"

# Three feeds. "old" has service today but is older than "current"; "future" is newer
# but has no service until 2027. So gtfs.feed_for(today) must pick "current".
FEEDS = [("old", "2026-10-01T12:00Z", "2026-10-01", "2026-11-29"),
         ("current", "2026-11-01T12:00Z", "2026-11-01", "2026-12-31"),
         ("future", "2026-11-20T12:00Z", "2027-01-01", "2027-06-30")]

ROUTES = [  # route_id, short, long, color (char(6): blank pads, NULL stays NULL)
    ("10", "10", "Simcoe", "006633"),
    ("101", "101", "Express", "FF0000"),
    ("2", "2", "No trips", None),
    ("N1", "N1", "Night", None),
]
STOPS = [  # stop_id, name, code
    ("1", "Garden Northbound @ Bassett", "1001"),
    ("2", "Simcoe Southbound @ King", "1002"),
    ("3", "King Westbound @ Simcoe", "1003"),
    ("4", "Oshawa Centre Terminal", "1004"),
    ("5", "Bassett Eastbound @ Garden", "1005"),
    ("6", "Gardenia Court", "1006"),
]
H = 3600
TRIPS = [  # trip_id, route, service, direction, headsign, shape, [(stop, dep_secs, tp)]
    ("t1", "10", "WK", 0, "Oshawa Centre", "S0", [("1", 8 * H, 1), ("2", 8 * H + 600, 0),
                                                  ("4", 8 * H + 1200, 1)]),
    ("t2", "10", "WK", 0, "Oshawa Centre", "S0", [("1", 9 * H, 1), ("2", 9 * H + 600, 0),
                                                  ("4", 9 * H + 1200, 1)]),
    # Short turn: earlier, but a less common pattern.
    ("t4", "10", "WK", 0, "Simcoe", "S0b", [("1", 7 * H, 1), ("2", 7 * H + 600, 1)]),
    ("t5", "10", "WK", 1, "Garden", "S1", [("4", 10 * H, 1), ("3", 10 * H + 600, 0),
                                           ("1", 10 * H + 1200, 1)]),
    ("t8", "101", "WK", 0, None, None, [("2", 12 * H, 1), ("3", 12 * H + 300, 1)]),
    ("t6", "N1", "WE", 0, "Night", None, [("1", 1 * H, 1), ("5", 1 * H + 600, 1)]),
    # Starts at 24:30, so it runs at 00:30 the next day and DRT sends that day as its
    # start_date. Its service, LT, runs only on Tuesday 2026-12-01 (calendar_dates), so
    # the static endpoints' dates never see it.
    ("t9", "10", "LT", 0, "Late", None, [("1", 24 * H + 1800, 1), ("2", 24 * H + 2400, 1)]),
]
CALENDAR_DATES = [("LT", "2026-12-01", 1)]  # service_id, date, exception_type
# +24 h copies, as DRT ships in Overnight; gtfs.mark_overnight_twins records them.
TWINS = {"t3": "t1", "t7": "t6"}
SHAPES = {"S0": [(1, -78.90, 43.90), (2, -78.91, 43.91), (3, -78.92, 43.92)],
          "S0b": [(1, -78.90, 43.90), (2, -78.91, 43.91)],
          "S1": [(1, -78.92, 43.92), (2, -78.90, 43.90)]}


@pytest.fixture(scope="session")
def static_dsn() -> Iterator[str]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; these tests need a real PostgreSQL")
    name = f"api_test_{os.getpid()}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(url, dbname=name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for f in ("01_schema.sql", "02_functions.sql"):
                conn.execute((SQL_DIR / f).read_text(encoding="utf-8"))
            with conn.transaction():
                load(conn)
        yield dsn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                          .format(sql.Identifier(name)))


def load(conn: psycopg.Connection) -> None:
    ids = {}
    for name, fetched, start, end in FEEDS:
        ids[name] = feed = conn.execute(
            "INSERT INTO gtfs.feed (sha256, zip_path, fetched_at, timezone) "
            "VALUES (%s, %s, %s, 'America/Toronto') RETURNING feed_id",
            [name, f"{name}.zip", fetched]).fetchone()[0]
        conn.execute("INSERT INTO gtfs.calendar VALUES "
                     "(%(f)s, 'WK', true, true, true, true, true, false, false, %(s)s, %(e)s),"
                     "(%(f)s, 'WE', false, false, false, false, false, true, true, %(s)s, %(e)s)",
                     {"f": feed, "s": start, "e": end})
    for name, stop in (("old", "Oldfield Terminal"), ("future", "Future Terminal")):
        conn.execute("INSERT INTO gtfs.stops (feed_id, stop_id, stop_name, stop_lat, stop_lon) "
                     "VALUES (%s, '1', %s, 43.9, -78.9)", [ids[name], stop])

    f = ids["current"]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO gtfs.routes (feed_id, route_id, route_short_name, "
                        "route_long_name, route_type, route_color) VALUES (%s, %s, %s, %s, 3, %s)",
                        [(f, *r) for r in ROUTES])
        cur.executemany("INSERT INTO gtfs.stops (feed_id, stop_id, stop_name, stop_lat, stop_lon, "
                        "stop_code) VALUES (%s, %s, %s, 43.9, -78.9, %s)",
                        [(f, *s) for s in STOPS])
        cur.executemany("INSERT INTO gtfs.calendar_dates VALUES (%s, %s, %s, %s)",
                        [(f, *c) for c in CALENDAR_DATES])
        cur.executemany("INSERT INTO gtfs.shapes VALUES (%s, %s, %s, %s, %s, NULL)",
                        [(f, sid, lat, lon, seq) for sid, pts in SHAPES.items()
                         for seq, lon, lat in reversed(pts)])
        twins = [(t, *rest) for t, canon in TWINS.items()
                 for tid, *rest in TRIPS if tid == canon]
        for trip_id, route, service, direction, headsign, shape, times in TRIPS + twins:
            day = 86400 if trip_id in TWINS else 0
            cur.execute("INSERT INTO gtfs.trips (feed_id, trip_id, route_id, service_id, "
                        "direction_id, shape_id, trip_headsign) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        [f, trip_id, route, service, direction, shape, headsign])
            cur.executemany("INSERT INTO gtfs.stop_times (feed_id, trip_id, stop_sequence, "
                            "stop_id, arrival_secs, departure_secs, timepoint) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [(f, trip_id, seq, stop, dep + day, dep + day, tp)
                             for seq, (stop, dep, tp) in enumerate(times, start=1)])
    assert conn.execute("SELECT gtfs.mark_overnight_twins(%s)", [f]).fetchone()[0] == len(TWINS)
