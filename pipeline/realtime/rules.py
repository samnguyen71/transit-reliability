"""Observed-times rule v3 (OBSERVED_TIMES.md), as pure functions.

Nothing here reads files or databases: the inputs are plain values, so every edge case can
be tested with a handful of hand-built reports. realtime.evidence gathers the inputs from
the parsed Parquet; realtime.observe runs this for a service date and writes the results.

All times are POSIX seconds (ints). Feed times are whole seconds.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import pairwise

RULE_VERSION = "3"

AGREE_S = 60              # TU time accepted if inside the VP departure bracket ±60 s
NEAR_M = 100.0            # "at the last stop" by position
LAST_STOP_SEARCH_S = 600  # last-stop search ends 10 min after the trip's last own report
GAP_S = 90                # more than this without a fresh snapshot is a gap
FROZEN_S = 120            # a snapshot whose feed timestamp is older than this is not fresh

TripKey = tuple[str, str]  # (canonical trip_id, start_date as YYYYMMDD)
Gap = tuple[int, int]      # (last fresh feed time before, first fresh feed time after)


# Inputs --------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Report:
    """One VehiclePositions report that is new evidence (see clean_reports)."""
    t: int                  # the vehicle's own timestamp
    vehicle_id: str
    trip: TripKey | None    # None if the report names no trip or no start_date
    seq: int | None         # current_stop_sequence
    stopped: bool           # current_status == STOPPED_AT
    lat: float | None
    lon: float | None


@dataclass(frozen=True, slots=True)
class TuStop:
    """The last fresh TripUpdates listing of one stop of one trip."""
    seen_at: int            # feed timestamp of that snapshot
    time: int | None        # arrival.time (DRT's is the departure)
    skipped: bool           # schedule_relationship == SKIPPED

    @property
    def recorded(self) -> int | None:
        """The time, only if it was already in the past at that snapshot (not a prediction)."""
        return self.time if self.time is not None and self.time <= self.seen_at else None


@dataclass(frozen=True, slots=True)
class Stop:
    seq: int
    stop_id: str
    sched_arr: int | None
    sched_dep: int | None
    timepoint: bool         # departure is scored (timepoint, or the trip's first stop)
    lat: float
    lon: float


@dataclass(frozen=True)
class Trip:
    key: TripKey
    route_id: str | None
    direction_id: int | None
    stops: list[Stop]       # in stop_sequence order


@dataclass(slots=True)
class Evidence:
    """Everything observed for one service date, already cleaned."""
    vp_gaps: list[Gap] = field(default_factory=list)
    tu_gaps: list[Gap] = field(default_factory=list)
    by_vehicle: dict[str, list[Report]] = field(default_factory=dict)  # each sorted by t
    by_trip: dict[TripKey, list[Report]] = field(default_factory=dict)  # each sorted by t
    tu_stops: dict[TripKey, dict[int, TuStop]] = field(default_factory=dict)
    tu_seen: set[TripKey] = field(default_factory=set)
    cancelled: set[TripKey] = field(default_factory=set)


# Output --------------------------------------------------------------------------------

@dataclass(slots=True)
class Visit:
    seq: int
    stop_id: str
    event: str              # "departure", or "arrival" at the last stop
    scored: bool            # timepoint departure, or last-stop arrival
    scheduled: int | None
    status: str             # observed, cancelled, skipped, no_data, lost, not_seen
    time: int | None = None
    source: str | None = None   # tu, tu_gps, vp, tu_only, vp_stopped, vp_position
    lo: int | None = None
    hi: int | None = None
    tu_time: int | None = None  # TU's recorded time, kept even when not used
    disagree: bool | None = None
    tu_gap: bool | None = None  # bracket overlaps a TripUpdates gap (for the disagreement report)
    vehicle_id: str | None = None


# Snapshots and gaps --------------------------------------------------------------------

def fresh_snapshots(rows: Iterable[tuple[str, int, str, int | None]],
                    stats: Counter | None = None) -> list[tuple[str, int]]:
    """Keep the fresh snapshots. rows: (file, fetched_at, status, feed timestamp) in fetch
    order. A snapshot is fresh if it was readable, its feed timestamp is newer than every
    earlier one, and it was no more than FROZEN_S old when fetched. Returns (file, feed time).
    """
    stats = stats if stats is not None else Counter()
    kept: list[tuple[str, int]] = []
    newest = None
    for file, fetched, status, header in rows:
        if status != "ok" or header is None:
            stats["snapshot not usable"] += 1
            continue
        if newest is not None and header <= newest:
            stats["snapshot not newer"] += 1
            continue
        newest = header
        if fetched - header > FROZEN_S:
            stats["snapshot frozen"] += 1
            continue
        kept.append((file, header))
    return kept


def find_gaps(times: list[int], start: int, end: int) -> list[Gap]:
    """Stretches of more than GAP_S without a fresh snapshot within [start, end].
    times: feed timestamps of the fresh snapshots, ascending."""
    points = [start, *(t for t in times if start < t < end), end]
    return [(a, b) for a, b in pairwise(points) if b - a > GAP_S]


def overlaps(gaps: list[Gap], lo: int, hi: int) -> bool:
    """Does the interval (lo, hi) overlap any gap?"""
    return any(a < hi and lo < b for a, b in gaps)


def near_gap(gaps: list[Gap], t: int) -> bool:
    """Is t within GAP_S of a gap? Used where evidence stops or starts: if it does so at a
    gap, the feed cut it short, not the vehicle."""
    return any(a - GAP_S <= t <= b + GAP_S for a, b in gaps)


# Vehicle reports -----------------------------------------------------------------------

def clean_reports(rows: Iterable[tuple], alias: dict[str, str],
                  stats: Counter | None = None
                  ) -> tuple[dict[str, list[Report]], dict[TripKey, list[Report]]]:
    """Turn VehiclePositions rows into per-vehicle and per-trip report lists.

    rows: (vehicle_id, t, trip_id, start_date, seq, stopped, lat, lon), grouped by vehicle
    and in fetch order within a vehicle. A report counts only if its timestamp is known and
    newer than that vehicle's last counted one. trip_ids are mapped through alias.
    """
    stats = stats if stats is not None else Counter()
    by_vehicle: dict[str, list[Report]] = {}
    by_trip: dict[TripKey, list[Report]] = {}
    keys: dict[tuple, TripKey] = {}  # one tuple per trip, not one per report
    for vehicle_id, t, trip_id, start_date, seq, stopped, lat, lon in rows:
        if vehicle_id is None:
            stats["vp no vehicle_id"] += 1
            continue
        if t is None:
            stats["vp no timestamp"] += 1
            continue
        reports = by_vehicle.setdefault(vehicle_id, [])
        if reports and t <= reports[-1].t:
            stats["vp not newer"] += 1
            continue
        key = None
        if trip_id is not None and start_date is not None:
            raw = (trip_id, start_date)
            key = keys.get(raw)
            if key is None:
                key = keys[raw] = (alias.get(trip_id, trip_id), start_date)
        r = Report(t, reports[0].vehicle_id if reports else vehicle_id, key, seq,
                   bool(stopped), lat, lon)
        reports.append(r)
        if key is not None:
            by_trip.setdefault(key, []).append(r)
    for reports in by_trip.values():
        reports.sort(key=lambda r: r.t)  # a trip may have had more than one vehicle
    return by_vehicle, by_trip


class Track:
    """A trip's own reports that have a stop_sequence, for the strict departure rule.

    seqs, if given, replaces each report's current_stop_sequence (see gps_track); reports
    whose entry is None are left out.
    """

    def __init__(self, reports: list[Report], seqs: list[int | None] | None = None):
        if seqs is None:
            seqs = [r.seq for r in reports]
        pairs = [(r, s) for r, s in zip(reports, seqs, strict=True) if s is not None]
        self.reports = [r for r, _ in pairs]
        self.seq_of = {id(r): s for r, s in pairs}  # report -> the stop_sequence used for it
        # suffix[i] = lowest stop_sequence from report i on. It never decreases.
        self.suffix: list[int] = []
        low = math.inf
        for _, s in reversed(pairs):
            low = min(low, s)
            self.suffix.append(low)
        self.suffix.reverse()

    def departure(self, seq: int) -> tuple[Report | None, Report | None]:
        """The departure bracket (lo, hi] for a stop.

        hi is the first report past the stop (current_stop_sequence > seq) after which no
        report goes back to the stop or earlier. lo is the report just before it, the last
        one at or before the stop. Either is None where the evidence doesn't reach: no lo
        means the trip was first seen already past the stop, no hi means it was never seen
        leaving it. Reports DRT attaches during layover, still carrying the previous trip's
        stop_sequence, are followed by reports that go back, so they never count.
        """
        i = bisect_right(self.suffix, seq)
        lo = self.reports[i - 1] if i > 0 else None
        hi = self.reports[i] if i < len(self.reports) else None
        return lo, hi


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres (equirectangular; exact enough at 100 m)."""
    k = math.pi / 180
    x = (lon2 - lon1) * k * math.cos((lat1 + lat2) / 2 * k)
    y = (lat2 - lat1) * k
    return 6_371_000 * math.hypot(x, y)


