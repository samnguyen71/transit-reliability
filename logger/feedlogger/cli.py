"""Command line: python -m feedlogger {run,peek,report}."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import requests

from feedlogger import __version__, peek
from feedlogger.collector import FeedWorker, describe
from feedlogger.config import (
    Config,
    ConfigError,
    display_url,
    load_config,
    read_env_file,
    redact,
)
from feedlogger.fmt import human_bytes, human_duration
from feedlogger.report import collect_stats, format_report

log = logging.getLogger("feedlogger")

WRITE_HELP = (
    "With Docker, create the folder before starting the logger: mkdir -p data "
    "(if Docker already created it, run: sudo chown -R 1000:1000 data). "
    "Outside Docker, pass --data-dir ./data/raw."
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m feedlogger",
        description="Archive GTFS-realtime and static GTFS feeds, and check on them.",
    )
    parser.add_argument("--version", action="version", version=f"feedlogger {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=os.environ.get("FEEDLOGGER_CONFIG", "config.toml"),
        help="config file (default: %(default)s)",
    )
    common.add_argument(
        "--env-file",
        help="KEY=value file for ${KEY} in the config (default: .env here or one folder up)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", parents=[common], help="poll and archive the feeds")
    run.add_argument("--data-dir", help="store snapshots here instead of data_dir")
    run.add_argument("--once", action="store_true", help="fetch each feed once and exit")
    run.set_defaults(handler=cmd_run)

    look = commands.add_parser(
        "peek", parents=[common], help="fetch a feed once and describe what's in it"
    )
    look.add_argument("source", help="a feed name from the config, a URL, or a saved snapshot")
    look.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'NAME: value'",
        help="extra HTTP header, for example an API key (repeatable)",
    )
    look.set_defaults(handler=cmd_peek)

    report = commands.add_parser(
        "report", parents=[common], help="stored data, gaps, and when the disk fills up"
    )
    report.add_argument("--data-dir", help="read snapshots from here instead of data_dir")
    report.add_argument("--days", type=int, default=7, help="days to look back (default: 7)")
    report.set_defaults(handler=cmd_report)
    return parser


# run ------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config, _environment(args.env_file))
    if args.data_dir:
        config = replace(config, data_dir=Path(args.data_dir))
    _setup_logging(config.secrets)
    try:
        _check_writable(config.data_dir)
    except OSError as e:
        log.error("can't write to %s (%s). %s", config.data_dir, e, WRITE_HELP)
        return 2
    workers = [FeedWorker(feed, config) for feed in config.feeds]
    if args.once:
        return _run_once(workers)
    return _run_forever(config, workers)


def _run_once(workers: list[FeedWorker]) -> int:
    ok = True
    for worker in workers:
        result = worker.poll_once()
        print(f"{worker.feed.name}: {describe(result)}")
        ok = ok and result.healthy
    print("All feeds OK." if ok else "Some feeds failed: see above.")
    return 0 if ok else 1


def _run_forever(config: Config, workers: list[FeedWorker]) -> int:
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        if not stop.is_set():
            log.info("stopping (%s)", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    free = shutil.disk_usage(config.data_dir).free
    log.info(
        "feedlogger %s: archiving %d feed(s) to %s (%s free)",
        __version__,
        len(workers),
        config.data_dir,
        human_bytes(free),
    )
    for worker in workers:
        feed = worker.feed
        alerts = "Healthchecks on" if feed.healthcheck_url else "no healthcheck_url: no alerts"
        log.info(
            "  %s: %s, every %s, from %s (%s)",
            feed.name,
            feed.kind,
            human_duration(feed.interval),
            display_url(feed.url),
            alerts,
        )

    threads = [
        threading.Thread(target=w.run, args=(stop,), name=w.feed.name, daemon=True)
        for w in workers
    ]
    for thread in threads:
        thread.start()
    while not stop.wait(5):
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.error("%s stopped unexpectedly; exiting so Docker restarts us", ", ".join(dead))
            return 1
    for thread in threads:
        thread.join(timeout=30)
    log.info("stopped")
    return 0


# peek -----------------------------------------------------------------------


def cmd_peek(args: argparse.Namespace) -> int:
    source = args.source
    is_url = source.startswith(("http://", "https://"))
    is_file = not is_url and Path(source).is_file()
    config, problem = None, None
    if Path(args.config).exists():
        try:
            config = load_config(args.config, _environment(args.env_file))
        except ConfigError as e:
            problem = e
    feed = config.get_feed(source) if config else None
    if not (feed or is_url or is_file):
        if problem:
            raise problem
        if config:
            names = ", ".join(f.name for f in config.feeds)
            raise ConfigError(
                f"'{source}' isn't a URL, a file, or a feed in {args.config} ({names})"
            )
        raise ConfigError(f"'{source}' isn't a URL or a file, and there's no {args.config}")

    secrets = config.secrets if config else frozenset()
    extra = {}
    for item in args.header:
        name, sep, value = item.partition(":")
        if not sep:
            raise ConfigError(f"--header should look like 'Name: value', not '{item}'")
        extra[name.strip()] = value.strip()
        secrets |= {value.strip()}

    fetched_at = None
    if is_file:
        data = Path(source).read_bytes()
        print(f"{source}, {human_bytes(len(data))}\n")
    else:
        url = feed.url if feed else source
        headers = {**(feed.headers if feed else {}), **extra}
        user_agent = config.user_agent if config else f"feedlogger/{__version__}"
        print(f"Fetching {display_url(url)}")
        try:
            status, reason, data, seconds = peek.fetch(
                url,
                headers,
                user_agent,
                timeout=feed.timeout if feed else 60,
                max_bytes=feed.max_bytes if feed else 500 * 1024 * 1024,
            )
        except requests.RequestException as e:
            print(f"Couldn't fetch it: {redact(str(e), secrets)}")
            return 1
        fetched_at = time.time()
        print(f"HTTP {status} {reason} in {seconds:.2f} s, {human_bytes(len(data))}\n")
        if status != 200:
            text = " ".join(data[:300].decode("utf-8", "replace").split())
            print(f"The server didn't send the feed. It said: {redact(text, secrets)}")
            return 1

    try:
        print(peek.describe(data, interval=feed.interval if feed else 30, fetched_at=fetched_at))
    except ValueError as e:
        text = " ".join(data[:200].decode("utf-8", "replace").split())
        print(f"This isn't a GTFS-realtime feed or a GTFS zip ({e}).")
        if text.isprintable():
            print(f"It starts with: {redact(text, secrets)}")
        return 1
    return 0


# report ---------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    reserve_gb = 2.0
    data_dir = Path(args.data_dir) if args.data_dir else None
    if Path(args.config).exists():
        config = load_config(args.config, _environment(args.env_file), strict=False)
        data_dir = data_dir or config.data_dir
        reserve_gb = config.min_free_gb
    if data_dir is None:
        raise ConfigError(f"no {args.config} to read data_dir from: pass --data-dir")
    if not data_dir.is_dir():
        raise ConfigError(f"{data_dir} doesn't exist yet. Has the logger run?")
    now = datetime.now(timezone.utc)
    usage = shutil.disk_usage(data_dir)
    print(
        format_report(
            collect_stats(data_dir, days=args.days, now=now),
            data_dir=data_dir,
            days=args.days,
            free_bytes=usage.free,
            total_bytes=usage.total,
            reserve_bytes=int(reserve_gb * 1024**3),
            now=now,
        )
    )
    return 0


# helpers --------------------------------------------------------------------


def _environment(env_file: str | None) -> dict[str, str]:
    """The environment plus a .env file, if any. Non-empty real variables win."""
    if env_file:
        path = Path(env_file)
        if not path.is_file():
            raise ConfigError(f"--env-file {env_file} not found")
    else:
        path = next((p for p in (Path(".env"), Path("..") / ".env") if p.is_file()), None)
    values = read_env_file(path) if path else {}
    values.update({k: v for k, v in os.environ.items() if v or k not in values})
    return values


def _check_writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".write-test-{os.getpid()}"
    probe.write_bytes(b"ok")
    probe.unlink()


class _RedactingFilter(logging.Filter):
    """Keeps API keys and ping URLs out of the logs, whoever logs them."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact(message, self.secrets)
        if clean != message:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
            record.exc_text = redact(text, self.secrets)
        return True


def _setup_logging(secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.addFilter(_RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    # urllib3 logs full URLs (with any key in them) when it retries.
    logging.getLogger("urllib3").setLevel(logging.ERROR)
