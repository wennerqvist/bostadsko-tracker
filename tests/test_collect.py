"""Tests for collect.main(): both sources are saved, and one failing does not hurt the other.
Fake collectors and a throwaway database are used, so nothing touches the internet."""

import json

import pytest
import requests

import collect
import store
from collectors import boplats, homeq


class FakeGeocoder:
    """Knows a few addresses; records every lookup so tests can see what was asked."""

    def __init__(self, known=None, error=None):
        self.known, self.error, self.asked = known or {}, error, []

    def lookup(self, address, kommun):
        self.asked.append(address)
        if self.error:
            raise self.error
        return self.known.get(address)


def listing(source, number, **extra):
    return {"id": f"{source}:{number}", "source": source, "address": f"Street {number}", **extra}


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Runs collect.main() with the given fake results; returns (exit code, rows in the JSON export)."""
    runs = iter(range(1, 100))  # each run gets its own timestamp, like runs hours apart

    def go(boplats_result, homeq_result, geocoder=None, me_path=None):
        monkeypatch.setattr(store, "now_iso", lambda: f"2026-09-19T{next(runs):02d}:00:00+00:00")

        def fake(result):
            def collector(*args, **kwargs):
                if isinstance(result, Exception):
                    raise result
                return result
            return collector

        monkeypatch.setattr(boplats, "collect", fake(boplats_result))
        monkeypatch.setattr(homeq, "collect", fake(homeq_result))
        export = tmp_path / "listings.json"
        code = collect.main([], db_path=tmp_path / "test.db", export_path=export,
                            geocoder=geocoder or FakeGeocoder(), me_path=me_path or tmp_path / "no-me.json")
        return code, json.loads(export.read_text(encoding="utf-8"))
    return go


def test_both_sources_are_saved_and_exported(run):
    code, rows = run([listing("boplats", 1)], [listing("homeq", 1, is_short_lease=True)])
    assert code == 0
    assert {row["id"] for row in rows} == {"boplats:1", "homeq:1"}
    assert {row["id"]: row["is_short_lease"] for row in rows}["homeq:1"] == 1


def test_homeq_failing_keeps_boplats_and_its_own_old_listings(run):
    run([listing("boplats", 1)], [listing("homeq", 1)])  # first run: both fine

    code, rows = run([listing("boplats", 1), listing("boplats", 2)], homeq.HomeQError("login refused"))
    assert code == 1  # the failure is visible to whoever runs this
    assert {row["id"] for row in rows} == {"boplats:1", "boplats:2", "homeq:1"}  # homeq:1 not closed


def test_boplats_failing_keeps_homeq(run):
    code, rows = run(boplats.PageChanged("layout changed"), [listing("homeq", 1)])
    assert code == 1
    assert {row["id"] for row in rows} == {"homeq:1"}


def test_a_listing_that_disappears_from_a_working_source_is_closed(run):
    run([listing("boplats", 1)], [listing("homeq", 1), listing("homeq", 2)])
    code, rows = run([listing("boplats", 1)], [listing("homeq", 1)])
    assert code == 0
    assert {row["id"] for row in rows} == {"boplats:1", "homeq:1"}


def test_homeq_gets_no_snapshot_rows_but_boplats_does(tmp_path, run):
    run([listing("boplats", 1)], [listing("homeq", 1)])
    conn = store.connect(tmp_path / "test.db")
    ids = [row["listing_id"] for row in conn.execute("SELECT listing_id FROM snapshots")]
    conn.close()
    assert ids == ["boplats:1"]


# --- coordinates -----------------------------------------------------------

def boplats_listing(number):
    return listing("boplats", number, address=f"Street {number}", kommun="Göteborg")


def test_coordinates_are_found_saved_and_exported(run):
    geocoder = FakeGeocoder({"Street 1": (57.7, 11.9)})
    code, rows = run([boplats_listing(1), boplats_listing(2)], [], geocoder)
    by_id = {row["id"]: row for row in rows}
    assert (by_id["boplats:1"]["lat"], by_id["boplats:1"]["lon"]) == (57.7, 11.9)
    assert by_id["boplats:2"]["lat"] is None  # not found: stays empty, run still succeeds
    assert code == 0


def test_an_address_that_has_coordinates_is_never_looked_up_again(run):
    geocoder = FakeGeocoder({"Street 1": (57.7, 11.9)})
    run([boplats_listing(1)], [], geocoder)
    run([boplats_listing(1)], [], geocoder)
    assert geocoder.asked == ["Street 1"]  # only in the first run


def test_homeq_listings_that_already_have_coordinates_are_not_looked_up(run):
    geocoder = FakeGeocoder()
    run([], [listing("homeq", 1, address="Street 1", kommun="Göteborg", lat=57.7, lon=11.9)], geocoder)
    assert geocoder.asked == []


def test_lookup_service_failing_stops_lookups_but_keeps_everything_else(run):
    geocoder = FakeGeocoder(error=requests.ConnectionError("no network"))
    code, rows = run([boplats_listing(1), boplats_listing(2)], [], geocoder)
    assert geocoder.asked == ["Street 1"]  # gave up after the first failure
    assert code == 0
    assert {row["id"] for row in rows} == {"boplats:1", "boplats:2"}


# --- queue dates and chance score --------------------------------------------

def test_export_has_a_chance_bucket_and_a_queue_file(tmp_path, run):
    me = tmp_path / "me.json"
    me.write_text('{"boplats_registered": "2000-01-01", "homeq_verified": "2000-01-01"}', encoding="utf-8")
    code, rows = run([listing("boplats", 1, allocation="queue", winners_queue_days=1000)], [], me_path=me)
    assert code == 0
    assert rows[0]["bucket"] == "likely"  # about 9 000 days against 1 000
    queue = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert queue == {"boplats_start": "2000-01-01", "homeq_start": "2000-01-01"}


def test_without_queue_dates_listings_are_exported_but_not_scored(run):
    code, rows = run([listing("boplats", 1, allocation="queue", winners_queue_days=1000)], [])
    assert code == 0
    assert rows[0]["bucket"] is None


def test_a_broken_dates_file_stops_the_run_before_fetching(tmp_path, monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("fetched although the dates file is broken")
    monkeypatch.setattr(boplats, "collect", must_not_be_called)
    monkeypatch.setattr(homeq, "collect", must_not_be_called)
    me = tmp_path / "me.json"
    me.write_text('{"boplats_registered": "21/4/2023"}', encoding="utf-8")

    code = collect.main([], db_path=tmp_path / "test.db", export_path=tmp_path / "listings.json", me_path=me)
    assert code == 1
    assert not (tmp_path / "listings.json").exists()


# --- HomeQ points readings ----------------------------------------------------

def test_a_points_reading_is_saved_exported_and_scored(tmp_path, run):
    me = tmp_path / "me.json"
    me.write_text('{"boplats_registered": "2000-01-01", "homeq_verified": "2000-01-01"}', encoding="utf-8")
    read = listing("homeq", 1, allocation="queue", insight_frame="queue_points_info", points_needed_top10=2546)
    unread = listing("homeq", 2, allocation="queue")  # not read this run: no snapshot row
    code, rows = run([], [read, unread], me_path=me)
    by_id = {row["id"]: row for row in rows}
    assert by_id["homeq:1"]["points_needed_top10"] == 2546 and by_id["homeq:1"]["bucket"] == "likely"
    assert by_id["homeq:2"]["points_needed_top10"] is None and by_id["homeq:2"]["bucket"] is None

    conn = store.connect(tmp_path / "test.db")
    saved = [(r["listing_id"], r["frame"]) for r in conn.execute("SELECT listing_id, frame FROM snapshots")]
    conn.close()
    assert saved == [("homeq:1", "queue_points_info")]


def test_a_reading_without_a_figure_is_saved_so_it_is_not_asked_again_at_once(tmp_path, run):
    run([], [listing("homeq", 1, insight_frame="first_to_apply")])
    conn = store.connect(tmp_path / "test.db")
    assert store.latest_insight_times(conn, "homeq") == {"homeq:1": "2026-09-19T01:00:00+00:00"}
    conn.close()


def test_the_collector_is_told_what_was_read_recently_and_where_the_cutoff_is(tmp_path, run, monkeypatch):
    run([], [listing("homeq", 1, insight_frame="first_to_apply")])  # run 1, at 01:00
    asked = {}

    def spy(*args, **kwargs):
        asked.update(kwargs)
        return [listing("homeq", 1)]

    monkeypatch.setattr(homeq, "collect", spy)
    collect.main([], db_path=tmp_path / "test.db", export_path=tmp_path / "listings.json",
                 geocoder=FakeGeocoder(), me_path=tmp_path / "no-me.json")  # run 2, at 02:00
    assert asked["last_insight"] == {"homeq:1": "2026-09-19T01:00:00+00:00"}
    assert asked["insight_cutoff"] == "2026-09-18T06:00:00+00:00"  # 20 hours before this run
