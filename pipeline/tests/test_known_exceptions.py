"""Known exceptions: the loader keeps every row, records twins in gtfs.trip_alias,
and only the derived functions treat them differently.

See pipeline/KNOWN_EXCEPTIONS.md for the evidence behind the rule.
"""

from datetime import date

import psycopg
import pytest


def aliases(conn, feed_id: int) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT trip_id, canonical_trip_id, reason FROM gtfs.trip_alias"
        " WHERE feed_id = %s ORDER BY trip_id",
        (feed_id,),
    ).fetchall()


def scheduled_trips(conn, feed_id: int, day: date) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT trip_id FROM gtfs.scheduled_stop_visits(%s, %s)", (feed_id, day)
    )
    return {r[0] for r in rows}


def test_only_the_exact_twin_is_aliased(conn, feed_id):
    # EARLY_ODD matches EARLY +24 h at its first stop but not its second, so it is
    # a real trip; NIGHT, WK1 and HOL1 have no twin at all.
    assert aliases(conn, feed_id) == [("EARLY_LATE", "EARLY", "overnight_twin_24h")]


def test_twin_rows_are_still_loaded(conn, feed_id):
    rows = conn.execute(
        "SELECT stop_sequence, stop_id, arrival_secs FROM gtfs.stop_times"
        " WHERE feed_id = %s AND trip_id = 'EARLY_LATE' ORDER BY stop_sequence",
        (feed_id,),
    ).fetchall()
    assert rows == [(1, "A", 24 * 3600 + 30 * 60), (2, "B", 25 * 3600 + 30 * 60)]


def test_scheduled_visits_skip_the_twin(conn, feed_id):
    trips = scheduled_trips(conn, feed_id, date(2026, 10, 28))
    assert "EARLY_LATE" not in trips
    assert {"EARLY", "EARLY_ODD", "NIGHT", "WK1"} <= trips


def test_canonical_trip_id(conn, feed_id):
    def canonical(trip_id: str) -> str:
        return conn.execute(
            "SELECT gtfs.canonical_trip_id(%s, %s)", (feed_id, trip_id)
        ).fetchone()[0]

    assert canonical("EARLY_LATE") == "EARLY"
    assert canonical("EARLY") == "EARLY"
    assert canonical("EARLY_ODD") == "EARLY_ODD"
    # A trip_id the static feed doesn't know comes back unchanged, so the
    # matcher can still see (and report) it.
    assert canonical("NOT_IN_FEED") == "NOT_IN_FEED"


def test_marking_twice_fails_instead_of_duplicating(conn, feed_id):
    # The loader marks once per feed inside its transaction; a second run is a bug
    # and should say so, not quietly add rows.
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute("SELECT gtfs.mark_overnight_twins(%s)", (feed_id,))
    assert len(aliases(conn, feed_id)) == 1
