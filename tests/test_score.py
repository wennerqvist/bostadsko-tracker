"""Tests for score.py: queue days and the likely / possible / unlikely rules from PLAN.md."""

from datetime import date

import pytest

import score

TODAY = date(2026, 9, 19)


def boplats(winners=1000, applicants=None, deadline=None, **extra):
    return {"source": "boplats", "allocation": "queue", "winners_queue_days": winners,
            "applicants": applicants, "deadline": deadline, **extra}


def homeq(allocation="queue", needed=None, **extra):
    return {"source": "homeq", "allocation": allocation, "points_needed_top10": needed, **extra}


def bucket(listing, boplats_days=1000, homeq_days=1000):
    return score.score_listing(listing, {"boplats": boplats_days, "homeq": homeq_days}, TODAY)[0]


# --- your dates ----------------------------------------------------------------

def test_queue_days_counts_one_per_day_since_signing_up():
    assert score.queue_days(date(2023, 4, 21), date(2023, 4, 21)) == 0
    assert score.queue_days(date(2023, 4, 21), date(2026, 9, 19)) == 1247
    assert score.queue_days(None, TODAY) is None


def write_me(tmp_path, text):
    path = tmp_path / "me.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_me_reads_both_dates(tmp_path):
    me = score.load_me(write_me(tmp_path, '{"boplats_registered": "2023-04-21", "homeq_verified": "2024-01-02"}'), TODAY)
    assert me == {"boplats_registered": date(2023, 4, 21), "homeq_verified": date(2024, 1, 2)}


def test_load_me_accepts_null_and_a_missing_file(tmp_path):
    empty = {"boplats_registered": None, "homeq_verified": None}
    assert score.load_me(write_me(tmp_path, '{"boplats_registered": null}'), TODAY) == empty
    assert score.load_me(tmp_path / "does-not-exist.json", TODAY) == empty


@pytest.mark.parametrize("text", [
    '{"boplats_registered": "21/4/2023"}',   # wrong format
    '{"boplats_registered": 20230421}',      # not text
    '{"homeq_verified": "2030-01-01"}',      # in the future
    "not json at all",
    "[]",
])
def test_load_me_rejects_bad_content_with_a_clear_error(tmp_path, text):
    with pytest.raises(score.MeError):
        score.load_me(write_me(tmp_path, text), TODAY)


def test_queue_json_has_iso_dates_or_null():
    me = {"boplats_registered": date(2023, 4, 21), "homeq_verified": None}
    assert score.queue_json(me) == {"boplats_start": "2023-04-21", "homeq_start": None}


# --- Boplats -------------------------------------------------------------------

@pytest.mark.parametrize("my_days, expected", [
    (1100, "likely"),      # exactly 110 %
    (1099, "possible"),
    (800, "possible"),     # exactly 80 %
    (799, "unlikely"),
    (5000, "likely"),
    (0, "unlikely"),
])
def test_boplats_bucket_from_your_days_against_the_winners_average(my_days, expected):
    assert bucket(boplats(winners=1000), boplats_days=my_days) == expected


def test_boplats_without_the_winners_figure_has_no_bucket_and_says_why():
    result = score.score_listing(boplats(winners=None), {"boplats": 1000}, TODAY)
    assert result[0] is None and "kötid" in result[1]


def test_few_applicants_close_to_the_deadline_bump_up_one_step():
    listing = boplats(applicants=5, deadline="2026-09-22")  # 3 days left
    assert bucket(listing, boplats_days=900) == "likely"    # would be "possible"


def test_few_applicants_far_from_the_deadline_change_nothing():
    listing = boplats(applicants=5, deadline="2026-09-23")  # 4 days left
    assert bucket(listing, boplats_days=900) == "possible"


def test_few_applicants_but_no_known_deadline_change_nothing():
    assert bucket(boplats(applicants=0, deadline=None), boplats_days=900) == "possible"


def test_a_bad_deadline_does_not_crash_the_scoring():
    assert bucket(boplats(applicants=0, deadline="soon"), boplats_days=900) == "possible"


def test_fifty_applicants_or_more_bump_down_one_step():
    assert bucket(boplats(applicants=50), boplats_days=900) == "unlikely"   # would be "possible"
    assert bucket(boplats(applicants=49), boplats_days=900) == "possible"


def test_bumps_stop_at_the_ends():
    assert bucket(boplats(applicants=99), boplats_days=100) == "unlikely"
    assert bucket(boplats(applicants=1, deadline="2026-09-20"), boplats_days=5000) == "likely"


def test_the_note_gives_the_numbers_in_swedish_format():
    note = score.score_listing(boplats(winners=1152), {"boplats": 1247}, TODAY)[1]
    assert note.startswith("Dina 1 247 dagar mot vinnarnas 1 152 dagar (108 %).")


# --- HomeQ ---------------------------------------------------------------------

def test_homeq_queue_without_points_needed_has_no_bucket_and_says_why():
    result = score.score_listing(homeq(needed=None), {"homeq": 1000}, TODAY)
    assert result[0] is None and "poäng" in result[1]


@pytest.mark.parametrize("my_points, expected", [
    (1000, "likely"),      # exactly what is needed
    (900, "possible"),     # exactly 10 % below
    (899, "unlikely"),
])
def test_homeq_strict_queue_against_points_needed_for_top_10(my_points, expected):
    assert bucket(homeq(needed=1000), homeq_days=my_points) == expected


def test_homeq_points_as_guidance_is_one_step_lower():
    assert bucket(homeq("queue_guidance", needed=1000), homeq_days=1000) == "possible"
    assert bucket(homeq("queue_guidance", needed=1000), homeq_days=900) == "unlikely"


def test_homeq_first_come_ignores_your_points():
    assert bucket(homeq("first_come"), homeq_days=0) == "possible"


def test_homeq_lottery_is_possible_and_writes_the_odds_when_applicants_are_known():
    assert bucket(homeq("lottery"), homeq_days=0) == "possible"
    assert "1 chans på 40" in score.score_listing(homeq("lottery", applicants=40), {}, TODAY)[1]
    assert "1 chans" not in score.score_listing(homeq("lottery"), {}, TODAY)[1]


def test_landlord_points_and_unknown_allocation_have_no_bucket():
    assert bucket(homeq("points_landlord")) is None
    assert bucket(homeq("unknown", needed=1)) is None
    assert bucket({"source": "homeq", "allocation": None}) is None


# --- missing queue date ----------------------------------------------------------

def test_a_missing_queue_date_means_no_bucket_for_queue_listings_only():
    result = score.score_listing(boplats(), {"boplats": None}, TODAY)
    assert result[0] is None and "me.local.json" in result[1]
    assert score.score_listing(homeq("lottery"), {"homeq": None}, TODAY)[0] == "possible"


def test_add_scores_adds_bucket_and_note_to_every_row():
    rows = [boplats(winners=1000), homeq("first_come")]
    me = {"boplats_registered": date(2023, 4, 21), "homeq_verified": None}
    score.add_scores(rows, me, TODAY)
    assert [r["bucket"] for r in rows] == ["likely", "possible"]
    assert all(r["bucket_note"] for r in rows)
