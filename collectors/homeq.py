"""HomeQ collector: logs in to homeq.se and returns listings in the unified schema.

The parse_* functions only turn saved JSON into data (no internet), so they can
be tested against the samples in tests/samples/. collect() does the fetching.
The login token lives in memory for one run and is never written to disk.

These are the same (unofficial, "internal") endpoints homeq.se's own web page
uses, so HomeQ can change them without notice. The login response contains
personal details; only the token is ever read from it.
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

LOGIN_URL = "https://api.homeq.se/api/v1/user/profile/login"
SEARCH_URL = "https://api.homeq.se/api/v3/search"
INSIGHTS_URL = "https://api.homeq.se/api/v3/free_insights/{id}/"  # HomeQ's own "free" tier: top-10 points, when shown
SITE_URL = "https://www.homeq.se/"
SHAPE = "metropolitan_area.8"  # HomeQ's id for the Göteborg area (the server ignores it, see WANTED_KOMMUNER)
# The search returns all of Sweden in one 8 MB answer, so we keep only these
# municipalities ourselves: the 13 that make up Göteborgsregionen.
WANTED_KOMMUNER = {
    "Ale", "Alingsås", "Göteborg", "Härryda", "Kungsbacka", "Kungälv", "Lerum",
    "Lilla Edet", "Mölndal", "Partille", "Stenungsund", "Tjörn", "Öckerö",
}
USER_AGENT = "bostadsko-tracker (personal use)"
PAUSE_SECONDS = 1.5
MAX_PAGES = 30  # safety stop, so a misbehaving API can't keep us looping
MAX_PAGE_FAILURES = 5  # this many listing pages failing in a row: stop fetching for this run
MAX_INSIGHTS_PER_RUN = 150  # readings per run, oldest first, so a first run or a long gap never means a huge burst
# How the landlord picks the tenant, as HomeQ's listing page names it -> our allocation values.
ALLOCATION_BY_MODE = {"queue_points": "queue", "random": "lottery", "first_come_first": "first_come"}
ENV_PATH = Path(__file__).parent.parent / ".env"


class HomeQError(Exception):
    """Login failed, credentials are missing, or the response looks different than expected."""


# --- credentials -----------------------------------------------------------

def load_credentials(env_path=ENV_PATH) -> tuple[str, str]:
    """(email, password) from real environment variables, else from the .env file.

    Environment variables win, so GitHub Actions can supply them as secrets
    without any .env file existing there.
    """
    values = {}
    if Path(env_path).exists():
        # utf-8-sig: Windows editors like Notepad may add an invisible byte-order mark
        for line in Path(env_path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("\"'")

    email = os.environ.get("HOMEQ_EMAIL") or values.get("HOMEQ_EMAIL")
    password = os.environ.get("HOMEQ_PASSWORD") or values.get("HOMEQ_PASSWORD")
    if not email or not password:
        raise HomeQError("HOMEQ_EMAIL and HOMEQ_PASSWORD must be set in .env (or as environment variables)")
    return email, password


# --- small helpers ---------------------------------------------------------

def _number(value):
    """A number as a float, accepting 52, '52,5' and '52.5'. None if absent or not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:[.,]\d+)?", value.replace("\xa0", "").replace(" ", ""))
        return float(match.group().replace(",", ".")) if match else None
    return None


def _iso_date(value):
    """'2026-10-01' or '2026-10-01T00:00:00' -> '2026-10-01'. None if it isn't a date."""
    match = re.match(r"\d{4}-\d{2}-\d{2}", value) if isinstance(value, str) else None
    return match.group() if match else None


def _clean(value):
    """Stripped text, or None if empty."""
    return value.strip() or None if isinstance(value, str) else None


# --- parsers (no network) --------------------------------------------------

def parse_token(data) -> str:
    """The login response -> the token string (found at user_info.token)."""
    user_info = data.get("user_info") if isinstance(data, dict) else None
    if isinstance(user_info, dict) and isinstance(user_info.get("token"), str) and user_info["token"]:
        return user_info["token"]
    keys = ", ".join(sorted(data)) if isinstance(data, dict) else f"a {type(data).__name__}, not an object"
    # Only the field names are shown, never the values: this response holds personal data.
    raise HomeQError(f"login response has no user_info.token (it contains: {keys})")


