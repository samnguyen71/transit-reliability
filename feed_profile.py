"""Profile one UTC day of realtime snapshots: what an observed-arrival rule can rely on.

Usage:
    python feed_profile.py TRIP_UPDATES_DAY VEHICLE_POSITIONS_DAY [STATIC_ZIP]

STATIC_ZIP (optional) splits the results by timepoint.

for example
    python feed_profile.py data/raw/trip_updates/2026-11-05 data/raw/vehicle_positions/2026-11-05

Reads every *.pb.gz and the _manifest.jsonl in both folders. Prints numbers, stores nothing.
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as pb

GAP_S = 90        # more than this between fresh snapshots is a gap: don't compare across it
PAST_S = 60       # a stop time this far behind the snapshot's timestamp is in the past
TOLERANCE_S = 60  # a TU arrival within the VP bracket +/- this agrees with it
NEAR_M = 100      # a vehicle this close to a stop's coordinates is at the stop

TRIP_REL = {v.number: v.name for v in pb.TripDescriptor.ScheduleRelationship.DESCRIPTOR.values}
STOP_REL = {
    v.number: v.name for v in pb.TripUpdate.StopTimeUpdate.ScheduleRelationship.DESCRIPTOR.values
}
STATUS = {v.number: v.name for v in pb.VehiclePosition.VehicleStopStatus.DESCRIPTOR.values}


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


def dist(values: list[float]) -> str:
    if not values:
        return "none"
    v = sorted(values)

    def q(p: float) -> float:
        return v[min(len(v) - 1, int(p * len(v)))]

    return (f"n={len(v)}  min {v[0]:.0f}  p5 {q(0.05):.0f}  median {q(0.5):.0f}"
            f"  p95 {q(0.95):.0f}  max {v[-1]:.0f}")


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%SZ")


def fresh_snapshots(folder: str, counts: Counter):
    """(feed timestamp, feed) in time order, skipping repeats and older copies."""
    last = None
    for path in sorted(Path(folder).glob("*.pb.gz")):
        feed = pb.FeedMessage()
        try:
            feed.ParseFromString(gzip.decompress(path.read_bytes()))
        except Exception:  # noqa: BLE001 - count it and carry on
            counts["unreadable"] += 1
            continue
        h = feed.header.timestamp
        if last is not None and h < last:
            counts["older than the previous snapshot (skipped)"] += 1
            continue
        if last is not None and h == last:
            counts["same feed timestamp as the previous one (skipped)"] += 1
            continue
        counts["fresh"] += 1
        last = h
        yield h, feed


def manifest(folder: str) -> list[str]:
    path = Path(folder) / "_manifest.jsonl"
    if not path.exists():
        return ["  no _manifest.jsonl"]
    statuses: Counter = Counter()
    polls, ages = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        t = datetime.fromisoformat(r["fetched_at"].replace("Z", "+00:00")).timestamp()
        statuses[r["status"]] += 1
        polls.append((t, r["status"]))
        if r.get("feed_age_s") is not None:
            ages.append(r["feed_age_s"])
    polls.sort()
    saved = [t for t, s in polls if s == "saved"]

    def longest(times: list[float]) -> str:
        gaps = sorted(((b - a, a) for a, b in zip(times, times[1:])), reverse=True)[:3]
        return ", ".join(f"{g:.0f}s from {hhmm(a)}" for g, a in gaps) or "-"

    return [
        "  polls: " + ", ".join(f"{s} {n}" for s, n in statuses.most_common()),
        f"  feed age at save (s): {dist(ages)}",
        f"  longest time between polls (logger not polling): {longest([t for t, _ in polls])}",
        f"  longest time without a new snapshot: {longest(saved)}",
    ]


def stop_time(u) -> int | None:
    """Absolute arrival time, else departure time, else None (delay only or nothing)."""
    for ev in ("arrival", "departure"):
        if u.HasField(ev) and getattr(u, ev).HasField("time"):
            return getattr(u, ev).time
    return None


def profile_trip_updates(folder: str):
    counts: Counter = Counter()
    c: Counter = Counter()
    trip_rel: Counter = Counter()
    stop_rel: Counter = Counter()
    drop_lead, end_lead, drops = [], [], []
    prev: dict = {}
    h_prev = None
    keys = set()
    first_seq: dict = {}  # (trip_id, start_date) -> lowest stop_sequence TU ever listed
    dep_prev: dict = {}
    for h, feed in fresh_snapshots(folder, counts):
        cur: dict = {}
        dep_cur: dict = defaultdict(dict)  # key -> {stop_sequence: departure time}
        for e in feed.entity:
            if not e.HasField("trip_update"):
                continue
            tu, t = e.trip_update, e.trip_update.trip
            c["trip updates"] += 1
            rel = t.schedule_relationship
            trip_rel[TRIP_REL.get(rel, rel) if t.HasField("schedule_relationship") else "(not set)"] += 1
            c["with vehicle id"] += tu.HasField("vehicle") and bool(tu.vehicle.id)
            c["with timestamp"] += tu.HasField("timestamp")
            c["with start_date"] += t.HasField("start_date")
            c["with start_time"] += t.HasField("start_time")
            c["with no stop updates"] += len(tu.stop_time_update) == 0
            key = (t.trip_id, t.start_date)
            keys.add(key)
            if key in cur:
                c["same trip twice in one snapshot"] += 1
            stops = {}
            for u in tu.stop_time_update:
                c["stop updates"] += 1
                stop_rel[STOP_REL.get(u.schedule_relationship, u.schedule_relationship)] += 1
                arr = u.arrival if u.HasField("arrival") else None
                if arr is not None and arr.HasField("time"):
                    c["arrival time"] += 1
                elif arr is not None and arr.HasField("delay"):
                    c["arrival delay only"] += 1
                else:
                    c["no arrival"] += 1
                for ev in ("arrival", "departure"):
                    if u.HasField(ev) and getattr(u, ev).HasField("uncertainty"):
                        c["uncertainty set"] += 1
                        c["uncertainty 0"] += getattr(u, ev).uncertainty == 0
                if not u.HasField("stop_sequence"):
                    c["no stop_sequence"] += 1
                    continue
                tm = stop_time(u)
                if tm is not None:
                    c["with a time"] += 1
                    c["time already past"] += tm < h - PAST_S
                stops[u.stop_sequence] = tm
                if u.HasField("departure") and u.departure.HasField("time"):
                    c["departure time"] += 1
                    if arr is not None and arr.HasField("time"):
                        c["both times"] += 1
                        c["arrival == departure"] += arr.time == u.departure.time
                    dep_cur[key][u.stop_sequence] = u.departure.time
                first_seq[key] = min(first_seq.get(key, u.stop_sequence), u.stop_sequence)
            cur[key] = stops

        if h_prev is not None and h - h_prev > GAP_S:
            c["gaps skipped"] += 1
        elif h_prev is not None:
            for key, old in prev.items():
                new = cur.get(key)
                if new is None:  # the trip vanished
                    if old:
                        last_seq = max(old)
                        if old[last_seq] is not None:
                            end_lead.append(old[last_seq] - h_prev)
                    continue
                if not new:
                    continue
                top = max(new)
                for s, tm in old.items():
                    if s not in new and s < top:
                        if tm is not None:
                            drop_lead.append(tm - h_prev)
                            drops.append((key, s, tm, h_prev, dep_prev.get(key, {}).get(s)))
                    elif s in new and tm is not None and new[s] is not None and tm < h_prev - PAST_S:
                        c["past stop seen again"] += 1
                        c["past stop time changed"] += new[s] != tm
        prev, h_prev, dep_prev = cur, h, dep_cur
    return counts, c, trip_rel, stop_rel, drop_lead, end_lead, drops, keys, first_seq


def profile_vehicles(folder: str):
    counts: Counter = Counter()
    c: Counter = Counter()
    status: Counter = Counter()
    lag = []
    by_trip: dict = defaultdict(list)  # (trip_id, start_date) -> [(time, seq, status)]
    extra = {
        "pos": {},                                        # (trip key, time) -> (lat, lon)
        "trip_vehicles": defaultdict(Counter),            # trip key -> Counter(vehicle id)
        "by_vehicle": defaultdict(list),  # vehicle -> [(time, key, seq, stop_id, status, pos)]
    }
    for h, feed in fresh_snapshots(folder, counts):
        for e in feed.entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            c["vehicles"] += 1
            has_trip = v.HasField("trip") and bool(v.trip.trip_id)
            c["with trip_id"] += has_trip
            for f in ("current_stop_sequence", "stop_id", "current_status", "timestamp"):
                c[f"with {f}"] += v.HasField(f)
            st = STATUS[v.current_status] if v.HasField("current_status") else None
            status[st or "(not set)"] += 1
            if v.HasField("timestamp") and v.timestamp == 0:
                c["timestamp 0 (report skipped)"] += 1
                continue
            ts = v.timestamp if v.HasField("timestamp") else h
            if v.HasField("timestamp"):
                lag.append(h - v.timestamp)
            if has_trip and v.HasField("current_stop_sequence"):
                key = (v.trip.trip_id, v.trip.start_date)
                reports = by_trip[key]
                if reports and reports[-1][0] == ts:
                    c["report not newer than the last one"] += 1
                    continue
                if reports and v.current_stop_sequence < reports[-1][1]:
                    c["stop_sequence went backwards"] += 1
                reports.append((ts, v.current_stop_sequence, st))
                if v.HasField("position"):
                    extra["pos"][(key, ts)] = (v.position.latitude, v.position.longitude)
                vid = v.vehicle.id if v.HasField("vehicle") else ""
                if vid:
                    extra["trip_vehicles"][key][vid] += 1
                    stream = extra["by_vehicle"][vid]
                    if not stream or stream[-1][0] != ts:
                        stop_id = v.stop_id if v.HasField("stop_id") else None
                        where = ((v.position.latitude, v.position.longitude)
                                 if v.HasField("position") else None)
                        stream.append((ts, key, v.current_stop_sequence, stop_id, st, where))
    return counts, c, status, lag, by_trip, extra


def bracket(reports, s, robust):
    """(lo, hi, how, trip_started_past) for stop s, or None if never reached.

    hi is the first report at the stop (STOPPED_AT) or past it. robust=True only accepts
    a report if no later report on the trip goes back before s, and "past the stop" only
    once the bus has been seen before the stop on this trip (so a bus still reporting its
    previous trip's stop_sequence under the new trip_id doesn't count).
    """
    floor = None
    if robust:
        floor = [0] * len(reports)
        m = float("inf")
        for i in range(len(reports) - 1, -1, -1):
            m = min(m, reports[i][1])
            floor[i] = m
    lo = None
    for i, (ts, seq, st) in enumerate(reports):
        reached = seq > s or (seq == s and st == "STOPPED_AT")
        how = "STOPPED_AT the stop" if seq == s else "seen past the stop"
        if reached and (not robust or (floor[i] >= s and (lo is not None or seq == s))):
            return lo, ts, how, lo is None
        if not reached:
            lo = ts
    return None


def size(d: float) -> str:
    if d < -TOLERANCE_S:
        return "TU earlier by >60s"
    if d <= TOLERANCE_S:
        return "within 60s"
    if d <= 300:
        return "TU later by 1-5 min"
    return "TU later by >5 min"


def floors(reports) -> list[int]:
    """floors[i] = the lowest stop_sequence in reports[i:]."""
    out = [0] * len(reports)
    m = float("inf")
    for i in range(len(reports) - 1, -1, -1):
        m = min(m, reports[i][1])
        out[i] = m
    return out


def departed(reports, floor, s, after_ts):
    """First report at or after after_ts that is past s for good: the VP departure time."""
    for i, (ts, seq, _) in enumerate(reports):
        if ts >= after_ts and seq > s and floor[i] > s:
            return ts
    return None


def load_static(zip_path: str) -> dict:
    import csv
    import io
    import zipfile

    st = {"timepoints": {}, "last_seq": {}, "stop_of": {}, "next_seq": {}, "coords": {}}
    seqs: dict = defaultdict(list)
    with zipfile.ZipFile(zip_path) as zf:
        rows = csv.DictReader(io.TextIOWrapper(zf.open("stop_times.txt"), "utf-8-sig"))
        for r in rows:
            seq = int(r["stop_sequence"])
            st["timepoints"][(r["trip_id"], seq)] = r.get("timepoint") == "1"
            st["stop_of"][(r["trip_id"], seq)] = r["stop_id"]
            seqs[r["trip_id"]].append(seq)
        if "stops.txt" in zf.namelist():
            rows = csv.DictReader(io.TextIOWrapper(zf.open("stops.txt"), "utf-8-sig"))
            for r in rows:
                st["coords"][r["stop_id"]] = (float(r["stop_lat"]), float(r["stop_lon"]))
    for trip_id, ss in seqs.items():
        ss.sort()
        st["last_seq"][trip_id] = ss[-1]
        for a, b in zip(ss, ss[1:]):
            st["next_seq"][(trip_id, a)] = b
    return st


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(h))


def where_was_the_bus(static, pos, key, s, ts) -> str:
    """At VP report time ts (which said the bus hadn't reached s yet), where was it really?

    Straight-line test against stop s and the next stop: rough, but enough to tell
    "VP's stop_sequence lagged" from "TU recorded the stop early".
    """
    p = pos.get((key, ts))
    nxt = static["next_seq"].get((key[0], s))
    here = static["coords"].get(static["stop_of"].get((key[0], s)))
    there = static["coords"].get(static["stop_of"].get((key[0], nxt))) if nxt else None
    if p is None or here is None or there is None:
        return "no position or stop coordinates"
    d_here, d_there, seg = metres(p, here), metres(p, there), metres(here, there)
    if d_here <= 75:
        return "within 75 m of the stop"
    if d_here + d_there <= 1.2 * seg + 50:
        return "already between the stop and the next (VP lagging)"
    return "not yet at the stop (TU early)"


def last_stop_coverage(by_trip, static, extra):
    """How each trip's vehicle reports end, relative to the trip's last scheduled stop.

    For trips without STOPPED_AT at the last stop under their own trip_id, follow the
    vehicle: is it STOPPED_AT that stop_id within 30 min, under another trip_id?
    """
    c: Counter = Counter()
    found: Counter = Counter()
    waits = []  # seconds from the trip's last own report to the recovered STOPPED_AT
    by_pos: Counter = Counter()
    near_waits = []  # seconds from the trip's last own report to the first report near the stop
    for key, reports in by_trip.items():
        last = static["last_seq"].get(key[0])
        if last is None:
            c["trip not in the static zip"] += 1
            continue
        if any(seq == last and st == "STOPPED_AT" for _, seq, st in reports):
            c["STOPPED_AT the last stop"] += 1
            continue
        kind = ("heading to the last stop, never STOPPED_AT it"
                if any(seq == last for _, seq, _ in reports) else "reports end before the last stop")
        c[kind] += 1
        vehicles = extra["trip_vehicles"].get(key)
        last_stop = static["stop_of"].get((key[0], last))
        if not vehicles or last_stop is None:
            found[f"{kind}: no vehicle id"] += 1
            continue
        vid = vehicles.most_common(1)[0][0]
        t_end = reports[-1][0]
        hit = near = None
        coords = static["coords"].get(last_stop)
        for ts, other, _, stop_id, st, p in extra["by_vehicle"][vid]:
            if ts < t_end - 600:
                continue
            if ts > t_end + 1800:
                break
            if hit is None and stop_id == last_stop and st == "STOPPED_AT" and ts >= t_end - 120:
                hit = (ts, other)
            if near is None and coords and p and metres(p, coords) <= NEAR_M:
                near = (ts, other)
        if near is None:
            by_pos[f"{kind}: no report within {NEAR_M} m"] += 1
        else:
            by_pos[f"{kind}: within {NEAR_M} m, "
                   + ("same trip_id" if near[1] == key else "under another trip_id")] += 1
            near_waits.append(near[0] - t_end)
        if hit is None:
            found[f"{kind}: not found"] += 1
        elif hit[1] == key:
            found[f"{kind}: found, same trip_id"] += 1
        else:
            found[f"{kind}: found under another trip_id"] += 1
            waits.append(hit[0] - t_end)
    return c, found, waits, by_pos, near_waits


def cross_check(drops, by_trip, first_seq, static=None, extra=None):
    """For each stop that dropped out of TripUpdates, bracket its arrival with VehiclePositions."""
    c: Counter = Counter()
    diffs = []
    groups: dict = defaultdict(Counter)  # breakdown label -> Counter(size bucket)
    strict: dict = defaultdict(Counter)  # same, strict brackets only, arrival vs departure
    floor_cache: dict = {}
    where: Counter = Counter()  # TU-early cases: bus position at VP's 'not yet' report
    for key, s, pred, h_last, tu_dep in drops:
        reports = by_trip.get(key)
        if not reports:
            c["no vehicle reports for the trip"] += 1
            continue
        b = bracket(reports, s, robust=False)
        if b is None:
            c["vehicle never shown at or past the stop"] += 1
            continue
        lo, hi, how, started_past = b
        c["bracketed"] += 1
        c["STOPPED_AT seen for the stop"] += how == "STOPPED_AT the stop"
        d = pred - hi
        diffs.append(d)
        inside = (lo if lo is not None else hi) - TOLERANCE_S <= pred <= hi + TOLERANCE_S
        c["TU inside the VP bracket"] += inside
        bucket = size(d)
        groups["all"][bucket] += 1
        groups["first stop of the trip" if s == first_seq.get(key) else "later stops"][bucket] += 1
        groups["trip's first VP report already at/past the stop" if started_past
               else "VP saw the bus before the stop"][bucket] += 1
        groups[f"hi from: {how}"][bucket] += 1
        groups["TU time still ahead when the stop dropped" if pred > h_last
               else "TU time already past when the stop dropped"][bucket] += 1
        rb = bracket(reports, s, robust=True)
        groups["robust hi (seen before stop, never goes back)" if rb else "robust: never reached"][
            size(pred - rb[1]) if rb else "n/a"] += 1
        if not rb:
            continue
        if key not in floor_cache:
            floor_cache[key] = floors(reports)
        kind = "stopped" if rb[2] == "STOPPED_AT the stop" else "passed without stopping"
        if kind == "stopped" and static is not None:
            tp = static["timepoints"].get((key[0], s))
            kind += ", timepoint" if tp else ", not a timepoint" if tp is not None else ", ?"
        strict[f"vs VP arrival   | {kind}"][size(pred - rb[1])] += 1
        dep = departed(reports, floor_cache[key], s, rb[1])
        if dep is not None:
            strict[f"vs VP departure | {kind}"][size(pred - dep)] += 1
        if kind == "passed without stopping":
            # Inside the whole bracket (last report before the stop .. first report past it)?
            lo = rb[0] if rb[0] is not None else rb[1]
            inside = lo - TOLERANCE_S <= pred <= rb[1] + TOLERANCE_S
            strict["passed without stopping: TU inside bracket"][
                "within 60s" if inside else size(pred - rb[1])] += 1
            if not inside and pred < lo - TOLERANCE_S and static is not None and rb[0] is not None:
                where[where_was_the_bus(static, extra["pos"], key, s, rb[0])] += 1
        if tu_dep is not None and tu_dep != pred:
            c["TU departure differs from TU arrival"] += 1
            strict[f"TU departure FIELD vs VP arrival   | {kind}"][size(tu_dep - rb[1])] += 1
            if dep is not None:
                strict[f"TU departure FIELD vs VP departure | {kind}"][size(tu_dep - dep)] += 1
    return c, diffs, groups, strict, where


def main() -> None:
    tu_dir, vp_dir = sys.argv[1:3]
    print(f"TripUpdates manifest ({tu_dir})")
    print("\n".join(manifest(tu_dir)))
    print(f"VehiclePositions manifest ({vp_dir})")
    print("\n".join(manifest(vp_dir)))

    (snaps, c, trip_rel, stop_rel, drop_lead, end_lead, drops, tu_keys,
     first_seq) = profile_trip_updates(tu_dir)
    print("\nTripUpdates snapshots: " + ", ".join(f"{k} {n}" for k, n in snaps.items()))
    n, s = c["trip updates"], c["stop updates"]
    print(f"  trip updates {n}: " + ", ".join(
        f"{k} {pct(c[k], n)}" for k in ("with vehicle id", "with timestamp", "with start_date",
                                        "with start_time", "with no stop updates")))
    print("  trip schedule_relationship: " + ", ".join(f"{k} {v}" for k, v in trip_rel.items()))
    if c["same trip twice in one snapshot"]:
        print(f"  same trip twice in one snapshot: {c['same trip twice in one snapshot']}")
    print(f"  stop updates {s}: " + ", ".join(
        f"{k} {pct(c[k], s)}" for k in ("arrival time", "arrival delay only", "no arrival",
                                        "no stop_sequence", "uncertainty set")))
    print("  stop schedule_relationship: " + ", ".join(f"{k} {v}" for k, v in stop_rel.items()))
    print(f"  stop times already past (>{PAST_S}s before the snapshot): "
          f"{pct(c['time already past'], c['with a time'])} of stop updates with a time")
    print(f"  past stops seen again in the next snapshot: {c['past stop seen again']}, "
          f"of which their time changed: {c['past stop time changed']}")
    print(f"  stops that dropped out while the trip continued: {len(drop_lead)}")
    print(f"    their last time minus the last snapshot that had them (s): {dist(drop_lead)}")
    print(f"  trips that vanished: {len(end_lead)}")
    print(f"    last stop's time minus the last snapshot that had the trip (s): {dist(end_lead)}")
    print(f"  comparisons skipped across gaps over {GAP_S}s: {c['gaps skipped']}")

    snaps, v, status, lag, by_trip, extra = profile_vehicles(vp_dir)
    print("\nVehiclePositions snapshots: " + ", ".join(f"{k} {n}" for k, n in snaps.items()))
    n = v["vehicles"]
    print(f"  vehicle positions {n}: " + ", ".join(
        f"{k} {pct(v[k], n)}" for k in ("with trip_id", "with current_stop_sequence", "with stop_id",
                                        "with current_status", "with timestamp")))
    print("  current_status: " + ", ".join(f"{k} {pct(x, n)}" for k, x in status.most_common()))
    print(f"  snapshot time minus vehicle timestamp (s): {dist(lag)}")
    print(f"  reports not newer than the vehicle's last one: {v['report not newer than the last one']}")
    print(f"  stop_sequence went backwards within a trip: {v['stop_sequence went backwards']}")
    vp_keys = set(by_trip)
    print(f"  trips in TripUpdates only {len(tu_keys - vp_keys)}, in VehiclePositions only "
          f"{len(vp_keys - tu_keys)}, in both {len(tu_keys & vp_keys)}")

    print(f"  vehicle timestamps of 0, skipped: {v['timestamp 0 (report skipped)']}")

    print(f"  stop updates with a departure time: {pct(c['departure time'], s)}; of those with "
          f"both times, arrival == departure: {pct(c['arrival == departure'], c['both times'])}")

    static = load_static(sys.argv[3]) if len(sys.argv) > 3 else None
    if static is not None:
        lc, found, waits, by_pos, near_waits = last_stop_coverage(by_trip, static, extra)
        print("  trips by how their vehicle reports end: "
              + ", ".join(f"{k} {pct(n, sum(lc.values()))}" for k, n in lc.most_common()))
        print("  following the vehicle to the last stop_id (STOPPED_AT within 30 min):")
        for k, n in sorted(found.items()):
            print(f"    {k}: {n}")
        print(f"    seconds from the trip's last own report to that STOPPED_AT: {dist(waits)}")
        print(f"  following the vehicle by GPS: first report within {NEAR_M} m of the last stop"
              " (from 10 min before the trip's last own report to 30 min after):")
        for k, n in sorted(by_pos.items()):
            print(f"    {k}: {n}")
        print(f"    seconds from the trip's last own report to that report: {dist(near_waits)}")
    x, diffs, groups, strict, where = cross_check(drops, by_trip, first_seq, static, extra)
    print("\nDropped stops checked against VehiclePositions")
    print("  " + ", ".join(f"{k} {n}" for k, n in x.items()))
    print(f"  TU last arrival minus first VP report at or past the stop (s): {dist(diffs)}")
    print(f"  TU inside the VP bracket +/-{TOLERANCE_S}s: "
          f"{pct(x['TU inside the VP bracket'], x['bracketed'])}")
    buckets = ["TU earlier by >60s", "within 60s", "TU later by 1-5 min", "TU later by >5 min"]
    print(f"\n  {'TU arrival minus VP hi, by group':52}" + "".join(f"{b:>21}" for b in buckets))
    for label, g in groups.items():
        total = sum(g.values())
        print(f"  {label[:44]:44} n={total:<6}" + "".join(f"{pct(g[b], total):>21}" for b in buckets))
    print(f"\n  {'Strict brackets only: TU time minus VP time':72}"
          + "".join(f"{b:>21}" for b in buckets))
    for label in sorted(strict):
        g = strict[label]
        total = sum(g.values())
        print(f"  {label[:64]:64} n={total:<6}" + "".join(f"{pct(g[b], total):>21}" for b in buckets))
    if where:
        print()
        print("  Passed without stopping, TU >60s before VP's last 'not yet' report."
              " Where was the bus at that report?")
        for k, n in where.most_common():
            print(f"    {k}: {pct(n, sum(where.values()))} ({n})")


if __name__ == "__main__":
    main()
