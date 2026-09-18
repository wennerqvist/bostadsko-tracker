"""Boplats collector: reads boplats.se and returns listings in the unified schema.

The parse_* functions only turn saved HTML/JSON into data (no internet), so they
can be tested against the pages in tests/samples/. collect() does the fetching.
"""

import json
import re
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://boplats.se/sok?types=1hand"
STATS_URL = "https://boplats.se/area_statistics/1hand/{id}"
USER_AGENT = "bostadsko-tracker (personal use)"
PAUSE_SECONDS = 1.5

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
RELATIVE_DAYS = {"idag": 0, "igår": 1, "i förrgår": 2}


class PageChanged(Exception):
    """The page does not look the way the parser expects (site redesign?)."""


# --- small helpers ---------------------------------------------------------

def _text(node) -> str:
    """Text of an element with all runs of whitespace (incl. non-breaking) collapsed."""
    return re.sub(r"\s+", " ", node.get_text(" ")).strip() if node else ""


def _number(text: str):
    """First number in the text as a float, accepting '52,5' and '52.5'. None if absent."""
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(match.group().replace(",", ".")) if match else None


def _swedish_date(text: str, today: date):
    """'24 september' or '1 jan. 2027' -> date. None if it isn't a date.

    Without a year we take the coming occurrence: a date more than 60 days in
    the past must be next year's (a listing seen in December closing 5 January).
    """
    match = re.search(r"(\d{1,2})\s+([a-zåäö]+)\.?\s*(\d{4})?", text.lower())
    if not match or match.group(2)[:3] not in MONTHS:
        return None
    day, month = int(match.group(1)), MONTHS[match.group(2)[:3]]
    if match.group(3):
        return date(int(match.group(3)), month, day)
    result = date(today.year, month, day)
    if result < today - timedelta(days=60):
        result = date(today.year + 1, month, day)
    return result


def _publication_date(text: str, today: date):
    """'Publ. idag' / 'Publ. igår' / 'Publ. i förrgår' / 'Publ. 2026-09-12' -> date."""
    text = text.replace("Publ.", "").strip().lower()
    if text in RELATIVE_DAYS:
        return today - timedelta(days=RELATIVE_DAYS[text])
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    return date(*map(int, match.groups())) if match else None


def _iso(d):
    return d.isoformat() if d else None


def clean_address(address: str) -> str:
    """Drop a stray second number after the house number, e.g. landlords typing
    'Distansgatan 66 66' or 'Kulvertkonstens väg 9 1004' (an apartment number).
    Ranges ('66-68') and letters ('14A') are left alone."""
    match = re.fullmatch(r"(.*\S\s+\d+[A-Za-z]?)\s+\d+", address)
    return match.group(1) if match else address


# --- parsers (no network) --------------------------------------------------

def parse_search_page(html: str, today: date) -> list[dict]:
    """One dict per listing card on the search page."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select("div.search-listing"):
        link = card.select_one("a.search-result-link")
        match = re.search(r"/objekt/1hand/(\w+)", link["href"]) if link else None
        if not match:
            raise PageChanged("a listing card has no link to /objekt/1hand/<id>")
        raw_id = match.group(1)

        rent = re.sub(r"\D", "", _text(card.select_one(".search-result-price")))
        floor = re.search(r"Våning\s+(-?\d+)", _text(card.select_one(".search-result-floors")))
        publ = _publication_date(_text(card.select_one(".publ-date")), today)

        listings.append({
            "id": f"boplats:{raw_id}",
            "source": "boplats",
            "url": link["href"],
            "area": _text(card.select_one(".search-result-area-name")),
            "address": clean_address(_text(card.select_one(".search-result-address"))),
            "rent_sek": int(rent) if rent else None,
            "size_m2": _number(_text(card.select_one("div.pure-u-2-5.right-align"))),
            "rooms": _number(_text(card.select_one("div.pure-u-1-4.right-align"))),
            "floor": int(floor.group(1)) if floor else None,
            "published": _iso(publ),
        })
    return listings


def parse_detail_page(html: str, today: date) -> dict:
    """Fields that only the detail page has, plus 'applicants' (for the snapshot)."""
    soup = BeautifulSoup(html, "html.parser")

    values = {}  # "Inflyttning:" -> "1 jan. 2027"
    for label in soup.select("span.detail-label"):
        values[_text(label).rstrip(":")] = _text(label.find_next_sibling("span"))

    # "Hasselbacken, Stenungsund, Stenungsund" = area, district, kommun
    area_line = [part.strip() for part in _text(soup.select_one("p.detail-area-desc")).split(",")]
    kommun = area_line[-1] if area_line[0] else None

    applicants = re.search(r"(\d+)\s+sökande", soup.get_text(" "))

    landlord = None
    heading = next((h for h in soup.select("h3") if _text(h) == "Hyresvärd"), None)
    if heading:
        name = heading.find_parent("div").find_next_sibling("div")
        landlord = _text(name.select_one("li")) if name and name.select_one("li") else None

    # The main ranking rule, e.g. "Rangordning: Ködagar hos Boplats."
    allocation = None
    ranking = next((b for b in soup.select("#criteria-list b") if "Rangordning" in _text(b)), None)
    if ranking and "Ködagar hos Boplats" in _text(ranking.parent):
        allocation = "queue"

    return {
        "kommun": kommun,
        "move_in": _iso(_swedish_date(values.get("Inflyttning", ""), today)),
        "deadline": _iso(_swedish_date(values.get("Anmäl senast", ""), today)),
        "applicants": int(applicants.group(1)) if applicants else None,
        "landlord": landlord,
        "allocation": allocation,
    }


def parse_area_stats(text: str):
    """The 'kötid för liknande lägenheter' JSON -> average queue days (int), or None."""
    try:
        return int(json.loads(text)["successData"]["averageQueueDays"])
    except (ValueError, KeyError, TypeError):
        return None


# --- fetching --------------------------------------------------------------

class PoliteFetcher:
    """One request at a time, with a pause between requests and a clear User-Agent."""

    def __init__(self, pause=PAUSE_SECONDS):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = None

    def get(self, url: str) -> str:
        if self._last_request is not None:
            wait = self.pause - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        try:
            response = self.session.get(url, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text


def collect(known: set[str], today: date | None = None, pause=PAUSE_SECONDS) -> list[dict]:
    """Fetch the search page; fetch detail pages only for listings not in `known`.

    Returns every listing on the search page. Ones we already know carry only
    the card fields; new ones also carry detail fields, winners_queue_days and
    'applicants'. A new listing whose detail fetch fails is left out, so it is
    tried again on the next run instead of being stored half-empty.
    """
    today = today or date.today()
    fetcher = PoliteFetcher(pause)

    listings = parse_search_page(fetcher.get(SEARCH_URL), today)
    if not listings:
        raise PageChanged("no listing cards found on the search page")

    new = [item for item in listings if item["id"] not in known]
    print(f"Boplats: {len(listings)} listings on the site, {len(new)} new.")

    complete = []
    for number, item in enumerate(new, start=1):
        raw_id = item["id"].split(":", 1)[1]
        print(f"  [{number}/{len(new)}] {item['address']}")
        try:
            item.update(parse_detail_page(fetcher.get(item["url"]), today))
            item["winners_queue_days"] = parse_area_stats(fetcher.get(STATS_URL.format(id=raw_id)))
        except requests.RequestException as error:
            print(f"    skipped (will retry next run): {error}")
            continue
        complete.append(item)

    failed = {item["id"] for item in new} - {item["id"] for item in complete}
    return [item for item in listings if item["id"] not in failed]
