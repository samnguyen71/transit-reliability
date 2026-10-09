"""Fetch each feed on a schedule and archive what comes back.

Each feed gets its own thread. One poll:

1. downloads the feed to a temporary file, hashing it on the way;
2. skips it if it's byte-for-byte the same as the last snapshot;
3. checks it's what we expect (a GTFS-realtime message, or a GTFS zip);
4. moves it into the day's folder with an atomic rename, so a crash never
   leaves half a file behind;
5. appends a line to the day's _manifest.jsonl, whatever happened.

The manifest is what lets Phase 1 tell "the feed didn't change" apart from
"the logger wasn't running", so failures are recorded too.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from feedlogger import gtfsrt
from feedlogger.config import Config, Feed
from feedlogger.fmt import human_bytes, human_duration, utc_day, utc_iso, utc_stamp

log = logging.getLogger("feedlogger")

PING_EVERY_SECONDS = 60
SUMMARY_EVERY_SECONDS = 15 * 60
REPEAT_ERROR_LOG_SECONDS = 10 * 60
MAX_BACKOFF_SECONDS = 300
MAX_RETRY_AFTER_SECONDS = 900
TMP_MAX_AGE_SECONDS = 15 * 60
# Failed polls that are worth backing off from, so a struggling server
# isn't hit every 30 seconds.
BACKOFF_STATUSES = {"network_error", "http_error", "invalid", "too_large", "error"}
GTFS_REQUIRED_FILES = ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")


@dataclass
class PollResult:
    status: str  # saved, duplicate, not_modified, invalid, http_error, ...
    healthy: bool  # True when we hold an up-to-date, valid copy of the feed
    record: dict = field(default_factory=dict)  # the manifest line
    message: str = ""
    retry_after: float | None = None


@dataclass
class FeedState:
    """Remembered between polls and restarts, in <feed>/_state.json."""

    last_sha256: str | None = None
    last_valid: bool = False
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class _Download:
    status: int
    reason: str
    headers: Mapping[str, str]  # case-insensitive, as requests returns them
    sha256: str = ""  # of the body, when status is 200
    snippet: str = ""  # start of the body, when status isn't 200


class _TooLarge(Exception):
    pass


class FeedWorker:
    def __init__(
        self,
        feed: Feed,
        config: Config,
        *,
        clock=time.time,
        session: requests.Session | None = None,
    ):
        self.feed = feed
        self.config = config
        self.clock = clock
        self.session = session or requests.Session()
        self.dir = config.data_dir / feed.name
        self.tmp_dir = self.dir / "_tmp"
        self.state_path = self.dir / "_state.json"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        # Clear partial downloads left by a crash. Recent ones may belong to
        # another running copy (say, `run --once` next to the service).
        for leftover in self.tmp_dir.iterdir():
            try:
                if time.time() - leftover.stat().st_mtime > TMP_MAX_AGE_SECONDS:
                    leftover.unlink()
            except OSError:
                pass
        self.state = self._load_state()

        # Bookkeeping for logs and Healthchecks pings.
        self._counts: Counter[str] = Counter()
        self._stored_bytes = 0
        self._last_feed_age: float | None = None
        self._summary_started = time.monotonic()
        self._first_save_logged = False
        self._failing_since: float | None = None
        self._failures = 0
        self._last_error = ""
        self._last_error_logged = 0.0
        self._last_ping: float | None = None
        self._ping_failures = 0

    # One poll ---------------------------------------------------------------

    def poll_once(self) -> PollResult:
        now = self.clock()
        started = time.monotonic()
        record: dict = {
            "fetched_at": utc_iso(now),
            "status": None,
            "http_status": None,
            "bytes": None,
            "stored_bytes": None,
            "sha256": None,
            "file": None,
            "feed_timestamp": None,
            "feed_age_s": None,
            "entities": None,
            "elapsed_ms": None,
            "error": None,
        }
        tmp = self.tmp_dir / f"{utc_stamp(now)}-{os.getpid()}.part"
        try:
            result = self._poll(now, tmp, record)
        finally:
            tmp.unlink(missing_ok=True)
            tmp.with_name(tmp.name + ".gz").unlink(missing_ok=True)
        record["status"] = result.status
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        if result.message and not result.healthy:
            result.message = self.config.redact(result.message)
            record["error"] = result.message
        result.record = record
        self._append_manifest(now, record)
        return result

    def _poll(self, now: float, tmp: Path, record: dict) -> PollResult:
        free = shutil.disk_usage(self.dir).free
        if free < self.config.min_free_gb * 1024**3:
            return PollResult(
                "disk_low",
                False,
                message=(
                    f"only {human_bytes(free)} of disk left (min_free_gb is "
                    f"{self.config.min_free_gb:g}); not fetching until space is freed"
                ),
            )

        try:
            download = self._download(tmp)
        except _TooLarge as e:
            return PollResult(
                "too_large",
                False,
                message=(
                    f"response is bigger than max_mb ({human_bytes(self.feed.max_bytes)}): "
                    f"{e}. Raise max_mb if this feed really is that big"
                ),
            )
        except requests.RequestException as e:
            return PollResult("network_error", False, message=f"{type(e).__name__}: {e}")
        except OSError as e:
            return PollResult("storage_error", False, message=f"couldn't write to disk: {e}")

        status = download.status
        record["http_status"] = status
        if status == 304:
            return PollResult(
                "not_modified",
                self.state.last_valid,
                message="" if self.state.last_valid else "still the same invalid response",
            )
        if status != 200:
            retry_after = None
            if status in (429, 503):
                retry_after = _retry_after(download.headers.get("Retry-After"))
            return PollResult(
                "http_error",
                False,
                message=_http_message(status, download.reason, download.snippet),
                retry_after=retry_after,
            )

        digest = download.sha256
        record["bytes"] = tmp.stat().st_size
        record["sha256"] = digest[:16]
        etag = download.headers.get("ETag")
        last_modified = download.headers.get("Last-Modified")

        if digest == self.state.last_sha256:
            self._update_state(etag=etag, last_modified=last_modified)
            if self.state.last_valid:
                return PollResult("duplicate", True)
            return PollResult("invalid", False, message="still the same invalid response")

        valid, info, problem = self._check(tmp)
        record.update(info)
        if info.get("feed_timestamp"):
            record["feed_age_s"] = round(now - info["feed_timestamp"])
        try:
            final = self._store(tmp, now, valid)
        except OSError as e:
            return PollResult("storage_error", False, message=f"couldn't save the snapshot: {e}")
        record["file"] = final.name
        record["stored_bytes"] = final.stat().st_size
        self._update_state(
            last_sha256=digest, last_valid=valid, etag=etag, last_modified=last_modified
        )
        if valid:
            return PollResult("saved", True)
        return PollResult("invalid", False, message=f"{problem} (kept as {final.name})")

    def _download(self, dest: Path) -> _Download:
        """Stream the feed into `dest`, hashing it on the way."""
        headers = {"User-Agent": self.config.user_agent, "Connection": "close"}
        headers.update(self.feed.headers)
        if self.state.etag:
            headers["If-None-Match"] = self.state.etag
        if self.state.last_modified:
            headers["If-Modified-Since"] = self.state.last_modified
        deadline = time.monotonic() + self.feed.timeout
        timeout = (min(10.0, self.feed.timeout), self.feed.timeout)
        with self.session.get(self.feed.url, headers=headers, timeout=timeout, stream=True) as resp:
            reason = resp.reason or ""
            if resp.status_code != 200:
                return _Download(resp.status_code, reason, resp.headers, snippet=_snippet(resp))
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > self.feed.max_bytes:
                raise _TooLarge(f"the server says {human_bytes(int(length))}")
            digest = hashlib.sha256()
            size = 0
            with dest.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    size += len(chunk)
                    if size > self.feed.max_bytes:
                        raise _TooLarge(f"stopped after {human_bytes(size)}")
                    if time.monotonic() > deadline:
                        raise requests.Timeout(
                            f"download took longer than timeout_seconds ({self.feed.timeout:g})"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            return _Download(200, reason, resp.headers, sha256=digest.hexdigest())

    def _check(self, path: Path) -> tuple[bool, dict, str]:
        """Is this the kind of file we expect? Returns (valid, manifest fields, problem)."""
        if self.feed.kind == "realtime":
            data = path.read_bytes()
            try:
                header = gtfsrt.read_header(data)
            except ValueError as e:
                return False, {}, f"not a GTFS-realtime feed ({e}){_preview(data)}"
            return True, {"feed_timestamp": header.timestamp, "entities": header.entity_count}, ""
        try:
            with zipfile.ZipFile(path) as archive:
                names = {name.rsplit("/", 1)[-1] for name in archive.namelist()}
        except (zipfile.BadZipFile, OSError):
            with path.open("rb") as f:
                start = f.read(500)
            return False, {}, f"not a zip file{_preview(start)}"
        missing = [name for name in GTFS_REQUIRED_FILES if name not in names]
        if missing:
            return False, {}, f"zip has no {', '.join(missing)}, so it isn't a GTFS feed"
        return True, {}, ""

    def _store(self, tmp: Path, now: float, valid: bool) -> Path:
        day_dir = self.dir / utc_day(now)
        day_dir.mkdir(parents=True, exist_ok=True)
        if self.feed.kind == "realtime":
            source = tmp.with_name(tmp.name + ".gz")
            with tmp.open("rb") as raw, gzip.open(source, "wb", compresslevel=6) as packed:
                shutil.copyfileobj(raw, packed)
            extension = ".pb.gz" if valid else ".invalid.gz"
        else:
            source = tmp  # a zip is already compressed
            extension = ".zip" if valid else ".invalid"
        final = _unique_path(day_dir, utc_stamp(now), extension)
        os.replace(source, final)  # atomic: the file appears whole or not at all
        return final

    def _append_manifest(self, now: float, record: dict) -> None:
        day_dir = self.dir / utc_day(now)
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            with (day_dir / "_manifest.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as e:
            log.error("%s: couldn't write to the manifest: %s", self.feed.name, e)

    def _load_state(self) -> FeedState:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            known = FeedState.__dataclass_fields__
            return FeedState(**{k: v for k, v in saved.items() if k in known})
        except FileNotFoundError:
            return FeedState()
        except (OSError, ValueError, TypeError) as e:
            log.warning("%s: ignoring unreadable %s (%s)", self.feed.name, self.state_path.name, e)
            return FeedState()

    def _update_state(self, **changes) -> None:
        before = asdict(self.state)
        for key, value in changes.items():
            setattr(self.state, key, value)
        if asdict(self.state) == before:
            return
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:  # the in-memory state still works until a restart
            log.warning("%s: couldn't save %s: %s", self.feed.name, self.state_path.name, e)

    # Running on a schedule --------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Poll until `stop` is set."""
        rng = random.Random()
        # Spread out start times so several feeds don't hit one server at once.
        if stop.wait(rng.uniform(0, min(5.0, self.feed.interval))):
            return
        failures = 0
        while not stop.is_set():
            started = time.monotonic()
            try:
                result = self.poll_once()
            except Exception as e:  # a bug in one poll shouldn't stop the logger
                log.exception("%s: unexpected error", self.feed.name)
                result = PollResult("error", False, message=f"unexpected error: {e}")
            self.handle_result(result)
            if result.status in BACKOFF_STATUSES:
                failures += 1
                wait = backoff_delay(self.feed.interval, failures, result.retry_after)
                wait *= rng.uniform(0.9, 1.1)
            else:
                failures = 0
                wait = started + self.feed.interval - time.monotonic()
            stop.wait(max(0.0, wait))

    def handle_result(self, result: PollResult) -> None:
        """Log what happened (without flooding the log) and ping Healthchecks."""
        now = time.monotonic()
        name = self.feed.name
        self._counts[result.status] += 1
        if result.status == "saved":
            self._stored_bytes += result.record.get("stored_bytes") or 0
            if result.record.get("feed_age_s") is not None:
                self._last_feed_age = result.record["feed_age_s"]
            if not self._first_save_logged:
                log.info("%s: first snapshot saved: %s", name, describe(result))
                self._first_save_logged = True

        if result.healthy:
            if self._failing_since is not None:
                log.info(
                    "%s: working again after %d failed polls over %s",
                    name,
                    self._failures,
                    human_duration(now - self._failing_since),
                )
                self._failing_since = None
                self._failures = 0
                self._last_error = ""
            self._maybe_ping(now)
        else:
            self._failures += 1
            if self._failing_since is None:
                self._failing_since = now
            if result.message != self._last_error:
                log.warning("%s: %s", name, result.message)
                self._last_error, self._last_error_logged = result.message, now
            elif now - self._last_error_logged >= REPEAT_ERROR_LOG_SECONDS:
                log.warning(
                    "%s: still failing (%d polls over %s): %s",
                    name,
                    self._failures,
                    human_duration(now - self._failing_since),
                    result.message,
                )
                self._last_error_logged = now

        if now - self._summary_started >= SUMMARY_EVERY_SECONDS:
            self._log_summary(now)

    def _maybe_ping(self, now: float) -> None:
        url = self.feed.healthcheck_url
        if not url or (self._last_ping is not None and now - self._last_ping < PING_EVERY_SECONDS):
            return
        self._last_ping = now
        try:
            resp = self.session.get(
                url,
                timeout=10,
                headers={"User-Agent": self.config.user_agent, "Connection": "close"},
            )
            resp.close()
            if resp.status_code >= 400:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            self._ping_failures = 0
        except requests.RequestException as e:
            self._ping_failures += 1
            if self._ping_failures == 1 or self._ping_failures % 30 == 0:
                log.warning(
                    "%s: couldn't ping Healthchecks (%s). Data is still being collected.",
                    self.feed.name,
                    e,
                )

    def _log_summary(self, now: float) -> None:
        counts = self._counts
        unchanged = counts["duplicate"] + counts["not_modified"]
        failed = sum(counts.values()) - counts["saved"] - unchanged
        age = (
            f"; feed was {human_duration(self._last_feed_age)} old when last saved"
            if self._last_feed_age is not None
            else ""
        )
        log.info(
            "%s: last %s: %d saved, %d unchanged, %d failed, %s stored%s",
            self.feed.name,
            human_duration(now - self._summary_started),
            counts["saved"],
            unchanged,
            failed,
            human_bytes(self._stored_bytes),
            age,
        )
        self._counts.clear()
        self._stored_bytes = 0
        self._summary_started = now