def parse_search_response(data) -> list[dict]:
    """The search response -> listings in the unified schema.

    Only single apartments in WANTED_KOMMUNER are kept. "Projects" (a whole new
    building, with no rent, rooms or size of its own) are left out.
    """
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise HomeQError("search response has no 'results' list")

    listings = []
    for item in data["results"]:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            raise HomeQError("a search result has no id")
        kommun = _clean(item.get("municipality"))
        if item.get("type") == "project" or kommun not in WANTED_KOMMUNER:
            continue
        rent = _number(item.get("rent"))
        uri = item.get("uri")
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        listings.append({
            "id": f"homeq:{item['id']}",
            "source": "homeq",
            "url": urljoin(SITE_URL, uri) if isinstance(uri, str) and uri else None,
            "address": _clean(item.get("title")),
            "area": _clean(item.get("city")),
            "kommun": kommun,
            "lat": _number(location.get("lat")),
            "lon": _number(location.get("lon")),
            "rent_sek": round(rent) if rent is not None else None,
            "size_m2": _number(item.get("area")),  # HomeQ's "area" is the size, not the district
            "rooms": _number(item.get("rooms")),
            "move_in": _iso_date(item.get("date_access")),
            "is_short_lease": bool(item["is_short_lease"]) if item.get("is_short_lease") is not None else None,
        })
    return listings


def parse_detail_page(html: str) -> dict:
    """Fields only a listing's own page has (no login needed): how the landlord
    picks the tenant, plus landlord, floor and publish date.

    The page carries its data as JSON in a __NEXT_DATA__ script tag. Raises
    HomeQError if that is missing (a redesign), so we never store "unknown" for
    everything because of it. A listing whose selection method is missing or
    new gets allocation "unknown": we looked, there was no answer.
    """
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    try:
        ad = json.loads(match.group(1))["props"]["pageProps"]["objectAd"]
    except (AttributeError, ValueError, KeyError, TypeError):
        ad = None
    if not isinstance(ad, dict):
        raise HomeQError("listing page has no objectAd data (has homeq.se changed?)")
    floor = ad.get("floor")
    return {
        "allocation": ALLOCATION_BY_MODE.get(ad.get("candidate_sorting_mode"), "unknown"),
        "landlord": _clean(ad.get("landlord_company")),
        "floor": floor if isinstance(floor, int) and not isinstance(floor, bool) else None,
        "published": _iso_date(ad.get("date_publish")),
    }


def parse_insights(data) -> dict:
    """HomeQ's free_insights answer -> {'insight_frame': ..., 'points_needed_top10': ...}.

    'frame' says what HomeQ shows on the listing: queue_points_info comes with
    queue_points_top_10 (the points that give a place among the top 10 applicants),
    first_to_apply comes without a number. We keep the frame text as it is and only
    trust the number. Raises HomeQError if there is no frame (has HomeQ changed?).
    """
    frame = _clean(data.get("frame")) if isinstance(data, dict) else None
    if frame is None:
        raise HomeQError("insights answer has no 'frame' (has HomeQ changed?)")
    points = data.get("queue_points_top_10")
    return {
        "insight_frame": frame,
        "points_needed_top10": points if isinstance(points, int) and not isinstance(points, bool) else None,
    }


# --- fetching --------------------------------------------------------------

