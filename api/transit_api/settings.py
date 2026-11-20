"""Settings, read from the environment. Every one has a default except DATABASE_URL."""

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
