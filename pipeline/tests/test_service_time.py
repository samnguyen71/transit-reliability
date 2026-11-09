"""gtfs.service_time and gtfs.parse_secs, evaluated by PostgreSQL itself.

GTFS times count from "noon minus 12 hours" on the service date, not from
midnight. On most days the two are the same; on DST days they differ by an
hour, and times past 24:00:00 land on the next calendar day. Expected values
are written in UTC (with local time in the comment) so there is nothing for
Python's time zone handling to get wrong on the way.
"""

from datetime import UTC, date, datetime

import pytest

TZ = "America/Toronto"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def hms(h: int, m: int = 0, s: int = 0) -> int:
    return h * 3600 + m * 60 + s


def service_time(conn, service_date: date, secs: int | None) -> datetime | None:
    return conn.execute(
        "SELECT gtfs.service_time(%s, %s, %s)", (service_date, secs, TZ)
    ).fetchone()[0]


@pytest.mark.parametrize(
    ("service_date", "secs", "expected"),
    [
        # An ordinary EDT day: 08:00 EDT.
        (date(2026, 10, 28), hms(8), utc(2026, 10, 28, 12)),
        # An ordinary EST day: 08:00 EST.
        (date(2026, 12, 1), hms(8), utc(2026, 12, 1, 13)),
        # 25:30 belongs to the 28th's service but happens at 01:30 EDT on the 29th.
        (date(2026, 10, 28), hms(25, 30), utc(2026, 10, 29, 5, 30)),
        # Durham's latest time, 30:11 on route 901: 06:11 EDT the next morning.
        (date(2026, 10, 28), hms(30, 11), utc(2026, 10, 29, 10, 11)),
        # Daytime on both DST days is the wall-clock time you'd expect.
        (date(2026, 11, 1), hms(8), utc(2026, 11, 1, 13)),   # 08:00 EST
        (date(2027, 3, 14), hms(8), utc(2027, 3, 14, 12)),   # 08:00 EDT
    ],
)
def test_service_time(conn, service_date, secs, expected):
    assert service_time(conn, service_date, secs) == expected


def test_fall_back_day_starts_at_one_am(conn):
    # On 2026-11-01 noon is EST, so "noon minus 12 h" is 05:00 UTC = 01:00 EDT.
    # 00:00:00 on that service date is 01:00 EDT, not midnight.
    assert service_time(conn, date(2026, 11, 1), 0) == utc(2026, 11, 1, 5)


def test_spring_forward_day_starts_the_evening_before(conn):
    # On 2027-03-14 noon is EDT, so "noon minus 12 h" is 04:00 UTC = 23:00 EST
    # on the 13th: 00:00:00 on that service date is the previous evening.
    assert service_time(conn, date(2027, 3, 14), 0) == utc(2027, 3, 14, 4)


def test_fall_back_night_has_two_one_fifteens(conn):
    # Durham runs each night trip twice in the Overnight service: once as 01:15
    # and once as 25:15. On the fall-back night the two copies read 01:15 on a
    # clock but are an hour apart, so de-duplicating by timestamp misses them.
    late_copy = service_time(conn, date(2026, 10, 31), hms(25, 15))   # 01:15 EDT
    early_copy = service_time(conn, date(2026, 11, 1), hms(1, 15))    # 01:15 EST
    assert late_copy == utc(2026, 11, 1, 5, 15)
    assert early_copy == utc(2026, 11, 1, 6, 15)


def test_session_time_zone_does_not_matter(conn):
    # The function takes the feed's zone as an argument, so the server's or
    # client's TimeZone setting must not change the instant it returns.
    def epoch() -> float:
        return conn.execute(
            "SELECT extract(epoch FROM gtfs.service_time('2026-11-01', 5400, %s))", (TZ,)
        ).fetchone()[0]

    expected = epoch()
    try:
        for zone in ("Asia/Tokyo", "America/Los_Angeles", "Europe/London"):
            conn.execute(f"SET TimeZone = '{zone}'")
            assert epoch() == expected
    finally:
        conn.execute("SET TimeZone = 'UTC'")


def test_null_secs_gives_null(conn):
    assert service_time(conn, date(2026, 10, 28), None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("08:00:00", hms(8)),
        ("8:00:00", hms(8)),           # the spec allows a one-digit hour
        ("00:00:00", 0),
        ("24:00:00", 86400),
        ("25:30:00", hms(25, 30)),
        ("30:11:00", hms(30, 11)),
        ("23:59:59", 86399),
        ("", None),                    # intermediate stops may be blank
        (None, None),
    ],
)
def test_parse_secs(conn, text, expected):
    assert conn.execute("SELECT gtfs.parse_secs(%s)", (text,)).fetchone()[0] == expected
