"""Stops in one static feed: search by name or code, one stop, and the routes serving it."""

from __future__ import annotations

import re
from datetime import date

import psycopg
from psycopg.rows import dict_row

from transit_api.queries.routes import ROUTE_COLS, ROUTE_ORDER

STOP_COLS = "s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, s.parent_station, s.stop_code"

# DRT names stops "Garden Northbound @ Bassett"; people type "garden and bassett",
# "garden/bassett" or "bassett garden". So match word by word, in any order.
SPLIT = re.compile(r"[\s&@/,]+")
IGNORED = {"and", "at"}


def tokenize(q: str) -> list[str]:
    """The query's words, lowercased, without separators, one-letter words or IGNORED."""
    return [w for w in SPLIT.split(q.lower()) if len(w) >= 2 and w not in IGNORED]


def search_stops(conn: psycopg.Connection, feed_id: int, q: str, limit: int) -> list[dict]:
    """An exact stop_code match first, then every stop whose name word-matches every token
    (pg_trgm's <%, threshold pg_trgm.word_similarity_threshold, 0.6 by default; so "gardn"
    finds Garden), best total word_similarity first."""
    tokens = tokenize(q)
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT {STOP_COLS}
            FROM gtfs.stops s
            CROSS JOIN LATERAL (
                SELECT bool_and(t <%% lower(s.stop_name)) AS every_word,
                       sum(word_similarity(t, lower(s.stop_name))) AS score
                FROM unnest(%(tokens)s::text[]) t
            ) m
            WHERE s.feed_id = %(feed)s
              AND (s.stop_code = %(raw)s OR m.every_word)
            ORDER BY s.stop_code IS NOT DISTINCT FROM %(raw)s DESC, m.score DESC NULLS LAST,
                     s.stop_name, s.stop_id
            LIMIT %(limit)s
        """, {"tokens": tokens, "raw": q.strip(), "feed": feed_id, "limit": limit}).fetchall()


def get_stop(conn: psycopg.Connection, feed_id: int, stop_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT {STOP_COLS} FROM gtfs.stops s
            WHERE s.feed_id = %s AND s.stop_id = %s
        """, [feed_id, stop_id]).fetchone()


def stop_routes(conn: psycopg.Connection, feed_id: int, stop_id: str, day: date) -> list[dict]:
    """The routes and directions with a trip stopping at stop_id on day, with their
    headsigns (most common first) and trip counts. Overnight +24 h copies don't count
    (gtfs.trips_on drops them)."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(f"""
            WITH heads AS (
                SELECT t.route_id, t.direction_id, t.trip_headsign,
                       count(DISTINCT t.trip_id) AS n
                FROM gtfs.trips_on(%(feed)s, %(day)s) t
                JOIN gtfs.stop_times st ON st.feed_id = t.feed_id AND st.trip_id = t.trip_id
                WHERE st.stop_id = %(stop)s
                GROUP BY t.route_id, t.direction_id, t.trip_headsign
            )
            SELECT {ROUTE_COLS}, h.direction_id,
                   coalesce(array_agg(h.trip_headsign ORDER BY h.n DESC, h.trip_headsign)
                            FILTER (WHERE h.trip_headsign IS NOT NULL), '{{}}') AS headsigns,
                   sum(h.n)::int AS trips
            FROM heads h
            JOIN gtfs.routes r ON r.feed_id = %(feed)s AND r.route_id = h.route_id
            GROUP BY r.route_id, r.route_short_name, r.route_long_name, r.route_color,
                     h.direction_id
            ORDER BY {ROUTE_ORDER}, h.direction_id NULLS LAST
        """, {"feed": feed_id, "stop": stop_id, "day": day}).fetchall()
    route_keys = ("route_id", "short_name", "long_name", "color")
    return [{"route": {k: r.pop(k) for k in route_keys}, **r} for r in rows]
