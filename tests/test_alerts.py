"""Tests for alerts.py: reading alerts.json and deciding which listings match."""

import json

import pytest

import alerts


def search(**changes):
    """A search as load_alerts returns it: the criteria you asked for, with any changes on top."""
    base = {
        "name": "Test", "max_rent": 9000, "min_size": 20, "min_rooms": 1,
        "sources": ("boplats", "homeq"), "kommuner": ["Göteborg", "Mölndal"],
        "include_first_come": True, "include_lottery": True, "min_chance": "possible",
    }
    return {**base, **changes}


def listing(**changes):
    base = {"source": "boplats", "allocation": "queue", "rent_sek": 8000, "size_m2": 35.0,
            "rooms": 1.5, "kommun": "Göteborg", "bucket": "likely"}
    return {**base, **changes}


def write_alerts(tmp_path, data):
    path = tmp_path / "alerts.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# --- your real alerts.json -----------------------------------------------------

def test_the_real_alerts_json_is_valid_and_holds_the_starting_criteria():
    (mine,) = alerts.load_alerts()
    assert (mine["max_rent"], mine["min_size"], mine["min_rooms"]) == (9000, 20, 1)
    assert mine["kommuner"] == ["Göteborg", "Mölndal"]
    assert mine["min_chance"] == "possible"


# --- reading the file ------------------------------------------------------------

def test_a_search_with_only_a_name_gets_no_limits(tmp_path):
    (only,) = alerts.load_alerts(write_alerts(tmp_path, {"searches": [{"name": "Allt"}]}))
    assert only["max_rent"] is None and only["kommuner"] is None and only["min_chance"] is None
    assert only["sources"] == ("boplats", "homeq")
    assert only["include_first_come"] and only["include_lottery"]


def test_a_missing_file_or_no_searches_means_no_alerts(tmp_path):
    assert alerts.load_alerts(tmp_path / "nope.json") == []
    assert alerts.load_alerts(write_alerts(tmp_path, {"searches": []})) == []


def test_min_chance_words_map_to_score_buckets(tmp_path):
    def chance(word):
        path = write_alerts(tmp_path, {"searches": [{"name": "x", "min_chance": word}]})
        return alerts.load_alerts(path)[0]["min_chance"]

    assert chance("god chans") == "likely"
    assert chance(" Möjlig ") == "possible"
    assert chance(None) is None


@pytest.mark.parametrize("bad", [
    {"name": "x", "min_chance": "låg chans"},      # not one of the two allowed words
    {"name": "x", "min_chance": "likely"},         # English word, not what the file uses
    {"name": "x", "max_rnt": 9000},                # typo in a setting name
    {"name": "x", "max_rent": "9000"},             # number written as text
    {"name": "x", "max_rent": True},
    {"name": "x", "sources": ["boplats", "blocket"]},
    {"name": "x", "kommuner": "Göteborg"},         # must be a list
    {"name": "x", "include_lottery": "yes"},       # must be true / false
    {"name": ""},
    {"max_rent": 9000},                            # no name
    "just text",
])
def test_bad_searches_give_a_clear_error(tmp_path, bad):
    with pytest.raises(alerts.AlertsError):
        alerts.load_alerts(write_alerts(tmp_path, {"searches": [bad]}))


@pytest.mark.parametrize("text", ["not json", "[]", '{"searches": "x"}'])
def test_bad_files_give_a_clear_error(tmp_path, text):
    with pytest.raises(alerts.AlertsError):
        alerts.load_alerts(write_alerts(tmp_path, text))


# --- matching ------------------------------------------------------------------------

def test_a_listing_inside_all_limits_matches():
    assert alerts.matches(listing(), search())


@pytest.mark.parametrize("changes", [
    {"rent_sek": 9001},
    {"size_m2": 19.5},
    {"rooms": 0.5},
    {"kommun": "Partille"},
    {"source": "blocket"},                         # a source the search does not list
])
def test_a_listing_outside_a_limit_does_not_match(changes):
    assert not alerts.matches(listing(**changes), search())


def test_the_limits_themselves_are_included():
    assert alerts.matches(listing(rent_sek=9000, size_m2=20, rooms=1), search())


def test_unknown_values_never_rule_a_listing_out():
    unknown = listing(rent_sek=None, size_m2=None, rooms=None, kommun=None)
    assert alerts.matches(unknown, search())


def test_kommun_ignores_upper_and_lower_case():
    assert alerts.matches(listing(kommun="MÖLNDAL"), search())


def test_a_null_limit_means_no_limit():
    open_search = search(max_rent=None, min_size=None, min_rooms=None, kommuner=None, min_chance=None)
    assert alerts.matches(listing(rent_sek=20000, size_m2=5, rooms=0.5, kommun="Kiruna", bucket="unlikely"), open_search)


def test_sources_can_be_limited():
    only_homeq = search(sources=("homeq",))
    assert not alerts.matches(listing(source="boplats"), only_homeq)
    assert alerts.matches(listing(source="homeq"), only_homeq)


def test_first_come_and_lottery_can_be_switched_off():
    assert not alerts.matches(listing(allocation="first_come"), search(include_first_come=False))
    assert not alerts.matches(listing(allocation="lottery"), search(include_lottery=False))
    assert alerts.matches(listing(allocation="first_come"), search(include_lottery=False))


# --- chance ------------------------------------------------------------------------------

@pytest.mark.parametrize("min_chance, bucket, expected", [
    ("possible", "likely", True),
    ("possible", "possible", True),
    ("possible", "unlikely", False),
    ("likely", "likely", True),
    ("likely", "possible", False),
    ("likely", "unlikely", False),
    (None, "unlikely", True),
])
def test_min_chance_keeps_that_bucket_and_better(min_chance, bucket, expected):
    assert alerts.matches(listing(bucket=bucket), search(min_chance=min_chance)) is expected


def test_a_listing_with_no_chance_bucket_still_matches():
    assert alerts.matches(listing(bucket=None), search(min_chance="likely"))
