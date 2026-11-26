"""/stops and /routes from Postgres, on the feeds in conftest.py. Today is Wednesday
2026-11-25."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from transit_api import deps
from transit_api.main import create_app
from transit_api.settings import Settings

TODAY = date(2026, 11, 25)


@pytest.fixture
def api(static_dsn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path, database_url=static_dsn))
    app.dependency_overrides[deps.get_today] = lambda: TODAY
    with TestClient(app) as c:
        yield c


def ids(results):
    return [r["stop_id"] for r in results]


# /stops/search -------------------------------------------------------------------------

def test_search_words_in_any_order(api):
    body = api.get("/stops/search", params={"q": "garden and bassett"}).json()
    assert body["query"] == "garden and bassett"
    # Both names have both words; "Gardenia Court" has no Bassett.
    assert ids(body["results"]) == ["5", "1"]


def test_search_typo(api):
    assert "1" in ids(api.get("/stops/search?q=gardn").json()["results"])


def test_search_intersection_separators(api):
    for q in ("simcoe @ king", "king/simcoe", "Simcoe & King"):
        assert sorted(ids(api.get("/stops/search", params={"q": q}).json()["results"])) == \
            ["2", "3"]


def test_search_stop_code_first(api):
    (first, *_) = api.get("/stops/search?q=1003").json()["results"]
    assert first == {"stop_id": "3", "stop_name": "King Westbound @ Simcoe", "stop_lat": 43.9,
                     "stop_lon": -78.9, "parent_station": None, "stop_code": "1003"}


def test_search_only_the_current_feed(api):
    assert api.get("/stops/search?q=oldfield").json()["results"] == []
    assert api.get("/stops/search?q=future").json()["results"] == []


def test_search_limit(api):
    assert len(api.get("/stops/search?q=garden&limit=1").json()["results"]) == 1


@pytest.mark.parametrize("q", ["at", "a & b", " "])
def test_search_needs_a_word(api, q):
    assert api.get("/stops/search", params={"q": q}).status_code == 422


# /stops/{stop_id} ----------------------------------------------------------------------

def test_stop_routes_today(api):
    body = api.get("/stops/1").json()
    assert (body["stop_name"], body["service_date"]) == ("Garden Northbound @ Bassett",
                                                         "2026-11-25")
    # N1 stops here only at weekends; t1's +24 h copy isn't counted.
    assert [(r["route"]["route_id"], r["direction_id"], r["headsigns"], r["trips"])
            for r in body["routes"]] == [("10", 0, ["Oshawa Centre", "Simcoe"], 3),
                                         ("10", 1, ["Garden"], 1)]
    assert body["routes"][0]["route"] == {"route_id": "10", "short_name": "10",
                                          "long_name": "Simcoe", "color": "006633"}


def test_stop_without_service_today(api):
    assert api.get("/stops/5").json()["routes"] == []


def test_stop_headsigns_may_be_empty(api):
    (r,) = api.get("/stops/3").json()["routes"][1:]  # 10 dir 1, then 101
    assert (r["route"]["route_id"], r["headsigns"], r["trips"]) == ("101", [], 1)


def test_stop_not_found(api):
    assert api.get("/stops/99").status_code == 404


# /routes -------------------------------------------------------------------------------

def test_routes_in_number_order(api):
    body = api.get("/routes").json()
    assert [r["route_id"] for r in body] == ["2", "10", "101", "N1"]
    assert body[0]["color"] is None


def test_route_main_pattern(api):
    body = api.get("/routes/10").json()
    assert body["service_date"] == "2026-11-25"
    d0, d1 = body["directions"]
    # t1, t2 and the t4 short turn; t3 (t1's +24 h copy) isn't counted.
    assert (d0["direction_id"], d0["trips"], d0["pattern_trips"], d0["headsign"]) == \
        (0, 3, 2, "Oshawa Centre")
    assert [(s["stop_id"], s["stop_sequence"], s["timepoint"]) for s in d0["stops"]] == \
        [("1", 1, True), ("2", 2, False), ("4", 3, True)]
    assert d0["shape"] == [[-78.90, 43.90], [-78.91, 43.91], [-78.92, 43.92]]
    assert (d1["direction_id"], d1["trips"], [s["stop_id"] for s in d1["stops"]]) == \
        (1, 1, ["4", "3", "1"])


def test_route_next_service_day(api):
    body = api.get("/routes/N1").json()
    assert body["service_date"] == "2026-11-28"  # Saturday
    (d0,) = body["directions"]
    assert (d0["trips"], d0["shape"], [s["stop_id"] for s in d0["stops"]]) == \
        (1, [], ["1", "5"])


def test_route_without_trips(api):
    body = api.get("/routes/2").json()
    assert (body["service_date"], body["directions"]) == (None, [])


def test_route_not_found(api):
    assert api.get("/routes/999").status_code == 404


def test_no_feed_for_today(api):
    api.app.dependency_overrides[deps.get_today] = lambda: date(2030, 1, 1)
    assert api.get("/routes").status_code == 503
