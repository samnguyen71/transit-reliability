import tempfile
import textwrap
import unittest
from pathlib import Path

from feedlogger.config import ConfigError, display_url, load_config, read_env_file, redact

LOGGER_DIR = Path(__file__).resolve().parents[1]


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, text: str) -> Path:
        path = self.dir / "config.toml"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def test_reads_feeds_with_defaults_and_env_values(self):
        path = self.write(
            """
            data_dir = "/data/raw"

            [[feeds]]
            name = "trip_updates"
            url = "https://transit.test/tu?api_key=${API_KEY}"
            healthcheck_url = "${HC_TU}"

            [[feeds]]
            name = "static_gtfs"
            kind = "static"
            url = "https://transit.test/gtfs.zip"
            headers = { "x-api-key" = "${API_KEY}" }
            """
        )
        env = {"API_KEY": "s3cr3t-key", "HC_TU": "https://hc-ping.com/0f1e2d3c-aaaa-bbbb"}
        config = load_config(path, env)
        tu, static = config.feeds
        self.assertEqual(tu.url, "https://transit.test/tu?api_key=s3cr3t-key")
        self.assertEqual(tu.interval, 30)
        self.assertEqual(tu.kind, "realtime")
        self.assertEqual(tu.healthcheck_url, env["HC_TU"])
        self.assertEqual(static.interval, 6 * 3600)
        self.assertEqual(static.max_bytes, 500 * 1024 * 1024)
        self.assertEqual(static.headers, {"x-api-key": "s3cr3t-key"})
        self.assertIn("s3cr3t-key", config.secrets)
        self.assertNotIn("s3cr3t-key", config.redact(f"failed: {tu.url}"))
        self.assertNotIn("0f1e2d3c", config.redact("ping failed: /0f1e2d3c-aaaa-bbbb"))

    def test_lists_every_problem_at_once(self):
        path = self.write(
            """
            [[feeds]]
            name = "Trip Updates"
            url = "ftp://transit.test/tu"
            interval = 30

            [[feeds]]
            name = "vp"
            url = "https://transit.test/vp?key=${MISSING_KEY}"
            interval_seconds = 2
            """
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(path, {})
        message = str(caught.exception)
        for expected in (
            "name must use lowercase",
            "https://",
            "unknown setting 'interval'",
            "${MISSING_KEY}",
            "interval_seconds must be at least 10",
        ):
            self.assertIn(expected, message)

    def test_duplicate_names_and_placeholder_urls(self):
        path = self.write(
            """
            [[feeds]]
            name = "vp"
            url = "https://AGENCY.example/vp"
            [[feeds]]
            name = "vp"
            url = "https://transit.test/vp"
            """
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(path, {})
        self.assertIn("two feeds have this name", str(caught.exception))
        self.assertIn("placeholder URL", str(caught.exception))

    def test_lenient_mode_allows_missing_values_and_placeholders(self):
        path = self.write(
            """
            [[feeds]]
            name = "vp"
            url = "https://AGENCY.example/vp?key=${MISSING_KEY}"
            """
        )
        config = load_config(path, {}, strict=False)
        self.assertEqual(config.feeds[0].url, "https://AGENCY.example/vp?key=")

    def test_helpful_errors_for_missing_file_and_folder(self):
        with self.assertRaisesRegex(ConfigError, "Copy config.example.toml"):
            load_config(self.dir / "nope.toml", {})
        folder = self.dir / "config.toml"
        folder.mkdir()
        with self.assertRaisesRegex(ConfigError, "is a folder"):
            load_config(folder, {})

    def test_example_config_is_valid(self):
        config = load_config(LOGGER_DIR / "config.example.toml", {}, strict=False)
        self.assertEqual(
            [f.name for f in config.feeds], ["trip_updates", "vehicle_positions", "static_gtfs"]
        )
        self.assertEqual(config.data_dir, Path("/data/raw"))


class HelpersTest(unittest.TestCase):
    def test_redact_hides_known_secrets_and_key_parameters(self):
        text = "GET https://t.test/feed?route=5&api_key=abc123&token=xyz&format=pb secret-value"
        clean = redact(text, {"secret-value"})
        self.assertNotIn("abc123", clean)
        self.assertNotIn("xyz", clean)
        self.assertNotIn("secret-value", clean)
        self.assertIn("route=5", clean)
        self.assertIn("format=pb", clean)

    def test_display_url_drops_query_and_credentials(self):
        self.assertEqual(
            display_url("https://user:pw@t.test:8443/feed?key=abc"), "https://t.test:8443/feed?..."
        )
        self.assertEqual(display_url("https://t.test/feed"), "https://t.test/feed")

    def test_read_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nTRANSIT_API_KEY=abc123  # my key\nexport HC_URL='https://hc.test/x'\n"
                'QUOTED="has # inside"\nEMPTY=\n',
                encoding="utf-8",
            )
            self.assertEqual(
                read_env_file(path),
                {
                    "TRANSIT_API_KEY": "abc123",
                    "HC_URL": "https://hc.test/x",
                    "QUOTED": "has # inside",
                    "EMPTY": "",
                },
            )
            path.write_text("not a setting\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "line 1"):
                read_env_file(path)


if __name__ == "__main__":
    unittest.main()
