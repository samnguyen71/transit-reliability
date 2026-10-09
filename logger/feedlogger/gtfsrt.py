"""A small reader for the GTFS-realtime format.

The logger stores feeds byte for byte. This module only looks inside them, to
check that a response really is a GTFS-realtime feed (not an HTML error page,
say) and to summarize what a feed contains for `peek`.

It reads the protobuf wire format directly, so the logger doesn't depend on
generated code. Field numbers come from the official gtfs-realtime.proto:
https://gtfs.org/documentation/realtime/proto/
For Phase 1, parse snapshots with the official gtfs-realtime-bindings package.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

# Protobuf wire types: https://protobuf.dev/programming-guides/encoding/
VARINT, I64, LEN, I32 = 0, 1, 2, 5

INCREMENTALITY = {0: "full dataset", 1: "differential"}
VEHICLE_STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}
TRIP_RELATIONSHIP = {
    0: "scheduled",
    1: "added",
    2: "unscheduled",
    3: "canceled",
    5: "replacement",
    6: "duplicated",
    7: "deleted",
    8: "new",
}

def iter_fields(buf: bytes | memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    """Yield (field number, wire type, value) for each field of one message.

    Varint and fixed-size values come back as ints; length-delimited values
    (strings, bytes, nested messages) as memoryviews into `buf`.
    Raises ValueError if the bytes aren't a well-formed message.
    """
    buf = memoryview(buf)
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("field number 0")
        if wire == VARINT:
            value, pos = _varint(buf, pos)
        elif wire == LEN:
            length, pos = _varint(buf, pos)
            if pos + length > end:
                raise ValueError("field runs past the end of the data")
            value = buf[pos : pos + length]
            pos += length
        elif wire in (I64, I32):
            size = 8 if wire == I64 else 4
            if pos + size > end:
                raise ValueError("field runs past the end of the data")
            value = int.from_bytes(buf[pos : pos + size], "little")
            pos += size
        else:
            raise ValueError(f"unexpected wire type {wire}")
        yield number, wire, value


def _varint(buf: memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("data ends in the middle of a number")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("number is too long")


def signed(value: int) -> int:
    """int32 and int64 fields store negative numbers as 64-bit two's complement."""
    return value - (1 << 64) if value >= 1 << 63 else value


def _text(value: int | memoryview) -> str:
    return bytes(value).decode("utf-8", "replace") if isinstance(value, memoryview) else ""


@dataclass(frozen=True)
class Header:
    version: str
    incrementality: int
    timestamp: int | None  # POSIX seconds; required by the spec since 2.0
    entity_count: int


def read_header(data: bytes) -> Header:
    """Check that `data` is a GTFS-realtime FeedMessage and return its header.

    Only the top level is read, so this is fast even for large feeds.
    Raises ValueError if `data` isn't a GTFS-realtime feed.
    """
    header = None
    entities = 0
    for number, wire, value in iter_fields(data):
        if number == 1:  # FeedMessage.header
            if wire != LEN:
                raise ValueError("header has the wrong type")
            header = value
        elif number == 2:  # FeedMessage.entity
            if wire != LEN:
                raise ValueError("entity has the wrong type")
            entities += 1
    if header is None:
        raise ValueError("no feed header")

    version, incrementality, timestamp = "", 0, None
    for number, wire, value in iter_fields(header):
        if number == 1 and wire == LEN:
            version = _text(value)
        elif number == 2 and wire == VARINT:
            incrementality = value
        elif number == 3 and wire == VARINT:
            timestamp = value
    if not version or len(version) > 16 or not version.isprintable():
        raise ValueError("feed header has no valid gtfs_realtime_version")
    return Header(version, incrementality, timestamp, entities)


