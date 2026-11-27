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
