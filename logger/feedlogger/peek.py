"""`python -m feedlogger peek`: fetch a feed once and describe what's in it.

Use it to check that a URL and API key work, and whether a feed has what
the project needs, before you settle on an agency.
"""

from __future__ import annotations

import csv
import gzip
import io
import statistics
import time
import zipfile
from collections.abc import Mapping

import requests

from feedlogger import gtfsrt
from feedlogger.fmt import human_bytes, human_duration

# Files whose rows are worth counting in a static GTFS zip.
_COUNTED = {
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "shapes.txt",
}


def fetch(
    url: str,
    headers: Mapping[str, str],
    user_agent: str,
    timeout: float = 60,
    max_bytes: int = 500 * 1024 * 1024,
) -> tuple[int, str, bytes, float]:
    """GET `url`. Returns (status, reason, body, seconds taken)."""
    all_headers = {"User-Agent": user_agent, "Connection": "close", **headers}
    started = time.monotonic()
    with requests.get(url, headers=all_headers, timeout=(10, timeout), stream=True) as resp:
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=256 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise requests.RequestException(f"response is bigger than {human_bytes(max_bytes)}")
            chunks.append(chunk)
        return resp.status_code, resp.reason or "", b"".join(chunks), time.monotonic() - started


def describe(data: bytes, interval: float = 30, fetched_at: float | None = None) -> str:
    """Describe a snapshot: GTFS-realtime bytes or a static GTFS zip."""
    if data[:2] == b"\x1f\x8b":  # a saved .pb.gz snapshot
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as e:
            raise ValueError(f"looks gzipped but won't decompress: {e}") from None
    if data[:4] == b"PK\x03\x04":
        return describe_static(data)
    summary = gtfsrt.summarize(data)  # raises ValueError if it isn't GTFS-realtime
    return describe_realtime(
        summary,
        size=len(data),
        gzipped=len(gzip.compress(data, compresslevel=6)),
        interval=interval,
        fetched_at=fetched_at,
    )


def describe_realtime(
    s: gtfsrt.Summary,
    *,
    size: int,
    gzipped: int,
    interval: float,
    fetched_at: float | None = None,
) -> str:
    h = s.header
    mode = gtfsrt.INCREMENTALITY.get(h.incrementality, f"incrementality {h.incrementality}")
    entities = "entity" if h.entity_count == 1 else "entities"
    lines = [f"GTFS-realtime {h.version}, {mode}, {h.entity_count:,} {entities}"]
    if h.timestamp:
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(h.timestamp))
        if fetched_at is not None:
            age = fetched_at - h.timestamp
            when += f" ({human_duration(age)} {'before' if age >= 0 else 'after'} this fetch)"
        lines.append(f"Feed timestamp: {when}")
    else:
        lines.append("Feed timestamp: missing (required since GTFS-realtime 2.0)")
    lines.append(f"Size: {human_bytes(size)}, {human_bytes(gzipped)} gzipped")

    lines += ["", "Contents"]
    for label, key in (
        ("trip updates", "trip_update"),
        ("vehicle positions", "vehicle"),
        ("alerts", "alert"),
        ("other", "other"),
        ("deleted", "deleted"),
    ):
        if s.kinds[key] or key in ("trip_update", "vehicle"):
            lines.append(f"  {label:<38}{s.kinds[key]:>8,}")

    if s.vehicles:
        n = s.vehicles
        statuses = {k: v for k, v in s.vehicle_status.items() if k != "not given"}
        given = sum(statuses.values())
        lines += ["", f"Vehicle positions ({n:,})"]
        lines.append(_row("say which trip they're on (trip_id)", s.vehicle_trip_id, n))
        lines.append(_row("give the stop sequence", s.vehicle_stop_sequence, n))
        lines.append(_row("give the stop_id", s.vehicle_stop_id, n))
        status_row = _row("give a stop status", given, n)
        if given:
            by_share = sorted(statuses.items(), key=lambda kv: -kv[1])
            status_row += ": " + ", ".join(f"{k} {v / given:.0%}" for k, v in by_share)
        lines.append(status_row)
        timestamp_row = _row("have their own timestamp", s.vehicle_timestamp, n)
        if s.vehicle_ages:
            typical = statistics.median(s.vehicle_ages)
            timestamp_row += f", typically {human_duration(typical)} older than the feed"
        lines.append(timestamp_row)

    if s.trip_updates:
        n = s.trip_updates
        unusual = {k: v for k, v in s.trip_relationship.items() if k != "scheduled"}
        lines += ["", f"Trip updates ({n:,})"]
        lines.append(_row("say which trip (trip_id)", s.trip_update_trip_id, n))
        if unusual:
            lines.append("  " + ", ".join(f"{v:,} {k}" for k, v in unusual.items()) + " trips")
        su = s.stop_updates
        per_trip = f" (about {su / n:.0f} per trip)" if n else ""
        lines.append(f"  {'stop time updates':<38}{su:>8,}{per_trip}")
        if su:
            lines.append(_row("  with a predicted time", s.stop_updates_with_time, su))
            lines.append(_row("  with only a delay", s.stop_updates_delay_only, su))
            if s.stop_updates_with_time:
                lines.append(
                    _row("  times already past", s.stop_updates_past, s.stop_updates_with_time)
                )
            if s.stop_updates_skipped:
                lines.append(_row("  skipped stops", s.stop_updates_skipped, su))

    notes = _notes(s, fetched_at)
    if notes:
        lines += ["", "What this means for the project"] + [f"  - {note}" for note in notes]

    per_day = gzipped * 86400 / interval
    lines += [
        "",
        f"Disk use at one fetch every {interval:g} s: about {human_bytes(per_day * 30)} "
        f"per 30 days, {human_bytes(per_day * 90)} per 90 days (less if the feed often "
        "doesn't change).",
    ]
    return "\n".join(lines)


