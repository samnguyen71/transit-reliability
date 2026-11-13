"""realtime.observe against a real PostgreSQL holding the mini feed (see conftest.py).

Skipped without DATABASE_URL, like the other database tests.
"""

from datetime import UTC, date, datetime

import pyarrow.parquet as pq
from test_observe import World

from realtime import observe

THU = date(2026, 11, 5)
T0 = int(datetime(2026, 11, 5, 13, 0, tzinfo=UTC).timestamp())  # 08:00 Toronto
A = (43.8971, -78.8658)
C = (43.9102, -78.8402)


def test_pick_static_feed(conn, feed_id):
    # The mini feed is the only one loaded; the loader tests roll their extra feeds back.
    assert observe.pick_static_feed(conn, THU) == feed_id
    assert observe.pick_static_feed(conn, date(2026, 10, 20)) is None  # before its calendar
    # Fetched 08:00 Toronto on 10-26, its first day: the only feed, so used anyway.
    assert observe.pick_static_feed(conn, date(2026, 10, 26)) == feed_id


def test_feed_fetched_during_a_date_waits_for_the_next(conn, feed_id):
    # A newer feed fetched 15:10 Toronto on Nov 5 (as DRT's often are): Nov 5's morning
    # ran on the old one, so it takes over on Nov 6.
    with conn.transaction(force_rollback=True):
        new = conn.execute("""
            INSERT INTO gtfs.feed (sha256, zip_path, fetched_at, timezone)
            VALUES ('test-newer', 'newer.zip', '2026-11-05 20:10Z', 'America/Toronto')
            RETURNING feed_id""").fetchone()[0]
        conn.execute("""
            INSERT INTO gtfs.calendar (feed_id, service_id, monday, tuesday, wednesday,
                thursday, friday, saturday, sunday, start_date, end_date)
            VALUES (%s, 'ALL', true, true, true, true, true, true, true,
                    '2026-11-01', '2026-12-31')""", (new,))
        assert observe.pick_static_feed(conn, THU) == feed_id
        assert observe.pick_static_feed(conn, date(2026, 11, 6)) == new


def test_load_schedule(conn, feed_id):
    s = observe.load_schedule(conn, THU, feed_id)
    trips = {t.key[0]: t for t in s.trips}
    assert sorted(trips) == ["EARLY", "EARLY_ODD", "NIGHT", "WK1"]  # EARLY_LATE is an alias
    assert s.alias == {"EARLY_LATE": "EARLY"}
    assert all(t.key[1] == "20261105" for t in s.trips)

    wk1 = trips["WK1"].stops
    assert [st.seq for st in wk1] == [1, 2, 3]
    assert [st.timepoint for st in wk1] == [True, False, True]   # B has no time
    assert wk1[0].sched_dep == T0 and wk1[1].sched_dep is None
    assert wk1[2].sched_arr == T0 + 1200
    assert (wk1[0].lat, wk1[0].lon) == A

    night = trips["NIGHT"].stops   # 24:40 on Nov 5 is 00:40 EST on Nov 6
    assert night[1].sched_dep == int(datetime(2026, 11, 6, 5, 40, tzinfo=UTC).timestamp())


def test_main_end_to_end(conn, feed_id, scratch_dsn, tmp_path, monkeypatch):
    parsed, out = tmp_path / "parsed", tmp_path / "out"
    w = World()
    for h in range(T0 - 300, T0 + 1500, 30):
        t = h - 15
        if t < T0 + 15:
            report = (1, "STOPPED_AT", *A)
        elif t < T0 + 1150:
            report = (2 if t < T0 + 600 else 3, "IN_TRANSIT_TO", None, None)
        else:
            report = (3, "STOPPED_AT", *C)
        w.vp(h, [("V1", t, "WK1", *report)])
        w.tu(h, [])
    w.write(parsed)

    monkeypatch.setenv("DATABASE_URL", scratch_dsn)
    assert observe.main([str(parsed), str(out), "--date", "2026-11-05"]) == 0
    visits = pq.read_table(out / "visits" / "2026-11-05.parquet")
    assert visits.schema.metadata[b"static_feed_id"] == str(feed_id).encode()
    v = {(r["trip_id"], r["stop_sequence"]): r for r in visits.to_pylist()}
    assert len(v) == 10
    assert (v["WK1", 1]["status"], v["WK1", 1]["source"]) == ("observed", "vp")
    assert v["WK1", 2]["scored"] is False
    assert (v["WK1", 3]["status"], v["WK1", 3]["source"]) == ("observed", "vp_stopped")
    # The night trips ran when no snapshots were logged: no data, not "not seen".
    assert {v["NIGHT", i]["status"] for i in (1, 2, 3)} == {"no_data"}

    # A database without the gtfs schema gets a clear message, not a traceback.
    url = scratch_dsn.replace(f"dbname={conn.info.dbname}", "dbname=template1")
    monkeypatch.setenv("DATABASE_URL", url)
    assert observe.main([str(parsed), str(out), "--date", "2026-11-05"]) == 2
    monkeypatch.setenv("DATABASE_URL", scratch_dsn)

    # A second run finds the date done and writes nothing.
    before = (out / "visits" / "2026-11-05.parquet").stat().st_mtime_ns
    assert observe.main([str(parsed), str(out), "--date", "2026-11-05"]) == 0
    assert (out / "visits" / "2026-11-05.parquet").stat().st_mtime_ns == before
