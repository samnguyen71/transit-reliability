"""`python -m feedlogger report`: what's stored, any gaps, and when the disk fills up.

It reads only the files on disk (snapshots and the daily manifests), so it
works whether or not the logger is running.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedlogger.fmt import human_bytes, human_duration

GOOD = {"saved", "duplicate", "not_modified"}
MANIFEST = "_manifest.jsonl"
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class FeedStats:
    name: str
    attempts: int = 0
    good: int = 0
    unchanged: int = 0  # good fetches that matched the previous snapshot
    failures: Counter[str] = field(default_factory=Counter)
    stored_bytes: int = 0
    first: datetime | None = None
    last: datetime | None = None
    last_good: datetime | None = None
    last_status: str = ""
    last_error: str = ""
    interval: float | None = None  # typical seconds between fetches
    outages: list[tuple[datetime, float]] = field(default_factory=list)  # (start, seconds)
    feed_ages: list[float] = field(default_factory=list)

    @property
    def bytes_per_day(self) -> float | None:
        """Disk use per day, or None with under an hour of data to go on."""
        if not self.first or not self.last:
            return None
        span_days = (self.last - self.first).total_seconds() / 86400
        if span_days < 1 / 24:
            return None
        return self.stored_bytes / span_days

    @property
    def outage_threshold(self) -> float:
        """A gap longer than this between good fetches counts as an outage."""
        return max(3 * (self.interval or 30), 300)


def collect_stats(data_dir: Path, days: int = 7, now: datetime | None = None) -> list[FeedStats]:
    """Read the last `days` UTC days of manifests and snapshots for every feed."""
    now = now or datetime.now(timezone.utc)
    first_day = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    all_stats = []
    feed_dirs = sorted(
        p for p in data_dir.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
    )
    for feed_dir in feed_dirs:
        stats = FeedStats(feed_dir.name)
        attempts: list[datetime] = []
        good: list[datetime] = []
        day_dirs = sorted(
            p for p in feed_dir.iterdir()
            if p.is_dir() and _DAY_RE.fullmatch(p.name) and p.name >= first_day
        )
        for day_dir in day_dirs:
            for file in day_dir.iterdir():
                if file.name != MANIFEST and file.is_file():
                    stats.stored_bytes += file.stat().st_size
            manifest = day_dir / MANIFEST
            if not manifest.is_file():
                continue
            with manifest.open(encoding="utf-8") as lines:
                for line in lines:
                    try:
                        record = json.loads(line)
                        when = datetime.fromisoformat(record["fetched_at"])
                    except (ValueError, KeyError, TypeError):
                        continue  # a line cut short by a crash, say
                    status = record.get("status") or "unknown"
                    stats.attempts += 1
                    attempts.append(when)
                    if status in GOOD:
                        stats.good += 1
                        good.append(when)
                        if status != "saved":
                            stats.unchanged += 1
                    else:
                        stats.failures[status] += 1
                    if isinstance(record.get("feed_age_s"), (int, float)):
                        stats.feed_ages.append(record["feed_age_s"])
                    stats.last_status = status
                    stats.last_error = record.get("error") or ""
        if attempts:
            attempts.sort()
            good.sort()
            stats.first, stats.last = attempts[0], attempts[-1]
            stats.last_good = good[-1] if good else None
            gaps = [(b - a).total_seconds() for a, b in zip(attempts, attempts[1:])]
            stats.interval = statistics.median(gaps) if gaps else None
            # Gaps between good fetches, including any before the first one.
            marks = [attempts[0], *good] if good and good[0] > attempts[0] else good
            for a, b in zip(marks, marks[1:]):
                gap = (b - a).total_seconds()
                if gap > stats.outage_threshold:
                    stats.outages.append((a, gap))
        all_stats.append(stats)
    return all_stats


def format_report(
    stats: list[FeedStats],
    *,
    data_dir: Path,
    days: int,
    free_bytes: int,
    total_bytes: int,
    reserve_bytes: int,
    now: datetime,
) -> str:
    lines = [
        f"{data_dir}, last {days} days (times in UTC). "
        f"Disk: {human_bytes(free_bytes)} free of {human_bytes(total_bytes)}.",
        "",
    ]
    if not any(s.attempts for s in stats):
        lines.append(
            "No fetches recorded yet. Is the logger running? Check with: docker compose ps"
        )
        return "\n".join(lines)

    lines.append(
        f"{'feed':<20} {'since':<13} {'fetches':>8} {'failed':>7} {'unchanged':>10} "
        f"{'stored/day':>11}  last good fetch"
    )
    for s in stats:
        since = f"{s.first:%b %d %H:%M}" if s.first else "-"
        unchanged = f"{s.unchanged / s.good:.0%}" if s.good else "-"
        per_day = human_bytes(s.bytes_per_day) if s.bytes_per_day is not None else "-"
        last_good = (
            f"{human_duration((now - s.last_good).total_seconds())} ago" if s.last_good else "never"
        )
        lines.append(
            f"{s.name:<20} {since:<13} {s.attempts:>8,} {s.attempts - s.good:>7,} "
            f"{unchanged:>10} {per_day:>11}  {last_good}"
        )

    notes = []
    for s in stats:
        if not s.attempts:
            continue
        quiet_for = (now - s.last).total_seconds() if s.last else 0
        if quiet_for > s.outage_threshold:
            notes.append(
                f"{s.name}: no fetches at all for {human_duration(quiet_for)}. Is the logger "
                "running? Check with: docker compose ps"
            )
        down_for = (now - s.last_good).total_seconds() if s.last_good else None
        if s.last_status not in GOOD and (down_for is None or down_for > s.outage_threshold):
            since_text = f"for {human_duration(down_for)}" if down_for is not None else "so far"
            notes.append(
                f"{s.name}: FAILING {since_text}. Latest error: {s.last_error or s.last_status}. "
                "See: docker compose logs --tail 50 logger"
            )
        if s.failures:
            kinds = ", ".join(f"{n:,} {status}" for status, n in s.failures.most_common())
            notes.append(f"{s.name}: failed fetches: {kinds}")
        if s.outages:
            start, longest = max(s.outages, key=lambda o: o[1])
            notes.append(
                f"{s.name}: {len(s.outages)} gap(s) with no good fetch for over "
                f"{human_duration(s.outage_threshold)}; longest {human_duration(longest)} "
                f"from {start:%b %d %H:%M}"
            )
        if s.feed_ages and statistics.median(s.feed_ages) > 120:
            notes.append(
                f"{s.name}: the agency's own timestamp is typically "
                f"{human_duration(statistics.median(s.feed_ages))} old when saved"
            )
        if s.good >= 20 and s.unchanged > 0.5 * s.good and s.interval and s.interval < 3600:
            notes.append(
                f"{s.name}: {s.unchanged / s.good:.0%} of fetches found nothing new, so the feed "
                "updates less often than you poll it. Polling less often would lose little."
            )
    if notes:
        lines += ["", "Notes"] + [f"  - {note}" for note in notes]

    rates = {s.name: s.bytes_per_day for s in stats if s.bytes_per_day}
    if not rates:
        lines += ["", "Disk projection: needs at least an hour of data. Run this again later."]
    else:
        per_day = sum(rates.values())
        lines += [
            "",
            f"Disk use: about {human_bytes(per_day)} per day, {human_bytes(per_day * 30)} per "
            f"30 days, {human_bytes(per_day * 90)} per 90 days.",
        ]
        days_left = max(0, free_bytes - reserve_bytes) / per_day
        full_on = now + timedelta(days=days_left)
        lines.append(
            f"At this rate the disk fills up in about {days_left:,.0f} days ({full_on:%b %d, %Y}), "
            f"keeping {human_bytes(reserve_bytes)} free."
        )
        if days_left < 150:
            biggest = max(rates, key=rates.get)
            lines.append(
                f"That's less than five months. {biggest} uses {rates[biggest] / per_day:.0%} of "
                "it: doubling its interval_seconds halves that. Or grow the disk, or move "
                "backed-up days off the VM."
            )
        if any(s.first and s.last and s.last - s.first < timedelta(days=1) for s in stats):
            lines.append("(Based on less than a day of data. Run this again tomorrow.)")
    return "\n".join(lines)
