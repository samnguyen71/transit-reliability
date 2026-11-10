"""Fixtures for tests that run against a real PostgreSQL.

Set DATABASE_URL to a server where you may create databases, e.g.

    DATABASE_URL=postgresql://postgres:secret@localhost:5432/postgres

The tests never touch the database DATABASE_URL names: they create a scratch
database next to it (gtfs_test_<pid>), build the schema there, and drop it at
the end. Without DATABASE_URL every test here is skipped.

The mini feed (tests/fixtures/mini_feed/) is America/Toronto, calendar
2026-10-26 to 2027-03-21, so it spans both DST changes
(fall back 2026-11-01, spring forward 2027-03-14). Services:

    WK   Mon-Fri, calendar.txt      trip WK1   (8:00, blank, 08:20)
    DAY  every day, calendar.txt    trip NIGHT (23:50, 24:40, 25:30)
                                    trip EARLY (00:30, 01:30)
                                    trip EARLY_LATE (24:30, 25:30): EARLY +24 h,
                                         like Durham's duplicated night trips
                                    trip EARLY_ODD (24:30, 25:40): near miss, not a twin
    HOL  calendar_dates.txt only    trip HOL1  (10:00, 10:15)

calendar_dates.txt: 2026-12-25 removes WK and adds HOL.

The files hold 6 trips and 14 stop_times, all loaded as-is. EARLY_LATE is recorded
in gtfs.trip_alias as a copy of EARLY and left out of scheduled stop visits:
weekday 10, Saturday/Sunday 7, 2026-12-25 9.
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gtfsdb import load

PIPELINE = Path(__file__).resolve().parent.parent
SQL_FILES = [PIPELINE / "sql" / "01_schema.sql", PIPELINE / "sql" / "02_functions.sql"]
MINI_FEED = Path(__file__).resolve().parent / "fixtures" / "mini_feed"

# The name the logger would give a zip fetched at 2026-10-26 12:00:00 UTC.
MINI_ZIP_NAME = "20261026T120000Z.zip"

WEEKDAY_VISITS = 10
WEEKEND_VISITS = 7


@pytest.fixture(scope="session")
def scratch_dsn() -> Iterator[str]:
    """Create a scratch database for this test run and drop it afterwards."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; these tests need a real PostgreSQL")

    name = f"gtfs_test_{os.getpid()}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        dsn = make_conninfo(url, dbname=name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            for path in SQL_FILES:
                conn.execute(path.read_text(encoding="utf-8"))
        yield dsn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def conn(scratch_dsn: str):
    # autocommit, so load_zip's conn.transaction() is a real BEGIN/COMMIT rather
    # than a savepoint inside a transaction the tests opened by accident.
    with psycopg.connect(scratch_dsn, autocommit=True) as c:
        # psycopg returns timestamptz in the session time zone. In a local zone,
        # Python treats the repeated 01:xx hour on a fall-back night as never equal
        # to a datetime in another zone (PEP 495), even when it is the same instant.
        # UTC has no repeated hour, so comparisons mean what they say.
        c.execute("SET TimeZone = 'UTC'")
        yield c


def build_zip(dest: Path, edits: dict[str, Callable[[str], str]] | None = None) -> Path:
    """Zip the mini feed to dest, optionally rewriting some files first.

    Two quirks are added here rather than in the checked-in files, because
    .gitattributes turns CRLF into LF and editors tend to strip BOMs:
    calendar_dates.txt gets CRLF line endings (as Durham's does), and
    stops.txt gets a UTF-8 BOM (which must not end up in the stop_id column name).
    """
    edits = edits or {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(MINI_FEED.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            if path.name in edits:
                text = edits[path.name](text)
            data = text.encode("utf-8")
            if path.name == "calendar_dates.txt":
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if path.name == "stops.txt":
                data = b"\xef\xbb\xbf" + data
            zf.writestr(path.name, data)
    return dest


@pytest.fixture(scope="session")
def mini_zip(tmp_path_factory) -> Path:
    return build_zip(tmp_path_factory.mktemp("feeds") / MINI_ZIP_NAME)


@pytest.fixture(scope="session")
def feed_id(conn, mini_zip: Path) -> int:
    """Load the mini feed once with the real loader, including its count check."""
    load.load_zip(conn, str(mini_zip), expect_weekday=WEEKDAY_VISITS, expect_weekend=WEEKEND_VISITS)
    row = conn.execute(
        "SELECT feed_id FROM gtfs.feed WHERE sha256 = %s", (load.calculate_sha256(str(mini_zip)),)
    ).fetchone()
    assert row, "load_zip did not create a feed row"
    return row[0]
