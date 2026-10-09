import gzip
import unittest

from feedlogger import peek
from tests.helpers import (
    FEED_TIME,
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
        self.assertIn("per 30 days", text)

    def test_reads_saved_gzipped_snapshots(self):
        data = gzip.compress(feed_message(vehicle_entity("v1")))
        self.assertIn("1 entity", peek.describe(data))
        with self.assertRaises(ValueError):
            peek.describe(b"\x1f\x8b" + b"not really gzip")

    def test_empty_and_stale_feeds_get_a_note(self):
        text = peek.describe(feed_message(), fetched_at=FEED_TIME + 3600)
        self.assertIn("The feed is empty right now", text)
        self.assertIn("may be frozen", text)

    def test_feed_that_drops_passed_stops(self):
        data = feed_message(trip_update_entity("tu1", "t1", (stop_update(5, time=FEED_TIME + 90),)))
        self.assertIn("Stops drop out of the feed", peek.describe(data, fetched_at=FEED_TIME))

    def test_static_zip(self):
        text = peek.describe(gtfs_zip())
        self.assertIn("Static GTFS zip", text)
        self.assertIn("stop_times.txt (3 rows)", text)  # last row has no newline
        self.assertIn("Service dates: 2026-09-07 to 2026-12-25", text)

    def test_not_a_feed(self):
        with self.assertRaises(ValueError):
            peek.describe(b"<html>Bad Gateway</html>")


if __name__ == "__main__":
    unittest.main()