class PoliteClient:
    """One request at a time, with a pause between requests and a clear User-Agent."""

    def __init__(self, pause=PAUSE_SECONDS):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = None

    def _wait(self):
        if self._last_request is not None:
            wait = self.pause - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)

    def get_text(self, url: str) -> str:
        self._wait()
        try:
            response = self.session.get(url, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    def get_json(self, url: str, token: str):
        self._wait()
        try:
            response = self.session.get(url, headers={"Authorization": f"JWT {token}"}, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response.json()

    def post_json(self, url: str, body: dict, token: str | None = None):
        self._wait()
        headers = {"Authorization": f"JWT {token}"} if token else {}
        try:
            response = self.session.post(url, json=body, headers=headers, timeout=90)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response.json()


def login(client: PoliteClient, email: str, password: str) -> str:
    try:
        data = client.post_json(LOGIN_URL, {"email": email, "password": password})
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code in (400, 401, 403):
            raise HomeQError("HomeQ login was refused (wrong email or password?)") from None
        raise
    return parse_token(data)


def read_insights(client, token, listings, last_insight, insight_cutoff):
    """Add 'insight_frame' and 'points_needed_top10' to listings whose last reading is older than the cutoff.

    `last_insight` is {id: time of last reading}; a listing never read counts as oldest.
    An empty cutoff reads nothing. A reading that fails is skipped and tried next run.
    """
    todo = [item for item in listings if last_insight.get(item["id"], "") < insight_cutoff]
    todo.sort(key=lambda item: last_insight.get(item["id"], ""))  # never read first, then the stalest
    todo = todo[:MAX_INSIGHTS_PER_RUN]
    if todo:
        print(f"HomeQ: reading the points figure of {len(todo)} listings.")
    failed_in_a_row = 0
    for item in todo:
        try:
            item.update(parse_insights(client.get_json(INSIGHTS_URL.format(id=item["id"].split(":", 1)[1]), token)))
            failed_in_a_row = 0
        except requests.RequestException as error:
            failed_in_a_row += 1
            print(f"  skipped points for {item['address']} (will retry next run): {error}")
            if failed_in_a_row >= MAX_PAGE_FAILURES:
                print(f"  {failed_in_a_row} readings in a row failed, stopping for this run.")
                break
        except HomeQError as error:  # the answer looks different: an extra, so do not fail the whole run
            print(f"  points readings stopped for this run: {error}")
            break


def collect(known_details=frozenset(), pause=PAUSE_SECONDS, last_insight=None, insight_cutoff="") -> list[dict]:
    """Log in, then fetch the search results and keep the Göteborg-region apartments.

    Each listing's own page is fetched once, for listings not in `known_details`
    (ids whose page we have already read). A page that fails is skipped and
    tried again next run; the listing is still returned, just without those fields.

    The answer normally holds every listing at once (total_hits says how many).
    If it ever comes in pages, we keep asking for the next page until we have
    them all. Raises HomeQError if nothing is left, so an empty answer is never
    mistaken for "every listing has closed".
    """
    email, password = load_credentials()
    client = PoliteClient(pause)
    token = login(client, email, password)

    listings = {}
    seen_ids = set()  # everything HomeQ returned, before filtering
    for page in range(1, MAX_PAGES + 1):
        data = client.post_json(SEARCH_URL, {"selectedShapes": SHAPE, "page": page}, token)
        found = parse_search_response(data)
        returned = {str(item["id"]) for item in data["results"]}
        if not returned - seen_ids:  # an empty page, or one we have already seen: that was the last one
            break
        seen_ids |= returned
        listings.update({item["id"]: item for item in found})
        total = data.get("total_hits")
        if isinstance(total, int) and len(seen_ids) >= total:
            break
    else:
        raise HomeQError(f"still getting new listings after {MAX_PAGES} pages, giving up")

    if not listings:
        raise HomeQError("no listings found in the search response")
    print(f"HomeQ: {len(seen_ids)} listings in Sweden, {len(listings)} in the Göteborg region.")

    todo = [item for item in listings.values() if item["id"] not in known_details and item["url"]]
    if todo:
        print(f"HomeQ: reading the page of {len(todo)} listings not seen before "
              f"(about {round(len(todo) * (pause + 0.3) / 60)} min).")
    failed_in_a_row = 0
    for number, item in enumerate(todo, start=1):
        try:
            item.update(parse_detail_page(client.get_text(item["url"])))
            failed_in_a_row = 0
        except requests.RequestException as error:
            failed_in_a_row += 1
            print(f"  skipped {item['address']} (will retry next run): {error}")
            if failed_in_a_row >= MAX_PAGE_FAILURES:
                print(f"  {failed_in_a_row} pages in a row failed, stopping for this run.")
                break
        if number % 25 == 0 or number == len(todo):
            print(f"  [{number}/{len(todo)}]")

    read_insights(client, token, list(listings.values()), last_insight or {}, insight_cutoff)
    return list(listings.values())
