"""The transit reliability API.

    cd api
    ..\\.venv\\Scripts\\uvicorn transit_api.main:app --reload     (Windows)
    ../.venv/bin/uvicorn transit_api.main:app --reload           (Linux)

Then open http://127.0.0.1:8000/docs. Set DATABASE_URL first for endpoints that need
names; DATA_DIR defaults to the repo's data/ folder (see settings.py).
"""

from contextlib import asynccontextmanager

import duckdb
from fastapi import FastAPI

from transit_api import rollups
from transit_api.routers import routes, stops
from transit_api.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One in-memory DuckDB for the app; requests use cursors on it (deps.get_duck).
        con = duckdb.connect(config={"memory_limit": settings.duckdb_memory_limit,
                                     "threads": settings.duckdb_threads})
        rollups.setup(con)
        app.state.duck = con
        yield
        con.close()

    app = FastAPI(title="Transit reliability", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(routes.router)
    app.include_router(stops.router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
