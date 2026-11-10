"""Which copy of each duplicated Overnight trip does DRT's realtime use?

Usage:
    python night_copies.py STATIC_ZIP SNAPSHOT_DIR [SNAPSHOT_DIR ...]

STATIC_ZIP is the static version whose trip_ids the snapshots use. Each
SNAPSHOT_DIR is a daily folder of .pb.gz files from trip_updates or
vehicle_positions, e.g. data/raw/trip_updates/2026-11-0*
"""

import csv
import gzip
import io
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from google.transit import gtfs_realtime_pb2


def night_pairs(zip_path: str) -> list[list[tuple[str, str, str]]]:
    """Overnight trips that have a twin exactly 24 h later on the same route,
    as [[(first_departure, trip_id, route), ...], ...], earlier copy first."""
    zf = zipfile.ZipFile(zip_path)

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(zf.open(name), "utf-8-sig"))

    routes = {r["route_id"]: r["route_short_name"] for r in rows("routes.txt")}
    trips = {t["trip_id"]: t for t in rows("trips.txt") if t["service_id"] == "Overnight"}
    first: dict[str, tuple[int, str]] = {}
    for st in rows("stop_times.txt"):
        tid = st["trip_id"]
        if tid in trips:
            seq = int(st["stop_sequence"])
            if tid not in first or seq < first[tid][0]:
                first[tid] = (seq, st["departure_time"])

    groups = defaultdict(list)
    for tid, (_, dep) in first.items():
        h, m, s = map(int, dep.split(":"))
        route = routes[trips[tid]["route_id"]]
        groups[(route, h % 24, m, s)].append((dep, tid, route))
    return sorted(sorted(v) for v in groups.values() if len(v) > 1)


def main() -> None:
    zip_path, *dirs = sys.argv[1:]
    pairs = night_pairs(zip_path)
    wanted = {tid for pair in pairs for _, tid, _ in pair}

    # trip_id -> (start_date, start_time) -> [snapshot names]
    seen: dict[str, dict[tuple[str, str], list[str]]] = defaultdict(lambda: defaultdict(list))
    files = bad = 0
    for d in dirs:
        for path in sorted(Path(d).glob("*.pb.gz")):
            feed = gtfs_realtime_pb2.FeedMessage()
            try:
                feed.ParseFromString(gzip.decompress(path.read_bytes()))
            except Exception:
                bad += 1
                continue
            files += 1
            for e in feed.entity:
                if e.HasField("trip_update"):
                    trip = e.trip_update.trip
                elif e.HasField("vehicle") and e.vehicle.HasField("trip"):
                    trip = e.vehicle.trip
                else:
                    continue
                if trip.trip_id in wanted:
                    key = (trip.start_date or "-", trip.start_time or "-")
                    seen[trip.trip_id][key].append(f"{path.parent.name}/{path.name.removesuffix('.pb.gz')}")

    print(f"{files} snapshots read, {bad} unreadable, {len(pairs)} duplicated pairs\n")
    for pair in pairs:
        print(f"route {pair[0][2]}")
        for dep, tid, _ in pair:
            if not seen[tid]:
                print(f"  {dep}  {tid}  never seen")
            for (start_date, start_time), names in sorted(seen[tid].items()):
                print(
                    f"  {dep}  {tid}  start_date={start_date} start_time={start_time}"
                    f"  {len(names)} snapshots, {names[0]} .. {names[-1]}"
                )
        print()


if __name__ == "__main__":
    main()
