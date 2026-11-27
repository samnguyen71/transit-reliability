"""/health: always 200 while the process is up; the body says what works."""

import psycopg
from fastapi import APIRouter

from transit_api import live
from transit_api.deps import LiveDep, NowDep, SettingsDep, StoreDep, TodayDep
from transit_api.models import ExcludedDay, Health, RealtimeStatus, RollupStatus

router = APIRouter()


@router.get("/health", include_in_schema=False)
def health(settings: SettingsDep, store: StoreDep, today: TodayDep, live_feed: LiveDep,
           now: NowDep) -> Health:
    snap = store.snapshot()
    dates = snap.dates()
    rollups = RollupStatus(
        version=snap.version, first_date=min(dates, default=None),
        last_date=max(dates, default=None), dates=len(dates),
        excluded=[ExcludedDay(service_date=d, coverage=c) for d, c in sorted(snap.coverage.items())
                  if store.low_coverage(snap, d)],
        rule_version=snap.meta.get("rule_version"), rule_versions=snap.rule_versions,
        rollup_version=snap.meta.get("rollup_version"),
        written_at=snap.meta.get("written_at"), scanned_at=snap.scanned_at)

    database, feed_id = "unset", None
    if settings.database_url:
        try:
            with psycopg.connect(settings.database_url, autocommit=True,
                                 connect_timeout=2) as conn:
                feed_id = conn.execute("SELECT gtfs.feed_for(%s)", [today]).fetchone()[0]
            database = "ok"
        except psycopg.Error:
            database = "error"
    status = "ok" if snap.version and database != "error" else "degraded"
    realtime = RealtimeStatus(**live.status(live_feed.snapshot(), now, settings.live_stale_s))
    return Health(status=status, database=database, static_feed_id=feed_id, rollups=rollups,
                  realtime=realtime)
