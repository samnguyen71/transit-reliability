"""realtime.parse on tiny hand-built snapshots. No database needed.

Each test writes a day folder the way the logger does: <stamp>.pb.gz files, perhaps an
.invalid.gz, and _manifest.jsonl. It then parses the folder and reads the Parquet back.
"""

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from google.transit import gtfs_realtime_pb2 as pb

from realtime import parse
from realtime.schemas import TABLES

DAY = "2026-11-05"
T0 = int(datetime(2026, 11, 5, 13, 0, tzinfo=UTC).timestamp())


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def message(ts: int = T0) -> pb.FeedMessage:
    f = pb.FeedMessage()
    f.header.gtfs_realtime_version = "2.0"
    f.header.incrementality = pb.FeedHeader.FULL_DATASET
    f.header.timestamp = ts
    return f


def write_day(raw: Path, files: dict, manifest: list | None = None,
              feed: str = "trip_updates", day: str = DAY) -> Path:
    """files: name -> FeedMessage (gzipped like the logger does) or raw bytes."""
    day_dir = raw / feed / day
    day_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        data = gzip.compress(content.SerializeToString()) if isinstance(content, pb.FeedMessage) \
            else content
        (day_dir / name).write_bytes(data)
    if manifest is not None:
        lines = [m if isinstance(m, str) else json.dumps(m) for m in manifest]
        (day_dir / "_manifest.jsonl").write_text("".join(line + "\n" for line in lines))
    return day_dir


def rows(out: Path, table: str, feed: str = "trip_updates", day: str = DAY) -> list[dict]:
    return pq.read_table(out / feed / table / f"{day}.parquet").to_pylist()


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "raw", tmp_path / "parsed"


def test_unset_fields_are_null_not_defaults(dirs):
    raw, out = dirs
    f = message()
    tu = f.entity.add(id="1").trip_update
    tu.trip.trip_id = "T1"
    u = tu.stop_time_update.add(stop_sequence=3)
    u.arrival.time = T0 + 60                       # no delay, no departure, no relationship
    vp = f.entity.add(id="2").vehicle
    vp.vehicle.id = "V7"
    vp.trip.trip_id = "T1"
    vp.position.latitude, vp.position.longitude = 43.9, -78.85
    write_day(raw, {"20261105T130000Z.pb.gz": f})
    parse.parse_day(raw, out, "trip_updates", DAY)

    trip = rows(out, "trip_updates")[0]
    assert trip["direction_id"] is None and trip["start_date"] is None
    assert trip["trip_schedule_relationship"] is None and trip["delay"] is None
    stop = rows(out, "stop_time_updates")[0]
    assert stop["arrival_time"] == utc(T0 + 60)
    assert stop["arrival_delay"] is None           # unset, not 0 ("on time")
    assert stop["departure_time"] is None and stop["schedule_relationship"] is None
    assert stop["stop_id"] is None
    v = rows(out, "vehicle_positions")[0]
    assert v["current_status"] is None             # unset, not IN_TRANSIT_TO
    assert v["current_stop_sequence"] is None      # unset, not 0
    assert v["timestamp"] is None and v["bearing"] is None


def test_zero_times_are_null(dirs):
    raw, out = dirs
    f = message()
    vp = f.entity.add(id="1").vehicle
    vp.vehicle.id = "V7"
    vp.timestamp = 0                               # DRT sends this
    tu = f.entity.add(id="2").trip_update
    tu.trip.trip_id = "T1"
    tu.timestamp = 0
    u = tu.stop_time_update.add(stop_sequence=1)
    u.arrival.time = 0
    u.arrival.delay = 30
    write_day(raw, {"20261105T130000Z.pb.gz": f})
    parse.parse_day(raw, out, "trip_updates", DAY)

    assert rows(out, "vehicle_positions")[0]["timestamp"] is None
    assert rows(out, "trip_updates")[0]["timestamp"] is None
    stop = rows(out, "stop_time_updates")[0]
    assert (stop["arrival_time"], stop["arrival_delay"]) == (None, 30)


