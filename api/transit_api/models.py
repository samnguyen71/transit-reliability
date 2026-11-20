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
