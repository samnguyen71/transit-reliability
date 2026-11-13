"""realtime.evidence and realtime.observe on tiny hand-built parsed days. No database:
the schedule is built by hand (test_observe_db.py covers reading it from PostgreSQL).

World collects rows the way realtime.parse would have written them and writes every table
of both feeds for both UTC days a service date needs.
"""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from realtime import evidence, observe
from realtime.rules import Stop, Trip
from realtime.schemas import TABLES

SERVICE = date(2026, 11, 5)
DAYS = ["2026-11-05", "2026-11-06"]
D = "20261105"
T0 = 1793883600  # 2026-11-05 13:00:00Z, 08:00 Toronto
FEEDS = ("trip_updates", "vehicle_positions")


def utc(s: int) -> datetime:
    return datetime.fromtimestamp(s, UTC)


class World:
    def __init__(self):
        self.rows = defaultdict(list)  # (feed, table, day) -> rows

    def _snapshot(self, feed: str, header: int, fetched: int | None = None) -> tuple[str, str]:
        fetched = fetched if fetched is not None else header + 20
        name = f"{utc(fetched):%Y%m%dT%H%M%SZ}.pb.gz"
        day = f"{utc(fetched):%Y-%m-%d}"
        self.rows[(feed, "snapshots", day)].append(
            {"file": name, "fetched_at": utc(fetched), "status": "ok", "stored_bytes": 1,
             "header_ts": utc(header)})
        return name, day

    def tu(self, header: int, trips: list[dict], fetched: int | None = None) -> None:
        """trips: {trip_id, start_date, rel, stops: [(seq, stop_id, arrival or None, rel)]}"""
        name, day = self._snapshot("trip_updates", header, fetched)
        for i, trip in enumerate(trips):
            src = {"file": name, "header_ts": utc(header), "entity_index": i}
            self.rows[("trip_updates", "trip_updates", day)].append({
                **src, "trip_id": trip["trip_id"], "start_date": trip.get("start_date", D),
                "route_id": trip.get("route_id", "R1"),
                "trip_schedule_relationship": trip.get("rel", "SCHEDULED"),
                "stop_time_updates": len(trip.get("stops", []))})
            for j, (seq, stop_id, arr, rel) in enumerate(trip.get("stops", [])):
                self.rows[("trip_updates", "stop_time_updates", day)].append({
                    **src, "update_index": j, "trip_id": trip["trip_id"],
                    "start_date": trip.get("start_date", D), "stop_sequence": seq,
                    "stop_id": stop_id, "arrival_time": utc(arr) if arr else None,
                    "departure_time": utc(arr) if arr else None,
                    "schedule_relationship": rel})

    def vp(self, header: int, reports: list[tuple]) -> None:
        """reports: (vehicle_id, t, trip_id, seq, status, lat, lon)"""
        name, day = self._snapshot("vehicle_positions", header)
        for i, (vehicle, t, trip_id, seq, status, lat, lon) in enumerate(reports):
            self.rows[("vehicle_positions", "vehicle_positions", day)].append({
                "file": name, "header_ts": utc(header), "entity_index": i,
                "trip_id": trip_id, "start_date": D, "vehicle_id": vehicle,
                "timestamp": utc(t) if t else None, "current_stop_sequence": seq,
                "current_status": status, "latitude": lat, "longitude": lon})

    def write(self, parsed) -> None:
        for feed in FEEDS:
            for table, schema in TABLES.items():
                for day in DAYS:
                    path = evidence.table_path(parsed, feed, table, day)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    rows = sorted(self.rows[(feed, table, day)], key=lambda r: r["file"])
                    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def stop(i: float) -> tuple[float, float]:
    """Stop i's coordinates; a fraction is that far along the way to the next stop."""
    return 43.90 + 0.01 * i, -78.86


def schedule_t1() -> observe.Schedule:
    stops = [Stop(i, f"S{i}", T0 + (i - 1) * 300, T0 + (i - 1) * 300, True, *stop(i))
             for i in range(1, 5)]
    return observe.Schedule(1, [Trip(("T1", D), "R1", 0, stops)], {})


def departed(i: int) -> int:
    return T0 + (i - 1) * 300 + 15


def run_reports(t: int) -> tuple:
    """Where T1's bus (V1) was at time t: stopped a minute at each stop, gone at +15 s."""
    for i in range(1, 5):
        arrive, leave = T0 + (i - 1) * 300 - 45, departed(i)
        if t < arrive:
            return (i, "IN_TRANSIT_TO", *stop(i - 0.6))
        if t < leave or i == 4:
            return (i, "STOPPED_AT", *stop(i))
    raise AssertionError