@dataclass
class Summary:
    """What's inside one snapshot, for `peek`."""

    header: Header
    kinds: Counter[str] = field(default_factory=Counter)

    vehicles: int = 0
    vehicle_trip_id: int = 0
    vehicle_stop_sequence: int = 0
    vehicle_stop_id: int = 0
    vehicle_status: Counter[str] = field(default_factory=Counter)
    vehicle_timestamp: int = 0
    vehicle_ages: list[int] = field(default_factory=list)  # feed time - vehicle time

    trip_updates: int = 0
    trip_update_trip_id: int = 0
    trip_relationship: Counter[str] = field(default_factory=Counter)
    stop_updates: int = 0
    stop_updates_with_time: int = 0
    stop_updates_delay_only: int = 0
    stop_updates_past: int = 0  # times more than a minute before the feed timestamp
    stop_updates_skipped: int = 0


def summarize(data: bytes) -> Summary:
    """Read every entity in a snapshot and count what it contains."""
    summary = Summary(header=read_header(data))
    feed_time = summary.header.timestamp
    for number, wire, entity in iter_fields(data):
        if number != 2 or wire != LEN:
            continue
        kind = "other"
        for e_number, e_wire, value in iter_fields(entity):
            if e_number == 2 and e_wire == VARINT and value:
                kind = "deleted"
            elif e_wire != LEN:
                continue
            elif e_number == 3:
                kind = "trip_update"
                _read_trip_update(value, summary, feed_time)
            elif e_number == 4:
                kind = "vehicle"
                _read_vehicle(value, summary, feed_time)
            elif e_number == 5:
                kind = "alert"
        summary.kinds[kind] += 1
    return summary


def _read_vehicle(buf: memoryview, s: Summary, feed_time: int | None) -> None:
    s.vehicles += 1
    has_trip_id = has_stop_id = has_sequence = False
    status = timestamp = None
    for number, wire, value in iter_fields(buf):
        if number == 1 and wire == LEN:  # trip
            has_trip_id = any(n == 1 and w == LEN and len(v) for n, w, v in iter_fields(value))
        elif number == 3 and wire == VARINT:
            has_sequence = True
        elif number == 4 and wire == VARINT:
            status = value
        elif number == 5 and wire == VARINT:
            timestamp = value
        elif number == 7 and wire == LEN and len(value):
            has_stop_id = True
    s.vehicle_trip_id += has_trip_id
    s.vehicle_stop_sequence += has_sequence
    s.vehicle_stop_id += has_stop_id
    if status is None:
        s.vehicle_status["not given"] += 1
    else:
        s.vehicle_status[VEHICLE_STATUS.get(status, f"value {status}")] += 1
    if timestamp is not None:
        s.vehicle_timestamp += 1
        if feed_time:
            s.vehicle_ages.append(feed_time - timestamp)


def _read_trip_update(buf: memoryview, s: Summary, feed_time: int | None) -> None:
    s.trip_updates += 1
    has_trip_id = False
    relationship = 0
    for number, wire, value in iter_fields(buf):
        if number == 1 and wire == LEN:  # trip
            for t_number, t_wire, t_value in iter_fields(value):
                if t_number == 1 and t_wire == LEN and len(t_value):
                    has_trip_id = True
                elif t_number == 4 and t_wire == VARINT:
                    relationship = t_value
        elif number == 2 and wire == LEN:  # stop_time_update
            _read_stop_update(value, s, feed_time)
    s.trip_update_trip_id += has_trip_id
    s.trip_relationship[TRIP_RELATIONSHIP.get(relationship, f"value {relationship}")] += 1


def _read_stop_update(buf: memoryview, s: Summary, feed_time: int | None) -> None:
    s.stop_updates += 1
    times: list[int] = []
    has_delay = skipped = False
    for number, wire, value in iter_fields(buf):
        if number in (2, 3) and wire == LEN:  # arrival, departure
            for ev_number, ev_wire, ev_value in iter_fields(value):
                if ev_number == 2 and ev_wire == VARINT:
                    times.append(signed(ev_value))
                elif ev_number == 1 and ev_wire == VARINT:
                    has_delay = True
        elif number == 5 and wire == VARINT and value == 1:  # SKIPPED
            skipped = True
    s.stop_updates_skipped += skipped
    if times:
        s.stop_updates_with_time += 1
        if feed_time and min(times) < feed_time - 60:
            s.stop_updates_past += 1
    elif has_delay:
        s.stop_updates_delay_only += 1
