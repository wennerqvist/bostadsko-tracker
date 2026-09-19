"""HomeQ collector: logs in to homeq.se and returns listings in the unified schema.

The parse_* functions only turn saved JSON into data (no internet), so they can
be tested against the samples in tests/samples/. collect() does the fetching.
The login token lives in memory for one run and is never written to disk.
"""

import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

LOGIN_URL = "https://api.homeq.se/api/v1/user/token/"
SEARCH_URL = "https://api.homeq.se/api/v3/search"
SITE_URL = "https://www.homeq.se/"
SHAPE = "metropolitan_area.8"  # HomeQ's id for the Göteborg area
USER_AGENT = "bostadsko-tracker (personal use)"
PAUSE_SECONDS = 1.5
MAX_PAGES = 30  # safety stop, so a misbehaving API can't keep us looping
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
    """The login response -> the token string."""
    if isinstance(data, dict):
        for key in ("token", "access_token", "key"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
        keys = ", ".join(sorted(data))
    else:
        keys = f"a {type(data).__name__}, not an object"
    # Only the field names are shown, never the values, in case one of them is a secret.
    raise HomeQError(f"login response has no token field (it contains: {keys})")


def parse_search_response(data) -> list[dict]:
    """One page of search results -> a list of listings in the unified schema."""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise HomeQError("search response has no 'results' list")

    listings = []
    for item in data["results"]:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            raise HomeQError("a search result has no id")
        rent = _number(item.get("rent"))
        uri = item.get("uri")
        listings.append({
            "id": f"homeq:{item['id']}",
            "source": "homeq",
            "url": urljoin(SITE_URL, uri) if isinstance(uri, str) and uri else None,
            "address": _clean(item.get("title")),
            "area": _clean(item.get("city")),
            "kommun": _clean(item.get("municipality")),
            "rent_sek": round(rent) if rent is not None else None,
            "size_m2": _number(item.get("area")),  # HomeQ's "area" is the size, not the district
            "rooms": _number(item.get("rooms")),
            "move_in": _iso_date(item.get("date_access")),
            "is_short_lease": bool(item.get("is_short_lease")),  # store.py ignores this for now
        })
    return listings


# --- fetching --------------------------------------------------------------

class PoliteClient:
    """One request at a time, with a pause between requests and a clear User-Agent."""

    def __init__(self, pause=PAUSE_SECONDS):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = None

    def post_json(self, url: str, body: dict, token: str | None = None):
        if self._last_request is not None:
            wait = self.pause - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self.session.post(url, json=body, headers=headers, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response.json()


def login(client: PoliteClient, email: str, password: str) -> str:
    try:
        data = client.post_json(LOGIN_URL, {"username": email, "password": password})
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code in (400, 401, 403):
            raise HomeQError("HomeQ login was refused (wrong email or password?)") from None
        raise
    return parse_token(data)


def collect(pause=PAUSE_SECONDS) -> list[dict]:
    """Log in, then fetch every page of search results for the Göteborg area.

    Raises HomeQError if there is nothing to return, so an empty answer is never
    mistaken for "every listing has closed".
    """
    email, password = load_credentials()
    client = PoliteClient(pause)
    token = login(client, email, password)

    listings = {}
    for page in range(1, MAX_PAGES + 1):
        found = parse_search_response(
            client.post_json(SEARCH_URL, {"selectedShapes": SHAPE, "page": page}, token)
        )
        added = [item for item in found if item["id"] not in listings]
        if not added:  # an empty page, or one we have already seen: that was the last one
            break
        listings.update({item["id"]: item for item in added})
    else:
        raise HomeQError(f"still getting new listings after {MAX_PAGES} pages, giving up")

    if not listings:
        raise HomeQError("no listings found in the search response")
    print(f"HomeQ: {len(listings)} listings on the site.")
    return list(listings.values())