def gps_track(trip: Trip, own: list[Report]) -> Track:
    """The trip's own reports, each placed at the trip's stop nearest its GPS position
    instead of at its current_stop_sequence. DRT's current_stop_sequence sometimes stops
    advancing while the bus drives on; its position doesn't."""
    if not trip.stops:
        return Track([], [])
    k = math.cos(trip.stops[0].lat * math.pi / 180)  # one scale for the whole trip
    stops = [(s.seq, s.lat, s.lon * k) for s in trip.stops]
    seqs: list[int | None] = []
    for r in own:
        if r.lat is None or r.lon is None:
            seqs.append(None)
            continue
        x = r.lon * k
        seqs.append(min(stops, key=lambda s: (s[1] - r.lat) ** 2 + (s[2] - x) ** 2)[0])
    return Track(own, seqs)


# The rule ------------------------------------------------------------------------------

def observe_trip(trip: Trip, ev: Evidence) -> list[Visit]:
    """One Visit per scheduled stop of the trip, in stop order."""
    key = trip.key
    own = ev.by_trip.get(key, [])
    tu = ev.tu_stops.get(key, {})
    track = Track(own)
    timed = [t for s in trip.stops for t in (s.sched_dep, s.sched_arr) if t is not None]
    span = (min(timed), max(timed)) if timed else None
    last = len(trip.stops) - 1

    def blank(i: int, s: Stop, status: str) -> Visit:
        if i == last:
            return Visit(s.seq, s.stop_id, "arrival", True, s.sched_arr, status)
        return Visit(s.seq, s.stop_id, "departure", s.timepoint, s.sched_dep, status)

    if not own and key not in ev.tu_seen:
        gaps = ev.vp_gaps + ev.tu_gaps
        status = "no_data" if span and overlaps(gaps, *span) else "not_seen"
        return [blank(i, s, status) for i, s in enumerate(trip.stops)]

    ran = own or any(u.recorded is not None for u in tu.values())
    if key in ev.cancelled and not ran:
        return [blank(i, s, "cancelled") for i, s in enumerate(trip.stops)]

    gps: list[Track] = []  # built on first use: only disagreements need it

    def gps_lazy() -> Track:
        if not gps:
            gps.append(gps_track(trip, own))
        return gps[0]

    visits = []
    for i, stop in enumerate(trip.stops):
        if i == last:
            prev = trip.stops[i - 1] if i > 0 else None
            visits.append(_last_stop(trip, stop, prev, track, own, tu.get(stop.seq), ev, span))
            continue
        if i == 0:
            v = _first_departure(stop, own, tu.get(stop.seq), ev, span)
        else:
            v = _departure(stop, track, gps_lazy, tu.get(stop.seq), ev, span)
        v.vehicle_id = v.vehicle_id or (own[-1].vehicle_id if own else None)
        visits.append(v)
    return visits


