"""Tests for the HomeQ collector. No internet: the parser runs on a saved response
and collect() runs against a fake client.

tests/samples/homeq_search.json is a trimmed copy of a real search response
(saved 2026-09-19, image lists removed): four apartments in the Göteborg region
(two short leases, one student flat, one without a move-in date), plus a
"project" and an apartment in Eslöv, which the collector must drop.
"""

import json
from pathlib import Path

import pytest
import requests

from collectors import homeq

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture(scope="module")
def listings():
    data = json.loads((SAMPLES / "homeq_search.json").read_text(encoding="utf-8"))
    return homeq.parse_search_response(data)


# --- search response -------------------------------------------------------

def test_keeps_only_apartments_in_the_goteborg_region(listings):
    assert [item["id"] for item in listings] == [
        "homeq:281903", "homeq:281846", "homeq:274188", "homeq:281320",
    ]  # the project (1330) and the Eslöv flat (281921) are gone


def test_first_listing_has_all_fields(listings):
    assert listings[0] == {
        "id": "homeq:281903",
        "source": "homeq",
        "url": "https://www.homeq.se/lagenhet/281903-2rum-västra-frölunda-västra-götalands-län-bergkristallsgatan-6",
        "address": "Bergkristallsgatan 6",
        "area": "Västra Frölunda",
        "kommun": "Göteborg",
        "lat": 57.6575282,
        "lon": 11.8935041,
        "rent_sek": 9002,
        "size_m2": 66.0,
        "rooms": 2.0,
        "move_in": "2026-11-01",
        "is_short_lease": False,
    }


def test_short_lease_is_flagged(listings):
    assert [item["is_short_lease"] for item in listings] == [False, True, False, True]


def test_missing_move_in_date_becomes_none(listings):
    assert listings[2]["move_in"] is None


def test_messy_values_are_tolerated():
    [item] = homeq.parse_search_response({"results": [
        {"id": 1, "municipality": "Göteborg ", "rent": "7 200", "title": "  ", "date_access": "2026-10-15T00:00:00"},
    ]})
    assert item["kommun"] == "Göteborg"  # trailing space removed
    assert item["rent_sek"] == 7200
    assert item["address"] is None
    assert item["move_in"] == "2026-10-15"
    assert (item["lat"], item["lon"], item["rooms"], item["size_m2"]) == (None, None, None, None)
    assert item["is_short_lease"] is None  # not stated: unknown, not "no"


@pytest.mark.parametrize("bad", [None, [], {}, {"results": "nope"}, {"results": [{"title": "no id"}]}])
def test_unexpected_response_raises(bad):
    with pytest.raises(homeq.HomeQError):
        homeq.parse_search_response(bad)


# --- token -----------------------------------------------------------------

def test_token_is_found_inside_user_info():
    assert homeq.parse_token({"user_info": {"token": "abc123", "ssn": "x"}}) == "abc123"


def test_missing_token_lists_field_names_but_not_values():
    with pytest.raises(homeq.HomeQError) as error:
        homeq.parse_token({"user_info": {"ssn": "secret-value"}, "other": 1})
    assert "user_info" in str(error.value)
    assert "secret-value" not in str(error.value)


# --- credentials -----------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_env(monkeypatch):
    monkeypatch.delenv("HOMEQ_EMAIL", raising=False)
    monkeypatch.delenv("HOMEQ_PASSWORD", raising=False)


def test_credentials_from_env_file(tmp_path):
    env = tmp_path / ".env"
    # utf-8-sig writes the invisible byte-order mark some Windows editors add
    env.write_text('# comment\nHOMEQ_EMAIL=jakob@example.com\nHOMEQ_PASSWORD="p=ss word"\n', encoding="utf-8-sig")
    assert homeq.load_credentials(env) == ("jakob@example.com", "p=ss word")


