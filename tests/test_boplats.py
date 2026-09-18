"""Parser tests for the Boplats collector, run against saved pages in tests/samples/.

If boplats.se changes its layout, these fail here instead of in the scheduled run.
"""

from datetime import date
from pathlib import Path

import pytest

from collectors import boplats

SAMPLES = Path(__file__).parent / "samples"
TODAY = date(2026, 9, 18)  # the day the samples were saved

STENUNGSUND_ID = "6AAD0824387D1325AB54CBBC"  # boplats_detail.html
GOTEBORG_ID = "6AAC10DB07B8C47623DFC382"  # boplats_detail_goteborg.html


def sample(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cards():
    return boplats.parse_search_page(sample("boplats_search.html"), TODAY)


# --- search page -----------------------------------------------------------

def test_finds_every_card_once(cards):
    assert len(cards) == 107
    assert len({card["id"] for card in cards}) == 107


def test_first_card_has_all_fields(cards):
    assert cards[0] == {
        "id": f"boplats:{STENUNGSUND_ID}",
        "source": "boplats",
        "url": f"https://boplats.se/objekt/1hand/{STENUNGSUND_ID}",
        "area": "Hasselbacken",
        "address": "Hasselbackevägen 4",
        "rent_sek": 7648,
        "size_m2": 67.0,
        "rooms": 2.0,
        "floor": 2,
        "published": "2026-09-18",
    }


def test_every_card_has_the_core_fields(cards):
    for card in cards:
        for field in ("area", "address", "rent_sek", "size_m2", "rooms", "published", "url"):
            assert card[field] not in (None, ""), f"{card['id']} is missing {field}"


def test_goteborg_card(cards):
    card = next(c for c in cards if c["id"] == f"boplats:{GOTEBORG_ID}")
    assert (card["area"], card["size_m2"], card["rooms"], card["floor"]) == ("Gamlestaden", 48.0, 2.0, 3)


def test_basement_floor_is_negative(cards):
    card = next(c for c in cards if c["address"] == "Norumshöjd 31")
    assert card["floor"] == -1


def test_blank_floor_is_none(cards):
    assert sum(card["floor"] is None for card in cards) == 8


@pytest.mark.parametrize("raw, expected", [
    ("Distansgatan 66 66", "Distansgatan 66"),
    ("Kulvertkonstens väg 9 1004", "Kulvertkonstens väg 9"),
    ("Lillhagsvinkeln 25 1508", "Lillhagsvinkeln 25"),
    ("Lars Kaggsgatan 14A", "Lars Kaggsgatan 14A"),  # house letter stays
    ("Distansgatan 66-68", "Distansgatan 66-68"),  # range stays
    ("Burggrevegatan 27 B", "Burggrevegatan 27 B"),  # letter after a space stays
    ("Hasselbackevägen 4", "Hasselbackevägen 4"),
])
def test_clean_address(raw, expected):
    assert boplats.clean_address(raw) == expected


def test_search_page_addresses_are_cleaned(cards):
    by_id = {card["id"]: card["address"] for card in cards}
    assert by_id["boplats:6AA3B560076B787CB831B08B"] == "Distansgatan 66"
    assert by_id["boplats:6AA24481FDCC65C0A24DB08B"] == "Distansgatan 66-68"
    assert not [a for a in by_id.values() if boplats.clean_address(a) != a]  # nothing left to clean


def test_empty_page_gives_no_cards():
    assert boplats.parse_search_page("<html><body></body></html>", TODAY) == []


# --- dates -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Publ. idag", date(2026, 9, 18)),
    ("Publ. igår", date(2026, 9, 17)),
    ("Publ. i förrgår", date(2026, 9, 16)),
    ("Publ. 2026-09-12", date(2026, 9, 12)),
    ("Publ. någon gång", None),
])
def test_publication_date(text, expected):
    assert boplats._publication_date(text, TODAY) == expected


