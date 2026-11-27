"""The transit reliability API.

    cd api
    ..\\.venv\\Scripts\\uvicorn transit_api.main:app --reload     (Windows)
    ../.venv/bin/uvicorn transit_api.main:app --reload           (Linux)

Then open http://127.0.0.1:8000/docs. Set DATABASE_URL first for endpoints that need
names; DATA_DIR defaults to the repo's data/ folder, and live departures read the
logger's snapshots in DATA_DIR/raw (see settings.py).
"""

from contextlib import asynccontextmanager

import duckdb
from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from transit_api import live, rollups
from transit_api.routers import health, reliability, routes, stops
from transit_api.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One in-memory DuckDB for the app; requests use cursors on it (deps.get_duck).
        con = duckdb.connect(config={"memory_limit": settings.duckdb_memory_limit,
                                     "threads": settings.duckdb_threads})
        app.state.duck = con
        app.state.rollups = rollups.RollupStore(
            con, settings.data_dir, min_day_coverage=settings.min_day_coverage,
            holidays=settings.holidays, rescan_s=settings.rollup_rescan_s,
            cache_size=settings.rollup_cache_size)
        app.state.live = live.LiveFeed(settings.raw_path, rescan_s=settings.live_rescan_s)
        # Postgres connections, kept open (deps.get_db). Not waited for: a database that's
        # down at startup is retried in the background, and requests answer 503 until it
        # is up. check: a connection broken by a database restart is replaced, not handed out.
        pool = None
        if settings.database_url:
            pool = ConnectionPool(settings.database_url, min_size=1,
                                  max_size=settings.db_pool_size, kwargs={"autocommit": True},
                                  check=ConnectionPool.check_connection, open=False,
                                  name="transit-api")
            pool.open(wait=False)
        app.state.pool = pool
        try:
            yield
        finally:
            if pool is not None:
                pool.close()
            con.close()

    app = FastAPI(title="Transit reliability", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(routes.router)
    app.include_router(stops.router)
    app.include_router(reliability.router)
    app.include_router(health.router)

    return app


app = create_app()