def backoff_delay(interval: float, failures: int, retry_after: float | None = None) -> float:
    """Seconds to wait after `failures` failed polls in a row.

    Doubles from the normal interval (at most 60 s to start) up to 5 minutes.
    A server's Retry-After can make the wait longer (up to 15 minutes), but
    never shorter, so a server saying "retry now" isn't hammered.
    """
    first = min(interval, 60.0)
    delay = min(first * 2 ** min(failures - 1, 16), max(MAX_BACKOFF_SECONDS, first))
    if retry_after is not None:
        delay = min(max(delay, retry_after), MAX_RETRY_AFTER_SECONDS)
    return delay


def describe(result: PollResult) -> str:
    """One line about a poll, for logs and `run --once`."""
    record = result.record
    if result.status == "saved":
        parts = [f"saved {record['file']}"]
        entities = record.get("entities")
        if entities is not None:
            parts.append(f"{entities} {'entity' if entities == 1 else 'entities'}")
        if record["file"].endswith(".gz"):
            parts.append(
                f"{human_bytes(record['bytes'])} ({human_bytes(record['stored_bytes'])} gzipped)"
            )
        else:
            parts.append(human_bytes(record["bytes"]))
        if record.get("feed_age_s") is not None:
            parts.append(f"feed timestamp {human_duration(record['feed_age_s'])} old")
        return ", ".join(parts)
    if result.status == "duplicate":
        return "unchanged since the last fetch, so not saved again"
    if result.status == "not_modified":
        return "unchanged (HTTP 304), nothing to save"
    return f"{result.status}: {result.message}"


