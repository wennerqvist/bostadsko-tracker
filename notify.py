"""Telegram messages: turn listings into short Swedish texts and send them.

Usage:
    python notify.py preview [N]   print what the alert would look like for N matching listings, sends nothing
    python notify.py test          send one greeting, to check that the bot works
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import alerts
import store

ENV_PATH = Path(__file__).parent / ".env"
LISTINGS_PATH = Path(__file__).parent / "site" / "listings.json"
SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

SOURCE_NAMES = {"boplats": "Boplats", "homeq": "HomeQ"}
CHANCE_LABELS = {"likely": "God chans", "possible": "Möjlig", "unlikely": "Låg chans"}  # same words as the map
CHANCE_ORDER = {"likely": 0, "possible": 1, "unlikely": 3}  # anything else (unknown) sorts in between, at 2

STOCKHOLM = ZoneInfo("Europe/Stockholm")  # the digest hour in alerts.json is Swedish time, summer and winter
DIGEST_MAX_CHARS = 3500  # Telegram refuses messages over 4096 characters; a longer digest is split
PAUSE_SECONDS = 1.1      # Telegram allows about one message per second to one chat


class NotifyError(Exception):
    """Missing Telegram settings, or Telegram did not accept the message."""


# --- settings -----------------------------------------------------------------

def load_credentials(env_path=ENV_PATH) -> tuple[str, str]:
    """(token, chat id) from real environment variables, else from the .env file.

    Environment variables win, so GitHub Actions can supply them as secrets.
    """
    values = {}
    if Path(env_path).exists():
        # utf-8-sig: Windows editors like Notepad may add an invisible byte-order mark
        for line in Path(env_path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("\"'")

    token = os.environ.get("TELEGRAM_TOKEN") or values.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or values.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise NotifyError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env (or as environment variables)")
    return token, chat_id


# --- the message -----------------------------------------------------------------

def _number(value) -> str:
    """7200 -> '7 200', 52.5 -> '52,5', 2.0 -> '2' (Swedish formats)."""
    text = f"{value:,.1f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def _title(listing: dict) -> str:
    """'Storgatan 5 (Majorna, Göteborg)'. Only known parts."""
    area, kommun = listing.get("area"), listing.get("kommun")
    if area and kommun and area.casefold() == kommun.casefold():  # HomeQ often names the kommun as the area
        kommun = None
    place = ", ".join(part for part in (area, kommun) if part)
    return (listing.get("address") or "adress saknas") + (f" ({place})" if place else "")


def _facts(listing: dict) -> str:
    """'7 200 kr/mån · 52,5 m² · 2 rum · vån 3'. Only known parts."""
    facts = []
    if listing.get("rent_sek") is not None:
        facts.append(f"{_number(listing['rent_sek'])} kr/mån")
    if listing.get("size_m2") is not None:
        facts.append(f"{_number(listing['size_m2'])} m²")
    if listing.get("rooms") is not None:
        facts.append(f"{_number(listing['rooms'])} rum")
    if listing.get("floor") is not None:
        facts.append("bottenvåning" if listing["floor"] == 0 else f"vån {listing['floor']}")
    return " · ".join(facts)


def format_message(listing: dict, search_names: list[str]) -> str:
    """One listing as its own message (delivery 'instant')."""
    lines = [f"{'⚡' if listing.get('allocation') == 'first_come' else '🏠'} Ny annons: {_title(listing)}"]
    if _facts(listing):
        lines.append(_facts(listing))

    label = CHANCE_LABELS.get(listing.get("bucket"), "Okänd chans")
    lines.append(f"{label}. {listing.get('bucket_note') or ''}".strip())

    dates = []
    if listing.get("deadline"):
        dates.append(f"Sista ansökningsdag: {listing['deadline']}")
    if listing.get("move_in"):
        dates.append(f"Inflytt: {listing['move_in']}")
    if listing.get("applicants") is not None:
        dates.append(f"{listing['applicants']} sökande")
    if dates:
        lines.append(" · ".join(dates))

    source = SOURCE_NAMES.get(listing.get("source"), listing.get("source") or "")
    lines.append(f"{source} · Sökning: {', '.join(search_names)}")
    if listing.get("url"):
        lines.append(listing["url"])
    return "\n".join(lines)


def _digest_entry(number: int, listing: dict, search_names=None) -> str:
    """One listing inside the digest: shorter than a message of its own."""
    lines = [f"{number}. {_title(listing)}"]
    if _facts(listing):
        lines.append(_facts(listing))
    detail = [CHANCE_LABELS.get(listing.get("bucket"), "Okänd chans")]
    if listing.get("deadline"):
        detail.append(f"sista dag {listing['deadline']}")
    detail.append(SOURCE_NAMES.get(listing.get("source"), listing.get("source") or ""))
    if search_names:
        detail.append(", ".join(search_names))
    lines.append(" · ".join(detail))
    if listing.get("url"):
        lines.append(listing["url"])
    return "\n".join(lines)


def digest_messages(items, show_names=False, max_chars=None) -> list[tuple[str, list[str]]]:
    """[(text, ids of the listings in it)] for the daily digest, best chance first, then soonest deadline.

    `items` is [(listing, names of the searches it matches)]. A digest that would be too long
    for one Telegram message is split into several.
    """
    max_chars = max_chars or DIGEST_MAX_CHARS
    items = sorted(items, key=lambda item: (
        CHANCE_ORDER.get(item[0].get("bucket"), 2), item[0].get("deadline") or "9999", item[0].get("address") or ""))
    parts, current, size = [], [], 0
    for number, (listing, names) in enumerate(items, start=1):
        entry = _digest_entry(number, listing, names if show_names else None)
        if current and size + len(entry) > max_chars:
            parts.append(current)
            current, size = [], 0
        current.append((listing["id"], entry))
        size += len(entry) + 2
    if current:
        parts.append(current)

    messages = []
    for index, part in enumerate(parts, start=1):
        header = f"📬 Nya annonser ({len(items)})" + (f" · del {index}/{len(parts)}" if len(parts) > 1 else "")
        messages.append((header + "\n\n" + "\n\n".join(entry for _, entry in part), [one for one, _ in part]))
    return messages


# --- sending ---------------------------------------------------------------------

def send_message(text: str, token: str, chat_id: str, session=requests):
    """Send one message. Errors never contain the token (it is part of the web address)."""
    try:
        response = session.post(
            SEND_URL.format(token=token),
            json={"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}},
            timeout=30,
        )
    except requests.RequestException as error:
        # `from None` and no str(error): the error text would include the address, and so the token
        raise NotifyError(f"could not reach Telegram ({type(error).__name__})") from None
    if not response.ok:
        try:
            reason = response.json().get("description", "")
        except ValueError:
            reason = ""
        raise NotifyError(f"Telegram refused the message (HTTP {response.status_code}): {reason}".rstrip(": "))


def run_alerts(conn, rows, searches, settings, now: datetime, session=requests, credentials=None, pause=None) -> str:
    """Send the alerts that are due and remember what was sent. Returns a one-line summary.

    `rows` are the scored listings (with 'alerted_at'); `now` must have a time zone.
    Raises NotifyError if a message cannot be sent; whatever went out before that stays marked as sent.

    * First time alerts run: everything already in the database is marked as seen, nothing is sent.
    * 'daily': at most one message a day, sent by the first run after the digest hour (Swedish time)
      that has something new. A new listing that arrives later the same day waits for tomorrow.
    * 'instant': one message per new listing, right away.
    """
    if not searches:
        return "no searches in alerts.json, nothing to do"
    stamp = now.isoformat(timespec="seconds")
    if not store.alerts_started(conn):
        marked = store.mark_all_alerted(conn, stamp)
        return f"first run with alerts on: {marked} listings marked as already seen, nothing sent"

    items = []
    for row in rows:
        if row.get("alerted_at") is None:
            names = [search["name"] for search in searches if alerts.matches(row, search)]
            if names:
                items.append((row, names))
    if not items:
        return "nothing new that matches"

    if settings["delivery"] == "daily":
        local = now.astimezone(STOCKHOLM)
        if local.hour < settings["digest_hour"]:
            return f"{len(items)} new, waiting for the {settings['digest_hour']:02d}:00 digest"
        last = store.last_alert_time(conn)
        if last and datetime.fromisoformat(last).astimezone(STOCKHOLM).date() == local.date():
            return f"{len(items)} new, but today's message has already gone out: they wait until tomorrow"
        messages = digest_messages(items, show_names=len(searches) > 1)
    else:
        messages = [(format_message(listing, names), [listing["id"]]) for listing, names in items]

    token, chat_id = credentials or load_credentials()
    for number, (text, ids) in enumerate(messages):
        if number:
            time.sleep(PAUSE_SECONDS if pause is None else pause)
        send_message(text, token, chat_id, session)
        store.mark_alerted(conn, ids, stamp)
    return f"sent {len(messages)} message(s) about {len(items)} listing(s)"


# --- command line ------------------------------------------------------------------

def preview(count: int, listings_path=LISTINGS_PATH, alerts_path=None) -> int:
    """Print what the alert would look like for the first `count` matching listings. Sends nothing."""
    searches = alerts.load_alerts(alerts_path)
    settings = alerts.load_settings(alerts_path)
    rows = json.loads(Path(listings_path).read_text(encoding="utf-8"))
    items = []
    for row in rows:
        names = [search["name"] for search in searches if alerts.matches(row, search)]
        if names:
            items.append((row, names))
        if len(items) == count:
            break
    if settings["delivery"] == "daily":
        texts = [text for text, _ in digest_messages(items, show_names=len(searches) > 1)]
    else:
        texts = [format_message(listing, names) for listing, names in items]
    for text in texts:
        print(text, end="\n\n---\n\n")
    print(f"({len(items)} listings shown, delivery '{settings['delivery']}'. Nothing was sent.)")
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console cannot show emoji or å/ä/ö by default
    parser = argparse.ArgumentParser(description="Telegram messages for the bostadskö tracker.")
    commands = parser.add_subparsers(dest="command", required=True)
    preview_parser = commands.add_parser("preview", help="print what the alert looks like, send nothing")
    preview_parser.add_argument("count", nargs="?", type=int, default=3)
    commands.add_parser("test", help="send one greeting to your Telegram")
    args = parser.parse_args(argv)

    try:
        if args.command == "preview":
            return preview(args.count)
        token, chat_id = load_credentials()
        send_message("Hej från Bostadskö Tracker! Om du ser det här fungerar Telegram-aviseringarna.", token, chat_id)
        print("Sent. Check Telegram.")
        return 0
    except (NotifyError, alerts.AlertsError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
