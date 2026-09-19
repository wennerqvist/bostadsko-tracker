"""Telegram messages: turn a listing into a short Swedish text and send it.

Usage:
    python notify.py preview [N]   print messages for N matching listings (default 3), sends nothing
    python notify.py test          send one greeting, to check that the bot works
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

import alerts

ENV_PATH = Path(__file__).parent / ".env"
LISTINGS_PATH = Path(__file__).parent / "site" / "listings.json"
SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

SOURCE_NAMES = {"boplats": "Boplats", "homeq": "HomeQ"}
CHANCE_LABELS = {"likely": "God chans", "possible": "Möjlig", "unlikely": "Låg chans"}  # same words as the map


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


def format_message(listing: dict, search_names: list[str]) -> str:
    """Plain text for one listing. Only facts we know are included."""
    first_come = listing.get("allocation") == "first_come"
    area, kommun = listing.get("area"), listing.get("kommun")
    if area and kommun and area.casefold() == kommun.casefold():  # HomeQ often names the kommun as the area
        kommun = None
    place = ", ".join(part for part in (area, kommun) if part)
    title = listing.get("address") or "adress saknas"
    lines = [f"{'⚡' if first_come else '🏠'} Ny annons: {title}" + (f" ({place})" if place else "")]

    facts = []
    if listing.get("rent_sek") is not None:
        facts.append(f"{_number(listing['rent_sek'])} kr/mån")
    if listing.get("size_m2") is not None:
        facts.append(f"{_number(listing['size_m2'])} m²")
    if listing.get("rooms") is not None:
        facts.append(f"{_number(listing['rooms'])} rum")
    if listing.get("floor") is not None:
        facts.append("bottenvåning" if listing["floor"] == 0 else f"vån {listing['floor']}")
    if facts:
        lines.append(" · ".join(facts))

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


# --- command line ------------------------------------------------------------------

def preview(count: int, listings_path=LISTINGS_PATH, alerts_path=None) -> int:
    """Print the messages the first `count` matching listings would give. Sends nothing."""
    searches = alerts.load_alerts(alerts_path)
    rows = json.loads(Path(listings_path).read_text(encoding="utf-8"))
    shown = 0
    for row in rows:
        names = [search["name"] for search in searches if alerts.matches(row, search)]
        if names:
            print(format_message(row, names), end="\n\n---\n\n")
            shown += 1
            if shown == count:
                break
    print(f"({shown} shown. Nothing was sent.)")
    return 0


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console cannot show emoji or å/ä/ö by default
    parser = argparse.ArgumentParser(description="Telegram messages for the bostadskö tracker.")
    commands = parser.add_subparsers(dest="command", required=True)
    preview_parser = commands.add_parser("preview", help="print messages, send nothing")
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
