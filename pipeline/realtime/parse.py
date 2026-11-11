"""Parse the logger's raw GTFS-realtime snapshots into Parquet.

    python -m realtime.parse RAW_DIR OUT_DIR [--feed NAME ...] [--day YYYY-MM-DD ...] [--force]

RAW_DIR is the logger's data/raw. For each realtime feed folder (static_gtfs is skipped)
and each UTC day, this writes OUT_DIR/<feed>/<table>/<day>.parquet for every table in
realtime.schemas.TABLES. Every table is written for every day, empty if need be, so a
reader can rely on the files being there.

- Only complete days are parsed. The logger is still writing today's folder (UTC).
- A day is skipped if all its tables exist, were written together by this parser
  version, and are newer than everything in its raw folder. --force reparses anyway.
- Output is written to temp files and renamed at the end. A failed day leaves its
  previous output, or nothing, never half a file.

The raw snapshots stay the source of truth: everything here can be rebuilt from them.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import zlib
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as pb

from realtime.schemas import TABLES

PARSER_VERSION = "1"
FLUSH_ROWS = 100_000  # rows buffered per table before writing a row group (bounds memory)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The logger's names: 20261105T130002Z.pb.gz, ..._1.pb.gz, ....invalid.gz
FILE_RE = re.compile(r"^(\d{8}T\d{6}Z)(?:_\d+)?\.(pb|invalid)\.gz$")
MANIFEST = "_manifest.jsonl"

_TRIP_REL = pb.TripDescriptor.ScheduleRelationship
_STOP_REL = pb.TripUpdate.StopTimeUpdate.ScheduleRelationship
_STATUS = pb.VehiclePosition.VehicleStopStatus
_CONGESTION = pb.VehiclePosition.CongestionLevel
_OCCUPANCY = pb.VehiclePosition.OccupancyStatus
_INCREMENTALITY = pb.FeedHeader.Incrementality


# Reading fields ------------------------------------------------------------------------

def opt(msg, field: str):
    """The field's value, or None if the feed didn't set it (never the protobuf default)."""
    return getattr(msg, field) if msg is not None and msg.HasField(field) else None


def opt_time(msg, field: str) -> int | None:
    """A POSIX time, converted from seconds to the tables' milliseconds; None if unset or 0."""
    seconds = opt(msg, field)
    return seconds * 1000 if seconds else None


def opt_enum(enum, msg, field: str) -> str | None:
    value = opt(msg, field)
    if value is None:
        return None
    try:
        return enum.Name(value)
    except ValueError:  # a value newer than these bindings
        return str(value)


def opt_msg(msg, field: str):
    """A sub-message, or None if unset (an unset sub-message reads as all defaults)."""
    return getattr(msg, field) if msg is not None and msg.HasField(field) else None


def _trip(t) -> tuple:
    return (
        opt(t, "trip_id"),
        opt(t, "route_id"),
        opt(t, "direction_id"),
        opt(t, "start_date"),
        opt(t, "start_time"),
        opt_enum(_TRIP_REL, t, "schedule_relationship"),
    )


def _event(ev) -> tuple:
    return opt_time(ev, "time"), opt(ev, "delay"), opt(ev, "uncertainty")


# Writing tables ------------------------------------------------------------------------

class TableWriter:
    """Buffers rows (tuples in schema order) and writes them as row groups to a temp file."""

    def __init__(self, path: Path, schema: pa.Schema, metadata: dict[str, str]):
        self.path = path
        self.tmp = path.with_name(path.name + ".tmp")
        self.schema = schema.with_metadata(metadata)
        self.buffer: list[tuple] = []
        self.count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.writer = pq.ParquetWriter(self.tmp, self.schema, compression="zstd")

    def add(self, row: tuple) -> None:
        self.buffer.append(row)
        if len(self.buffer) >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        columns = zip(*self.buffer, strict=True)
        arrays = [pa.array(col, type=f.type) for col, f in zip(columns, self.schema, strict=True)]
        self.writer.write_batch(pa.record_batch(arrays, schema=self.schema))
        self.count += len(self.buffer)
        self.buffer = []

    def commit(self) -> None:
        self.flush()
        self.writer.close()
        os.replace(self.tmp, self.path)

    def abort(self) -> None:
        try:
            self.writer.close()
        finally:
            self.tmp.unlink(missing_ok=True)


# Parsing -------------------------------------------------------------------------------

def _iso_ms(text: str) -> int:
    return round(datetime.fromisoformat(text).timestamp() * 1000)


