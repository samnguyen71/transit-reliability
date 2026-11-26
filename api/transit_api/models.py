"""Response models. Every endpoint returns one of these, so /docs shows the real shape."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

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


class RouteReliability(BaseModel):
    route: Route
    start: date
    end: date
    directions: list[DirectionReliability]


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
