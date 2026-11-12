"""realtime.rules: observed-times rule v1, one edge case per test. No files, no database.

The trip in most tests, T1 on 2026-11-05, has four stops five minutes apart, all
timepoints, starting 08:00 Toronto (13:00Z). Stops are about 1.1 km apart, so only a report
placed at a stop's coordinates is "near" it.
"""

from collections import Counter

from realtime.rules import (
    GAP_S,
    Evidence,
    Report,
    Stop,
    Trip,
    TuStop,
    clean_reports,
    find_gaps,
    fresh_snapshots,
    observe_trip,
)

D = "20261105"
T0 = 1793883600  # 2026-11-05 13:00:00Z
KEY = ("T1", D)


def stop_at(i: int) -> tuple[float, float]:
    return 43.90 + 0.01 * i, -78.86


def make_trip(n: int = 4, key=KEY, last_at: int | None = None) -> Trip:
    stops = []
    for i in range(1, n + 1):
        lat, lon = stop_at(last_at if (i == n and last_at) else i)
        t = T0 + (i - 1) * 300
        stops.append(Stop(i, f"S{i}", t, t, True, lat, lon))
    return Trip(key, "R1", 0, stops)


def row(t, seq, stopped=False, trip="T1", vehicle="V1", at=None, start_date=D):
    """A VehiclePositions row as realtime.evidence returns it. at: place it at stop `at`."""
    lat, lon = stop_at(at) if at else (None, None)
    return (vehicle, t, trip, start_date, seq, stopped, lat, lon)


def standard_run() -> list[tuple]:
    """Stops at each stop for a minute, reports every 30 s around the stops."""
    rows = []
    for i in range(1, 5):
        t = T0 + (i - 1) * 300
        rows += [row(t - 60, i), row(t - 30, i, True), row(t, i, True)]
        if i < 4:
            rows.append(row(t + 30, i + 1))
    return rows


def evidence(rows=(), tu=None, cancelled=(), vp_gaps=(), tu_gaps=(), tu_seen=(),
             alias=None) -> Evidence:
    rows = sorted(rows, key=lambda r: r[0])  # grouped by vehicle, order kept within one
    by_vehicle, by_trip = clean_reports(rows, alias or {})
    tu = tu or {}
    return Evidence(list(vp_gaps), list(tu_gaps), by_vehicle, by_trip,
                    {KEY: tu} if tu else {}, set(tu_seen) | ({KEY} if tu else set()),
                    set(cancelled))


def by_seq(visits):
    return {v.seq: v for v in visits}


# Departures ----------------------------------------------------------------------------

def test_tu_time_inside_the_bracket_is_used():
    tu = {1: TuStop(seen_at=T0 + 40, time=T0 + 10, skipped=False)}
    v = by_seq(observe_trip(make_trip(), evidence(standard_run(), tu)))[1]
    assert (v.status, v.source, v.time) == ("observed", "tu", T0 + 10)
    assert (v.lo, v.hi) == (T0, T0 + 30)
    assert v.disagree is False and v.scored and v.event == "departure"
    assert v.vehicle_id == "V1"


def test_tu_time_outside_the_bracket_loses_to_vp():
    tu = {2: TuStop(seen_at=T0 + 400, time=T0 + 300 - 200, skipped=False)}
    v = by_seq(observe_trip(make_trip(), evidence(standard_run(), tu)))[2]
    assert (v.status, v.source, v.time) == ("observed", "vp", T0 + 330)
    assert v.disagree is True and v.tu_time == T0 + 100


def test_tu_prediction_is_not_a_recorded_time():
    # The last listing still had the stop in the future: a prediction, not used.
    tu = {2: TuStop(seen_at=T0 + 280, time=T0 + 310, skipped=False)}
    v = by_seq(observe_trip(make_trip(), evidence(standard_run(), tu)))[2]
    assert (v.source, v.tu_time, v.disagree) == ("vp", None, False)


def test_tu_only_trip_is_flagged_and_last_stop_lost():
    tu = {1: TuStop(T0 + 40, T0 + 5, False), 2: TuStop(T0 + 330, T0 + 305, False)}
    visits = by_seq(observe_trip(make_trip(), evidence((), tu)))
    assert (visits[1].status, visits[1].source, visits[1].time) == ("observed", "tu_only", T0 + 5)
    assert visits[1].lo is None and visits[1].hi is None
    assert visits[3].status == "lost"        # no TU time, no vehicle
    assert visits[4].status == "lost" and visits[4].event == "arrival"


