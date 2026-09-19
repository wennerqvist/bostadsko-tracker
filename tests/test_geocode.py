"""Tests for the address lookup. No internet: parsing uses saved Nominatim answers
(tests/samples/nominatim_*.json) and Geocoder runs against a fake session."""

import json
from pathlib import Path

import pytest
import requests

import geocode

SAMPLES = Path(__file__).parent / "samples"


def sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


# --- parse_result ----------------------------------------------------------

def test_found_place_gives_lat_lon_as_numbers():
    assert geocode.parse_result(sample("nominatim_found.json")) == (58.0622692, 11.8419275)


def test_empty_answer_means_not_found():
    assert geocode.parse_result(sample("nominatim_empty.json")) is None


def test_place_outside_sweden_is_rejected():
    assert geocode.parse_result([{"lat": "48.85", "lon": "2.35"}]) is None  # Paris


@pytest.mark.parametrize("bad", [{"error": "blocked"}, None, [{"lon": "11.9"}], [{"lat": "x", "lon": "y"}]])
def test_unexpected_answer_raises(bad):
    with pytest.raises(geocode.GeocodeError):
        geocode.parse_result(bad)


# --- address_queries -------------------------------------------------------

@pytest.mark.parametrize("address, expected", [
    ("Hasselbackevägen 4", ["Hasselbackevägen 4, Stenungsund", "Hasselbackevägen, Stenungsund"]),
    ("Artillerigatan 30 .", ["Artillerigatan 30, Stenungsund", "Artillerigatan, Stenungsund"]),
    ("Merkuriusgatan 2 A", ["Merkuriusgatan 2A, Stenungsund", "Merkuriusgatan, Stenungsund"]),
    ("Bunkebergsgatan 1C .", ["Bunkebergsgatan 1C, Stenungsund", "Bunkebergsgatan, Stenungsund"]),
    ("Distansgatan 66-68", ["Distansgatan 66, Stenungsund", "Distansgatan, Stenungsund"]),
    ("Måns Bryntessonsgatan 20 D", ["Måns Bryntessonsgatan 20D, Stenungsund", "Måns Bryntessonsgatan, Stenungsund"]),
    ("Torget", ["Torget, Stenungsund"]),  # no number: only one query
])
def test_odd_addresses_are_tidied(address, expected):
    assert geocode.address_queries(address, "Stenungsund") == expected


# --- Geocoder (fake session) -----------------------------------------------

class FakeSession:
    """Answers each search from a dict {query text: answer}; anything else is 'not found'."""

    def __init__(self, answers):
        self.headers = {}
        self.answers = answers
        self.asked = []

    def get(self, url, params=None, timeout=None):
        self.asked.append(params["q"])
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(self.answers.get(params["q"], [])).encode()
        return response


def geocoder_with(answers):
    geocoder = geocode.Geocoder(pause=0)
    geocoder.session = FakeSession(answers)
    return geocoder


FOUND = [{"lat": "57.7", "lon": "11.9"}]


def test_lookup_stops_at_the_first_query_that_finds_something():
    geocoder = geocoder_with({"Storgatan 5, Göteborg": FOUND})
    assert geocoder.lookup("Storgatan 5", "Göteborg") == (57.7, 11.9)
    assert geocoder.session.asked == ["Storgatan 5, Göteborg"]


def test_lookup_falls_back_to_the_street_alone():
    geocoder = geocoder_with({"Spelargången, Partille": FOUND})
    assert geocoder.lookup("Spelargången 4", "Partille") == (57.7, 11.9)
    assert geocoder.session.asked == ["Spelargången 4, Partille", "Spelargången, Partille"]


def test_lookup_returns_none_when_nothing_is_found():
    assert geocoder_with({}).lookup("Nowhere 1", "Göteborg") is None
