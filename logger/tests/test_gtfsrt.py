import unittest

from feedlogger import gtfsrt
from tests.helpers import (
    FEED_TIME,
    alert_entity,
    feed_message,
    gtfs_zip,
    stop_update,
    trip_update_entity,
    vehicle_entity,
)


class ReadHeaderTest(unittest.TestCase):
    def test_reads_version_timestamp_and_entity_count(self):
        data = feed_message(vehicle_entity("a"), vehicle_entity("b"), alert_entity("c"))
        header = gtfsrt.read_header(data)
        self.assertEqual(header.version, "2.0")
        self.assertEqual(header.timestamp, FEED_TIME)
        self.assertEqual(header.entity_count, 3)
        self.assertEqual(header.incrementality, 0)

    def test_empty_feed_is_valid(self):
        header = gtfsrt.read_header(feed_message())
        self.assertEqual(header.entity_count, 0)

    def test_differential_and_missing_timestamp(self):
        header = gtfsrt.read_header(feed_message(timestamp=None, incrementality=1))
        self.assertIsNone(header.timestamp)
        self.assertEqual(header.incrementality, 1)

    def test_rejects_things_that_are_not_feeds(self):
        not_feeds = {
            "empty body": b"",
            "html error page": b"<html><body>502 Bad Gateway</body></html>",
            "json": b'{"error": "invalid api key"}',
            "plain text": b"Unauthorized\n",
            "zip": gtfs_zip(),
            "truncated feed": feed_message(vehicle_entity("a"))[:-3],
        }
        for label, data in not_feeds.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    gtfsrt.read_header(data)

    def test_signed_handles_negative_int32(self):
        self.assertEqual(gtfsrt.signed((1 << 64) - 90), -90)
        self.assertEqual(gtfsrt.signed(90), 90)


class SummarizeTest(unittest.TestCase):
    def test_vehicle_positions(self):
        data = feed_message(
            vehicle_entity(
                "v1", trip_id="t1", stop_sequence=4, status=1, timestamp=FEED_TIME - 10, stop_id="s"
            ),
            vehicle_entity("v2", trip_id="t2", status=0, timestamp=FEED_TIME - 30),
            vehicle_entity("v3", stop_sequence=7, status=2),
            vehicle_entity("v4"),
        )
        s = gtfsrt.summarize(data)
        self.assertEqual(s.vehicles, 4)
        self.assertEqual(s.kinds["vehicle"], 4)
        self.assertEqual(s.vehicle_trip_id, 2)
        self.assertEqual(s.vehicle_stop_sequence, 2)
        self.assertEqual(s.vehicle_stop_id, 1)
        self.assertEqual(
            dict(s.vehicle_status),
            {"STOPPED_AT": 1, "INCOMING_AT": 1, "IN_TRANSIT_TO": 1, "not given": 1},
        )
        self.assertEqual(s.vehicle_timestamp, 2)
        self.assertEqual(sorted(s.vehicle_ages), [10, 30])

    def test_trip_updates(self):
        data = feed_message(
            trip_update_entity(
                "e1",
                "t1",
                (
                    stop_update(1, time=FEED_TIME - 600),  # already passed
                    stop_update(2, time=FEED_TIME + 120, delay=-90),  # early
                    stop_update(3, delay=45),  # delay only
                    stop_update(4, skipped=True),
                ),
            ),
            trip_update_entity("e2", "t2", relationship=3),  # canceled
            alert_entity("a1"),
        )
        s = gtfsrt.summarize(data)
        self.assertEqual(s.trip_updates, 2)
        self.assertEqual(s.kinds["trip_update"], 2)
        self.assertEqual(s.kinds["alert"], 1)
        self.assertEqual(s.trip_update_trip_id, 2)
        self.assertEqual(s.trip_relationship["canceled"], 1)
        self.assertEqual(s.stop_updates, 4)
        self.assertEqual(s.stop_updates_with_time, 2)
        self.assertEqual(s.stop_updates_delay_only, 1)
        self.assertEqual(s.stop_updates_past, 1)
        self.assertEqual(s.stop_updates_skipped, 1)

    def test_negative_delay_survives_the_round_trip(self):
        update = stop_update(1, delay=-90)
        event = next(v for n, w, v in gtfsrt.iter_fields(update) if n == 2)
        delay = next(v for n, w, v in gtfsrt.iter_fields(event) if n == 1)
        self.assertEqual(gtfsrt.signed(delay), -90)


if __name__ == "__main__":
    unittest.main()