def test_layover_reports_with_the_previous_trips_sequence_do_not_count():
    # DRT puts the next trip_id on the bus during layover, still carrying the previous
    # trip's stop_sequence (9). Taken at face value that's "past every stop" at T0-300.
    rows = [row(T0 - 300, 9), row(T0 - 270, 9, True)] + standard_run()
    visits = by_seq(observe_trip(make_trip(), evidence(rows)))
    assert (visits[1].lo, visits[1].hi) == (T0, T0 + 30)
    assert (visits[2].lo, visits[2].hi) == (T0 + 300, T0 + 330)
    assert all(v.status == "observed" for v in visits.values())


def test_trip_first_seen_mid_route_is_lost_before_that():
    rows = [r for r in standard_run() if r[1] >= T0 + 540]  # first report heading to stop 3
    visits = by_seq(observe_trip(make_trip(), evidence(rows)))
    assert visits[1].status == "lost" and visits[2].status == "lost"
    assert visits[3].status == "observed"


def test_vehicle_that_stops_reporting_is_lost_after():
    rows = [r for r in standard_run() if r[1] <= T0 + 300]  # last report stopped at stop 2
    visits = by_seq(observe_trip(make_trip(), evidence(rows)))
    assert visits[1].status == "observed"
    assert visits[2].status == "lost" and visits[2].lo == T0 + 300 and visits[2].hi is None
    assert visits[3].status == "lost" and visits[4].status == "lost"


def test_evidence_cut_short_by_a_feed_gap_is_no_data():
    rows = [r for r in standard_run() if r[1] <= T0 + 300]
    gap = (T0 + 310, T0 + 4000)  # the feed went quiet right after the last report
    visits = by_seq(observe_trip(make_trip(), evidence(rows, vp_gaps=[gap])))
    assert visits[1].status == "observed"
    assert visits[2].status == "no_data" and visits[3].status == "no_data"
    assert visits[4].status == "no_data"


def test_bracket_across_a_gap_is_no_data_but_keeps_its_bounds():
    rows = [r for r in standard_run() if not T0 + 300 < r[1] < T0 + 540]
    gap = (T0 + 305, T0 + 535)
    v = by_seq(observe_trip(make_trip(), evidence(rows, vp_gaps=[gap])))[2]
    assert v.status == "no_data" and (v.lo, v.hi) == (T0 + 300, T0 + 540)
    assert v.time is None


def test_tripupdates_gap_is_flagged_not_fatal():
    v = by_seq(observe_trip(make_trip(), evidence(standard_run(),
                                                  tu_gaps=[(T0 - 100, T0 + 20)])))[1]
    assert (v.status, v.source, v.tu_gap) == ("observed", "vp", True)


def test_skipped_stop_wins_over_the_vehicle_passing_it():
    tu = {2: TuStop(T0 + 340, None, True)}
    v = by_seq(observe_trip(make_trip(), evidence(standard_run(), tu)))[2]
    assert v.status == "skipped" and v.time is None


def test_report_going_back_moves_the_departure_later():
    # The bus looked past stop 1 at T0+30, then reported heading to stop 1 again at T0+60.
    rows = standard_run()
    rows.insert(rows.index(row(T0 + 30, 2)) + 1, row(T0 + 60, 1))
    rows.insert(rows.index(row(T0 + 60, 1)) + 1, row(T0 + 90, 2))
    v = by_seq(observe_trip(make_trip(), evidence(rows)))[1]
    assert (v.lo, v.hi) == (T0 + 60, T0 + 90)


# Whole trips ---------------------------------------------------------------------------

def test_cancelled_trip_never_seen_running():
    visits = observe_trip(make_trip(), evidence(cancelled=[KEY], tu_seen=[KEY]))
    assert {v.status for v in visits} == {"cancelled"}


def test_cancelled_trip_that_ran_is_scored_normally():
    visits = by_seq(observe_trip(make_trip(), evidence(standard_run(), cancelled=[KEY])))
    assert visits[1].status == "observed"


def test_trip_never_in_realtime():
    assert {v.status for v in observe_trip(make_trip(), evidence())} == {"not_seen"}
    gap = (T0 + 100, T0 + 200)
    assert {v.status for v in observe_trip(make_trip(), evidence(vp_gaps=[gap]))} == {"no_data"}


def test_trip_ids_go_through_the_alias():
    rows = [r[:2] + ("T1_LATE",) + r[3:] for r in standard_run()]
    visits = observe_trip(make_trip(), evidence(rows, alias={"T1_LATE": "T1"}))
    assert all(v.status == "observed" for v in visits)


