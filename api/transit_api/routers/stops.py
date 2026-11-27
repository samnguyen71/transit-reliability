"""Bus stops: /stops/... The static endpoints and live departures use the feed covering
today (deps.get_feed); reliability spans feeds, so it names the stop from the newest one."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from transit_api import board, live, window
from transit_api.deps import (
    DbDep,
    DuckDep,
    FeedDep,
    LiveDep,
    NowDep,
    RollupsDep,
    SettingsDep,
    StoreDep,
    TodayDep,
)
from transit_api.models import (
    Reliability,
    Route,
    RouteKinds,
    StopDepartures,
    StopDetail,
    StopReliability,
    StopSearchResponse,
)
from transit_api.queries import departures, routes, stops

router = APIRouter(prefix="/stops", tags=["stops"])


# Declared before /{stop_id}, or "search" would be taken for a stop_id.
@router.get("/search")
def search_stops(conn: DbDep, feed_id: FeedDep,
                 q: Annotated[str, Query(min_length=1, max_length=100,
                                         description="stop name words or a stop code")],
                 limit: Annotated[int, Query(ge=1, le=100)] = 10) -> StopSearchResponse:
    """Stops by name, typos allowed, words in any order ("simcoe king" finds
    "King Westbound @ Simcoe"), or by the code on the stop's sign."""
    if not stops.tokenize(q):
        raise HTTPException(422, "the query needs a word of at least two letters "
                                 "other than 'and' or 'at'")
    return StopSearchResponse(query=q, results=stops.search_stops(conn, feed_id, q, limit))


@router.get("/{stop_id}")
def get_stop(stop_id: str, conn: DbDep, feed_id: FeedDep, today: TodayDep) -> StopDetail:
    """The stop and the routes that stop there today."""
    stop = stops.get_stop(conn, feed_id, stop_id)
    if stop is None:
        raise HTTPException(404, f"no stop {stop_id!r} in the current static feed")
    return StopDetail(**stop, service_date=today,
                      routes=stops.stop_routes(conn, feed_id, stop_id, today))


@router.get("/{stop_id}/departures")
def stop_departures(
        stop_id: str, response: Response, conn: DbDep, feed_id: FeedDep, live_feed: LiveDep,
        settings: SettingsDep, now: NowDep, today: TodayDep,
        limit: Annotated[int, Query(ge=1, le=30)] = 10,
        horizon_min: Annotated[int, Query(
            ge=15, le=180, description="minutes ahead; past about 60, most departures are "
                                       "scheduled only (trips enter the feed 60 min ahead)")
        ] = 90) -> StopDepartures:
    """The next departures from the stop: scheduled times, with the live feed's
    predictions, cancellations and skipped stops. Not cached: poll it."""
    stop = stops.get_stop(conn, feed_id, stop_id)
    if stop is None:
        raise HTTPException(404, f"no stop {stop_id!r} in the current static feed")
    snap = live_feed.snapshot()
    rt = live.status(snap, now, settings.live_stale_s)
    horizon = timedelta(minutes=horizon_min)
    # Yesterday: its trips after 24:00 run tonight. Tomorrow: a late-evening window
    # reaches its first overnight runs.
    days = [today - timedelta(days=1), today, today + timedelta(days=1)]
    rows = departures.scheduled(conn, stop_id, days, now - board.LOOKBACK,
                                now + horizon + board.EARLY_MARGIN)
    response.headers["Cache-Control"] = "no-store"
    return StopDepartures(
        stop=stop, now=now, horizon_min=horizon_min, realtime=rt,
        departures=board.merge(rows, snap, rt["status"] == "ok", now, horizon)[:limit])


@router.get("/{stop_id}/reliability")
def stop_reliability(snap: RollupsDep, stop_id: str, w: window.WindowDep, store: StoreDep,
                     conn: DbDep, cur: DuckDep, today: TodayDep) -> StopReliability:
    """On time, coverage and delay at the stop, for each route and kind of stop, and for
    all its routes together."""
    stop = stops.get_stop_any_feed(conn, stop_id)
    if stop is None:
        raise HTTPException(404, f"no stop {stop_id!r} in any static feed")
    fields, sel = window.resolve(w, store, snap, "by_stop", today)
    filters = {"stop_id": stop_id}
    overall = store.summarize(snap, cur, sel, (), filters)
    by_route = window.group(store.summarize(snap, cur, sel, ("route_id",), filters), "route_id")
    names = routes.routes_by_id(conn, [r for r in by_route if r is not None])
    known = [names.get(r) or {"route_id": r, "short_name": None, "long_name": None,
                              "color": None} for r in by_route if r is not None]
    return StopReliability(
        **fields, stop=stop, all_routes=[Reliability(**r) for r in overall],
        routes=[RouteKinds(route=Route(**r), kinds=[Reliability(**k)
                                                    for k in by_route[r["route_id"]]])
                for r in sorted(known, key=routes.route_sort_key)])
