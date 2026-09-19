"""Tests for the database layer and the JSON export, using a throwaway database."""

import json
from datetime import date
from pathlib import Path

import pytest

import collect
import store
from collectors import boplats

SAMPLES = Path(__file__).parent / "samples"
RUN_1 = "2026-09-18T10:00:00+00:00"
RUN_2 = "2026-09-18T13:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def cards():
    html = (SAMPLES / "boplats_search.html").read_text(encoding="utf-8")
    return boplats.parse_search_page(html, date(2026, 9, 18))


def run(conn, listings, now):
    """The same steps collect.main() does after fetching."""
    with conn:
        new = sum(store.upsert_listing(conn, item, now) for item in listings)
        for item in listings:
            store.snapshot(conn, item["id"], now, applicants=item.get("applicants"))
        closed = store.close_unseen(conn, "boplats", now)
    return new, closed


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_running_twice_adds_no_duplicate_listings(conn, cards):
    assert run(conn, cards, RUN_1) == (107, 0)
    assert run(conn, cards, RUN_2) == (0, 0)
    assert count(conn, "listings") == 107
    assert count(conn, "snapshots") == 214  # one row per listing per run


def test_seen_again_updates_last_seen_but_keeps_details(conn):
    detailed = {"id": "boplats:A", "source": "boplats", "landlord": "Poseidon", "rent_sek": 5000}
    store.upsert_listing(conn, detailed, RUN_1)
    store.upsert_listing(conn, {"id": "boplats:A", "source": "boplats", "rent_sek": 5000}, RUN_2)
    row = conn.execute("SELECT * FROM listings WHERE id = 'boplats:A'").fetchone()
    assert (row["first_seen"], row["last_seen"], row["landlord"]) == (RUN_1, RUN_2, "Poseidon")


def test_listing_that_disappears_is_closed_and_can_return(conn):
    a = {"id": "boplats:A", "source": "boplats"}
    b = {"id": "boplats:B", "source": "boplats"}
    run(conn, [a, b], RUN_1)
    assert run(conn, [a], RUN_2) == (0, 1)
    assert [row["id"] for row in store.export_rows(conn)] == ["boplats:A"]
    run(conn, [a, b], "2026-09-18T16:00:00+00:00")
    assert len(store.export_rows(conn)) == 2


def test_export_uses_latest_known_applicant_count(conn):
    item = {"id": "boplats:A", "source": "boplats", "applicants": 6}
    run(conn, [item], RUN_1)
    run(conn, [{"id": "boplats:A", "source": "boplats"}], RUN_2)  # not re-fetched: no count
    assert store.export_rows(conn)[0]["applicants"] == 6


def test_export_json_keeps_swedish_letters(conn, tmp_path):
    store.upsert_listing(conn, {"id": "boplats:A", "source": "boplats", "address": "Hasselbackevägen 4"}, RUN_1)
    out = tmp_path / "site" / "listings.json"
    assert collect.export_json(conn, out) == 1
    assert json.loads(out.read_text(encoding="utf-8"))[0]["address"] == "Hasselbackevägen 4"


def test_short_lease_flag_is_saved_as_yes_no_or_unknown(conn):
    store.upsert_listing(conn, {"id": "homeq:1", "source": "homeq", "is_short_lease": True}, RUN_1)
    store.upsert_listing(conn, {"id": "homeq:2", "source": "homeq", "is_short_lease": False}, RUN_1)
    store.upsert_listing(conn, {"id": "boplats:3", "source": "boplats"}, RUN_1)  # Boplats: unknown
    flags = {r["id"]: r["is_short_lease"] for r in conn.execute("SELECT id, is_short_lease FROM listings")}
    assert flags == {"homeq:1": 1, "homeq:2": 0, "boplats:3": None}


def test_old_database_without_the_column_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA.replace("is_short_lease     INTEGER,", ""))
    old.execute("INSERT INTO listings (id, source, first_seen, last_seen) VALUES ('boplats:A', 'boplats', 'x', 'x')")
    old.commit()
    old.close()

    conn = store.connect(path)  # must not fail, and must keep the existing row
    row = conn.execute("SELECT id, is_short_lease FROM listings").fetchone()
    assert (row["id"], row["is_short_lease"]) == ("boplats:A", None)
    conn.close()


def test_seen_again_fills_in_empty_fields_but_never_overwrites(conn):
    store.upsert_listing(conn, {"id": "homeq:A", "source": "homeq", "rent_sek": 5000}, RUN_1)
    store.upsert_listing(
        conn, {"id": "homeq:A", "source": "homeq", "rent_sek": 9999, "allocation": "lottery", "floor": 3}, RUN_2)
    row = conn.execute("SELECT * FROM listings WHERE id = 'homeq:A'").fetchone()
    assert (row["rent_sek"], row["allocation"], row["floor"]) == (5000, "lottery", 3)  # gaps filled, rent kept


def test_ids_with_allocation_lists_only_listings_whose_page_was_read(conn):
    store.upsert_listing(conn, {"id": "homeq:1", "source": "homeq", "allocation": "unknown"}, RUN_1)
    store.upsert_listing(conn, {"id": "homeq:2", "source": "homeq"}, RUN_1)
    store.upsert_listing(conn, {"id": "boplats:3", "source": "boplats", "allocation": "queue"}, RUN_1)
    assert store.ids_with_allocation(conn, "homeq") == {"homeq:1"}
