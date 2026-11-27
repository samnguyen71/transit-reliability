"""Queries on the pipeline's rollups: DATA_DIR/rollups/<table>/<date>.parquet.

The rules from pipeline/realtime/schemas.py ("Rollups"): counts add up across rows and
dates, so a slice is a sum; median_delay_s and p90_delay_s don't, so a slice's quantiles
come from its summed delay_hist instead.

RollupStore watches the folder. The nightly job rewrites files only when their inputs
change, so a Snapshot (the files, each date's coverage, a version hash) holds until the
folder changes; query results are cached per version, and the version is the ETag.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import duckdb
import pyarrow.parquet as pq

# The delay_hist layout, copied from pipeline/realtime/schemas.py (HIST_*): bin 0 is
# under HIST_LO_S, bins 1..35 are a minute each, the last is HIST_HI_S or more. If the
# pipeline changes it, its ROLLUP_VERSION changes too; change these to match.
HIST_LO_S = -300
HIST_HI_S = 1800
HIST_BIN_S = 60
HIST_BINS = (HIST_HI_S - HIST_LO_S) // HIST_BIN_S + 2

TABLES = ("by_route", "by_stop", "by_hour")
COUNTS = ("scheduled", "observed", "on_time", "early", "late",
          "cancelled", "skipped", "lost", "no_data", "not_seen")
SUM_COUNTS = ", ".join(f"sum({c})::INTEGER AS {c}" for c in COUNTS)
# Element-wise sum of a group's histograms, one bin at a time (DuckDB lists are 1-based).
HIST_SUM = "list_value(" + ", ".join(
    f"sum(delay_hist[{i}])::INTEGER" for i in range(1, HIST_BINS + 1)) + ") AS delay_hist"
KIND_ORDER = "CASE kind WHEN 'first_stop' THEN 0 WHEN 'timepoint' THEN 1 ELSE 2 END"

DayType = Literal["weekday", "weekend"]


def day_type(d: date, holidays: frozenset[date] = frozenset()) -> DayType:
    """Which kind of service the date ran. DRT has one SatSun service, and a holiday runs
    it too (Thanksgiving, Monday 2026-10-12, has weekend-sized rollups)."""
    return "weekend" if d.weekday() >= 5 or d in holidays else "weekday"


# Summing ------------------------------------------------------------------------------

def hist_quantile(hist: list[int], q: float) -> int | None:
    """The q-quantile of a delay_hist, interpolated within its bin. The two open bins
    give their edge: "at least 30 min late" reads as 1800."""
    n = sum(hist)
    if n == 0:
        return None
    target = q * n
    seen = 0
    for i, count in enumerate(hist):
        if count and seen + count >= target:
            if i == 0:
                return HIST_LO_S
            if i == len(hist) - 1:
                return HIST_HI_S
            lo = HIST_LO_S + (i - 1) * HIST_BIN_S
            return round(lo + (target - seen) / count * HIST_BIN_S)
        seen += count
    return HIST_HI_S  # not reached: the loop ends at the last non-empty bin


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def finish(row: dict) -> dict:
    """Add the rates and quantiles to a row of summed counts and delay_hist."""
    hist = row.pop("delay_hist")
    ran = row["scheduled"] - row["cancelled"] - row["skipped"]
    return {**row,
            "on_time_rate": _rate(row["on_time"], row["observed"]),
            "coverage": _rate(row["observed"], ran),
            "median_delay_s": hist_quantile(hist, 0.5),
            "p90_delay_s": hist_quantile(hist, 0.9)}


def _dicts(rel: duckdb.DuckDBPyConnection) -> list[dict]:
    cols = [d[0] for d in rel.description]
    return [dict(zip(cols, r, strict=True)) for r in rel.fetchall()]


def summarize(cur: duckdb.DuckDBPyConnection, paths: list[str], keys: tuple[str, ...],
              filters: dict[str, object]) -> list[dict]:
    """Summed counts and delay_hist per keys + kind, over paths, where each filter column
    equals its value (NULL matches NULL). keys and filter names come from code, never
    from a request. Rows come ordered by keys (NULLs last), then kind as in KIND_ORDER."""
    if not paths:
        return []
    group = ", ".join((*keys, "kind"))
    where = " AND ".join(f"{c} IS NOT DISTINCT FROM ?" for c in filters) or "true"
    order = ", ".join([*(f"{k} NULLS LAST" for k in keys), KIND_ORDER])
    rel = cur.execute(f"""
        SELECT {group}, count(DISTINCT service_date)::INTEGER AS days,
               {SUM_COUNTS}, {HIST_SUM}
        FROM read_parquet(?)
        WHERE {where}
        GROUP BY {group}
        ORDER BY {order}
    """, [paths, *filters.values()])
    return [finish(r) for r in _dicts(rel)]


# The store ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Selection:
    """The files a query reads, and the dates left out of it."""
    paths: list[str]
    dates: list[date]
    excluded: list[tuple[date, float]]  # (date, coverage), coverage under the minimum


@dataclass
class Snapshot:
    version: str | None  # None when there are no rollups
    files: dict[str, dict[date, str]]  # table -> service date -> path
    coverage: dict[date, float | None]  # network-wide, from by_route
    meta: dict[str, str]  # rule_version, rollup_version, written_at of the newest file
    scanned_at: datetime
    rule_versions: dict[str, int] = field(default_factory=dict)  # by_route dates per version
    cache: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def latest(self, table: str) -> date | None:
        return max(self.files.get(table, {}), default=None)

    def dates(self) -> list[date]:
        return sorted(set().union(*(f.keys() for f in self.files.values())))


def _listing(data_dir: Path) -> list[tuple[str, Path, int, int]]:
    out = []
    for table in TABLES:
        for p in sorted((data_dir / "rollups" / table).glob("*.parquet")):
            st = p.stat()
            out.append((table, p, st.st_size, st.st_mtime_ns))
    return out


class RollupStore:
    """The rollups as of the last scan. snapshot() rescans at most every rescan_s
    seconds and builds a new Snapshot only when a file was added, removed or rewritten."""

    def __init__(self, con: duckdb.DuckDBPyConnection, data_dir: Path, *,
                 min_day_coverage: float, holidays: frozenset[date] = frozenset(),
                 rescan_s: float = 10, cache_size: int = 256):
        self.con, self.data_dir = con, data_dir
        self.min_day_coverage, self.holidays = min_day_coverage, holidays
        self.rescan_s, self.cache_size = rescan_s, cache_size
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._scanned = float("-inf")

    def snapshot(self) -> Snapshot:
        with self._lock:
            if self._snap is None or time.monotonic() - self._scanned >= self.rescan_s:
                listing = _listing(self.data_dir)
                version = _version(listing)
                if self._snap is None or version != self._snap.version:
                    self._snap = self._build(listing, version)
                self._scanned = time.monotonic()
            return self._snap

    def _build(self, listing, version: str | None) -> Snapshot:
        files: dict[str, dict[date, str]] = {t: {} for t in TABLES}
        for table, p, _size, _mtime in listing:
            files[table][date.fromisoformat(p.stem)] = p.as_posix()
        meta: dict[str, str] = {}
        if listing:
            newest = max(listing, key=lambda f: f[3])[1]
            raw = pq.read_schema(newest).metadata or {}
            meta = {k.decode(): v.decode() for k, v in raw.items()
                    if k in (b"rule_version", b"rollup_version", b"written_at")}
        # Dates per rule version: while the nightly job rebuilds after a rule change, the
        # two versions sit side by side, and this shows how far it has got.
        rule_versions: dict[str, int] = {}
        for path in files["by_route"].values():
            v = (pq.read_schema(path).metadata or {}).get(b"rule_version", b"?").decode()
            rule_versions[v] = rule_versions.get(v, 0) + 1
        coverage: dict[date, float | None] = {}
        if files["by_route"]:
            cur = self.con.cursor()
            try:
                for d, obs, ran in cur.execute("""
                    SELECT service_date, sum(observed), sum(scheduled - cancelled - skipped)
                    FROM read_parquet(?) GROUP BY service_date
                """, [list(files["by_route"].values())]).fetchall():
                    coverage[d] = _rate(obs, ran)
            finally:
                cur.close()
        return Snapshot(version, files, coverage, meta, datetime.now(UTC),
                        rule_versions=dict(sorted(rule_versions.items())))

    def low_coverage(self, snap: Snapshot, d: date) -> bool:
        c = snap.coverage.get(d)
        return c is not None and c < self.min_day_coverage

    def select(self, snap: Snapshot, table: str, start: date, end: date,
               day: DayType | None) -> Selection:
        """The table's files for service dates start..end of the given day type, without
        the dates whose network-wide coverage is under min_day_coverage (feed outages:
        11-15..11-17 had 2%, 0% and 65%; a normal day has about 95%)."""
        paths, dates, excluded = [], [], []
        for d, path in sorted(snap.files.get(table, {}).items()):
            if not start <= d <= end or (day and day_type(d, self.holidays) != day):
                continue
            if self.low_coverage(snap, d):
                excluded.append((d, snap.coverage[d]))
            else:
                paths.append(path)
                dates.append(d)
        return Selection(paths, dates, excluded)

    def summarize(self, snap: Snapshot, cur: duckdb.DuckDBPyConnection, sel: Selection,
                  keys: tuple[str, ...], filters: dict[str, object]) -> list[dict]:
        """summarize(), cached in the snapshot (so a new version starts empty). The rows
        are copies, so callers may change them."""
        key = (tuple(sel.paths), keys, tuple(filters.items()))
        rows = self._cached(snap, key, lambda: summarize(cur, sel.paths, keys, filters))
        return [dict(r) for r in rows]

    def _cached(self, snap: Snapshot, key, compute: Callable[[], list[dict]]) -> list[dict]:
        with self._lock:
            if key in snap.cache:
                snap.cache.move_to_end(key)
                return snap.cache[key]
        rows = compute()  # outside the lock: two requests may both compute; that's fine
        with self._lock:
            snap.cache[key] = rows
            while len(snap.cache) > self.cache_size:
                snap.cache.popitem(last=False)
        return rows


def _version(listing) -> str | None:
    if not listing:
        return None
    h = hashlib.sha256()
    for table, p, size, mtime in listing:
        h.update(f"{table}/{p.name}:{size}:{mtime}\n".encode())
    return h.hexdigest()[:16]
