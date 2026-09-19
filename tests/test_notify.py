"""Tests for notify.py: settings, the message text, and sending (with a fake Telegram, no network)."""

import json
from datetime import datetime, timezone
from json import dumps

import pytest
import requests

import notify
import store

TOKEN = "123456789:AAsecretsecretsecretsecretsecret12345"


def listing(**changes):
    base = {
        "id": "boplats:1", "source": "boplats", "allocation": "queue", "address": "Storgatan 5", "area": "Majorna",
        "kommun": "Göteborg", "rent_sek": 7200, "size_m2": 52.5, "rooms": 2.0, "floor": 3,
        "deadline": "2026-09-25", "move_in": "2026-11-01", "applicants": 12, "bucket": "likely",
        "bucket_note": "Dina 1 247 dagar mot vinnarnas 1 100 dagar (113 %).",
        "url": "https://boplats.se/objekt/1hand/123",
    }
    return {**base, **changes}


class FakeSession:
    """Stands in for `requests`: remembers what was sent, answers as told."""

    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error, self.sent = status, body if body is not None else {"ok": True}, error, []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        response._content = dumps(self.body).encode()  # `json` is taken: it is the name of post()'s argument
        return response


# --- settings -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_settings(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_credentials_are_read_from_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('HOMEQ_EMAIL=a@b.se\n# a comment\nTELEGRAM_TOKEN="abc:def"\nTELEGRAM_CHAT_ID = 42\n', encoding="utf-8")
    assert notify.load_credentials(env) == ("abc:def", "42")


def test_environment_variables_win_over_the_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_TOKEN=from-file\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_TOKEN", "from-secret")
    assert notify.load_credentials(env) == ("from-secret", "1")


def test_missing_credentials_give_a_clear_error(tmp_path):
    with pytest.raises(notify.NotifyError, match="TELEGRAM_TOKEN"):
        notify.load_credentials(tmp_path / "no-such-file")
    (tmp_path / ".env").write_text("TELEGRAM_TOKEN=x\n", encoding="utf-8")  # chat id missing
    with pytest.raises(notify.NotifyError):
        notify.load_credentials(tmp_path / ".env")


# --- the message -----------------------------------------------------------------------

def test_message_uses_swedish_number_formats_and_shows_the_essentials():
    text = notify.format_message(listing(), ["Min sökning"])
    lines = text.split("\n")
    assert lines[0] == "🏠 Ny annons: Storgatan 5 (Majorna, Göteborg)"
    assert lines[1] == "7 200 kr/mån · 52,5 m² · 2 rum · vån 3"
    assert lines[2].startswith("God chans. Dina 1 247 dagar")
    assert lines[3] == "Sista ansökningsdag: 2026-09-25 · Inflytt: 2026-11-01 · 12 sökande"
    assert lines[4] == "Boplats · Sökning: Min sökning"
    assert lines[5] == "https://boplats.se/objekt/1hand/123"


def test_message_leaves_out_what_we_do_not_know():
    bare = {"source": "homeq", "address": "Kortgatan 1", "bucket": None, "bucket_note": "Chansen kan inte räknas ut."}
    text = notify.format_message(bare, ["A", "B"])
    assert text.split("\n") == [
        "🏠 Ny annons: Kortgatan 1",
        "Okänd chans. Chansen kan inte räknas ut.",
        "HomeQ · Sökning: A, B",
    ]


def test_place_does_not_repeat_the_kommun_and_floor_zero_is_ground_floor():
    text = notify.format_message(listing(area="Göteborg", kommun="Göteborg", floor=0), ["x"])
    lines = text.split("\n")
    assert lines[0] == "🏠 Ny annons: Storgatan 5 (Göteborg)"
    assert lines[1].endswith("bottenvåning")


def test_first_come_listings_get_a_lightning_bolt():
    assert notify.format_message(listing(allocation="first_come"), ["x"]).startswith("⚡")


@pytest.mark.parametrize("value, text", [
    (7200, "7 200"), (9000, "9 000"), (52.5, "52,5"), (2.0, "2"), (1.5, "1,5"), (100, "100"), (20, "20"),
    (1234567, "1 234 567"),
])
def test_number_format(value, text):
    assert notify._number(value) == text


# --- sending ------------------------------------------------------------------------------

def test_send_posts_the_text_to_the_bots_address():
    session = FakeSession()
    notify.send_message("hej", TOKEN, "42", session)
    (url, body), = session.sent
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "42" and body["text"] == "hej"