def test_values_and_enum_names(dirs):
    raw, out = dirs
    f = message()
    tu = f.entity.add(id="e1").trip_update
    tu.trip.trip_id, tu.trip.route_id, tu.trip.direction_id = "T1", "R1", 0
    tu.trip.start_date = "20261105"
    tu.vehicle.id, tu.vehicle.label = "V7", "1207"
    tu.timestamp = T0 - 5
    u = tu.stop_time_update.add(stop_sequence=4, stop_id="S4")
    u.arrival.time = u.departure.time = T0 + 90
    u.schedule_relationship = pb.TripUpdate.StopTimeUpdate.SKIPPED
    vp = f.entity.add(id="e2").vehicle
    vp.vehicle.id = "V7"
    vp.trip.trip_id = "T1"
    vp.current_stop_sequence, vp.stop_id = 4, "S4"
    vp.current_status = pb.VehiclePosition.STOPPED_AT
    vp.timestamp = T0 - 10
    vp.position.latitude, vp.position.longitude, vp.position.bearing = 43.9, -78.85, 90.0
    write_day(raw, {"20261105T130000Z.pb.gz": f})
    parse.parse_day(raw, out, "trip_updates", DAY)

    trip = rows(out, "trip_updates")[0]
    assert (trip["file"], trip["header_ts"], trip["entity_index"], trip["entity_id"]) == \
        ("20261105T130000Z.pb.gz", utc(T0), 0, "e1")
    assert (trip["trip_id"], trip["route_id"], trip["direction_id"]) == ("T1", "R1", 0)
    assert (trip["vehicle_id"], trip["vehicle_label"], trip["timestamp"]) == \
        ("V7", "1207", utc(T0 - 5))
    stop = rows(out, "stop_time_updates")[0]
    assert (stop["entity_index"], stop["update_index"], stop["trip_id"], stop["start_date"]) == \
        (0, 0, "T1", "20261105")
    assert (stop["departure_time"], stop["schedule_relationship"]) == (utc(T0 + 90), "SKIPPED")
    v = rows(out, "vehicle_positions")[0]
    assert (v["entity_index"], v["current_status"], v["current_stop_sequence"]) == \
        (1, "STOPPED_AT", 4)
    assert v["latitude"] == pytest.approx(43.9) and v["bearing"] == 90.0
    snap = rows(out, "snapshots")[0]
    assert (snap["status"], snap["gtfs_realtime_version"], snap["incrementality"]) == \
        ("ok", "2.0", "FULL_DATASET")
    assert (snap["entities"], snap["trip_updates"], snap["vehicles"], snap["alerts"]) == \
        (2, 1, 1, 0)


def test_cancelled_trip_without_stop_updates_still_has_a_row(dirs):
    raw, out = dirs
    f = message()
    tu = f.entity.add(id="1").trip_update
    tu.trip.trip_id = "T9"
    tu.trip.schedule_relationship = pb.TripDescriptor.CANCELED
    write_day(raw, {"20261105T130000Z.pb.gz": f})
    parse.parse_day(raw, out, "trip_updates", DAY)

    trip = rows(out, "trip_updates")[0]
    assert (trip["trip_id"], trip["trip_schedule_relationship"], trip["stop_time_updates"]) == \
        ("T9", "CANCELED", 0)
    assert rows(out, "stop_time_updates") == []


def test_same_trip_twice_is_kept_as_sent(dirs):
    # DRT does this ~400 times a day. Deciding what to do with it is the rule's job.
    raw, out = dirs
    f = message()
    for n in (1, 2):
        tu = f.entity.add(id=f"e{n}").trip_update
        tu.trip.trip_id = "T1"
        tu.stop_time_update.add(stop_sequence=1).arrival.time = T0 + 10 * n
    write_day(raw, {"20261105T130000Z.pb.gz": f})
    parse.parse_day(raw, out, "trip_updates", DAY)

    assert [(r["entity_index"], r["trip_id"]) for r in rows(out, "trip_updates")] == \
        [(0, "T1"), (1, "T1")]
    assert [(r["entity_index"], r["arrival_time"]) for r in rows(out, "stop_time_updates")] == \
        [(0, utc(T0 + 10)), (1, utc(T0 + 20))]


