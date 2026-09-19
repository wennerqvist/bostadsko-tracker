"""Tests for collect.main(): both sources are saved, and one failing does not hurt the other.
Fake collectors and a throwaway database are used, so nothing touches the internet."""

import json

import pytest

import collect
import store
from collectors import boplats, homeq


def listing(source, number, **extra):
    return {"id": f"{source}:{number}", "source": source, "address": f"Street {number}", **extra}


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Runs collect.main() with the given fake results; returns (exit code, rows in the JSON export)."""
    runs = iter(range(1, 100))  # each run gets its own timestamp, like runs hours apart

    def go(boplats_result, homeq_result):
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
        code = collect.main([], db_path=tmp_path / "test.db", export_path=export)
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
