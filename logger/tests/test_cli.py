import contextlib
import io
import logging
import textwrap
import unittest

from feedlogger import cli
from tests.helpers import ServerTestCase, feed_message, gtfs_zip, vehicle_entity

SECRET = "sekrit-api-key-123"


class CliTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        # `run` sets up logging; put it back afterwards so other tests aren't affected.
        root, ours = logging.getLogger(), logging.getLogger("feedlogger")
        saved = (root.handlers[:], root.level, ours.level)

        def restore():
            root.handlers[:], root.level, ours.level = saved[0], saved[1], saved[2]

        self.addCleanup(restore)
        self.server.serve("/vp", (200, {}, feed_message(vehicle_entity("v1", trip_id="t1"))))
        self.server.serve("/gtfs.zip", (200, {}, gtfs_zip()))
        self.config = self.data_dir / "config.toml"
        self.config.write_text(
            textwrap.dedent(
                f"""
                data_dir = "{(self.data_dir / "raw").as_posix()}"
                min_free_gb = 0

                [[feeds]]
                name = "vp"
                url = "{self.server.url("/vp")}?api_key=${{API_KEY}}"

                [[feeds]]
                name = "static_gtfs"
                kind = "static"
                url = "{self.server.url("/gtfs.zip")}"
                """
            ),
            encoding="utf-8",
        )
        self.env_file = self.data_dir / ".env"
        self.env_file.write_text(f"API_KEY={SECRET}\n", encoding="utf-8")

    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main([*args, "--config", str(self.config), "--env-file", str(self.env_file)])
        return code, out.getvalue()

    def test_run_once_then_report(self):
        code, out = self.cli("run", "--once")
        self.assertEqual(code, 0, out)
        self.assertIn("vp: saved", out)
        self.assertIn("static_gtfs: saved", out)
        self.assertIn("All feeds OK.", out)
        self.assertIn(f"api_key={SECRET}", self.server.requests[0][0])  # the key was sent...
        self.assertNotIn(SECRET, out)  # ...but never shown

        code, out = self.cli("report")
        self.assertEqual(code, 0, out)
        self.assertIn("vp", out)
        self.assertIn("static_gtfs", out)

    def test_run_once_reports_failures(self):
        self.server.serve("/vp", (401, {}, b"Unauthorized"))
        code, out = self.cli("run", "--once")
        self.assertEqual(code, 1, out)
        self.assertIn("HTTP 401", out)
        self.assertIn("Some feeds failed", out)

    def test_peek_by_feed_name_and_url(self):
        code, out = self.cli("peek", "vp")
        self.assertEqual(code, 0, out)
        self.assertIn("GTFS-realtime 2.0", out)
        self.assertNotIn(SECRET, out)

        code, out = self.cli("peek", self.server.url("/gtfs.zip"))
        self.assertEqual(code, 0, out)
        self.assertIn("Static GTFS zip", out)

    def test_peek_explains_an_error_page(self):
        self.server.serve("/vp", (200, {}, b"<html>Maintenance window</html>"))
        code, out = self.cli("peek", "vp")
        self.assertEqual(code, 1)
        self.assertIn("isn't a GTFS-realtime feed", out)
        self.assertIn("Maintenance window", out)

    def test_missing_api_key_is_a_clear_error(self):
        self.env_file.write_text("API_KEY=\n", encoding="utf-8")
        code, out = self.cli("run", "--once")
        self.assertEqual(code, 2)
        self.assertIn("${API_KEY}", out)
        self.assertIn("add it to .env", out)


if __name__ == "__main__":
    unittest.main()