def normal_day(w: World, until: int = T0 + 1500) -> None:
    """Both feeds every 30 s from T0-600; T1 runs as in run_reports."""
    for h in range(T0 - 600, until, 30):
        seq, status, lat, lon = run_reports(h - 15)
        w.vp(h, [("V1", h - 15, "T1", seq, status, lat, lon)])
        stops = [(i, f"S{i}", departed(i), "SCHEDULED") for i in range(1, 5)
                 if departed(i) > h - 60]
        w.tu(h, [{"trip_id": "T1", "stops": stops}] if stops else [])


@pytest.fixture
def con(tmp_path):
    c = evidence.connect("200MB", tmp_path / "duck")
    yield c
    c.close()


def gather_tu(con, parsed):
    stats = Counter()
    kept = evidence.snapshots(con, [evidence.table_path(parsed, "trip_updates", "snapshots", d)
                                    for d in DAYS], stats)
    con.register("fresh_tu", pa.table({"file": [f for f, _ in kept]}))
    paths = [[evidence.table_path(parsed, "trip_updates", t, d) for d in DAYS]
             for t in ("trip_updates", "stop_time_updates")]
    return evidence.trip_updates(con, *paths, D, {"T1_LATE": "T1"}, stats), stats


# Evidence ------------------------------------------------------------------------------

def test_conflicting_copies_are_left_out_and_identical_copies_count_once(tmp_path, con):
    w = World()
    same = {"trip_id": "T1", "stops": [(1, "S1", T0 + 10, "SCHEDULED")]}
    w.tu(T0 + 40, [same, same])
    w.tu(T0 + 70, [{"trip_id": "T1", "stops": [(1, "S1", T0 + 99, "SCHEDULED")]},
                   {"trip_id": "T1", "stops": [(1, "S1", T0 + 11, "SCHEDULED")]}])
    w.write(tmp_path)
    (tu_stops, *_), stats = gather_tu(con, tmp_path)
    assert tu_stops[("T1", D)][1].time == T0 + 10        # the conflicting snapshot is ignored
    assert tu_stops[("T1", D)][1].seen_at == T0 + 40
    assert stats["tu trip copies in one snapshot, identical"] == 1
    assert stats["tu trip copies in one snapshot, conflicting (left out)"] == 1


def test_last_listing_without_a_time_is_not_replaced_by_an_older_one(tmp_path, con):
    w = World()
    w.tu(T0, [{"trip_id": "T1", "stops": [(2, "S2", T0 + 300, "SCHEDULED")]}])
    w.tu(T0 + 30, [{"trip_id": "T1", "stops": [(2, "S2", None, "NO_DATA")]}])
    w.write(tmp_path)
    (tu_stops, *_), _ = gather_tu(con, tmp_path)
    assert tu_stops[("T1", D)][2].time is None
    assert tu_stops[("T1", D)][2].seen_at == T0 + 30


def test_stale_snapshot_is_not_evidence(tmp_path, con):
    w = World()
    w.tu(T0, [{"trip_id": "T1", "stops": [(1, "S1", T0 - 5, "SCHEDULED")]}])
    w.tu(T0 - 60, [{"trip_id": "T1", "stops": [(1, "S1", T0 - 50, "SCHEDULED")]}],
         fetched=T0 + 50)  # feed time went backwards
    w.write(tmp_path)
    (tu_stops, *_), stats = gather_tu(con, tmp_path)
    assert tu_stops[("T1", D)][1].time == T0 - 5
    assert stats["snapshot not newer"] == 1


def test_cancellation_alias_and_skipped(tmp_path, con):
    w = World()
    w.tu(T0, [{"trip_id": "T1_LATE", "stops": [(3, "S3", None, "SKIPPED")]},
              {"trip_id": "T2", "rel": "CANCELED"}])
    w.write(tmp_path)
    (tu_stops, seen, cancelled, info), _ = gather_tu(con, tmp_path)
    assert tu_stops[("T1", D)][3].skipped
    assert seen == {("T1", D), ("T2", D)} and cancelled == {("T2", D)}
    assert info["T2"]["relationships"] == ["CANCELED"]


# A whole service date ------------------------------------------------------------------

