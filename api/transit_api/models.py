"""Response models. Every endpoint returns one of these, so /docs shows the real shape."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from transit_api.rollups import DayType

Kind = Literal["first_stop", "timepoint", "last_stop"]


class Route(BaseModel):
    route_id: str
    short_name: str | None
    long_name: str | None
    color: str | None = Field(description="hex without #, as in routes.txt")


class Reliability(BaseModel):
    """One slice of the rollups: the counts summed over its rows and dates, and the rates
    worked out from those sums (never an average of daily rates)."""
    kind: Kind
    days: int = Field(description="service dates with at least one row in the slice")
    scheduled: int = Field(description="scored visits, all statuses")
    observed: int
    on_time: int = Field(description="departures -60..+300 s; last-stop arrivals <= +300 s")
    early: int
    late: int
    cancelled: int
    skipped: int
    lost: int
    no_data: int
    not_seen: int
    on_time_rate: float | None = Field(description="on_time / observed")
    coverage: float | None = Field(
        description="observed / (scheduled - cancelled - skipped): the share of the service "
                    "that ran that has an observed time")
    median_delay_s: int | None = Field(
        description="from the summed delay histogram, to within its 60 s bins; clamped to "
                    "-300..1800")
    p90_delay_s: int | None


class DirectionReliability(BaseModel):
    direction_id: int | None
    kinds: list[Reliability] = Field(description="first_stop, timepoint, last_stop, if any")


class ExcludedDay(BaseModel):
    service_date: date
    coverage: float = Field(description="network-wide observed / visits that ran")


class Window(BaseModel):
    """The service dates an answer covers."""
    start: date
    end: date
    day_type: DayType | None = Field(description="weekday or weekend (holidays run weekend "
                                                 "service); null for every day")
    dates: int = Field(description="service dates with rollups in the window, after the "
                                   "exclusions")
    excluded: list[ExcludedDay] = Field(
        description="dates in the window and day type left out because the feed covered "
                    "too little of the day (MIN_DAY_COVERAGE)")


class RouteReliability(Window):
    route: Route
    directions: list[DirectionReliability]


class RouteKinds(BaseModel):
    route: Route
    kinds: list[Reliability]


class HourKinds(BaseModel):
    hour: int = Field(description="of the scheduled time, local; trips after midnight stay "
                                  "on their service date, under hours 0-3")
    kinds: list[Reliability]


class HourReliability(Window):
    route_id: str | None
    direction_id: int | None
    hours: list[HourKinds]


class RollupStatus(BaseModel):
    version: str | None = Field(description="the ETag of every reliability answer")
    first_date: date | None
    last_date: date | None
    dates: int
    excluded: list[ExcludedDay] = Field(description="every low-coverage date, in any window")
    rule_version: str | None = Field(description="observe's rule, from the newest file")
    rule_versions: dict[str, int] = Field(
        description="service dates per observe rule version (by_route); more than one "
                    "while a rebuild after a rule change is under way")
    rollup_version: str | None
    written_at: str | None = Field(description="when observe wrote the newest file's input")
    scanned_at: datetime


class Health(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="degraded: no rollups, or the "
                                                          "database is set but unreachable")
    database: Literal["ok", "unset", "error"]
    static_feed_id: int | None = Field(description="the feed covering today")
    rollups: RollupStatus


class Stop(BaseModel):
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    parent_station: str | None = None
    stop_code: str | None = Field(None, description="the number on the stop's sign")


class StopSearchResponse(BaseModel):
    query: str
    results: list[Stop] = Field(description="an exact stop_code match first, then by how "
                                            "well the name matches every word")


class StopRoute(BaseModel):
    """A route and direction that serve a stop."""
    route: Route
    direction_id: int | None
    headsigns: list[str] = Field(description="the trips' headsigns at this stop, most "
                                             "common first")
    trips: int = Field(description="trips that stop here on service_date")


class StopDetail(Stop):
    service_date: date
    routes: list[StopRoute] = Field(description="routes stopping here on service_date, in "
                                                "route order; empty if none do")


class PatternStop(Stop):
    stop_sequence: int
    timepoint: bool = Field(description="timepoint 1, or blank in a feed that marks no "
                                        "timepoints (observe.py's rule)")


class RouteDirection(BaseModel):
    direction_id: int | None
    headsign: str | None = Field(description="the main pattern's most common headsign")
    trips: int = Field(description="all trips in this direction on service_date")
    pattern_trips: int = Field(description="of those, the trips that follow the main pattern")
    stops: list[PatternStop] = Field(description="the main pattern: the most common stop "
                                                 "sequence on service_date")
    shape: list[tuple[float, float]] = Field(
        description="the main pattern's path as [lon, lat] pairs (GeoJSON order); empty if "
                    "its trips have no shape")


class RouteDetail(BaseModel):
    route: Route
    service_date: date | None = Field(
        description="the day the patterns come from: today, or the route's next service day "
                    "within a week; null if it has none")
    directions: list[RouteDirection]


class StopReliability(Window):
    stop: Stop
    all_routes: list[Reliability] = Field(description="every route at the stop together, "
                                                      "by kind")
    routes: list[RouteKinds]
