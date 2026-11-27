"""Day types, the low-coverage exclusion, /reliability/hours, stop reliability, the ETag,
the store's rescans and cache, and /health, on hand-built rollup files."""

from datetime import date

import duckdb
import pytest
from fastapi.testclient import TestClient
from rollup_files import hist, row, write

from transit_api import rollups
from transit_api.main import create_app
from transit_api.models import Route
from transit_api.routers import routes
from transit_api.settings import Settings

HOLIDAY = date(2026, 10, 12)  # a Monday, in Settings.holidays
FRI, SAT = date(2026, 11, 13), date(2026, 11, 14)
OUTAGE = date(2026, 11, 16)  # a Monday with 5% coverage
ALL = {"start": "2026-10-01"}  # the default window would start at 10-20


def write_days(data_dir):
    write(data_dir, HOLIDAY, [row(HOLIDAY, h=hist(b6=4), on_time=4)])
    write(data_dir, FRI, [row(FRI, h=hist(b6=10), on_time=10),
                          row(FRI, kind="last_stop", h=hist(b16=2), late=2),
                          row(FRI, route="R2", h=hist(b6=5), on_time=5)])
    write(data_dir, SAT, [row(SAT, h=hist(b16=4), late=4)])
    write(data_dir, OUTAGE, [row(OUTAGE, h=hist(b6=5), scheduled=100, no_data=95, on_time=5)])
    write(data_dir, FRI, [row(FRI, hour=7, h=hist(b6=10), on_time=10),
                          row(FRI, hour=17, h=hist(b16=2), late=2),
                          row(FRI, hour=7, route="R2", h=hist(b6=5), on_time=5)], "by_hour")
    write(data_dir, OUTAGE, [row(OUTAGE, hour=7, h=hist(b6=5), scheduled=100, no_data=95,
                                 on_time=5)], "by_hour")


def make_app(tmp_path, **settings):
    app = create_app(Settings(data_dir=tmp_path, rollup_rescan_s=0,
                              **{"database_url": None, **settings}))
    app.dependency_overrides[routes.get_route] = lambda route_id: Route(
        route_id=route_id, short_name="1", long_name="Test", color=None)
    return app


@pytest.fixture
def client(tmp_path):
    write_days(tmp_path)
    with TestClient(make_app(tmp_path)) as c:
        yield c


def timepoint(body, direction=0):
    (d,) = [d for d in body["directions"] if d["direction_id"] == direction]
    return next(k for k in d["kinds"] if k["kind"] == "timepoint")


# Exclusion and day types ---------------------------------------------------------------

def test_low_coverage_day_is_left_out_and_listed(client):
    body = client.get("/routes/R1/reliability", params=ALL).json()
    assert body["dates"] == 3
    assert body["excluded"] == [{"service_date": "2026-11-16", "coverage": 0.05}]
    tp = timepoint(body)
    assert (tp["days"], tp["observed"], tp["on_time"], tp["late"]) == (3, 18, 14, 4)
    kinds = [k["kind"] for k in body["directions"][0]["kinds"]]
    assert kinds == ["timepoint", "last_stop"]  # KIND_ORDER, not alphabetical


def test_threshold_is_a_setting(tmp_path):
    write_days(tmp_path)
    with TestClient(make_app(tmp_path, min_day_coverage=0.01)) as c:
        body = c.get("/routes/R1/reliability", params=ALL).json()
    assert (body["dates"], body["excluded"]) == (4, [])


def test_day_type_weekend_includes_holidays(client):
    body = client.get("/routes/R1/reliability", params={**ALL, "day_type": "weekend"}).json()
    assert (body["day_type"], body["dates"], body["excluded"]) == ("weekend", 2, [])
    assert timepoint(body)["observed"] == 8  # 10-12 and 11-14


def test_day_type_weekday(client):
    body = client.get("/routes/R1/reliability", params={**ALL, "day_type": "weekday"}).json()
    assert body["dates"] == 1
    assert [e["service_date"] for e in body["excluded"]] == ["2026-11-16"]
    assert timepoint(body)["observed"] == 10


def test_bad_day_type(client):
    assert client.get("/routes/R1/reliability?day_type=sunday").status_code == 422


def test_day_type_function():
    assert rollups.day_type(FRI) == "weekday"
    assert rollups.day_type(SAT) == "weekend"
    assert rollups.day_type(HOLIDAY) == "weekday"
    assert rollups.day_type(HOLIDAY, frozenset({HOLIDAY})) == "weekend"


# /reliability/hours --------------------------------------------------------------------

def test_hours_network(client):
    body = client.get("/reliability/hours", params=ALL).json()
    assert (body["route_id"], body["dates"]) == (None, 1)
    assert [h["hour"] for h in body["hours"]] == [7, 17]
    (tp7,) = body["hours"][0]["kinds"]
    assert (tp7["observed"], tp7["on_time"], tp7["on_time_rate"]) == (15, 15, 1.0)


def test_hours_one_route(client):
    body = client.get("/reliability/hours", params={**ALL, "route_id": "R2"}).json()
    assert [(h["hour"], h["kinds"][0]["observed"]) for h in body["hours"]] == [(7, 5)]
    body = client.get("/reliability/hours",
                      params={**ALL, "route_id": "R1", "direction_id": 1}).json()
    assert body["hours"] == []


