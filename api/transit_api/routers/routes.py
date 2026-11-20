"""Bus routes: /routes/..."""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

from transit_api import rollups
from transit_api.deps import DbDep, DuckDep, SettingsDep
from transit_api.models import DirectionReliability, Reliability, Route, RouteReliability

router = APIRouter(prefix="/routes", tags=["routes"])

DEFAULT_DAYS = 28  # four whole weeks, so every weekday counts the same


def get_route(route_id: str, conn: DbDep) -> Route:
    """The route's names from the newest static feed that has it, or 404."""
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
