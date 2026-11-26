"""Routes in one static feed: the list, and each direction's main pattern on a date."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row

# r.* as the Route model's fields.
ROUTE_COLS = """r.route_id, r.route_short_name AS short_name, r.route_long_name AS long_name,
                nullif(trim(r.route_color), '') AS color"""
# Numbered routes by number (101, 112, ..., 921), then the rest (N1, N2).
ROUTE_ORDER = """r.route_short_name !~ '^[0-9]+$', length(r.route_short_name),
                 r.route_short_name, r.route_id"""

LOOKAHEAD_DAYS = 7  # a whole week, so a weekday-only route is found on a Saturday


def list_routes(conn: psycopg.Connection, feed_id: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT {ROUTE_COLS} FROM gtfs.routes r
            WHERE r.feed_id = %s
            ORDER BY {ROUTE_ORDER}
        """, [feed_id]).fetchall()


def get_route(conn: psycopg.Connection, feed_id: int, route_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT {ROUTE_COLS} FROM gtfs.routes r
            WHERE r.feed_id = %s AND r.route_id = %s
        """, [feed_id, route_id]).fetchone()


def next_service_day(conn: psycopg.Connection, feed_id: int, route_id: str,
                     today: date) -> date | None:
    """The first day from today, within LOOKAHEAD_DAYS, on which the route has a trip."""
    for day in (today + timedelta(days=i) for i in range(LOOKAHEAD_DAYS)):
        if conn.execute("SELECT EXISTS (SELECT 1 FROM gtfs.trips_on(%s, %s) WHERE route_id = %s)",
                        [feed_id, day, route_id]).fetchone()[0]:
            return day
    return None


def main_patterns(conn: psycopg.Connection, feed_id: int, route_id: str,
                  day: date) -> list[dict]:
    """One row per direction on day: its trip count and its main pattern (the most common
    stop sequence; ties go to the longer one, then the earlier first departure), with the
    pattern's first trip, most common shape and most common headsign."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            WITH trips AS (
                SELECT t.trip_id, t.direction_id, t.trip_headsign, t.shape_id,
                       array_agg(st.stop_id ORDER BY st.stop_sequence) AS stops,
                       min(st.departure_secs) AS first_dep
                FROM gtfs.trips_on(%(feed)s, %(day)s) t
                JOIN gtfs.stop_times st ON st.feed_id = t.feed_id AND st.trip_id = t.trip_id
                WHERE t.route_id = %(route)s
                GROUP BY t.trip_id, t.direction_id, t.trip_headsign, t.shape_id
            ), patterns AS (
                SELECT direction_id, stops, count(*)::int AS pattern_trips,
                       min(first_dep) AS first_dep,
                       (array_agg(trip_id ORDER BY first_dep, trip_id))[1] AS trip_id,
                       mode() WITHIN GROUP (ORDER BY shape_id) AS shape_id,
                       mode() WITHIN GROUP (ORDER BY trip_headsign) AS headsign
                FROM trips
                GROUP BY direction_id, stops
            )
            SELECT DISTINCT ON (direction_id)
                   direction_id, headsign, trip_id, shape_id, pattern_trips,
                   (sum(pattern_trips) OVER (PARTITION BY direction_id))::int AS trips
            FROM patterns
            ORDER BY direction_id NULLS LAST, pattern_trips DESC, cardinality(stops) DESC,
                     first_dep NULLS LAST
        """, {"feed": feed_id, "route": route_id, "day": day}).fetchall()


def trip_stops(conn: psycopg.Connection, feed_id: int,
               trip_ids: list[str]) -> dict[str, list[dict]]:
    """Each trip's stops in order, as PatternStop dicts."""
    out: dict[str, list[dict]] = {t: [] for t in trip_ids}
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("""
            SELECT st.trip_id, st.stop_sequence,
                   coalesce(st.timepoint = 1,
                            NOT EXISTS (SELECT 1 FROM gtfs.stop_times x
                                        WHERE x.feed_id = %(feed)s AND x.timepoint IS NOT NULL)
                   ) AS timepoint,
                   s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, s.parent_station, s.stop_code
            FROM gtfs.stop_times st
            JOIN gtfs.stops s ON s.feed_id = st.feed_id AND s.stop_id = st.stop_id
            WHERE st.feed_id = %(feed)s AND st.trip_id = ANY(%(trips)s)
            ORDER BY st.trip_id, st.stop_sequence
        """, {"feed": feed_id, "trips": trip_ids}).fetchall()
    for r in rows:
        out[r.pop("trip_id")].append(r)
    return out


def shapes(conn: psycopg.Connection, feed_id: int,
           shape_ids: list[str]) -> dict[str, list[tuple[float, float]]]:
    """Each shape's points in order, as (lon, lat)."""
    out: dict[str, list[tuple[float, float]]] = {s: [] for s in shape_ids}
    rows = conn.execute("""
        SELECT shape_id, shape_pt_lon, shape_pt_lat FROM gtfs.shapes
        WHERE feed_id = %s AND shape_id = ANY(%s)
        ORDER BY shape_id, shape_pt_sequence
    """, [feed_id, shape_ids]).fetchall()
    for shape_id, lon, lat in rows:
        out[shape_id].append((lon, lat))
    return out
