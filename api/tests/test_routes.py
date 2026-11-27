"""The routes endpoints on hand-built rollup files. No database: get_route is replaced."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from rollup_files import hist, row, write

from transit_api import rollups
from transit_api.main import create_app
from transit_api.models import Route
from transit_api.routers import routes
from transit_api.settings import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url=None))
    app.dependency_overrides[routes.get_route] = lambda route_id: Route(
        route_id=route_id, short_name="1", long_name="Test", color=None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def days(tmp_path):
    write(tmp_path, date(2026, 10, 1), [row(date(2026, 10, 1), h=hist(b30=50))])  # too old
    write(tmp_path, date(2026, 11, 5), [
        row(date(2026, 11, 5), h=hist(b6=6, b16=2), scheduled=10, on_time=6, late=2,
            cancelled=1, lost=1)])
    write(tmp_path, date(2026, 11, 6), [
        row(date(2026, 11, 6), h=hist(b6=10), on_time=10),
        row(date(2026, 11, 6), kind="first_stop", h=hist(b6=1), on_time=1),
        row(date(2026, 11, 6), direction=1, kind="last_stop", h=hist(b6=1), on_time=1),
        row(date(2026, 11, 6), route="R2", h=hist(b30=99)),
    ])


def test_sums_across_dates_and_defaults_to_28_days(client, days):
    r = client.get("/routes/R1/reliability")
    assert r.status_code == 200
    body = r.json()
    assert (body["start"], body["end"]) == ("2026-10-10", "2026-11-06")
    d0, d1 = body["directions"]
    assert d0["direction_id"] == 0 and d1["direction_id"] == 1
    assert [k["kind"] for k in d0["kinds"]] == ["first_stop", "timepoint"]

    tp = d0["kinds"][1]
    assert (tp["days"], tp["scheduled"], tp["observed"], tp["on_time"], tp["late"]) == \
        (2, 20, 18, 16, 2)
    assert tp["on_time_rate"] == round(16 / 18, 4)
    assert tp["coverage"] == round(18 / 19, 4)  # the cancelled visit didn't run
    # Summed histogram: 16 in [0, 60), 2 in [600, 660).
    assert tp["median_delay_s"] == 34   # 9th of 16 in [0, 60): 9/16 of the way
    assert tp["p90_delay_s"] == 606     # 16.2th: 0.2 of 2 into [600, 660)


def test_date_range(client, days):
    body = client.get("/routes/R1/reliability?start=2026-11-05&end=2026-11-05").json()
    (d0,) = body["directions"]
    (tp,) = d0["kinds"]
    assert (tp["days"], tp["observed"], tp["median_delay_s"]) == (1, 8, 40)


def test_no_rollups_yet(client):
    body = client.get("/routes/R1/reliability").json()
    assert body["directions"] == []


def test_start_after_end(client, days):
    r = client.get("/routes/R1/reliability?start=2026-11-06&end=2026-11-05")
    assert r.status_code == 422


def test_hist_quantile_open_bins():
    assert rollups.hist_quantile([0] * rollups.HIST_BINS, 0.5) is None
    assert rollups.hist_quantile(hist(b0=3), 0.5) == rollups.HIST_LO_S
    assert rollups.hist_quantile(hist(b36=3), 0.5) == rollups.HIST_HI_S