def read_manifest(path: Path, stats: Counter) -> Iterator[tuple]:
    """Rows for the polls table. A line that isn't valid JSON (say, cut off by a crash
    mid-write) is counted in stats and skipped."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            row = (
                _iso_ms(r["fetched_at"]),
                r["status"],
                r.get("http_status"),
                r.get("bytes"),
                r.get("stored_bytes"),
                r.get("sha256"),
                r.get("file"),
                (r.get("feed_timestamp") or 0) * 1000 or None,
                r.get("feed_age_s"),
                r.get("entities"),
                r.get("elapsed_ms"),
                r.get("error"),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            stats["manifest lines skipped"] += 1
            continue
        yield row


def parse_snapshot(path: Path, out: dict[str, TableWriter]) -> str:
    """Add one snapshot file's rows to the tables. Returns its status."""
    m = FILE_RE.match(path.name)
    stamp = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    fetched = int(stamp.timestamp()) * 1000
    size = path.stat().st_size
    name = path.name
    if m.group(2) == "invalid":
        out["snapshots"].add((name, fetched, "invalid", None, size) + (None,) * 7)
        return "invalid"

    feed = pb.FeedMessage()
    try:
        feed.ParseFromString(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, zlib.error, DecodeError) as e:
        out["snapshots"].add((name, fetched, "unreadable", f"{type(e).__name__}: {e}", size)
                             + (None,) * 7)
        return "unreadable"

    header = feed.header
    h = opt_time(header, "timestamp")
    n_tu = n_vp = n_alerts = 0
    for i, e in enumerate(feed.entity):
        source = (name, h, i, opt(e, "id"))
        deleted = opt(e, "is_deleted")
        if e.HasField("trip_update"):
            n_tu += 1
            _trip_update(out, source, deleted, e.trip_update)
        if e.HasField("vehicle"):
            n_vp += 1
            _vehicle(out, source, deleted, e.vehicle)
        if e.HasField("alert"):
            n_alerts += 1
    out["snapshots"].add((
        name, fetched, "ok", None, size, h,
        opt(header, "gtfs_realtime_version"),
        opt_enum(_INCREMENTALITY, header, "incrementality"),
        len(feed.entity), n_tu, n_vp, n_alerts,
    ))
    return "ok"


def _trip_update(out: dict[str, TableWriter], source: tuple, deleted, tu) -> None:
    t = opt_msg(tu, "trip")
    v = opt_msg(tu, "vehicle")
    out["trip_updates"].add(source + _trip(t) + (
        deleted, opt(v, "id"), opt(v, "label"),
        opt_time(tu, "timestamp"), opt(tu, "delay"), len(tu.stop_time_update),
    ))
    trip_id, start_date = opt(t, "trip_id"), opt(t, "start_date")
    stops = out["stop_time_updates"]
    for j, u in enumerate(tu.stop_time_update):
        stops.add(source + (j, trip_id, start_date, opt(u, "stop_sequence"), opt(u, "stop_id"))
                  + _event(opt_msg(u, "arrival")) + _event(opt_msg(u, "departure"))
                  + (opt_enum(_STOP_REL, u, "schedule_relationship"),))


def _vehicle(out: dict[str, TableWriter], source: tuple, deleted, vp) -> None:
    v = opt_msg(vp, "vehicle")
    p = opt_msg(vp, "position")
    out["vehicle_positions"].add(source + _trip(opt_msg(vp, "trip")) + (
        deleted, opt(v, "id"), opt(v, "label"),
        opt(p, "latitude"), opt(p, "longitude"), opt(p, "bearing"), opt(p, "speed"),
        opt(vp, "current_stop_sequence"), opt(vp, "stop_id"),
        opt_enum(_STATUS, vp, "current_status"),
        opt_time(vp, "timestamp"),
        opt_enum(_CONGESTION, vp, "congestion_level"),
        opt_enum(_OCCUPANCY, vp, "occupancy_status"),
    ))


def output_paths(out: Path, feed: str, day: str) -> dict[str, Path]:
    return {table: out / feed / table / f"{day}.parquet" for table in TABLES}