def test_hours_direction_needs_route(client):
    assert client.get("/reliability/hours?direction_id=0").status_code == 422


# ETag and the store --------------------------------------------------------------------

def test_etag_304_then_new_file(client, tmp_path):
    r = client.get("/routes/R1/reliability", params=ALL)
    etag = r.headers["etag"]
    assert r.headers["cache-control"] == "no-cache"
    for tag in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        again = client.get("/routes/R1/reliability", params=ALL,
                           headers={"If-None-Match": tag})
        assert (again.status_code, again.content, again.headers["etag"]) == (304, b"", etag)

    d = date(2026, 11, 20)
    write(tmp_path, d, [row(d, h=hist(b6=1), on_time=1)])  # the nightly job ran
    new = client.get("/routes/R1/reliability", params=ALL, headers={"If-None-Match": etag})
    assert new.status_code == 200
    assert new.headers["etag"] != etag
    assert (new.json()["end"], timepoint(new.json())["observed"]) == ("2026-11-20", 19)
    # The same tag is used for every reliability answer.
    assert client.get("/reliability/hours").headers["etag"] == new.headers["etag"]


def test_no_etag_without_rollups(tmp_path):
    with TestClient(make_app(tmp_path)) as c:
        r = c.get("/routes/R1/reliability")
    assert r.status_code == 200 and "etag" not in r.headers


def test_store_waits_rescan_s(tmp_path):
    write_days(tmp_path)
    store = rollups.RollupStore(duckdb.connect(), tmp_path, min_day_coverage=0.75,
                                rescan_s=3600)
    first = store.snapshot()
    write(tmp_path, date(2026, 11, 20), [row(date(2026, 11, 20))])
    assert store.snapshot() is first
    store.rescan_s = 0
    assert store.snapshot().version != first.version


def test_store_keeps_snapshot_when_nothing_changed(tmp_path):
    write_days(tmp_path)
    store = rollups.RollupStore(duckdb.connect(), tmp_path, min_day_coverage=0.75, rescan_s=0)
    assert store.snapshot() is store.snapshot()


def test_answers_are_cached_per_version(client, monkeypatch):
    calls = []
    real = rollups.summarize
    monkeypatch.setattr(rollups, "summarize", lambda *a: calls.append(1) or real(*a))
    first = client.get("/routes/R1/reliability", params=ALL).json()
    assert client.get("/routes/R1/reliability", params=ALL).json() == first
    assert len(calls) == 1


# /health -------------------------------------------------------------------------------

def test_health(client):
    body = client.get("/health").json()
    assert (body["status"], body["database"], body["static_feed_id"]) == ("ok", "unset", None)
    r = body["rollups"]
    assert (r["first_date"], r["last_date"], r["dates"]) == ("2026-10-12", "2026-11-16", 4)
    assert r["excluded"] == [{"service_date": "2026-11-16", "coverage": 0.05}]
    assert (r["rule_version"], r["rollup_version"], r["rule_versions"]) == ("3", "3", {"3": 4})
    assert r["version"] == client.get("/reliability/hours").headers["etag"].strip('"')


def test_health_without_rollups(tmp_path):
    with TestClient(make_app(tmp_path)) as c:
        body = c.get("/health").json()
    assert (body["status"], body["rollups"]["version"], body["rollups"]["dates"]) == \
        ("degraded", None, 0)


def test_health_database_down(tmp_path):
    url = "postgresql://nobody@127.0.0.1:1/none"
    with TestClient(make_app(tmp_path, database_url=url)) as c:
        body = c.get("/health").json()
    assert (body["status"], body["database"]) == ("degraded", "error")


# /stops/{stop_id}/reliability (Postgres: conftest.py's feeds) --------------------------

@pytest.fixture
def db_client(tmp_path, static_dsn):
    rows = [row(FRI, stop_id="2", route="10", h=hist(b6=3), on_time=3),
            row(FRI, stop_id="2", route="101", h=hist(b16=1), late=1),
            row(FRI, stop_id="2", route="GONE", h=hist(b6=1), on_time=1),
            row(FRI, stop_id="3", route="10", h=hist(b6=50), on_time=50)]
    write(tmp_path, FRI, rows, "by_stop")
    with TestClient(create_app(Settings(data_dir=tmp_path, database_url=static_dsn,
                                        rollup_rescan_s=0))) as c:
        yield c


def test_stop_reliability(db_client):
    body = db_client.get("/stops/2/reliability").json()
    assert body["stop"]["stop_name"] == "Simcoe Southbound @ King"
    (tp,) = body["all_routes"]
    assert (tp["observed"], tp["on_time"], tp["late"]) == (5, 4, 1)
    assert [(r["route"]["route_id"], r["route"]["short_name"], r["kinds"][0]["observed"])
            for r in body["routes"]] == [("10", "10", 3), ("101", "101", 1), ("GONE", None, 1)]


def test_stop_reliability_not_found(db_client):
    assert db_client.get("/stops/99/reliability").status_code == 404