def _row(label: str, count: int, total: int) -> str:
    return f"  {label:<38}{count:>8,} ({count / total:.0%})"


def _notes(s: gtfsrt.Summary, fetched_at: float | None) -> list[str]:
    h = s.header
    notes = []
    if h.entity_count == 0:
        notes.append(
            "The feed is empty right now. That's normal outside service hours; "
            "try again during the day."
        )
    if fetched_at is not None and h.timestamp and fetched_at - h.timestamp > 300:
        notes.append(
            f"The feed timestamp is {human_duration(fetched_at - h.timestamp)} old, so the feed "
            "may be frozen. Check again in a few minutes."
        )
    if h.incrementality == 1:
        notes.append(
            "This feed is DIFFERENTIAL: each snapshot holds only changes, so missing one "
            "loses data. Prefer a full-dataset feed if the agency has one."
        )
    if h.entity_count and not s.vehicles and not s.trip_updates:
        notes.append(
            "No trip updates or vehicle positions here, so this is probably an alerts feed. "
            "Check whether the agency publishes the others at separate URLs."
        )
    if s.vehicles:
        if s.vehicle_trip_id < 0.5 * s.vehicles:
            notes.append(
                "Most vehicles don't say which trip they're on, which makes matching them "
                "to the schedule hard."
            )
        if s.vehicle_status.get("STOPPED_AT"):
            notes.append(
                "Vehicles report when they're stopped at a stop (STOPPED_AT), which helps "
                "pin down actual arrival times."
            )
        if not s.trip_updates:
            notes.append(
                "No trip updates in this feed. Many agencies publish them at a separate URL; "
                "if not, vehicle positions alone can still measure delays."
            )
    if s.stop_updates_with_time:
        if s.stop_updates_past >= 0.05 * s.stop_updates_with_time:
            notes.append(
                "Stops a vehicle has already passed stay in the feed, so actual arrival "
                "times can be read almost directly."
            )
        else:
            notes.append(
                "Stops drop out of the feed once a vehicle passes them. To get actual arrival "
                "times, use the last prediction before each stop disappears, checked against "
                "vehicle positions (Phase 1)."
            )
    if s.stop_updates and s.stop_updates_delay_only > 0.5 * s.stop_updates:
        notes.append(
            "Most stop updates give only a delay, so you'll add it to the scheduled time "
            "from the static GTFS."
        )
    return notes


def describe_static(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {
            info.filename.rsplit("/", 1)[-1]: info
            for info in archive.infolist()
            if not info.is_dir()
        }
        lines = [f"Static GTFS zip, {human_bytes(len(data))}, {len(members)} files"]
        for name in sorted(members):
            rows = _count_rows(archive, members[name]) if name in _COUNTED else None
            if rows is None:
                lines.append(f"  {name}")
            else:
                lines.append(f"  {name} ({rows:,} {'row' if rows == 1 else 'rows'})")

        required = ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")
        missing = [name for name in required if name not in members]
        if missing:
            lines.append(f"Missing {', '.join(missing)}, so this isn't a complete GTFS feed.")

        dates: list[str] = []
        for row in _rows(archive, members.get("calendar.txt")):
            dates += [row.get("start_date", ""), row.get("end_date", "")]
        for row in _rows(archive, members.get("calendar_dates.txt")):
            dates.append(row.get("date", ""))
        dates = sorted(d for d in dates if len(d) == 8 and d.isdigit())
        if dates:
            lines.append(f"Service dates: {_date(dates[0])} to {_date(dates[-1])}")
        for row in _rows(archive, members.get("feed_info.txt")):
            version = row.get("feed_version")
            start, end = row.get("feed_start_date"), row.get("feed_end_date")
            if version:
                lines.append(f"Feed version: {version}")
            if start and end:
                lines.append(f"Feed valid: {_date(start)} to {_date(end)}")
            break
    lines += [
        "",
        "Agencies replace this zip when schedules change. The logger keeps every version, so",
        "older realtime data can still be matched to the schedule that was running then.",
    ]
    return "\n".join(lines)


def _rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo | None):
    if info is None:
        return
    with archive.open(info) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        for row in csv.DictReader(text):
            # Rows with extra or missing cells give non-string keys or values; skip those.
            yield {
                k.strip(): v.strip()
                for k, v in row.items()
                if isinstance(k, str) and isinstance(v, str)
            }


def _count_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    lines = 0
    last = b"\n"
    with archive.open(info) as f:
        while chunk := f.read(1 << 20):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        lines += 1  # no newline after the final row
    return max(0, lines - 1)  # minus the header line


def _date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}" if len(yyyymmdd) == 8 else yyyymmdd
