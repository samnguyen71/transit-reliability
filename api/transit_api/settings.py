"""Settings, read from the environment. Every one has a default except DATABASE_URL."""

from datetime import date
from pathlib import Path

from pydantic_settings import BaseSettings

REPO = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # DATA_DIR: the pipeline's data folder, which holds rollups/<table>/<date>.parquet.
    data_dir: Path = REPO / "data"
    # DATABASE_URL: the static GTFS (route and stop names). Without it, endpoints that
    # need names answer 503 and the rest still work.
    database_url: str | None = None
    # TIMEZONE: the agency's, for "today" when there are no rollups yet.
    timezone: str = "America/Toronto"
    # DuckDB, for the rollups. Small on purpose: on the VM, 887 MiB is shared with
    # Postgres, the logger and the nightly job.
    duckdb_memory_limit: str = "64MB"
    duckdb_threads: int = 1
    # MIN_DAY_COVERAGE: service dates whose network-wide coverage (observed / visits that
    # ran) is under this are left out of every reliability answer, and listed as excluded.
    # A normal day is 0.89-0.97; the 11-15..11-17 DRT outage gave 0.02, 0.00 and 0.65.
    min_day_coverage: float = 0.75
    # HOLIDAYS: dates that ran weekend service, as JSON: '["2026-10-12", "2026-12-25"]'.
    holidays: frozenset[date] = frozenset({date(2026, 10, 12)})  # Thanksgiving
    # ROLLUP_RESCAN_S: how often to look for new rollup files (the nightly job's output).
    rollup_rescan_s: float = 10
    # ROLLUP_CACHE_SIZE: reliability answers kept per rollup version.
    rollup_cache_size: int = 256
    # RAW_DIR: the logger's snapshots (raw/trip_updates/<UTC day>/*.pb.gz), for live
    # departures. Default DATA_DIR/raw, as on the VM, where the API runs next to the logger.
    raw_dir: Path | None = None
    # LIVE_RESCAN_S: how often to look for a newer TripUpdates snapshot. The logger saves
    # one every 30 s when the feed changes.
    live_rescan_s: float = 5
    # LIVE_STALE_S: a snapshot whose feed timestamp is older than this isn't used. Normal
    # age is about 20 s; frozen feeds (77 min on 2026-10-20) are what this catches.
    live_stale_s: float = 120
    # DB_POOL_SIZE, DB_POOL_TIMEOUT_S: Postgres connections kept open, and how long a
    # request waits for one before a 503. Opening one per request cost about 50 ms.
    db_pool_size: int = 4
    db_pool_timeout_s: float = 5

    @property
    def raw_path(self) -> Path:
        return self.raw_dir or self.data_dir / "raw"
