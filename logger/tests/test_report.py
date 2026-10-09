import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from feedlogger.report import collect_stats, format_report

START = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
MB = 1024 * 1024
GB = 1024 * MB


class ReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)

    def add_fetches(self, feed, start, count, every_s=30, status="saved", file_mb=1.0, error=None):
        for i in range(count):
            when = start + timedelta(seconds=i * every_s)
            day = self.data_dir / feed / when.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            name = None
            if status == "saved":
                name = when.strftime("%Y%m%dT%H%M%SZ") + ".pb.gz"
                (day / name).write_bytes(b"x" * int(file_mb * MB))
            record = {
                "fetched_at": when.isoformat().replace("+00:00", "Z"),
                "status": status,
                "file": name,
                "feed_age_s": 15 if status == "saved" else None,
                "error": error,
            }
            with (day / "_manifest.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")

    def test_rates_gaps_and_disk_projection(self):
        # Two hours at one fetch every 30 s, with a 20-minute outage in the middle.
        self.add_fetches("vp", START, 120, file_mb=0.1)
        self.add_fetches("vp", START + timedelta(minutes=60), 40, status="network_error",
                         error="ConnectionError: refused")
        self.add_fetches("vp", START + timedelta(minutes=80), 81, file_mb=0.1)
        now = START + timedelta(minutes=121)

        (stats,) = collect_stats(self.data_dir, days=7, now=now)
        self.assertEqual(stats.attempts, 241)
        self.assertEqual(stats.good, 201)
        self.assertEqual(stats.failures["network_error"], 40)
        self.assertEqual(stats.interval, 30)
        self.assertEqual(len(stats.outages), 1)
        self.assertAlmostEqual(stats.outages[0][1], 20.5 * 60)
        # 201 files of 0.1 MB over exactly 2 hours = 20.1 MB per 2 h.
        self.assertAlmostEqual(stats.bytes_per_day / MB, 20.1 * 12, places=0)

        text = format_report(
            [stats],
            data_dir=self.data_dir,
            days=7,
            free_bytes=20 * GB,
            total_bytes=30 * GB,
            reserve_bytes=2 * GB,
            now=now,
        )
        self.assertIn("40 network_error", text)
        self.assertIn("1 gap(s)", text)
        self.assertIn("fills up in about 76 days", text)  # 18 GB / 241 MB a day
        self.assertIn("less than five months", text)
        self.assertIn("less than a day of data", text)
        self.assertNotIn("FAILING", text)

    def test_flags_a_feed_that_is_down_now(self):
        self.add_fetches("tu", START, 10)
        self.add_fetches("tu", START + timedelta(minutes=5), 30, status="http_error",
                         error="HTTP 403 Forbidden")
        now = START + timedelta(minutes=21)
        text = format_report(
            collect_stats(self.data_dir, days=7, now=now),
            data_dir=self.data_dir,
            days=7,
            free_bytes=20 * GB,
            total_bytes=30 * GB,
            reserve_bytes=2 * GB,
            now=now,
        )
        self.assertIn("tu: FAILING", text)
        self.assertIn("HTTP 403 Forbidden", text)

    def test_no_data_yet(self):
        (self.data_dir / "vp").mkdir()
        text = format_report(
            collect_stats(self.data_dir, now=START),
            data_dir=self.data_dir,
            days=7,
            free_bytes=GB,
            total_bytes=GB,
            reserve_bytes=0,
            now=START,
        )
        self.assertIn("No fetches recorded yet", text)


if __name__ == "__main__":
    unittest.main()
