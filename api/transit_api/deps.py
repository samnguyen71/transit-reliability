"""Per-request dependencies: settings, a DuckDB cursor, a Postgres connection, the time
and today's date, the static feed that covers it, the rollups (with their ETag) and the
live TripUpdates."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import duckdb
import psycopg
from fastapi import Depends, HTTPException, Request, Response
from psycopg_pool import PoolTimeout

from transit_api.live import LiveFeed
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


def get_db(request: Request, settings: SettingsDep) -> Iterator[psycopg.Connection]:
    """A Postgres connection from the app's pool (main.py), returned to it after the
    request. 503 if DATABASE_URL isn't set, or no connection frees up in
    DB_POOL_TIMEOUT_S (the database is down, or every connection is busy)."""
    pool = request.app.state.pool
    if pool is None:
        raise HTTPException(503, "DATABASE_URL is not set")
    try:
        conn = pool.getconn(timeout=settings.db_pool_timeout_s)
    except PoolTimeout:
        raise HTTPException(503, "the database is unavailable") from None
    try:
        yield conn
    finally:
        pool.putconn(conn)  # the pool resets it, or discards it if it's broken


def get_now() -> datetime:
    """The current time, aware, in UTC. (Tests override this.)"""
    return datetime.now(UTC)


NowDep = Annotated[datetime, Depends(get_now)]


def get_today(settings: SettingsDep, now: NowDep) -> date:
    """Today's date in the agency's timezone. (Tests override this or get_now.)"""
    return now.astimezone(ZoneInfo(settings.timezone)).date()


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


def get_live(request: Request) -> LiveFeed:
    return request.app.state.live


LiveDep = Annotated[LiveFeed, Depends(get_live)]