def test_observe_date_end_to_end(tmp_path, con):
    w = World()
    normal_day(w)
    w.vp(T0 + 1500, [("V9", T0 + 1490, "X9", 1, "IN_TRANSIT_TO", None, None)])
    w.tu(T0 + 1500, [{"trip_id": "X9", "rel": "ADDED", "route_id": "R9",
                      "stops": [(1, "Q1", T0 + 1600, "SCHEDULED")]}])
    w.write(tmp_path)

    tables, stats = observe.observe_date(con, tmp_path, SERVICE, schedule_t1())
    v = {r["stop_sequence"]: r for r in tables["visits"].to_pylist()}
    for i in (1, 2, 3):
        source = "vp_position" if i == 1 else "tu"   # first stops go by position
        assert (v[i]["status"], v[i]["source"]) == ("observed", source), v[i]
        assert v[i]["observed"] == utc(departed(i)) and v[i]["delay_s"] == 15
    assert (v[4]["event"], v[4]["status"], v[4]["source"]) == ("arrival", "observed", "vp_stopped")
    assert v[4]["observed"] < utc(T0 + 900)                # arrived early: that's on time

    u = tables["unscheduled"].to_pylist()
    assert [(r["trip_id"], r["relationships"], r["in_tu"], r["in_vp"]) for r in u] == [
        ("X9", "ADDED", True, True)]

    gaps = tables["gaps"].to_pylist()   # before the first and after the last snapshot only
    assert {g["feed"] for g in gaps} == set(FEEDS) and len(gaps) == 4
    assert stats["trips"] == 1
    line = observe.summary(SERVICE, tables, stats, 1, 0.0)
    assert "4 scored visits: observed 100.0%" in line
    assert "disagree at 0 of 3 departures" in line


def test_outage_mid_trip(tmp_path, con):
    w = World()
    normal_day(w)
    # VehiclePositions lost from T0+290 to T0+560 (stop 2's departure falls inside)
    key = ("vehicle_positions", "vehicle_positions", DAYS[0])
    snaps = ("vehicle_positions", "snapshots", DAYS[0])
    dropped = {r["file"] for r in w.rows[snaps] if T0 + 290 < r["header_ts"].timestamp() < T0 + 560}
    w.rows[key] = [r for r in w.rows[key] if r["file"] not in dropped]
    w.rows[snaps] = [r for r in w.rows[snaps] if r["file"] not in dropped]
    w.write(tmp_path)

    tables, _ = observe.observe_date(con, tmp_path, SERVICE, schedule_t1())
    v = {r["stop_sequence"]: r for r in tables["visits"].to_pylist()}
    assert v[1]["status"] == "observed"
    assert v[2]["status"] == "no_data" and v[2]["tu_time"] == utc(departed(2))
    assert v[3]["status"] == "observed"


def test_missing_parsed_day_is_an_error(tmp_path, con):
    w = World()
    w.write(tmp_path)
    evidence.table_path(tmp_path, "vehicle_positions", "snapshots", DAYS[1]).unlink()
    with pytest.raises(FileNotFoundError, match="not parsed yet"):
        observe.observe_date(con, tmp_path, SERVICE, schedule_t1())


def test_outputs_round_trip_and_skip(tmp_path, con):
    parsed, out = tmp_path / "parsed", tmp_path / "out"
    w = World()
    normal_day(w)
    w.write(parsed)
    assert observe.parsed_dates(parsed, FEEDS) == [SERVICE]   # 11-06 has no 11-07

    tables, _ = observe.observe_date(con, parsed, SERVICE, schedule_t1())
    meta = {"rule_version": observe.RULE_VERSION, "static_feed_id": "1",
            "written_at": "now"}
    observe.write_outputs(out, SERVICE, tables, meta)
    assert observe.is_done(out, parsed, SERVICE, 1, FEEDS)
    assert not observe.is_done(out, parsed, SERVICE, 2, FEEDS)   # another static feed
    back = pq.read_table(out / "visits" / "2026-11-05.parquet")
    assert back.num_rows == 4 and back.schema.metadata[b"rule_version"] == observe.RULE_VERSION.encode()
    assert not list(out.rglob("*.tmp"))


def test_build_trips_scoring():
    rows = [
        ("A", "R", 0, 1, "S1", 100, 100, 0, 0.0, 0.0),        # first stop: scored anyway
        ("A", "R", 0, 2, "S2", None, None, 0, 0.0, 0.0),      # no time: not scored
        ("A", "R", 0, 3, "S3", 300, 300, 1, 0.0, 0.0),        # timepoint
        ("A", "R", 0, 4, "S4", 400, 400, 0, 0.0, 0.0),        # approximate
        ("A", "R", 0, 5, "S5", 500, 500, None, 0.0, 0.0),     # blank in a feed that marks them
        ("A", "R", 0, 6, "S6", 600, 600, 1, 0.0, 0.0),        # last stop
        ("B", "R", 1, 1, "S6", 700, 700, 1, 0.0, 0.0),
    ]
    trips = observe.build_trips(rows, SERVICE)
    assert [t.key for t in trips] == [("A", D), ("B", D)]
    assert [s.timepoint for s in trips[0].stops] == [True, False, True, False, False, True]

    # A feed with no timepoints marked at all: times given are exact (GTFS).
    blank = [(*r[:7], None, *r[8:]) for r in rows]
    trips = observe.build_trips(blank, SERVICE)
    assert [s.timepoint for s in trips[0].stops] == [True, False, True, True, True, True]