def test_invalid_and_unreadable_files_are_listed_and_skipped(dirs):
    raw, out = dirs
    ok = message()
    ok.entity.add(id="1").vehicle.vehicle.id = "V7"
    write_day(raw, {
        "20261105T130000Z.pb.gz": ok,
        "20261105T130030Z.invalid.gz": gzip.compress(b"<html>Service Unavailable</html>"),
        "20261105T130100Z.pb.gz": b"not gzip at all",
        "20261105T130130Z.pb.gz": gzip.compress(b"\xff\xff\xff not protobuf"),
    })
    stats = parse.parse_day(raw, out, "trip_updates", DAY)

    snaps = rows(out, "snapshots")
    assert [(s["file"][9:15], s["status"]) for s in snaps] == [
        ("130000", "ok"), ("130030", "invalid"), ("130100", "unreadable"), ("130130", "unreadable")]
    assert snaps[2]["error"].startswith("BadGzipFile")
    assert snaps[3]["error"].startswith("DecodeError")
    assert snaps[1]["header_ts"] is None and snaps[1]["stored_bytes"] > 0
    assert len(rows(out, "vehicle_positions")) == 1
    assert (stats["snapshots ok"], stats["snapshots invalid"], stats["snapshots unreadable"]) == \
        (1, 1, 2)


def test_empty_feed_gives_empty_tables_with_the_full_schema(dirs):
    raw, out = dirs
    write_day(raw, {"20261105T130000Z.pb.gz": message()})
    parse.parse_day(raw, out, "trip_updates", DAY)

    assert rows(out, "snapshots")[0]["entities"] == 0
    for table, schema in TABLES.items():
        t = pq.read_table(out / "trip_updates" / table / f"{DAY}.parquet")
        assert t.schema.remove_metadata() == schema, table
    assert rows(out, "trip_updates") == [] and rows(out, "polls") == []


def test_polls_keep_failures_and_skip_a_cut_off_line(dirs):
    raw, out = dirs
    manifest = [
        {"fetched_at": "2026-11-05T13:00:02.123Z", "status": "saved", "http_status": 200,
         "bytes": 100, "stored_bytes": 80, "sha256": "ab", "file": "20261105T130000Z.pb.gz",
         "feed_timestamp": T0 - 20, "feed_age_s": 20, "entities": 0, "elapsed_ms": 150,
         "error": None},
        {"fetched_at": "2026-11-05T13:00:32.000Z", "status": "network_error",
         "error": "ConnectTimeout: timed out"},
        {"fetched_at": "2026-11-05T13:01:02.000Z", "status": "duplicate", "http_status": 200},
        '{"fetched_at": "2026-11-05T13:01:32.0',   # the logger was killed mid-write
    ]
    write_day(raw, {"20261105T130000Z.pb.gz": message()}, manifest)
    stats = parse.parse_day(raw, out, "trip_updates", DAY)

    polls = rows(out, "polls")
    assert [p["status"] for p in polls] == ["saved", "network_error", "duplicate"]
    assert polls[0]["fetched_at"] == datetime(2026, 11, 5, 13, 0, 2, 123000, tzinfo=UTC)
    assert (polls[0]["feed_timestamp"], polls[0]["feed_age_s"]) == (utc(T0 - 20), 20.0)
    assert polls[1]["error"] == "ConnectTimeout: timed out" and polls[1]["http_status"] is None
    assert stats["manifest lines skipped"] == 1


def test_files_in_time_order_with_fetch_time_from_the_name(dirs):
    raw, out = dirs
    write_day(raw, {
        "20261105T130030Z.pb.gz": message(T0 + 20),
        "20261105T130000Z_1.pb.gz": message(T0 + 1),   # a second save in the same second
        "20261105T130000Z.pb.gz": message(T0),
    })
    parse.parse_day(raw, out, "trip_updates", DAY)

    assert [(s["file"], s["fetched_at"], s["header_ts"]) for s in rows(out, "snapshots")] == [
        ("20261105T130000Z.pb.gz", utc(T0), utc(T0)),
        ("20261105T130000Z_1.pb.gz", utc(T0), utc(T0 + 1)),
        ("20261105T130030Z.pb.gz", utc(T0 + 30), utc(T0 + 20)),
    ]


