"""Test helpers: hand-built GTFS-realtime messages and a tiny local HTTP server.

The encoder below follows https://protobuf.dev/programming-guides/encoding/
and is deliberately separate from feedlogger.gtfsrt, so a mistake in one
shows up as a failing test instead of cancelling out.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from feedlogger.config import Config, Feed

FEED_TIME = 1_791_000_000  # 2026-10-03 04:00 UTC, a made-up "now" for feed headers


def varint(n: int) -> bytes:
    n &= (1 << 64) - 1  # negative int32/int64 values go out as 10-byte two's complement
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        if n:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def int_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def bytes_field(number: int, data: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(data)) + data


def str_field(number: int, text: str) -> bytes:
    return bytes_field(number, text.encode("utf-8"))


def feed_message(
    *entities: bytes,
    timestamp: int | None = FEED_TIME,
    version: str = "2.0",
    incrementality: int | None = None,
) -> bytes:
    header = str_field(1, version)
    if incrementality is not None:
        header += int_field(2, incrementality)
    if timestamp is not None:
        header += int_field(3, timestamp)
    return bytes_field(1, header) + b"".join(bytes_field(2, e) for e in entities)


def vehicle_entity(
    entity_id: str,
    *,
    trip_id: str | None = None,
    stop_sequence: int | None = None,
    status: int | None = None,
    timestamp: int | None = None,
    stop_id: str | None = None,
) -> bytes:
    vehicle = b""
    if trip_id is not None:
        vehicle += bytes_field(1, str_field(1, trip_id))
    if stop_sequence is not None:
        vehicle += int_field(3, stop_sequence)
    if status is not None:
        vehicle += int_field(4, status)
    if timestamp is not None:
        vehicle += int_field(5, timestamp)
    if stop_id is not None:
        vehicle += str_field(7, stop_id)
    return str_field(1, entity_id) + bytes_field(4, vehicle)


def stop_update(
    sequence: int,
    *,
    time: int | None = None,
    delay: int | None = None,
    skipped: bool = False,
) -> bytes:
    update = int_field(1, sequence)
    event = b""
    if delay is not None:
        event += int_field(1, delay)
    if time is not None:
        event += int_field(2, time)
    if event:
        update += bytes_field(2, event)  # arrival
    if skipped:
        update += int_field(5, 1)
    return update


def trip_update_entity(
    entity_id: str,
    trip_id: str,
    stop_updates: tuple[bytes, ...] = (),
    *,
    relationship: int | None = None,
) -> bytes:
    trip = str_field(1, trip_id)
    if relationship is not None:
        trip += int_field(4, relationship)
    update = bytes_field(1, trip) + b"".join(bytes_field(2, s) for s in stop_updates)
    return str_field(1, entity_id) + bytes_field(3, update)


def alert_entity(entity_id: str) -> bytes:
    return str_field(1, entity_id) + bytes_field(5, b"")


def gtfs_zip(extra: dict[str, str] | None = None, *, leave_out: tuple[str, ...] = ()) -> bytes:
    files = {
        "agency.txt": (
            "agency_name,agency_url,agency_timezone\nTest Transit,https://example.org,Etc/UTC\n"
        ),
        "routes.txt": "route_id,route_short_name,route_type\n1,1,3\n",
        "trips.txt": "route_id,service_id,trip_id\n1,wk,t1\n1,wk,t2\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\ns1,First,1.0,2.0\ns2,Second,1.1,2.1\n",
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t1,08:00:00,08:00:00,s1,1\nt1,08:05:00,08:05:00,s2,2\n"
            "t2,25:10:00,25:10:00,s1,1"  # no newline at the end, on purpose
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
            "start_date,end_date\nwk,1,1,1,1,1,0,0,20260907,20261219\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\nwk,20261225,2\n",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            if name not in leave_out:
                archive.writestr(name, text)
    return buffer.getvalue()


class FakeServer:
    """Serves queued responses on 127.0.0.1 and records each request's headers.

    A response is (status, headers, body) or a function taking the request
    headers and returning one. The last queued response repeats forever.
    """

    def __init__(self) -> None:
        self.responses: dict[str, list] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                headers = dict(self.headers.items())
                server.requests.append((self.path, headers))
                queue = server.responses.get(path) or [(404, {}, b"no such feed")]
                response = queue.pop(0) if len(queue) > 1 else queue[0]
                if callable(response):
                    response = response(headers)
                status, extra_headers, body = response
                self.send_response(status)
                for name, value in extra_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass  # keep test output quiet

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"

    def serve(self, path: str, *responses) -> None:
        self.responses[path] = list(responses)

    def hits(self, path: str) -> int:
        return sum(1 for p, _ in self.requests if p.split("?", 1)[0] == path)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerTestCase(unittest.TestCase):
    """Gives each test a FakeServer and an empty data folder."""

    def setUp(self) -> None:
        # Reach the local server directly even if the machine has a proxy set.
        patch = mock.patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)

    def make_config(self, *feeds: Feed, min_free_gb: float = 0, secrets=()) -> Config:
        return Config(
            data_dir=self.data_dir,
            user_agent="feedlogger-tests",
            min_free_gb=min_free_gb,
            feeds=feeds,
            secrets=frozenset(secrets),
        )


def make_feed(url: str, **overrides) -> Feed:
    settings = dict(
        name="vehicles",
        url=url,
        kind="realtime",
        interval=30.0,
        timeout=5.0,
        max_bytes=10 * 1024 * 1024,
        headers={},
        healthcheck_url=None,
    )
    settings.update(overrides)
    return Feed(**settings)
