"""Per-request dependencies: settings, a DuckDB cursor, a Postgres connection, today's
date and the static feed that covers it."""

from collections.abc import Iterator
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import duckdb
import psycopg
from fastapi import Depends, HTTPException, Request

from transit_api.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_duck(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """A cursor on the app's DuckDB connection. Sync endpoints run in a thread pool and a
    connection isn't safe to share across threads; a cursor per request is."""
    cur = request.app.state.duck.cursor()
    try:
        yield cur
    finally:
        cur.close()


def get_db(settings: SettingsDep) -> Iterator[psycopg.Connection]:
    """A Postgres connection for the request. One connection per request is fine while
    traffic is light; switch to psycopg_pool when it isn't."""
    if not settings.database_url:
        raise HTTPException(503, "DATABASE_URL is not set")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        yield conn


def get_today(settings: SettingsDep) -> date:
    """Today's date in the agency's timezone. (Tests override this.)"""
    return datetime.now(ZoneInfo(settings.timezone)).date()


DuckDep = Annotated[duckdb.DuckDBPyConnection, Depends(get_duck)]
DbDep = Annotated[psycopg.Connection, Depends(get_db)]
TodayDep = Annotated[date, Depends(get_today)]


def get_feed(conn: DbDep, today: TodayDep) -> int:
    """The static feed covering today: the newest one with any service on it."""
    feed_id = conn.execute("SELECT gtfs.feed_for(%s)", [today]).fetchone()[0]
    if feed_id is None:
        raise HTTPException(503, f"no static feed has service on {today}")
    return feed_id


FeedDep = Annotated[int, Depends(get_feed)]
