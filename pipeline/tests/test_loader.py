"""The real loader, run on the mini feed.

The feed_id fixture (conftest.py) has already loaded it once, including the
loader's own visit-count check with the expected 8 weekday / 5 weekend visits.
"""

from datetime import UTC, datetime

import pytest
from conftest import build_zip

from gtfsdb import load


def count(conn, table: str, feed_id: int) -> int:
    return conn.execute(
        f"SELECT count(*) FROM gtfs.{table} WHERE feed_id = %s", (feed_id,)
    ).fetchone()[0]


def feed_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM gtfs.feed").fetchone()[0]


def test_feed_row(conn, feed_id, mini_zip):
    sha, zip_path, fetched_at, tz = conn.execute(
        "SELECT sha256, zip_path, fetched_at, timezone FROM gtfs.feed WHERE feed_id = %s",
        (feed_id,),
    ).fetchone()
    assert sha == load.calculate_sha256(str(mini_zip))
    assert zip_path.endswith("20261026T120000Z.zip")
    assert fetched_at == datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert tz == "America/Toronto"


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("agency", 1),
        ("stops", 3),
        ("routes", 2),
        ("trips", 4),
        ("stop_times", 10),
        ("calendar", 2),
        ("calendar_dates", 2),   # this file has CRLF line endings in the zip
    ],
)
def test_row_counts(conn, feed_id, table, expected):
    assert count(conn, table, feed_id) == expected


def test_missing_agency_id_becomes_empty_string(conn, feed_id):
    rows = conn.execute(
        "SELECT agency_id, agency_timezone FROM gtfs.agency WHERE feed_id = %s", (feed_id,)
    ).fetchall()
    assert rows == [("", "America/Toronto")]


def test_bom_does_not_break_first_column(conn, feed_id):
    # stops.txt starts with a UTF-8 BOM; stop_id must still be read.
    rows = conn.execute(
        "SELECT stop_id, stop_name, stop_lat FROM gtfs.stops WHERE feed_id = %s ORDER BY stop_id",
        (feed_id,),
    ).fetchall()
    assert [r[0] for r in rows] == ["A", "B", "C"]
    assert rows[0][1:] == ("Alpha", 43.8971)


def test_casts(conn, feed_id):
    trip = conn.execute(
        "SELECT route_id, service_id, direction_id, block_id, shape_id"
        " FROM gtfs.trips WHERE feed_id = %s AND trip_id = 'NIGHT'",
        (feed_id,),
    ).fetchone()
    # block_id and shape_id are absent from trips.txt and should come out NULL.
    assert trip == ("R2", "DAY", 1, None, None)

    st = conn.execute(
        "SELECT arrival_secs, departure_secs, timepoint, pickup_type, drop_off_type"
        " FROM gtfs.stop_times WHERE feed_id = %s AND trip_id = 'NIGHT' AND stop_sequence = 3",
        (feed_id,),
    ).fetchone()
    assert st == (91800, 91800, 1, 1, 0)

    cal = conn.execute(
        "SELECT monday, saturday, sunday, start_date::text, end_date::text"
        " FROM gtfs.calendar WHERE feed_id = %s AND service_id = 'WK'",
        (feed_id,),
    ).fetchone()
    assert cal == (True, False, False, "2026-10-26", "2027-03-21")


def test_same_zip_twice_is_a_no_op(conn, feed_id, mini_zip):
    before = feed_count(conn)
    load.load_zip(conn, str(mini_zip))
    assert feed_count(conn) == before


def test_bad_row_rolls_back_the_whole_feed(conn, feed_id, tmp_path):
    # A stop_time at a stop missing from stops.txt fails the foreign key,
    # after feed, agency, calendar, ... have already been inserted.
    bad = build_zip(
        tmp_path / "20261027T000000Z.zip",
        {"stop_times.txt": lambda t: t + "HOL1,10:30:00,10:30:00,ZZZ,3,0,0,1\n"},
    )
    before = feed_count(conn)
    with pytest.raises(Exception, match="foreign key"):
        load.load_zip(conn, str(bad))
    assert feed_count(conn) == before


def test_wrong_visit_count_rolls_back(conn, feed_id, tmp_path):
    other = build_zip(
        tmp_path / "20261028T000000Z.zip",
        {"agency.txt": lambda t: t.replace("Mini Transit", "Mini Transit 2")},
    )
    before = feed_count(conn)
    with pytest.raises(ValueError, match="expected 9"):
        load.load_zip(conn, str(other), expect_weekday=9)
    assert feed_count(conn) == before


def test_name_without_timestamp_uses_mtime(tmp_path):
    path = build_zip(tmp_path / "durham.zip")
    got = load.fetched_at_from_name(str(path))
    assert got.tzinfo is not None
    assert abs(got.timestamp() - path.stat().st_mtime) < 1