def test_environment_variables_win_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HOMEQ_EMAIL=file@example.com\nHOMEQ_PASSWORD=file\n", encoding="utf-8")
    monkeypatch.setenv("HOMEQ_EMAIL", "secret@example.com")
    assert homeq.load_credentials(env) == ("secret@example.com", "file")


@pytest.mark.parametrize("content", [None, "", "HOMEQ_EMAIL=\nHOMEQ_PASSWORD=\n", "HOMEQ_EMAIL=a@b.se\n"])
def test_missing_credentials_raise(tmp_path, content):
    env = tmp_path / ".env"
    if content is not None:
        env.write_text(content, encoding="utf-8")
    with pytest.raises(homeq.HomeQError):
        homeq.load_credentials(env)


# --- collect() with a fake client ------------------------------------------

LOGIN_OK = {"user_info": {"token": "T"}}


def result(*ids, total=None, kommun="Göteborg"):
    items = [{"id": i, "title": f"Street {i}", "municipality": kommun, "type": "individual"} for i in ids]
    return {"results": items, "total_hits": total if total is not None else len(items)}


class FakeClient:
    """Stands in for PoliteClient: hands out the queued responses and records requests."""

    responses = []
    calls = []

    def __init__(self, pause=0):
        pass

    def post_json(self, url, body, token=None):
        FakeClient.calls.append((url, body, token))
        response = FakeClient.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(homeq, "PoliteClient", FakeClient)
    monkeypatch.setattr(homeq, "load_credentials", lambda: ("me@example.com", "pw"))
    return FakeClient


def test_collect_logs_in_then_searches_once_when_everything_arrives_at_once(fake):
    fake.responses = [LOGIN_OK, result(1, 2, 3)]
    found = homeq.collect(pause=0)

    assert [item["id"] for item in found] == ["homeq:1", "homeq:2", "homeq:3"]
    assert fake.calls == [
        (homeq.LOGIN_URL, {"email": "me@example.com", "password": "pw"}, None),
        (homeq.SEARCH_URL, {"selectedShapes": "metropolitan_area.8", "page": 1}, "T"),
    ]  # exactly two requests: login and one search


def test_collect_asks_for_more_pages_only_if_total_hits_says_so(fake):
    fake.responses = [LOGIN_OK, result(1, 2, total=3), result(3, total=3)]
    assert len(homeq.collect(pause=0)) == 3
    assert [call[1].get("page") for call in fake.calls[1:]] == [1, 2]


def test_collect_stops_when_a_page_repeats(fake):
    fake.responses = [LOGIN_OK, result(1, 2, total=99), result(1, 2, total=99)]
    assert len(homeq.collect(pause=0)) == 2


def test_collect_drops_listings_outside_the_region(fake):
    both = result(1, kommun="Göteborg")["results"] + result(2, kommun="Eslöv")["results"]
    fake.responses = [LOGIN_OK, {"results": both, "total_hits": 2}]
    assert [item["id"] for item in homeq.collect(pause=0)] == ["homeq:1"]


def test_collect_refuses_a_result_with_nothing_in_the_region(fake):
    fake.responses = [LOGIN_OK, result(1, 2, kommun="Eslöv")]
    with pytest.raises(homeq.HomeQError):
        homeq.collect(pause=0)


def test_wrong_password_gives_a_clear_error(fake):
    refused = requests.HTTPError(response=requests.Response())
    refused.response.status_code = 403
    fake.responses = [refused]
    with pytest.raises(homeq.HomeQError, match="wrong email or password"):
        homeq.collect(pause=0)


def test_token_is_sent_as_jwt_not_bearer():
    sent = {}

    class FakeSession:
        headers = {}

        def post(self, url, json=None, headers=None, timeout=None):
            sent["headers"] = headers
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    client = homeq.PoliteClient(pause=0)
    client.session = FakeSession()
    client.post_json("https://example.com", {}, token="T")
    assert sent["headers"] == {"Authorization": "JWT T"}