def test_a_refusal_from_telegram_is_reported_with_its_reason():
    session = FakeSession(status=400, body={"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(notify.NotifyError, match="chat not found"):
        notify.send_message("hej", TOKEN, "42", session)


def test_a_network_error_never_leaks_the_token():
    session = FakeSession(error=requests.ConnectionError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage"))
    with pytest.raises(notify.NotifyError) as caught:
        notify.send_message("hej", TOKEN, "42", session)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__  # nothing chained that could print it


# --- preview ----------------------------------------------------------------------------------

def test_preview_prints_only_matching_listings_and_sends_nothing(tmp_path, capsys):
    rows = [listing(id="boplats:1", address="Match 1"), listing(id="boplats:2", address="Too dear", rent_sek=12000),
            listing(id="boplats:3", address="Match 2")]
    listings_path = tmp_path / "listings.json"
    listings_path.write_text(json.dumps(rows), encoding="utf-8")
    alerts_path = tmp_path / "alerts.json"
    alerts_path.write_text(json.dumps({"searches": [{"name": "S", "max_rent": 9000}]}), encoding="utf-8")

    assert notify.preview(5, listings_path, alerts_path) == 0
    out = capsys.readouterr().out
    assert "Match 1" in out and "Match 2" in out and "Too dear" not in out
    assert "2 listings shown" in out and "Nothing was sent." in out


# --- the daily digest -----------------------------------------------------------------------

def test_digest_lists_best_chance_first_then_soonest_deadline():
    items = [
        (listing(id="a", address="Unknown", bucket=None, deadline="2026-09-21"), ["S"]),
        (listing(id="b", address="Possible late", bucket="possible", deadline="2026-09-30"), ["S"]),
        (listing(id="c", address="Good late", bucket="likely", deadline="2026-09-28"), ["S"]),
        (listing(id="d", address="Good soon", bucket="likely", deadline="2026-09-22"), ["S"]),
        (listing(id="e", address="Possible soon", bucket="possible", deadline="2026-09-23"), ["S"]),
    ]
    ((text, ids),) = notify.digest_messages(items)
    assert ids == ["d", "c", "e", "b", "a"]
    assert text.startswith("📬 Nya annonser (5)\n\n1. Good soon (Majorna, Göteborg)\n")


def test_digest_entry_is_short_and_names_the_search_only_when_asked():
    items = [(listing(), ["Min sökning"])]
    ((plain, _),) = notify.digest_messages(items)
    assert plain.split("\n\n")[1].split("\n") == [
        "1. Storgatan 5 (Majorna, Göteborg)",
        "7 200 kr/mån · 52,5 m² · 2 rum · vån 3",
        "God chans · sista dag 2026-09-25 · Boplats",
        "https://boplats.se/objekt/1hand/123",
    ]
    ((named, _),) = notify.digest_messages(items, show_names=True)
    assert "Boplats · Min sökning" in named


def test_a_long_digest_is_split_and_every_listing_is_in_exactly_one_part():
    items = [(listing(id=f"boplats:{n}", address=f"Street {n}"), ["S"]) for n in range(30)]
    messages = notify.digest_messages(items, max_chars=1000)
    assert len(messages) > 1
    assert all(len(text) < 1100 for text, _ in messages)
    assert "del 1/" in messages[0][0] and f"del {len(messages)}/{len(messages)}" in messages[-1][0]
    assert sorted(one for _, ids in messages for one in ids) == sorted(f"boplats:{n}" for n in range(30))


# --- run_alerts: what gets sent, and when -----------------------------------------------------------

SEARCH = {"name": "S", "max_rent": 9000, "min_size": None, "min_rooms": None, "sources": ("boplats", "homeq"),
          "kommuner": None, "include_first_come": True, "include_lottery": True, "min_chance": None}
DAILY = {"delivery": "daily", "digest_hour": 7}
INSTANT = {"delivery": "instant", "digest_hour": 7}


def at(day, hour, minute=0):
    """A moment in September 2026 at this hour in Swedish time (summer time, UTC+2), as UTC."""
    return datetime(2026, 9, day, hour - 2, minute, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def add(conn, number, **changes):
    """Save a listing and return it (a dict like the exported rows)."""
    row = listing(id=f"boplats:{number}", address=f"Street {number}", **changes)
    store.upsert_listing(conn, row, "2026-09-18T00:00:00+00:00")
    return row


def go(conn, rows, now, settings=DAILY, searches=(SEARCH,), session=None):
    """One run of run_alerts, with each row's alerted_at read from the database like collect.py does."""
    marked = {r["id"]: r["alerted_at"] for r in conn.execute("SELECT id, alerted_at FROM listings")}
    rows = [{**row, "alerted_at": marked[row["id"]]} for row in rows]
    return notify.run_alerts(conn, rows, list(searches), settings, now,
                             session=session or FakeSession(), credentials=("tok", "42"), pause=0)


def test_the_first_run_marks_everything_as_seen_and_sends_nothing(db):
    rows = [add(db, 1), add(db, 2)]
    session = FakeSession()
    summary = go(db, rows, at(19, 9), session=session)
    assert "2 listings marked" in summary and session.sent == []
    assert store.alerts_started(db)


def test_a_new_match_is_sent_in_the_digest_after_the_digest_hour_and_only_once(db):
    old = add(db, 1)
    go(db, [old], at(19, 9))                              # first run: everything so far counts as seen
    rows = [old, add(db, 2), add(db, 3, rent_sek=12000)]  # 2 is new and matches; 3 is new but too dear
    session = FakeSession()
    go(db, rows, at(20, 8), session=session)
    (url, body), = session.sent
    assert "Street 2" in body["text"] and "Street 3" not in body["text"] and "Street 1" not in body["text"]
    assert body["text"].startswith("📬 Nya annonser (1)")
    again = FakeSession()
    assert go(db, rows, at(21, 8), session=again) == "nothing new that matches"
    assert again.sent == []


def test_nothing_is_sent_before_the_digest_hour_but_the_listing_is_kept_for_later(db):
    old = add(db, 1)
    go(db, [old], at(19, 9))
    rows = [old, add(db, 2)]
    early = FakeSession()
    assert "waiting for the 07:00 digest" in go(db, rows, at(20, 6, 30), session=early)
    assert early.sent == []
    later = FakeSession()
    go(db, rows, at(20, 8), session=later)
    assert len(later.sent) == 1


def test_the_digest_hour_is_swedish_time_in_winter_too(db):
    old = add(db, 1)
    go(db, [old], datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc))
    rows = [old, add(db, 2)]
    early = FakeSession()    # 05:59 UTC is 06:59 in Sweden in December (UTC+1)
    go(db, rows, datetime(2026, 12, 2, 5, 59, tzinfo=timezone.utc), session=early)
    assert early.sent == []
    on_time = FakeSession()  # 06:00 UTC is 07:00
    go(db, rows, datetime(2026, 12, 2, 6, 0, tzinfo=timezone.utc), session=on_time)
    assert len(on_time.sent) == 1


def test_at_most_one_digest_a_day_and_later_arrivals_wait_for_tomorrow(db):
    old = add(db, 1)
    go(db, [old], at(19, 9))
    second, third = add(db, 2), add(db, 3)
    go(db, [old, second], at(20, 8))                       # today's digest
    same_day = FakeSession()
    summary = go(db, [old, second, third], at(20, 14), session=same_day)
    assert same_day.sent == [] and "already gone out" in summary
    next_day = FakeSession()
    go(db, [old, second, third], at(21, 8), session=next_day)
    assert "Street 3" in next_day.sent[0][1]["text"]


def test_a_first_run_early_in_the_day_holds_the_first_digest_until_tomorrow(db):
    old = add(db, 1)
    go(db, [old], at(20, 6))                               # first run marks everything at 06:00
    rows = [old, add(db, 2)]
    same_day = FakeSession()
    go(db, rows, at(20, 9), session=same_day)
    assert same_day.sent == []


def test_instant_delivery_sends_one_message_per_new_listing_at_any_hour(db):
    old = add(db, 1)
    go(db, [old], at(19, 9), settings=INSTANT)
    rows = [old, add(db, 2), add(db, 3)]
    session = FakeSession()
    summary = go(db, rows, at(20, 3), settings=INSTANT, session=session)
    assert summary == "sent 2 message(s) about 2 listing(s)"
    assert all(body["text"].startswith("🏠 Ny annons") for _, body in session.sent)


def test_a_failed_send_marks_nothing_so_the_listing_is_tried_again(db):
    old = add(db, 1)
    go(db, [old], at(19, 9))
    rows = [old, add(db, 2)]
    with pytest.raises(notify.NotifyError):
        go(db, rows, at(20, 8), session=FakeSession(status=500, body={"ok": False, "description": "oops"}))
    retry = FakeSession()
    go(db, rows, at(20, 11), session=retry)
    assert len(retry.sent) == 1


def test_when_the_second_message_fails_the_first_stays_marked(db, monkeypatch):
    old = add(db, 1)
    go(db, [old], at(19, 9))
    rows = [old] + [add(db, n) for n in range(2, 5)]
    monkeypatch.setattr(notify, "DIGEST_MAX_CHARS", 1)     # one listing per message

    class FailsSecond(FakeSession):
        def post(self, url, json=None, timeout=None):
            if self.sent:
                raise requests.ConnectionError("down")
            return super().post(url, json=json, timeout=timeout)

    with pytest.raises(notify.NotifyError):
        go(db, rows, at(20, 8), session=FailsSecond())
    assert db.execute("SELECT COUNT(*) FROM listings WHERE alerted_at LIKE '2026-09-20%'").fetchone()[0] == 1


def test_no_searches_means_no_alerts_and_no_marking(db):
    rows = [add(db, 1)]
    assert "no searches" in go(db, rows, at(19, 9), searches=())
    assert not store.alerts_started(db)


def test_two_matching_searches_give_one_entry_naming_both(db):
    old = add(db, 1)
    go(db, [old], at(19, 9))
    rows = [old, add(db, 2)]
    session = FakeSession()
    go(db, rows, at(20, 8), searches=(SEARCH, {**SEARCH, "name": "T"}), session=session)
    (url, body), = session.sent
    assert body["text"].count("Street 2") == 1 and "S, T" in body["text"]
