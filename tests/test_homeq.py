"""Tests for the HomeQ collector. No internet: the parser runs on a saved response
and collect() runs against a fake client.

NOTE: tests/samples/homeq_search.json was written by hand from the field list
(id, uri, city, municipality, rent, rooms, area, date_access, title,
is_short_lease), not saved from the real API. Replace it with a real saved
response once you have one, and adjust the expected values below.
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

def test_first_listing_has_all_fields(listings):
    assert listings[0] == {
        "id": "homeq:101234",
        "source": "homeq",
        "url": "https://www.homeq.se/apartment/101234",
        "address": "Karl Johansgatan 12",
        "area": "Majorna",
        "kommun": "Göteborg",
        "rent_sek": 8450,
        "size_m2": 52.5,
        "rooms": 2.0,
        "move_in": "2026-11-01",
        "is_short_lease": False,
    }


def test_short_lease_and_timestamp_date(listings):
    assert listings[1]["is_short_lease"] is True
    assert listings[1]["move_in"] == "2026-10-15"
    assert listings[1]["rooms"] == 1.5


def test_missing_and_messy_values_become_none(listings):
    messy = listings[2]
    assert messy["rent_sek"] == 7200  # "7 200" as text
    assert messy["url"] == "https://www.homeq.se/apartment/101377"  # already absolute
    assert messy["rooms"] is None
    assert messy["size_m2"] is None
    assert messy["move_in"] is None
    assert messy["address"] is None  # blank title


@pytest.mark.parametrize("bad", [None, [], {}, {"results": "nope"}, {"results": [{"title": "no id"}]}])
def test_unexpected_response_raises(bad):
    with pytest.raises(homeq.HomeQError):
        homeq.parse_search_response(bad)


# --- token -----------------------------------------------------------------

def test_token_is_found():
    assert homeq.parse_token({"token": "abc123"}) == "abc123"


def test_missing_token_lists_field_names_but_not_values():
    with pytest.raises(homeq.HomeQError) as error:
        homeq.parse_token({"jwt": "secret-value", "user": "x"})
    assert "jwt" in str(error.value)
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

def result(*ids):
    return {"results": [{"id": i, "title": f"Street {i}"} for i in ids]}


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


def test_collect_logs_in_then_reads_every_page(fake):
    fake.responses = [{"token": "T"}, result(1, 2), result(3), result()]
    found = homeq.collect(pause=0)

    assert [item["id"] for item in found] == ["homeq:1", "homeq:2", "homeq:3"]
    login, page1, page2, page3 = fake.calls
    assert login == (homeq.LOGIN_URL, {"username": "me@example.com", "password": "pw"}, None)
    assert page1 == (homeq.SEARCH_URL, {"selectedShapes": "metropolitan_area.8", "page": 1}, "T")
    assert page2[1]["page"] == 2 and page3[1]["page"] == 3


def test_collect_stops_when_a_page_repeats(fake):
    fake.responses = [{"token": "T"}, result(1, 2), result(1, 2)]  # API ignores page numbers past the end
    assert len(homeq.collect(pause=0)) == 2


def test_collect_refuses_an_empty_result(fake):
    fake.responses = [{"token": "T"}, result()]
    with pytest.raises(homeq.HomeQError):
        homeq.collect(pause=0)


def test_wrong_password_gives_a_clear_error(fake):
    refused = requests.HTTPError(response=requests.Response())
    refused.response.status_code = 401
    fake.responses = [refused]
    with pytest.raises(homeq.HomeQError, match="wrong email or password"):
        homeq.collect(pause=0)
