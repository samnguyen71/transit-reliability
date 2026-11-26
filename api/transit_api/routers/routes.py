"""Bus routes: /routes/..."""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from transit_api import rollups
from transit_api.deps import DbDep, DuckDep, FeedDep, SettingsDep, TodayDep
from transit_api.models import (
    DirectionReliability,
    Reliability,
    Route,
    RouteDetail,
    RouteDirection,
    RouteReliability,
)
from transit_api.queries import routes as q

router = APIRouter(prefix="/routes", tags=["routes"])

DEFAULT_DAYS = 28  # four whole weeks, so every weekday counts the same


@router.get("")
def list_routes(conn: DbDep, feed_id: FeedDep) -> list[Route]:
    """Every route in the static feed covering today, numbered routes first."""
    return [Route(**r) for r in q.list_routes(conn, feed_id)]


@router.get("/{route_id}")
def route_detail(route_id: str, conn: DbDep, feed_id: FeedDep,
                 today: TodayDep) -> RouteDetail:
    """Each direction's main pattern (stops in order and the path to draw), from today's
    trips, or from the route's next service day within a week if it doesn't run today."""
    route = q.get_route(conn, feed_id, route_id)
    if route is None:
        raise HTTPException(404, f"no route {route_id!r} in the current static feed")

    day = q.next_service_day(conn, feed_id, route_id, today)
    patterns = q.main_patterns(conn, feed_id, route_id, day) if day else []
    stops = q.trip_stops(conn, feed_id, [p["trip_id"] for p in patterns])
    paths = q.shapes(conn, feed_id, [p["shape_id"] for p in patterns if p["shape_id"]])
    return RouteDetail(
        route=Route(**route), service_date=day,
        directions=[RouteDirection(direction_id=p["direction_id"], headsign=p["headsign"],
                                   trips=p["trips"], pattern_trips=p["pattern_trips"],
                                   stops=stops[p["trip_id"]],
                                   shape=paths.get(p["shape_id"], []))
                    for p in patterns])


def get_route(route_id: str, conn: DbDep) -> Route:
    """The route's names from the newest static feed that has it, or 404. (For reliability,
    which spans feeds; the static endpoints use the feed covering today.)"""
    row = conn.execute("""
        SELECT r.route_id, r.route_short_name, r.route_long_name, trim(r.route_color)
        FROM gtfs.routes r JOIN gtfs.feed f USING (feed_id)
        WHERE r.route_id = %s
        ORDER BY f.fetched_at DESC LIMIT 1
    """, [route_id]).fetchone()
    if row is None:
        raise HTTPException(404, f"no route {route_id!r} in any static feed")
    return Route(route_id=row[0], short_name=row[1], long_name=row[2], color=row[3] or None)


@router.get("/{route_id}/reliability")
def route_reliability(route: Annotated[Route, Depends(get_route)], cur: DuckDep,
                      settings: SettingsDep, start: date | None = None,
                      end: date | None = None) -> RouteReliability:
    """On time, coverage and delay for each direction and kind of stop, over service
    dates start..end. Default: the 28 days ending at the newest rollup."""
    end = (end or rollups.latest_date(settings.data_dir, "by_route")
           or datetime.now(ZoneInfo(settings.timezone)).date())
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise HTTPException(422, "start is after end")

    paths = rollups.files(settings.data_dir, "by_route", start, end)
    directions: dict[int | None, list[Reliability]] = {}
    for row in rollups.route_reliability(cur, paths, route.route_id):
        directions.setdefault(row.pop("direction_id"), []).append(Reliability(**row))
    return RouteReliability(
        route=route, start=start, end=end,
        directions=[DirectionReliability(direction_id=d, kinds=k) for d, k in directions.items()])