@pytest.mark.parametrize("text, today, expected", [
    ("24 september", date(2026, 9, 18), date(2026, 9, 24)),
    ("1 jan. 2027", date(2026, 9, 18), date(2027, 1, 1)),
    ("5 januari", date(2026, 12, 20), date(2027, 1, 5)),  # no year: next occurrence
    ("17 september", date(2026, 9, 18), date(2026, 9, 17)),  # just closed, not next year
    ("Omgående", date(2026, 9, 18), None),
    ("", date(2026, 9, 18), None),
])
def test_swedish_date(text, today, expected):
    assert boplats._swedish_date(text, today) == expected


# --- detail pages ----------------------------------------------------------

def test_detail_page_stenungsund():
    assert boplats.parse_detail_page(sample("boplats_detail.html"), TODAY) == {
        "kommun": "Stenungsund",
        "move_in": "2027-01-01",
        "deadline": "2026-09-24",
        "applicants": 6,
        "landlord": "Stenungsundshem",
        "allocation": "queue",
    }


def test_detail_page_goteborg():
    assert boplats.parse_detail_page(sample("boplats_detail_goteborg.html"), TODAY) == {
        "kommun": "Göteborg",
        "move_in": "2026-12-01",
        "deadline": "2026-09-20",
        "applicants": 49,
        "landlord": "Bostads AB Poseidon",
        "allocation": "queue",
    }


# --- queue time of similar flats -------------------------------------------

def test_area_stats_gives_days():
    assert boplats.parse_area_stats(sample("boplats_area_stats.json")) == 1269


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"successData": {}}'])
def test_area_stats_bad_input_gives_none(text):
    assert boplats.parse_area_stats(text) is None


# --- collect(): which pages get fetched ------------------------------------

class FakeFetcher:
    """Stands in for PoliteFetcher: serves the saved pages and records what was asked for."""

    requested = []
    fail_on = None

    def __init__(self, pause):
        pass

    def get(self, url):
        FakeFetcher.requested.append(url)
        if url == FakeFetcher.fail_on:
            raise boplats.requests.ConnectionError("boom")
        if url == boplats.SEARCH_URL:
            return sample("boplats_search.html")
        if url.endswith(f"/objekt/1hand/{STENUNGSUND_ID}"):
            return sample("boplats_detail.html")
        if url.endswith(f"/area_statistics/1hand/{STENUNGSUND_ID}"):
            return sample("boplats_area_stats.json")
        raise AssertionError(f"unexpected request: {url}")


@pytest.fixture
def fake_site(monkeypatch, cards):
    FakeFetcher.requested = []
    FakeFetcher.fail_on = None
    monkeypatch.setattr(boplats, "PoliteFetcher", FakeFetcher)
    return {card["id"] for card in cards}


def test_collect_fetches_details_only_for_new_listings(fake_site):
    known = fake_site - {f"boplats:{STENUNGSUND_ID}"}
    result = boplats.collect(known, today=TODAY, pause=0)

    assert len(FakeFetcher.requested) == 3  # search + one detail + one stats
    assert len(result) == 107
    new = next(item for item in result if item["id"] == f"boplats:{STENUNGSUND_ID}")
    assert new["winners_queue_days"] == 1269
    assert new["applicants"] == 6
    assert new["landlord"] == "Stenungsundshem"
    old = next(item for item in result if item["id"] != new["id"])
    assert "landlord" not in old


def test_collect_with_everything_known_only_fetches_search_page(fake_site):
    boplats.collect(fake_site, today=TODAY, pause=0)
    assert FakeFetcher.requested == [boplats.SEARCH_URL]


def test_collect_leaves_out_a_listing_whose_detail_fetch_fails(fake_site):
    FakeFetcher.fail_on = f"https://boplats.se/objekt/1hand/{STENUNGSUND_ID}"
    known = fake_site - {f"boplats:{STENUNGSUND_ID}"}
    result = boplats.collect(known, today=TODAY, pause=0)
    assert len(result) == 106
    assert f"boplats:{STENUNGSUND_ID}" not in {item["id"] for item in result}
