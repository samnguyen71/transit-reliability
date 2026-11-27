"""Per-request dependencies: settings, a DuckDB cursor, a Postgres connection, today's
date, the static feed that covers it, and the rollups (with their ETag)."""

from collections.abc import Iterator
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import duckdb
import psycopg
from fastapi import Depends, HTTPException, Request, Response

from transit_api.rollups import RollupStore, Snapshot
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


def get_store(request: Request) -> RollupStore:
    return request.app.state.rollups


StoreDep = Annotated[RollupStore, Depends(get_store)]


def _etag_matches(header: str | None, etag: str) -> bool:
    """If-None-Match: '*' or a list of tags; weak tags (W/"...") compare by value."""
    if not header:
        return False
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag in tags


def get_rollups(request: Request, response: Response, store: StoreDep) -> Snapshot:
    """The current rollups. Their version is the response's ETag: an answer changes only
    when the nightly job rewrites a file, so a client holding the tag gets 304 without a
    query being run. List this first among an endpoint's parameters, so the 304 comes
    before any database work."""
    snap = store.snapshot()
    if snap.version:
        headers = {"ETag": f'"{snap.version}"', "Cache-Control": "no-cache"}
        if _etag_matches(request.headers.get("if-none-match"), headers["ETag"]):
            raise HTTPException(304, headers=headers)
        response.headers.update(headers)
    return snap


RollupsDep = Annotated[Snapshot, Depends(get_rollups)]
