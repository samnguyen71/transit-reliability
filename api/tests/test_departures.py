"""Live departures. live.py on snapshot files (live_files.py), board.py's merge on plain
data, and GET /stops/{id}/departures on Postgres (conftest.py's feeds) plus snapshots."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from live_files import local, stu, trip, write_snapshot

from transit_api import board, deps, live
from transit_api.live import LiveFeed, RtStop, RtTrip, Snapshot
from transit_api.main import create_app
from transit_api.settings import Settings

S = timedelta(seconds=1)
M = timedelta(minutes=1)
NOON = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)


# live.py: finding and reading snapshots -------------------------------------------------

def test_newest_file_is_parsed_once(tmp_path):
    write_snapshot(tmp_path, NOON, [trip("a", "20261125")])
    write_snapshot(tmp_path, NOON + 30 * S, [trip("b", "20261125")])
    newest = write_snapshot(tmp_path, NOON + 30 * S, [trip("c", "20261125")], suffix="_1")
    feed = LiveFeed(tmp_path, rescan_s=0)
    snap = feed.snapshot()
    assert snap.file == newest.name  # a same-second _1 file is the later one
    assert set(snap.trips) == {("c", "20261125")}
    feed.snapshot()
    feed.snapshot()
    assert feed.parses == 1


def test_rescans_at_most_every_rescan_s(tmp_path):
    t = [0.0]
    write_snapshot(tmp_path, NOON, [trip("a", "20261125")])
    feed = LiveFeed(tmp_path, rescan_s=5, clock=lambda: t[0])
    assert ("a", "20261125") in feed.snapshot().trips
    write_snapshot(tmp_path, NOON + 30 * S, [trip("b", "20261125")])
    t[0] = 4.9
    assert ("a", "20261125") in feed.snapshot().trips
    t[0] = 5.0
    assert ("b", "20261125") in feed.snapshot().trips


def test_new_utc_day_without_a_snapshot_yet(tmp_path):
    before = write_snapshot(tmp_path, datetime(2026, 11, 25, 23, 59, 50, tzinfo=UTC), [])
    folder = tmp_path / "trip_updates"
    (folder / "2026-11-26").mkdir()
    (folder / "2026-11-26" / "20261126T000003Z.invalid.gz").write_bytes(b"junk")
    (folder / "_tmp").mkdir()  # the logger's downloads; sorts after every day folder
    (folder / "_tmp" / "20261126T000033Z.pb.gz").write_bytes(b"half a download")
    (folder / "_state.json").write_text("{}")
    assert LiveFeed(tmp_path, rescan_s=0).snapshot().file == before.name


def test_no_snapshots(tmp_path):
    assert LiveFeed(tmp_path / "nothing", rescan_s=0).snapshot() == Snapshot()


def test_parse_reads_what_the_board_needs(tmp_path):
    ts = int(NOON.timestamp())
    write_snapshot(tmp_path, NOON, [
        trip("a", "20261125",
             stu(1, "S1", NOON),                          # arrival.time: DRT's departure
             stu(2, "S2", NOON + M, arrival=False),       # only departure.time
             stu(3, "S3", None, rel="NO_DATA"),
             stu(4, "S4", NOON, rel="SKIPPED"), vehicle="V9"),
        trip("c", "20261125", rel="CANCELED", vehicle=None),
    ], deleted=1)
    snap = LiveFeed(tmp_path, rescan_s=0).snapshot()
    assert (snap.entities, snap.feed_ts) == (3, NOON - 20 * S)
    a = snap.trips["a", "20261125"]
    assert (a.relationship, a.vehicle_id) == (None, "V9")
    assert a.stops == {1: RtStop("S1", ts, False), 2: RtStop("S2", ts + 60, False),
                       3: RtStop("S3", None, False), 4: RtStop("S4", ts, True)}
    c = snap.trips["c", "20261125"]
    assert (c.relationship, c.vehicle_id, c.stops) == ("CANCELED", None, {})
    assert len(snap.trips) == 2  # the deleted entity isn't a trip


def test_unreadable_file_keeps_the_last_good_snapshot(tmp_path):
    good = write_snapshot(tmp_path, NOON, [trip("a", "20261125")])
    feed = LiveFeed(tmp_path, rescan_s=0)
    feed.snapshot()
    good.with_name("20261125T170030Z.pb.gz").write_bytes(b"not gzip")
    assert feed.snapshot().file == good.name
    assert feed.parses == 1


def test_status():
    def state(**kw):
        return live.status(Snapshot(**kw), NOON, 120)["status"]
    assert state() == "missing"
    assert state(file="f", feed_ts=NOON - 20 * S, entities=3) == "ok"
    assert state(file="f", feed_ts=NOON - 121 * S, entities=3) == "stale"
    assert state(file="f", feed_ts=None, entities=3) == "stale"
    assert state(file="f", feed_ts=NOON - 20 * S, entities=0) == "empty"
    assert live.status(Snapshot(file="f", feed_ts=NOON - 20 * S, entities=3), NOON,
                       120)["age_s"] == 20


# board.py: the merge, one case per rule -------------------------------------------------

NOW = local(2026, 11, 25, 7, 55)


def row(trip_id="T", *, at, start, seq=2, stop_id="S", start_date="20261125", rt_ids=None):
    """A scheduled departure at stop_sequence 2, `at` from NOW; the trip starts `start`
    from NOW."""
    return {"trip_id": trip_id, "service_date": date(2026, 11, 25), "stop_sequence": seq,
            "stop_id": stop_id, "scheduled": NOW + at, "trip_start": NOW + start,
            "start_date": start_date, "rt_trip_ids": rt_ids or [trip_id],
            "route": {"route_id": "10"}, "direction_id": 0, "headsign": "H",
            "timepoint": True}


def rt(stops=None, rel=None):
    """A feed trip; stops: {seq: (stop_id, time from NOW or None, skipped)}."""
    return RtTrip(rel, "V1", {seq: RtStop(sid, int((NOW + t).timestamp()) if t is not None
                                          else None, skipped)
                              for seq, (sid, t, skipped) in (stops or {}).items()})


def snap(trips: dict) -> Snapshot:
    return Snapshot(file="f", feed_ts=NOW, entities=len(trips), trips=trips)


def merged(rows, s, use=True, horizon=90 * M):
    return [(d["trip_id"], d["status"], d["delay_s"])
            for d in board.merge(rows, s, use, NOW, horizon)]


UNDER_WAY = {"start": -10 * M}
CASES = {
    "live": ([row(at=5 * M, **UNDER_WAY)],
             {("T", "20261125"): rt({2: ("S", 8 * M, False)})}, [("T", "live", 180)]),
    "not started": ([row(at=10 * M, start=5 * M)],
                    {("T", "20261125"): rt({2: ("S", 12 * M, False)})},
                    [("T", "not_started", 120)]),
    "not in the feed yet": ([row(at=80 * M, start=60 * M)], {}, [("T", "scheduled", None)]),
    "missing from the feed": ([row(at=40 * M, start=30 * M)], {},
                              [("T", "no_realtime", None)]),
    "cancelled": ([row(at=5 * M, **UNDER_WAY)],
                  {("T", "20261125"): rt({2: ("S", 8 * M, False)}, rel="CANCELED")},
                  [("T", "cancelled", None)]),
    "skipped": ([row(at=5 * M, **UNDER_WAY)],
                {("T", "20261125"): rt({2: ("S", 5 * M, True)})}, [("T", "skipped", None)]),
    "passed: only later stops listed": ([row(at=5 * M, **UNDER_WAY)],
                                        {("T", "20261125"): rt({3: ("S3", 9 * M, False)})}, []),
    "passed: frozen time in the past": ([row(at=-5 * M, **UNDER_WAY)],
                                        {("T", "20261125"): rt({2: ("S", -2 * M, False)})}, []),
    "late bus from long ago": ([row(at=-50 * M, start=-60 * M)],
                               {("T", "20261125"): rt({2: ("S", 2 * M, False)})},
                               [("T", "live", 3120)]),
    "detour: another stop_id": ([row(at=5 * M, **UNDER_WAY)],
                                {("T", "20261125"): rt({2: ("X", 8 * M, False)})},
                                [("T", "no_realtime", None)]),
    "no time for the stop": ([row(at=5 * M, **UNDER_WAY)],
                             {("T", "20261125"): rt({2: ("S", None, False)})},
                             [("T", "no_realtime", None)]),
    "overnight copy's trip_id": ([row(at=5 * M, rt_ids=["T", "T+24h"], **UNDER_WAY)],
                                 {("T+24h", "20261125"): rt({2: ("S", 8 * M, False)})},
                                 [("T", "live", 180)]),
    "another day's run": ([row(at=5 * M, **UNDER_WAY)],
                          {("T", "20261124"): rt({2: ("S", 8 * M, False)})},
                          [("T", "no_realtime", None)]),
    "predicted past the horizon": ([row(at=85 * M, start=80 * M)],
                                   {("T", "20261125"): rt({2: ("S", 95 * M, False)})}, []),
}


@pytest.mark.parametrize("rows, trips, want", CASES.values(), ids=CASES.keys())
def test_merge(rows, trips, want):
    assert merged(rows, snap(trips)) == want


def test_merge_without_a_usable_feed_uses_the_schedule():
    rows = [row(at=5 * M, **UNDER_WAY), row("P", at=-5 * M, **UNDER_WAY)]
    trips = {("T", "20261125"): rt({2: ("S", 8 * M, False)})}
    assert merged(rows, snap(trips), use=False) == [("T", "scheduled", None)]


def test_merge_sorts_by_the_time_shown():
    rows = [row("A", at=5 * M, **UNDER_WAY), row("B", at=10 * M, start=60 * M),
            row("C", at=15 * M, **UNDER_WAY)]
    trips = {("A", "20261125"): rt({2: ("S", 20 * M, False)}),
             ("C", "20261125"): rt({2: ("S", 14 * M, False)})}
    assert [t for t, *_ in merged(rows, snap(trips))] == ["B", "C", "A"]
    (a,) = [d for d in board.merge(rows, snap(trips), True, NOW, 90 * M) if d["trip_id"] == "A"]
    assert (a["vehicle_id"], a["predicted"], a["scheduled"]) == ("V1", NOW + 20 * M, NOW + 5 * M)


# GET /stops/{stop_id}/departures --------------------------------------------------------

WED = local(2026, 11, 25, 7, 55)  # Wednesday: t4 07:00, t1 08:00, t2 09:00 leave stop 1


class Board:
    def __init__(self, client: TestClient, raw):
        self.client, self.raw, self.now = client, raw, WED

    def get(self, stop="1", **params):
        return self.client.get(f"/stops/{stop}/departures", params=params)

    def departures(self, **params):
        return [(d["trip_id"], d["status"], d["delay_s"])
                for d in self.get(**params).json()["departures"]]

    def snapshot(self, trips, **kw):
        return write_snapshot(self.raw, self.now, trips, **kw)


@pytest.fixture
def api(static_dsn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url=static_dsn, live_rescan_s=0))
    with TestClient(app) as c:
        b = Board(c, tmp_path / "raw")
        app.dependency_overrides[deps.get_now] = lambda: b.now
        yield b


def test_board(api):
    api.snapshot([
        trip("t4", "20261125", stu(1, "1", local(2026, 11, 25, 7, 58)),
             stu(2, "2", local(2026, 11, 25, 8, 8)), vehicle="V4"),
        trip("t1", "20261125", stu(1, "1", local(2026, 11, 25, 8, 2)),
             stu(2, "2", local(2026, 11, 25, 8, 12)), stu(3, "4", local(2026, 11, 25, 8, 22))),
    ])
    r = api.get()
    assert (r.status_code, r.headers["cache-control"]) == (200, "no-store")
    body = r.json()
    assert (body["stop"]["stop_id"], body["now"], body["horizon_min"]) == \
        ("1", "2026-11-25T12:55:00Z", 90)
    assert body["realtime"] == {"status": "ok", "file": "20261125T125500Z.pb.gz",
                                "feed_timestamp": "2026-11-25T12:54:40Z", "age_s": 20,
                                "trips": 2}
    # t4, scheduled 07:00, is 58 min late and still coming; t1 hasn't started; t2 starts
    # after the feed's 60 min lead. t5 also calls at stop 1 (10:20, inside 180 min), but
    # it's its last stop: nobody boards there.
    want = [("t4", "live", 3480), ("t1", "not_started", 120), ("t2", "scheduled", None)]
    assert api.departures() == want
    assert api.departures(horizon_min=180) == want
    first = body["departures"][0]
    assert first == {
        "trip_id": "t4", "service_date": "2026-11-25",
        "route": {"route_id": "10", "short_name": "10", "long_name": "Simcoe",
                  "color": "006633"},
        "direction_id": 0, "headsign": "Simcoe", "scheduled": "2026-11-25T12:00:00Z",
        "predicted": "2026-11-25T12:58:00Z", "delay_s": 3480, "status": "live",
        "vehicle_id": "V4", "timepoint": True}


def test_overnight_run_under_its_copys_trip_id(api):
    # Sunday 00:55. Before 03:00 DRT reports N1's 01:00 run as t7, t6's +24 h copy, with
    # the calendar date as start_date: it's t6 on service date 11-29.
    api.now = local(2026, 11, 29, 0, 55)
    api.snapshot([trip("t7", "20261129", stu(1, "1", local(2026, 11, 29, 1, 3)),
                       stu(2, "5", local(2026, 11, 29, 1, 13)))])
    (d,) = api.get().json()["departures"]
    assert (d["trip_id"], d["service_date"], d["route"]["route_id"], d["status"],
            d["delay_s"]) == ("t6", "2026-11-29", "N1", "not_started", 180)


def test_after_midnight_trip_uses_the_next_days_start_date(api):
    # Wednesday 00:20: t9 is Tuesday's 24:30, so DRT sends it with start_date 20261202.
    api.now = local(2026, 12, 2, 0, 20)
    api.snapshot([trip("t9", "20261202", stu(1, "1", local(2026, 12, 2, 0, 33)),
                       stu(2, "2", local(2026, 12, 2, 0, 43)))])
    (d,) = api.get().json()["departures"]
    assert (d["trip_id"], d["service_date"], d["status"], d["delay_s"]) == \
        ("t9", "2026-12-01", "not_started", 180)
    # Under 20261201 it would be Monday's run (rule v4): not matched.
    api.now += 30 * S
    api.snapshot([trip("t9", "20261201", stu(1, "1", local(2026, 12, 2, 0, 33)))])
    assert api.departures() == [("t9", "no_realtime", None)]


def test_stale_feed_gives_scheduled_times_only(api):
    api.snapshot([trip("t1", "20261125", stu(1, "1", local(2026, 11, 25, 8, 2)))],
                 header=api.now - 10 * M)
    body = api.get().json()
    assert (body["realtime"]["status"], body["realtime"]["age_s"]) == ("stale", 600)
    assert api.departures() == [("t1", "scheduled", None), ("t2", "scheduled", None)]


def test_empty_feed(api):
    api.snapshot([])
    assert api.get().json()["realtime"]["status"] == "empty"
    assert api.departures() == [("t1", "scheduled", None), ("t2", "scheduled", None)]


def test_no_snapshots_yet(api):
    body = api.get().json()
    assert body["realtime"] == {"status": "missing", "file": None, "feed_timestamp": None,
                                "age_s": None, "trips": 0}
    assert api.departures() == [("t1", "scheduled", None), ("t2", "scheduled", None)]


def test_limit_and_horizon(api):
    assert api.departures(horizon_min=15) == [("t1", "scheduled", None)]
    assert api.departures(limit=1) == [("t1", "scheduled", None)]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 31}, {"horizon_min": 14},
                                    {"horizon_min": 181}])
def test_bad_parameters(api, params):
    assert api.get(**params).status_code == 422


def test_unknown_stop(api):
    assert api.get("99").status_code == 404


def test_health_shows_the_snapshot(api):
    api.snapshot([trip("t1", "20261125")])
    rt_status = api.client.get("/health").json()["realtime"]
    assert (rt_status["status"], rt_status["file"], rt_status["trips"]) == \
        ("ok", "20261125T125500Z.pb.gz", 1)


def test_database_down_is_a_503(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, db_pool_timeout_s=0.5,
                              database_url="postgresql://nobody@127.0.0.1:1/none"))
    with TestClient(app) as c:
        assert c.get("/stops/1/departures").status_code == 503
