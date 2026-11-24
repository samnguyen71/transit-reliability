"""Per-request dependencies: settings, a DuckDB cursor and a Postgres connection."""

from collections.abc import Iterator
from typing import Annotated

import duckdb
import psycopg
from fastapi import Depends, HTTPException, Request

from transit_api.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_duck(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """A cursor on the app's DuckDB connection. Sync endpoints run in a thread pool and a
    connection isn't safe to share across threads; a cursor per request is."""
    cur = request.app.state.duck.cursor()
    try:
        yield cur
    finally:
        cur.close()


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[psycopg.Connection]:
    """A Postgres connection for the request. One connection per request is fine while
    traffic is light; switch to psycopg_pool when it isn't."""
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is not set")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        yield conn


SettingsDep = Annotated[Settings, Depends(get_settings)]
DuckDep = Annotated[duckdb.DuckDBPyConnection, Depends(get_duck)]
DbDep = Annotated[psycopg.Connection, Depends(get_db)]
