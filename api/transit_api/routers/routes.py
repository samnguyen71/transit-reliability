"""Bus routes: /routes/..."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from transit_api import window
from transit_api.deps import DbDep, DuckDep, FeedDep, RollupsDep, StoreDep, TodayDep
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
    found = q.routes_by_id(conn, [route_id])
    if not found:
        raise HTTPException(404, f"no route {route_id!r} in any static feed")
    return Route(**found[route_id])


@router.get("/{route_id}/reliability")
def route_reliability(snap: RollupsDep, route: Annotated[Route, Depends(get_route)],
                      w: window.WindowDep, store: StoreDep, cur: DuckDep,
                      today: TodayDep) -> RouteReliability:
    """On time, coverage and delay for each direction and kind of stop."""
    fields, sel = window.resolve(w, store, snap, "by_route", today)
    rows = store.summarize(snap, cur, sel, ("direction_id",), {"route_id": route.route_id})
    return RouteReliability(
        **fields, route=route,
        directions=[DirectionReliability(direction_id=d, kinds=[Reliability(**r) for r in k])
                    for d, k in window.group(rows, "direction_id").items()])
