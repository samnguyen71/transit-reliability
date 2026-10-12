import gzip
import os
import time
import unittest
from unittest import mock

from feedlogger import peek
from tests.helpers import (
    FEED_TIME,
    alert_entity,
    feed_message,
    gtfs_zip,
    stop_update,
    trip_update_entity,
    vehicle_entity,
)


class DescribeTest(unittest.TestCase):
    def test_realtime_feed(self):
        data = feed_message(
            vehicle_entity("v1", trip_id="t1", stop_sequence=3, status=1, timestamp=FEED_TIME - 5),
            vehicle_entity("v2", trip_id="t2", stop_sequence=8, status=2, timestamp=FEED_TIME - 9),
            trip_update_entity(
                "tu1",
                "t1",
                (stop_update(2, time=FEED_TIME - 300), stop_update(3, time=FEED_TIME + 60)),
            ),
        )
        text = peek.describe(data, interval=30, fetched_at=FEED_TIME + 20)
        self.assertIn("GTFS-realtime 2.0, full dataset, 3 entities", text)
        self.assertIn("20 s before this fetch", text)
        self.assertIn("STOPPED_AT 50%", text)
        self.assertIn("already passed stay in the feed", text)
        self.assertNotIn("No vehicle positions", text)
        self.assertIn("per 30 days", text)
        self.assertIn("if every snapshot were this size", text)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset, which Windows lacks")
    def test_feed_timestamp_shows_local_time_and_utc(self):
        data = feed_message(vehicle_entity("v1"))  # timestamped 2026-10-03 04:00:00 UTC
        self.addCleanup(time.tzset)  # runs after the TZ change below is undone
        with mock.patch.dict(os.environ, {"TZ": "America/Toronto"}):
            time.tzset()
            toronto = peek.describe(data, fetched_at=FEED_TIME + 2)
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            utc = peek.describe(data, fetched_at=FEED_TIME + 2)
        self.assertIn(
            "Feed timestamp: 2026-10-03 00:00:00 local time (04:00:00 UTC), 2 s before this fetch",
            toronto,
        )
        self.assertIn("Feed timestamp: 2026-10-03 04:00:00 UTC, 2 s before this fetch", utc)

    def test_reads_saved_gzipped_snapshots(self):
        data = gzip.compress(feed_message(vehicle_entity("v1")))
        self.assertIn("1 entity", peek.describe(data))
        with self.assertRaises(ValueError):
            peek.describe(b"\x1f\x8b" + b"not really gzip")

    def test_empty_and_stale_feeds_get_a_note(self):
        text = peek.describe(feed_message(), fetched_at=FEED_TIME + 3600)
        self.assertIn("The feed is empty right now", text)
        self.assertIn("may be frozen", text)

    def test_trip_updates_that_drop_passed_stops_and_have_no_vehicles(self):
        data = feed_message(trip_update_entity("tu1", "t1", (stop_update(5, time=FEED_TIME + 90),)))
        text = peek.describe(data, fetched_at=FEED_TIME)
        self.assertIn("Stops drop out of the feed", text)
        self.assertIn("No vehicle positions in this feed", text)
        self.assertIn("VehiclePositions", text)

    def test_alerts_feed(self):
        # Alerts can go hours without changing, so an old timestamp isn't a problem.
        data = feed_message(alert_entity("a1"), alert_entity("a2"), timestamp=FEED_TIME - 7200)
        text = peek.describe(data, fetched_at=FEED_TIME)
        self.assertIn("This is a service alerts feed", text)
        self.assertNotIn("may be frozen", text)

    def test_static_zip(self):
        text = peek.describe(gtfs_zip())
        self.assertIn("Static GTFS zip", text)
        self.assertIn("stop_times.txt (3 rows)", text)  # last row has no newline
        self.assertIn("routes.txt (1 row)", text)
        self.assertIn("Service dates: 2026-09-07 to 2026-12-25", text)

    def test_not_a_feed(self):
        with self.assertRaises(ValueError):
            peek.describe(b"<html>Bad Gateway</html>")


if __name__ == "__main__":
    unittest.main()
