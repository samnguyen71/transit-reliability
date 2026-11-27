"""Hand-built rollup files, laid out as pipeline/realtime/schemas.py ROLLUP_TABLES."""

from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

from transit_api import rollups

_COUNTS = [(c, pa.int32()) for c in rollups.COUNTS] + [
    ("median_delay_s", pa.int32()), ("p90_delay_s", pa.int32()),
    ("delay_hist", pa.list_(pa.int32(), rollups.HIST_BINS))]
_DAY = ("service_date", pa.date32())
SCHEMAS = {
    "by_route": pa.schema([_DAY, ("route_id", pa.string()), ("direction_id", pa.int32()),
                           ("kind", pa.string()), *_COUNTS]),
    "by_stop": pa.schema([_DAY, ("stop_id", pa.string()), ("route_id", pa.string()),
                          ("kind", pa.string()), *_COUNTS]),
    "by_hour": pa.schema([_DAY, ("hour", pa.int32()), ("route_id", pa.string()),
                          ("direction_id", pa.int32()), ("kind", pa.string()), *_COUNTS]),
}


def hist(**bins: int) -> list[int]:
    """A delay_hist from {"b<i>": count}; b6 is [0, 60) s late, b16 is [600, 660)."""
    h = [0] * rollups.HIST_BINS
    for k, v in bins.items():
        h[int(k[1:])] = v
    return h


def row(d, route="R1", direction=0, kind="timepoint", h=None, **cols):
    """A rollup row. observed and scheduled default to the histogram's total; other
    columns (counts, stop_id, hour) come from cols."""
    h = h or [0] * rollups.HIST_BINS
    base = dict.fromkeys(rollups.COUNTS, 0) | {"observed": sum(h), "scheduled": sum(h)}
    return {"service_date": d, "route_id": route, "direction_id": direction, "kind": kind,
            **base, "median_delay_s": None, "p90_delay_s": None, "delay_hist": h, **cols}


def write(data_dir, d: date, rows, table="by_route"):
    path = data_dir / "rollups" / table / f"{d}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMAS[table]
    rows = [{k: r.get(k) for k in schema.names} for r in rows]
    meta = {b"rollup_version": b"3", b"rule_version": b"3", b"written_at": b"2026-11-23T00:00Z"}
    pq.write_table(pa.Table.from_pylist(rows, schema=schema).replace_schema_metadata(meta), path)
