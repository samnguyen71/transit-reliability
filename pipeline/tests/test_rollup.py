"""realtime.rollup on hand-built visits files. No database."""

import os
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from realtime import rollup
from realtime.schemas import HIST_BINS, ROLLUP_TABLES, VISITS

D = date(2026, 11, 5)
T0 = int(datetime(2026, 11, 5, 13, 0, tzinfo=UTC).timestamp())  # 08:00 Toronto (EST)
OLD = 10**18  # 2001 in ns, a whole number of 100 ns (Windows mtime resolution)


def visit(trip, seq, delay, status="observed", event="departure", scored=True,
          route="R1", stop=None, at=T0):
    observed = status == "observed"
    return {"service_date": D, "trip_id": trip, "route_id": route, "direction_id": 0,
            "stop_sequence": seq, "stop_id": stop or f"S{seq}", "event": event,
            "scored": scored, "scheduled": datetime.fromtimestamp(at, UTC), "status": status,
            "observed": datetime.fromtimestamp(at + delay, UTC) if observed else None,
            "delay_s": delay if observed else None, "source": "tu" if observed else None,
            "lo": None, "hi": None, "tu_time": None, "disagree": None, "tu_gap": None,
            "vehicle_id": "V1"}


def write_visits(observed, rows, written_at="w1", rule="3"):
    path = rollup.visits_path(observed, D)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=VISITS).replace_schema_metadata(
        {"rule_version": rule, "written_at": written_at})
    pq.write_table(table, path)


def day_rows():
    return [
        # T1: first stop on time at the -60 s edge, a timepoint 301 s late (late),
        # an unscored stop, and an early last-stop arrival (on time).
        visit("T1", 1, -60),
        visit("T1", 2, 0, scored=False),
        visit("T1", 3, 301),
        visit("T1", 4, -200, event="arrival"),
        # T2, on route R2 an hour later: first stop 61 s early, timepoint lost and
        # skipped, last stop exactly 300 s late (on time).
        visit("T2", 1, -61, route="R2", at=T0 + 3600),
        visit("T2", 2, 0, status="lost", route="R2", at=T0 + 3600),
        visit("T2", 3, 0, status="skipped", route="R2", at=T0 + 3600),
        visit("T2", 4, 300, event="arrival", route="R2", at=T0 + 3600),
    ]


def read(out, table):
    t = pq.read_table(out / table / f"{D}.parquet")
    assert t.schema.equals(ROLLUP_TABLES[table])
    return t.to_pylist()


def test_counts_kinds_and_edges(tmp_path):
    observed, out = tmp_path / "observed", tmp_path / "rollups"
    write_visits(observed, day_rows())
    assert rollup.main([str(observed), str(out)]) == 0

    r = {(x["route_id"], x["kind"]): x for x in read(out, "by_route")}
    assert set(r) == {(rt, k) for rt in ("R1", "R2")
                      for k in ("first_stop", "timepoint", "last_stop")}
    assert (r["R1", "first_stop"]["on_time"], r["R1", "first_stop"]["early"]) == (1, 0)
    assert (r["R2", "first_stop"]["on_time"], r["R2", "first_stop"]["early"]) == (0, 1)
    assert (r["R1", "timepoint"]["scheduled"], r["R1", "timepoint"]["late"]) == (1, 1)  # unscored left out
    assert (r["R1", "last_stop"]["on_time"], r["R1", "last_stop"]["early"]) == (1, 0)  # early arrival
    assert r["R2", "last_stop"]["on_time"] == 1                                        # exactly +300 s
    tp = r["R2", "timepoint"]
    assert (tp["scheduled"], tp["observed"], tp["lost"], tp["skipped"]) == (2, 0, 1, 1)
    assert tp["median_delay_s"] is None

    hours = {(x["hour"], x["route_id"], x["kind"]): x["scheduled"]
             for x in read(out, "by_hour")}
    assert hours == {(8, "R1", "first_stop"): 1, (8, "R1", "timepoint"): 1,
                     (8, "R1", "last_stop"): 1, (9, "R2", "first_stop"): 1,
                     (9, "R2", "timepoint"): 2, (9, "R2", "last_stop"): 1}

    stops = read(out, "by_stop")
    assert sum(x["scheduled"] for x in stops) == 7  # every scored visit once
    meta = pq.read_schema(out / "by_stop" / f"{D}.parquet").metadata
    assert meta[b"rollup_version"] == rollup.ROLLUP_VERSION.encode()
    assert meta[b"rule_version"] == b"3"


def test_delay_histogram_bins(tmp_path):
    observed, out = tmp_path / "observed", tmp_path / "rollups"
    # Timepoints of one trip, at and around every kind of bin edge.
    delays = [-301, -300, -61, -60, 0, 299, 300, 1799, 1800, 5000]
    rows = [visit("T1", 1, 0)] + [visit("T1", i + 2, d) for i, d in enumerate(delays)]
    rows.append(visit("T1", 99, 0, status="lost"))  # not observed: in no bin
    write_visits(observed, rows)
    assert rollup.main([str(observed), str(out)]) == 0

    tp = next(x for x in read(out, "by_route") if x["kind"] == "timepoint")
    hist = tp["delay_hist"]
    assert len(hist) == HIST_BINS == 37
    assert sum(hist) == tp["observed"] == len(delays)
    # Bin i >= 1 starts at -300 + (i-1)*60 s; 0 is under -300 s, 36 is 1800 s or more.
    assert {i: n for i, n in enumerate(hist) if n} == {
        0: 1,                 # -301
        1: 1,                 # -300
        4: 1, 5: 1, 6: 1,     # -61 in [-120, -60), -60 in [-60, 0), 0 in [0, 60)
        10: 1, 11: 1,         # 299 in [240, 300), 300 in [300, 360)
        35: 1,                # 1799 in [1740, 1800)
        36: 2,                # 1800 and 5000: over, and counted once each
    }


def test_skips_done_dates_and_redoes_after_a_new_observe(tmp_path):
    observed, out = tmp_path / "observed", tmp_path / "rollups"
    write_visits(observed, day_rows())
    assert rollup.main([str(observed), str(out)]) == 0
    path = out / "by_route" / f"{D}.parquet"
    os.utime(path, ns=(OLD, OLD))  # an mtime a rewrite would replace

    assert rollup.main([str(observed), str(out)]) == 0
    assert path.stat().st_mtime_ns == OLD                   # done: left alone
    assert rollup.main([str(observed), str(out), "--force"]) == 0
    assert path.stat().st_mtime_ns != OLD

    os.utime(path, ns=(OLD, OLD))
    write_visits(observed, day_rows()[:4], written_at="w2")  # observe ran again
    assert rollup.main([str(observed), str(out)]) == 0
    assert path.stat().st_mtime_ns != OLD
    assert {x["route_id"] for x in read(out, "by_route")} == {"R1"}


def test_missing_visits_file_is_reported(tmp_path, capsys):
    assert rollup.main([str(tmp_path / "observed"), str(tmp_path / "out"),
                        "--date", "2026-11-05"]) == 1
    assert "observe it first" in capsys.readouterr().err