def test_small_row_groups_give_the_same_rows(dirs, monkeypatch):
    raw, out = dirs
    files = {}
    for k in range(5):
        f = message(T0 + 30 * k)
        tu = f.entity.add(id="1").trip_update
        tu.trip.trip_id = "T1"
        for s in range(1, 4):
            tu.stop_time_update.add(stop_sequence=s).arrival.time = T0 + 100 * s
        files[f"20261105T1300{k}0Z.pb.gz"] = f
    write_day(raw, files)
    parse.parse_day(raw, out, "trip_updates", DAY)
    whole = rows(out, "stop_time_updates")

    monkeypatch.setattr(parse, "FLUSH_ROWS", 2)
    parse.parse_day(raw, out, "trip_updates", DAY)
    path = out / "trip_updates" / "stop_time_updates" / f"{DAY}.parquet"
    assert pq.ParquetFile(path).num_row_groups > 1
    assert rows(out, "stop_time_updates") == whole and len(whole) == 15


def test_a_failed_day_leaves_no_files(dirs, monkeypatch):
    raw, out = dirs
    write_day(raw, {"20261105T130000Z.pb.gz": message(), "20261105T130030Z.pb.gz": message()})

    calls = []

    def boom(path, writers):
        calls.append(path)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return "ok"

    monkeypatch.setattr(parse, "parse_snapshot", boom)
    with pytest.raises(RuntimeError):
        parse.parse_day(raw, out, "trip_updates", DAY)
    assert [p for p in out.rglob("*") if p.is_file()] == []


def test_main_skips_today_static_and_parsed_days(dirs, capsys):
    raw, out = dirs
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    write_day(raw, {"20261105T130000Z.pb.gz": message()})
    write_day(raw, {"20261105T130000Z.pb.gz": message()}, day=today)
    static = raw / "static_gtfs" / DAY
    static.mkdir(parents=True)
    (static / "20261105T130000Z.zip").write_bytes(b"PK")

    assert parse.main([str(raw), str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"trip_updates {DAY}: 1 snapshots" in printed
    assert f"trip_updates {today}: skipped" in printed
    assert not (out / "static_gtfs").exists()

    first = (out / "trip_updates" / "snapshots" / f"{DAY}.parquet").stat().st_mtime_ns
    assert parse.main([str(raw), str(out)]) == 0      # already parsed: left alone
    assert f"trip_updates {DAY}" not in capsys.readouterr().out
    assert (out / "trip_updates" / "snapshots" / f"{DAY}.parquet").stat().st_mtime_ns == first

    assert parse.main([str(raw), str(out), "--day", DAY, "--force"]) == 0
    assert f"trip_updates {DAY}: 1 snapshots" in capsys.readouterr().out


def test_a_new_raw_file_makes_the_day_stale(dirs):
    raw, out = dirs
    day_dir = write_day(raw, {"20261105T130000Z.pb.gz": message()})
    parse.parse_day(raw, out, "trip_updates", DAY)
    assert parse.is_parsed(raw, out, "trip_updates", DAY)

    late = day_dir / "20261105T235959Z.pb.gz"
    late.write_bytes(gzip.compress(message().SerializeToString()))
    newest = max(p.stat().st_mtime for p in (out / "trip_updates").rglob("*.parquet")) + 5
    os.utime(late, (newest, newest))
    assert not parse.is_parsed(raw, out, "trip_updates", DAY)


def test_missing_day_is_a_failure(dirs, capsys):
    raw, out = dirs
    write_day(raw, {"20261105T130000Z.pb.gz": message()})
    assert parse.main([str(raw), str(out), "--day", "2026-11-04"]) == 1
    assert "no such day folder" in capsys.readouterr().err