# Last stop -----------------------------------------------------------------------------

def test_last_stop_stopped_at_under_own_trip():
    v = by_seq(observe_trip(make_trip(), evidence(standard_run())))[4]
    # first report STOPPED_AT stop 4 is at T0+870; the one before it is T0+840
    assert (v.status, v.source, v.time) == ("observed", "vp_stopped", T0 + 870)
    assert (v.lo, v.hi, v.event, v.scored) == (T0 + 840, T0 + 870, "arrival", True)


def test_last_stop_by_position_under_the_next_trip_id():
    own = [r for r in standard_run() if r[1] <= T0 + 840]   # heading to stop 4, never stopped
    nxt = [row(T0 + 880, 1, True, trip="T2", at=4), row(T0 + 910, 1, True, trip="T2", at=4)]
    v = by_seq(observe_trip(make_trip(), evidence(own + nxt)))[4]
    assert (v.status, v.source, v.time, v.lo) == ("observed", "vp_position", T0 + 880, T0 + 840)
    assert v.vehicle_id == "V1"


def test_last_stop_ignores_passing_the_terminal_before_the_second_to_last_stop():
    # A loop: the terminal is where stop 1 is. Early reports sit at those coordinates.
    trip = make_trip(last_at=1)
    rows = [r[:6] + stop_at(1) if r[4] == 1 else r for r in standard_run()]
    rows = [r for r in rows if r[1] <= T0 + 840]
    rows.append(row(T0 + 900, 1, True, trip="T2", at=1))
    v = by_seq(observe_trip(trip, evidence(rows)))[4]
    assert (v.status, v.time) == ("observed", T0 + 900)


def test_last_stop_search_ends_ten_minutes_after_the_trips_last_report():
    own = [r for r in standard_run() if r[1] <= T0 + 840]
    late = [row(T0 + 840 + 601, 1, True, trip="T2", at=4)]
    v = by_seq(observe_trip(make_trip(), evidence(own + late)))[4]
    assert v.status == "lost" and v.time is None


def test_last_stop_search_over_a_gap_is_no_data():
    own = [r for r in standard_run() if r[1] <= T0 + 840]
    v = by_seq(observe_trip(make_trip(), evidence(own, vp_gaps=[(T0 + 900, T0 + 2000)])))[4]
    assert v.status == "no_data"


# Cleaning, snapshots, gaps -------------------------------------------------------------

def test_clean_reports_keeps_only_new_evidence():
    stats = Counter()
    rows = [row(T0, 1), row(T0, 1), row(T0 - 30, 1), row(None, 1), row(T0 + 30, 2),
            ("V2", T0 + 10, "T1", D, 1, False, None, None)]
    by_vehicle, by_trip = clean_reports(rows, {}, stats)
    assert [r.t for r in by_vehicle["V1"]] == [T0, T0 + 30]
    assert [r.t for r in by_trip[KEY]] == [T0, T0 + 10, T0 + 30]  # two vehicles, merged
    assert stats["vp not newer"] == 2 and stats["vp no timestamp"] == 1
    assert isinstance(by_trip[KEY][0], Report)


def test_report_without_trip_still_follows_the_vehicle():
    by_vehicle, by_trip = clean_reports([row(T0, None, trip=None)], {})
    assert by_vehicle["V1"][0].trip is None and not by_trip


def test_fresh_snapshots():
    stats = Counter()
    rows = [
        ("a", T0 + 20, "ok", T0),
        ("b", T0 + 50, "ok", T0),            # same feed time: not newer
        ("c", T0 + 80, "invalid", None),
        ("d", T0 + 110, "ok", T0 - 200),     # older than one already kept
        ("e", T0 + 300, "ok", T0 + 100),     # 200 s old when fetched: frozen
        ("f", T0 + 330, "ok", T0 + 310),
    ]
    assert fresh_snapshots(rows, stats) == [("a", T0), ("f", T0 + 310)]
    assert stats == Counter({"snapshot not newer": 2, "snapshot not usable": 1,
                             "snapshot frozen": 1})


def test_find_gaps_includes_the_ends():
    times = [T0 + 30 * i for i in range(10)] + [T0 + 1000, T0 + 1030]
    assert find_gaps(times, T0 - 10, T0 + 1030 + GAP_S) == [(T0 + 270, T0 + 1000)]
    assert find_gaps(times, T0 - 500, T0 + 5000) == [
        (T0 - 500, T0), (T0 + 270, T0 + 1000), (T0 + 1030, T0 + 5000)]
    assert find_gaps([], T0, T0 + 50) == []