def _unique_path(folder: Path, stamp: str, extension: str) -> Path:
    """A new file name for this second. A second snapshot in the same second
    (only possible around a restart) gets _1, which sorts after the first."""
    path = folder / f"{stamp}{extension}"
    n = 1
    while path.exists():
        path = folder / f"{stamp}_{n}{extension}"
        n += 1
    return path


def _retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header: either seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _http_message(status: int, reason: str, snippet: str) -> str:
    hints = {
        401: "the server wants credentials: check the API key and how the agency says to send it",
        403: "access denied: check the API key, and that it's sent the way the agency's docs say",
        404: "nothing at this URL: check it against the agency's developer page",
        429: "too many requests: the agency wants you to poll less often",
    }
    hint = hints.get(status, "a problem on the agency's side" if status >= 500 else "")
    message = f"HTTP {status} {reason}".rstrip()
    if hint:
        message += f" ({hint})"
    if snippet:
        message += f'. The response starts: "{snippet}"'
    return message


def _snippet(resp: requests.Response) -> str:
    """The start of an error response, which often says what's wrong."""
    try:
        chunk = next(resp.iter_content(chunk_size=300), b"")
    except requests.RequestException:
        return ""
    return _printable(chunk, 200)


def _preview(data: bytes) -> str:
    text = _printable(data[:300], 120)
    return f'; it starts with "{text}"' if text else ""


def _printable(data: bytes, limit: int) -> str:
    """Up to `limit` characters of `data` if it looks like text, else ''."""
    text = data.decode("utf-8", "replace")
    if not text or sum(ch.isprintable() or ch.isspace() for ch in text) < 0.9 * len(text):
        return ""
    return " ".join(text.split())[:limit]