def parse_day(raw: Path, out: Path, feed: str, day: str) -> Counter:
    """Parse RAW/<feed>/<day> into OUT/<feed>/<table>/<day>.parquet. Returns counts."""
    day_dir = raw / feed / day
    meta = {
        "parser_version": PARSER_VERSION,
        "feed": feed,
        "day": day,
        "written_at": datetime.now(UTC).isoformat(),  # the same for all tables of a run
    }
    writers = {table: TableWriter(path, TABLES[table], meta)
               for table, path in output_paths(out, feed, day).items()}
    stats: Counter = Counter()
    try:
        manifest = day_dir / MANIFEST
        if manifest.exists():
            for row in read_manifest(manifest, stats):
                writers["polls"].add(row)
        for path in sorted(day_dir.iterdir()):
            if FILE_RE.match(path.name):
                stats[f"snapshots {parse_snapshot(path, writers)}"] += 1
            elif path.name != MANIFEST:
                stats["other files ignored"] += 1
    except BaseException:
        for w in writers.values():
            w.abort()
        raise
    for table, w in writers.items():
        w.commit()
        stats[f"rows {table}"] = w.count
    return stats


def is_parsed(raw: Path, out: Path, feed: str, day: str) -> bool:
    paths = list(output_paths(out, feed, day).values())
    if not all(p.exists() for p in paths):
        return False
    metas = [pq.read_schema(p).metadata or {} for p in paths]
    if any(m.get(b"parser_version") != PARSER_VERSION.encode() for m in metas):
        return False
    if len({m.get(b"written_at") for m in metas}) != 1:  # tables from different runs
        return False
    raw_times = [p.stat().st_mtime for p in (raw / feed / day).iterdir()]
    return min(p.stat().st_mtime for p in paths) >= max(raw_times, default=0)


def realtime_feeds(raw: Path) -> list[str]:
    """Feed folders holding realtime snapshots (static_gtfs holds zips, so it's left out)."""
    feeds = []
    for feed_dir in sorted(p for p in raw.iterdir() if p.is_dir()):
        days = (d for d in feed_dir.iterdir() if d.is_dir() and DAY_RE.match(d.name))
        if any(FILE_RE.match(f.name) for d in days for f in d.iterdir()):
            feeds.append(feed_dir.name)
    return feeds


def _summary(feed: str, day: str, stats: Counter, seconds: float) -> str:
    snaps = sum(n for k, n in stats.items() if k.startswith("snapshots "))
    bad = ", ".join(f"{stats[k]} {k.split()[1]}" for k in ("snapshots unreadable",
                                                           "snapshots invalid") if stats[k])
    rows = ", ".join(f"{k.split()[1]} {n:,}" for k, n in stats.items()
                     if k.startswith("rows ") and n)
    extra = ", ".join(f"{k} {stats[k]}" for k in ("manifest lines skipped", "other files ignored")
                      if stats[k])
    parts = [f"{snaps} snapshots" + (f" ({bad})" if bad else ""), rows, extra]
    return f"{feed} {day}: " + "; ".join(p for p in parts if p) + f" in {seconds:.0f}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Parse raw GTFS-realtime snapshots into Parquet.")
    ap.add_argument("raw", type=Path, help="the logger's data/raw folder")
    ap.add_argument("out", type=Path, help="where to write <feed>/<table>/<day>.parquet")
    ap.add_argument("--feed", action="append", help="only this feed (repeatable)")
    ap.add_argument("--day", action="append", help="only this UTC day, YYYY-MM-DD (repeatable)")
    ap.add_argument("--force", action="store_true", help="reparse days already parsed")
    args = ap.parse_args(argv)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    failures = 0
    for feed in args.feed or realtime_feeds(args.raw):
        feed_dir = args.raw / feed
        if not feed_dir.is_dir():
            print(f"{feed}: no folder {feed_dir}", file=sys.stderr)
            failures += 1
            continue
        days = args.day or sorted(d.name for d in feed_dir.iterdir()
                                  if d.is_dir() and DAY_RE.match(d.name))
        for day in days:
            if not DAY_RE.match(day) or not (feed_dir / day).is_dir():
                print(f"{feed} {day}: no such day folder", file=sys.stderr)
                failures += 1
                continue
            if day >= today:
                print(f"{feed} {day}: skipped, the day isn't over yet (UTC)")
                continue
            if not args.force and is_parsed(args.raw, args.out, feed, day):
                continue
            started = time.monotonic()
            try:
                stats = parse_day(args.raw, args.out, feed, day)
            except Exception as e:  # noqa: BLE001 - report the day and go on to the next
                print(f"{feed} {day}: failed, nothing written: {type(e).__name__}: {e}",
                      file=sys.stderr)
                failures += 1
                continue
            print(_summary(feed, day, stats, time.monotonic() - started))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
