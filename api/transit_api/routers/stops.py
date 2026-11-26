"""Bus stops: /stops/... All from the static feed covering today (deps.get_feed)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from transit_api.deps import DbDep, FeedDep, TodayDep
from transit_api.models import StopDetail, StopSearchResponse
from transit_api.queries import stops

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
