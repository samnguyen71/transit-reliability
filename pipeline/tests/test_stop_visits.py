"""gtfs.active_services and gtfs.scheduled_stop_visits on the mini feed.

See conftest.py for what the mini feed contains.
"""

from datetime import UTC, date, datetime

import pytest
from conftest import WEEKDAY_VISITS, WEEKEND_VISITS


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def active(conn, feed_id: int, day: date) -> set[str]:
    rows = conn.execute("SELECT service_id FROM gtfs.active_services(%s, %s)", (feed_id, day))
    return {r[0] for r in rows}


def visits(conn, feed_id: int, day: date) -> dict[tuple[str, int], tuple]:
    """(trip_id, stop_sequence) -> (service_date, stop_id, arrival_secs, arrival_at)."""
    rows = conn.execute(
        """SELECT trip_id, stop_sequence, service_date, stop_id, arrival_secs, arrival_at
           FROM gtfs.scheduled_stop_visits(%s, %s)""",
        (feed_id, day),
    )
    return {(r[0], r[1]): r[2:] for r in rows}


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 28), {"WK", "DAY"}),   # Wednesday
        (date(2026, 10, 31), {"DAY"}),         # Saturday
        (date(2026, 11, 1), {"DAY"}),          # Sunday
        (date(2026, 12, 25), {"DAY", "HOL"}),  # Friday: WK removed, HOL added
        (date(2026, 10, 26), {"WK", "DAY"}),   # first day of the calendar
        (date(2027, 3, 21), {"DAY"}),          # last day (a Sunday)
        (date(2026, 10, 25), set()),           # before the calendar
        (date(2027, 3, 22), set()),            # after it
    ],
)
def test_active_services(conn, feed_id, day, expected):
    assert active(conn, feed_id, day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 28), WEEKDAY_VISITS),       # WK1 3 + NIGHT 3 + EARLY 2 + EARLY_ODD 2
        (date(2026, 10, 31), WEEKEND_VISITS),       # NIGHT 3 + EARLY 2 + EARLY_ODD 2
        (date(2026, 12, 25), WEEKEND_VISITS + 2),   # DAY 7 + HOL1 2
        (date(2027, 3, 22), 0),
    ],
)
def test_visit_counts(conn, feed_id, day, expected):
    assert len(visits(conn, feed_id, day)) == expected


def test_holiday_only_service_appears(conn, feed_id):
    v = visits(conn, feed_id, date(2026, 12, 25))
    assert v[("HOL1", 1)][3] == utc(2026, 12, 25, 15)      # 10:00 EST
    assert ("WK1", 1) not in v


def test_past_midnight_stays_on_its_service_date(conn, feed_id):
    v = visits(conn, feed_id, date(2026, 10, 28))
    service_date, stop_id, secs, at = v[("NIGHT", 3)]
    assert service_date == date(2026, 10, 28)
    assert (stop_id, secs) == ("A", 25 * 3600 + 30 * 60)
    assert at == utc(2026, 10, 29, 5, 30)                  # 01:30 EDT on the 29th


def test_blank_time_gives_null(conn, feed_id):
    _, stop_id, secs, at = visits(conn, feed_id, date(2026, 10, 28))[("WK1", 2)]
    assert (stop_id, secs, at) == ("B", None, None)


def test_single_digit_hour_is_parsed(conn, feed_id):
    # WK1 starts at "8:00:00" in stop_times.txt.
    assert visits(conn, feed_id, date(2026, 10, 28))[("WK1", 1)][3] == utc(2026, 10, 28, 12)


def test_fall_back_night(conn, feed_id):
    # Matching realtime for the night of Oct 31 -> Nov 1 needs both service dates.
    sat = visits(conn, feed_id, date(2026, 10, 31))
    sun = visits(conn, feed_id, date(2026, 11, 1))

    # NIGHT's 25:30 from Saturday's service and EARLY's 00:30 from Sunday's land
    # on the same instant: Sunday's service day starts at 01:00 EDT, not midnight.
    assert sat[("NIGHT", 3)][3] == utc(2026, 11, 1, 5, 30)   # 01:30 EDT
    assert sun[("EARLY", 1)][3] == utc(2026, 11, 1, 5, 30)   # 01:30 EDT

    # EARLY's 01:30 is the second 01:30 of the night.
    assert sun[("EARLY", 2)][3] == utc(2026, 11, 1, 6, 30)   # 01:30 EST


def test_spring_forward_night(conn, feed_id):
    # On 2027-03-14 the service day starts at 23:00 EST on the 13th, so EARLY's
    # 00:30 happens on the previous calendar day.
    sun = visits(conn, feed_id, date(2027, 3, 14))
    assert sun[("EARLY", 1)][3] == utc(2027, 3, 14, 4, 30)   # 23:30 EST on the 13th
    assert sun[("EARLY", 2)][3] == utc(2027, 3, 14, 5, 30)   # 00:30 EST
