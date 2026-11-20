"""Queries on the pipeline's rollups: DATA_DIR/rollups/<table>/<date>.parquet.

The rules from pipeline/realtime/schemas.py ("Rollups"): counts add up across rows and
dates, so a slice is a sum; median_delay_s and p90_delay_s don't, so a slice's quantiles
come from its summed delay_hist instead.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

# The delay_hist layout, copied from pipeline/realtime/schemas.py (HIST_*): bin 0 is
# under HIST_LO_S, bins 1..35 are a minute each, the last is HIST_HI_S or more. If the
# pipeline changes it, its ROLLUP_VERSION changes too; change these to match.
HIST_LO_S = -300
HIST_HI_S = 1800
HIST_BIN_S = 60
HIST_BINS = (HIST_HI_S - HIST_LO_S) // HIST_BIN_S + 2

COUNTS = ("scheduled", "observed", "on_time", "early", "late",
          "cancelled", "skipped", "lost", "no_data", "not_seen")
SUM_COUNTS = ", ".join(f"sum({c})::INTEGER AS {c}" for c in COUNTS)
KIND_ORDER = "CASE kind WHEN 'first_stop' THEN 0 WHEN 'timepoint' THEN 1 ELSE 2 END"


def setup(con: duckdb.DuckDBPyConnection) -> None:
    """Run once on the app's connection; its cursors share what this creates."""
    # hist_sum(list(delay_hist)): the element-wise sum of a group's histograms. (Summing
    # bin by bin needs a nested aggregate, which DuckDB doesn't allow.)
    con.execute("""
        CREATE MACRO hist_sum(hs) AS
            list_reduce(hs, lambda a, b: list_transform(a, lambda v, i: v + b[i]))
    """)


def files(data_dir: Path, table: str, start: date, end: date) -> list[str]:
    """The table's files for service dates start..end. Picked by file name, so DuckDB
    opens only those, and an empty range is an empty list rather than an error."""
    folder = data_dir / "rollups" / table
    return sorted(p.as_posix() for p in folder.glob("*.parquet")
                  if start <= date.fromisoformat(p.stem) <= end)


def latest_date(data_dir: Path, table: str) -> date | None:
    dates = [date.fromisoformat(p.stem) for p in (data_dir / "rollups" / table).glob("*.parquet")]
    return max(dates, default=None)


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


def route_reliability(cur: duckdb.DuckDBPyConnection, paths: list[str],
                      route_id: str) -> list[dict]:
    """One row per (direction_id, kind) of the route, over the given by_route files."""
    if not paths:
        return []
    rel = cur.execute(f"""
        SELECT direction_id, kind, count(DISTINCT service_date)::INTEGER AS days,
               {SUM_COUNTS}, hist_sum(list(delay_hist)) AS delay_hist
        FROM read_parquet(?)
        WHERE route_id = ?
        GROUP BY direction_id, kind
        ORDER BY direction_id NULLS LAST, {KIND_ORDER}
    """, [paths, route_id])
    return [finish(r) for r in _dicts(rel)]
