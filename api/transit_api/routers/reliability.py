"""Reliability across routes: /reliability/... Rollups only, so no database needed."""

from fastapi import APIRouter, HTTPException

from transit_api import window
from transit_api.deps import DuckDep, RollupsDep, StoreDep, TodayDep
from transit_api.models import HourKinds, HourReliability, Reliability

router = APIRouter(prefix="/reliability", tags=["reliability"])


@router.get("/hours")
def by_hour(snap: RollupsDep, w: window.WindowDep, store: StoreDep, cur: DuckDep,
            today: TodayDep, route_id: str | None = None,
            direction_id: int | None = None) -> HourReliability:
    """On time, coverage and delay by hour of the scheduled time and kind of stop: the
    whole network, or one route (and direction). Hours without scored visits are left
    out."""
    if direction_id is not None and route_id is None:
        raise HTTPException(422, "direction_id needs route_id")
    fields, sel = window.resolve(w, store, snap, "by_hour", today)
    filters = {k: v for k, v in (("route_id", route_id), ("direction_id", direction_id))
               if v is not None}
    rows = store.summarize(snap, cur, sel, ("hour",), filters)
    return HourReliability(
        **fields, route_id=route_id, direction_id=direction_id,
        hours=[HourKinds(hour=h, kinds=[Reliability(**r) for r in k])
               for h, k in window.group(rows, "hour").items()])