def _first_departure(stop: Stop, own: list[Report], tu: TuStop | None, ev: Evidence,
                     span: tuple[int, int] | None) -> Visit:
    """Departure from a trip's first stop, by position.

    Both feeds move on from the first stop while the bus is still waiting there (a median
    8 min early on 2026-11-05), so neither TU's time nor current_stop_sequence is used.
    The bracket runs from the last own report within NEAR_M of the stop to the next one,
    the first beyond it. TU's time is kept for the disagreement report.
    """
    v = Visit(stop.seq, stop.stop_id, "departure", stop.timepoint, stop.sched_dep, "")
    v.tu_time = tu.recorded if tu else None
    if tu is not None and tu.skipped:
        v.status = "skipped"
        return v
    placed = [r for r in own if r.lat is not None and r.lon is not None]
    lo = hi = None
    for r in placed:
        if distance_m(r.lat, r.lon, stop.lat, stop.lon) <= NEAR_M:
            lo = r
        elif lo is not None:
            hi = r
            break
    v.lo, v.hi = (lo.t if lo else None), (hi.t if hi else None)
    v.vehicle_id = (hi or lo).vehicle_id if (hi or lo) else None
    if lo and hi:
        v.tu_gap = overlaps(ev.tu_gaps, lo.t, hi.t)
        if overlaps(ev.vp_gaps, lo.t, hi.t):
            v.status = "no_data"
        else:
            v.status, v.time, v.source = "observed", hi.t, "vp_position"
            v.disagree = (v.tu_time is not None
                          and not lo.t - AGREE_S <= v.tu_time <= hi.t + AGREE_S)
    elif lo:         # still at the stop when the evidence ends
        v.status = "no_data" if near_gap(ev.vp_gaps, lo.t) else "lost"
    elif placed:     # never seen at the stop: first seen after leaving, or GPS never near it
        v.status = "no_data" if near_gap(ev.vp_gaps, placed[0].t) else "lost"
    else:
        v.status = "no_data" if span and overlaps(ev.vp_gaps, *span) else "lost"
    return v


