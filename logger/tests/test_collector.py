import gzip
import json
import os
import socket
import threading
import time
import unittest

from feedlogger import collector
from feedlogger.collector import FeedWorker, backoff_delay
from feedlogger.fmt import utc_day
from tests.helpers import (
    FEED_TIME,
    ServerTestCase,
    feed_message,
    gtfs_zip,
    make_feed,
    vehicle_entity,
)

NOW = FEED_TIME + 12.0  # the clock the worker sees: 12 s after the feed's timestamp


class CollectorTest(ServerTestCase):
    def worker(self, feed=None, *, config=None, clock=None):
        feed = feed or make_feed(self.server.url("/vp"))
        config = config or self.make_config(feed)
        return FeedWorker(feed, config, clock=clock or (lambda: NOW))

    def manifest(self, feed_name="vehicles"):
        path = self.data_dir / feed_name / utc_day(NOW) / "_manifest.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def day_files(self, feed_name="vehicles"):
        folder = self.data_dir / feed_name / utc_day(NOW)
        return sorted(p.name for p in folder.iterdir() if p.name != "_manifest.jsonl")

    def test_saves_a_valid_snapshot_and_records_it(self):
        body = feed_message(vehicle_entity("v1"), vehicle_entity("v2"))
        self.server.serve("/vp", (200, {}, body))
        result = self.worker().poll_once()

        self.assertEqual(result.status, "saved")
        self.assertTrue(result.healthy)
        files = self.day_files()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("Z.pb.gz"))
        stored = self.data_dir / "vehicles" / utc_day(NOW) / files[0]
        self.assertEqual(gzip.decompress(stored.read_bytes()), body)  # byte for byte

        (record,) = self.manifest()
        self.assertEqual(record["status"], "saved")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["bytes"], len(body))
        self.assertEqual(record["entities"], 2)
        self.assertEqual(record["feed_timestamp"], FEED_TIME)
        self.assertEqual(record["feed_age_s"], 12)
        self.assertEqual(record["file"], files[0])
        # The server saw who we are, and nothing is left in the temp folder.
        _, headers = self.server.requests[0]
        self.assertEqual(headers["User-Agent"], "feedlogger-tests")
        self.assertEqual(list((self.data_dir / "vehicles" / "_tmp").iterdir()), [])

    def test_identical_snapshot_is_not_stored_twice(self):
        body = feed_message(vehicle_entity("v1"))
        self.server.serve("/vp", (200, {}, body))
        worker = self.worker()
        first, second = worker.poll_once(), worker.poll_once()
        self.assertEqual((first.status, second.status), ("saved", "duplicate"))
        self.assertTrue(second.healthy)
        self.assertEqual(len(self.day_files()), 1)
        self.assertEqual([r["status"] for r in self.manifest()], ["saved", "duplicate"])

    def test_changed_snapshot_in_the_same_second_gets_its_own_name(self):
        self.server.serve(
            "/vp",
            (200, {}, feed_message(vehicle_entity("v1"))),
            (200, {}, feed_message(vehicle_entity("v2"))),
        )
        worker = self.worker()
        worker.poll_once()
        worker.poll_once()
        files = self.day_files()  # sorted by name, which must also be oldest first
        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].endswith("Z.pb.gz"), files)
        self.assertTrue(files[1].endswith("Z_1.pb.gz"), files)

    def test_sends_etag_back_and_accepts_304(self):
        body = feed_message(vehicle_entity("v1"))

        def respond(headers):
            if headers.get("If-None-Match") == '"v1"':
                return 304, {}, b""
            return 200, {"ETag": '"v1"'}, body

        self.server.serve("/vp", respond)
        worker = self.worker()
        self.assertEqual(worker.poll_once().status, "saved")
        result = worker.poll_once()
        self.assertEqual(result.status, "not_modified")
        self.assertTrue(result.healthy)

    def test_html_error_page_is_kept_aside_and_unhealthy(self):
        page = b"<html><body>Service temporarily unavailable</body></html>"
        self.server.serve("/vp", (200, {"Content-Type": "text/html"}, page))
        worker = self.worker()
        result = worker.poll_once()
        self.assertEqual(result.status, "invalid")
        self.assertFalse(result.healthy)
        self.assertIn("not a GTFS-realtime feed", result.message)
        self.assertIn("Service temporarily unavailable", result.message)
        self.assertTrue(self.day_files()[0].endswith(".invalid.gz"))
        # The same page again isn't stored again, and still isn't healthy.
        again = worker.poll_once()
        self.assertEqual(again.status, "invalid")
        self.assertFalse(again.healthy)
        self.assertEqual(len(self.day_files()), 1)

    def test_http_errors_explain_themselves(self):
        self.server.serve("/vp", (403, {}, b'{"message": "Invalid API key"}'))
        result = self.worker().poll_once()
        self.assertEqual(result.status, "http_error")
        self.assertIn("HTTP 403", result.message)
        self.assertIn("API key", result.message)
        self.assertIn("Invalid API key", result.message)  # what the server said
        self.assertEqual(self.day_files(), [])

    def test_rate_limit_reads_retry_after(self):
        self.server.serve("/vp", (429, {"Retry-After": "120"}, b"slow down"))
        result = self.worker().poll_once()
        self.assertEqual(result.status, "http_error")
        self.assertEqual(result.retry_after, 120)

    def test_network_error_never_leaks_the_api_key(self):
        with socket.socket() as s:  # find a port with nothing listening
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        feed = make_feed(f"http://127.0.0.1:{port}/vp?api_key=topsecret123")
        config = self.make_config(feed, secrets={"topsecret123"})
        result = self.worker(feed, config=config).poll_once()
        self.assertEqual(result.status, "network_error")
        self.assertNotIn("topsecret123", result.message)
        self.assertNotIn("topsecret123", json.dumps(self.manifest()))

    def test_oversized_response_is_refused(self):
        big = feed_message(*[vehicle_entity(f"v{i}") for i in range(50)])
        self.server.serve("/vp", (200, {}, big))
        feed = make_feed(self.server.url("/vp"), max_bytes=100)
        result = self.worker(feed).poll_once()
        self.assertEqual(result.status, "too_large")
        self.assertEqual(self.day_files(), [])

    def test_low_disk_skips_the_fetch(self):
        feed = make_feed(self.server.url("/vp"))
        config = self.make_config(feed, min_free_gb=10**9)  # more than any disk has
        result = self.worker(feed, config=config).poll_once()
        self.assertEqual(result.status, "disk_low")
        self.assertFalse(result.healthy)
        self.assertEqual(self.server.requests, [])

    def test_static_zip_is_kept_once_even_across_restarts(self):
        self.server.serve("/gtfs.zip", (200, {}, gtfs_zip()))
        feed = make_feed(self.server.url("/gtfs.zip"), name="static_gtfs", kind="static")
        self.assertEqual(self.worker(feed).poll_once().status, "saved")
        restarted = self.worker(feed)  # a new worker reads _state.json
        self.assertEqual(restarted.poll_once().status, "duplicate")
        files = self.day_files("static_gtfs")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith("Z.zip"))

    def test_zip_without_gtfs_files_is_invalid(self):
        self.server.serve("/gtfs.zip", (200, {}, gtfs_zip(leave_out=("stop_times.txt",))))
        feed = make_feed(self.server.url("/gtfs.zip"), name="static_gtfs", kind="static")
        result = self.worker(feed).poll_once()
        self.assertEqual(result.status, "invalid")
        self.assertIn("stop_times.txt", result.message)

    def test_pings_healthchecks_only_when_healthy_and_at_most_once_a_minute(self):
        self.server.serve("/vp", (200, {}, feed_message(vehicle_entity("v1"))))
        self.server.serve("/ping", (200, {}, b"OK"))
        feed = make_feed(self.server.url("/vp"), healthcheck_url=self.server.url("/ping"))
        worker = self.worker(feed)
        worker.handle_result(worker.poll_once())
        worker.handle_result(worker.poll_once())  # within a minute: no second ping
        self.assertEqual(self.server.hits("/ping"), 1)

        worker._last_ping = None  # pretend a minute went by...
        self.server.serve("/vp", (500, {}, b"oops"))
        with self.assertLogs("feedlogger", level="WARNING") as logs:
            worker.handle_result(worker.poll_once())  # ...but this poll failed
        self.assertEqual(self.server.hits("/ping"), 1)
        self.assertIn("HTTP 500", logs.output[0])

    def test_run_stops_promptly(self):
        self.server.serve("/vp", (200, {}, feed_message()))
        worker = self.worker(clock=time.time)
        stop = threading.Event()
        thread = threading.Thread(target=worker.run, args=(stop,))
        thread.start()
        time.sleep(0.2)
        stop.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_stale_temp_files_are_cleared_on_start_but_fresh_ones_kept(self):
        tmp_dir = self.data_dir / "vehicles" / "_tmp"
        tmp_dir.mkdir(parents=True)
        stale = tmp_dir / "20261001T000000Z-111.part"
        stale.write_bytes(b"half a download from a crash")
        an_hour_ago = time.time() - 3600
        os.utime(stale, (an_hour_ago, an_hour_ago))
        fresh = tmp_dir / "20261001T000000Z-222.part"
        fresh.write_bytes(b"another copy's download in progress")
        self.worker()
        self.assertEqual(list(tmp_dir.iterdir()), [fresh])


class BackoffTest(unittest.TestCase):
    def test_doubles_up_to_five_minutes(self):
        delays = [backoff_delay(30, n) for n in range(1, 8)]
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300, 300])

    def test_static_feeds_retry_sooner_than_their_interval(self):
        self.assertEqual(backoff_delay(6 * 3600, 1), 60)
        self.assertEqual(backoff_delay(6 * 3600, 10), 300)

    def test_retry_after_can_lengthen_the_wait_but_not_shorten_it(self):
        self.assertEqual(backoff_delay(30, 1, retry_after=120), 120)
        self.assertEqual(backoff_delay(30, 1, retry_after=0), 30)
        self.assertEqual(backoff_delay(30, 4, retry_after=60), 240)
        self.assertEqual(backoff_delay(30, 1, retry_after=10**6), collector.MAX_RETRY_AFTER_SECONDS)

    def test_never_overflows(self):
        self.assertEqual(backoff_delay(30, 10**6), 300)


if __name__ == "__main__":
    unittest.main()
