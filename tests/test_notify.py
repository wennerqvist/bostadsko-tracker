"""Tests for notify.py: settings, the message text, and sending (with a fake Telegram, no network)."""

import json
from json import dumps

import pytest
import requests

import notify

TOKEN = "123456789:AAsecretsecretsecretsecretsecret12345"


def listing(**changes):
    base = {
        "source": "boplats", "allocation": "queue", "address": "Storgatan 5", "area": "Majorna",
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
    rows = [listing(address="Match 1"), listing(address="Too dear", rent_sek=12000), listing(address="Match 2")]
    listings_path = tmp_path / "listings.json"
    listings_path.write_text(json.dumps(rows), encoding="utf-8")
    alerts_path = tmp_path / "alerts.json"
    alerts_path.write_text(json.dumps({"searches": [{"name": "S", "max_rent": 9000}]}), encoding="utf-8")

    assert notify.preview(5, listings_path, alerts_path) == 0
    out = capsys.readouterr().out
    assert "Match 1" in out and "Match 2" in out and "Too dear" not in out
    assert "2 shown. Nothing was sent." in out