def _departure(stop: Stop, track: Track, gps: Callable[[], Track], tu: TuStop | None,
               ev: Evidence, span: tuple[int, int] | None) -> Visit:
    v = Visit(stop.seq, stop.stop_id, "departure", stop.timepoint, stop.sched_dep, "")
    lo, hi = track.departure(stop.seq)
    v.lo = lo.t if lo else None
    v.hi = hi.t if hi else None
    v.vehicle_id = hi.vehicle_id if hi else None
    v.tu_time = tu.recorded if tu else None

    if tu is not None and tu.skipped:
        v.status = "skipped"
    elif lo and hi:
        v.tu_gap = overlaps(ev.tu_gaps, lo.t, hi.t)
        if overlaps(ev.vp_gaps, lo.t, hi.t):
            v.status = "no_data"
        elif v.tu_time is not None and lo.t - AGREE_S <= v.tu_time <= hi.t + AGREE_S:
            v.status, v.time, v.source, v.disagree = "observed", v.tu_time, "tu", False
        elif v.tu_time is not None and _gps_agrees(gps(), stop.seq, v.tu_time):
            # current_stop_sequence lagged behind the bus: GPS puts TU's time right.
            v.status, v.time, v.source, v.disagree = "observed", v.tu_time, "tu_gps", True
        elif v.tu_time is not None and gps().seq_of.get(id(lo), -1) > stop.seq:
            # GPS has the bus past the stop at lo, so VP's bracket is stale, but it doesn't
            # back TU's time either: no time to trust.
            v.status, v.disagree = "lost", True
        else:
            v.status, v.time, v.source = "observed", hi.t, "vp"
            v.disagree = v.tu_time is not None
    elif v.tu_time is not None:
        v.status, v.time, v.source = "observed", v.tu_time, "tu_only"
    else:
        v.status = _no_evidence(ev, lo, hi, span)
    return v


def _gps_agrees(gps: Track, seq: int, t: int) -> bool:
    """Is t inside the GPS departure bracket for the stop, ±AGREE_S?"""
    lo, hi = gps.departure(seq)
    return lo is not None and hi is not None and lo.t - AGREE_S <= t <= hi.t + AGREE_S


def _no_evidence(ev: Evidence, lo: Report | None, hi: Report | None,
                 span: tuple[int, int] | None) -> str:
    """no_data if a feed gap explains the missing evidence, else lost."""
    if lo and not hi:      # evidence ends before the bus left
        return "no_data" if near_gap(ev.vp_gaps, lo.t) else "lost"
    if hi and not lo:      # evidence starts after the bus had left
        return "no_data" if near_gap(ev.vp_gaps, hi.t) else "lost"
    # no reports with a stop_sequence at all (e.g. only in TripUpdates)
    return "no_data" if span and overlaps(ev.vp_gaps, *span) else "lost"


def _last_stop(trip: Trip, stop: Stop, prev: Stop | None, track: Track, own: list[Report],
               tu: TuStop | None, ev: Evidence, span: tuple[int, int] | None) -> Visit:
    v = Visit(stop.seq, stop.stop_id, "arrival", True, stop.sched_arr, "")
    if tu is not None and tu.skipped:
        v.status = "skipped"
        return v
    if not own:
        v.status = "no_data" if span and overlaps(ev.vp_gaps, *span) else "lost"
        return v

    # Start: once the bus is past the second-to-last stop for good (hi of its departure).
    anchor = track.departure(prev.seq)[1] if prev else track.reports[0] if track.reports else None
    end = own[-1].t + LAST_STOP_SEARCH_S
    if anchor is None:
        v.vehicle_id = own[-1].vehicle_id
        gap = overlaps(ev.vp_gaps, own[-1].t, end) or near_gap(ev.vp_gaps, own[-1].t)
        v.status = "no_data" if gap else "lost"
        return v

    v.vehicle_id = anchor.vehicle_id
    stream = ev.by_vehicle.get(anchor.vehicle_id, [])
    i = bisect_left([r.t for r in stream], anchor.t)
    for j in range(i, len(stream)):
        r = stream[j]
        if r.t > end:
            break
        own_stop = r.trip == trip.key and r.stopped and r.seq == stop.seq
        near = (r.lat is not None and r.lon is not None
                and distance_m(r.lat, r.lon, stop.lat, stop.lon) <= NEAR_M)
        if own_stop or near:
            v.lo = stream[j - 1].t if j > 0 else None
            v.hi = r.t
            if v.lo is not None and overlaps(ev.vp_gaps, v.lo, v.hi):
                v.status = "no_data"
            else:
                v.status, v.time = "observed", r.t
                v.source = "vp_stopped" if own_stop else "vp_position"
            return v
    v.status = "no_data" if overlaps(ev.vp_gaps, anchor.t, end) else "lost"
    return v
