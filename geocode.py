"""Turn a street address into coordinates with OpenStreetMap's Nominatim service.

Boplats gives only addresses, and the map needs coordinates. Results are saved
in the database, so each address is looked up once. Nominatim's rules: at most
one request per second and a User-Agent that names the app.

parse_result() and address_queries() need no internet, so they can be tested
against the samples in tests/samples/. Geocoder does the fetching.
"""

import re
import time

import requests

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "bostadsko-tracker (personal use)"
PAUSE_SECONDS = 1.2

# Rough box around Sweden, to catch a nonsense answer.
LAT_RANGE = (55.0, 69.5)
LON_RANGE = (10.5, 24.5)


class GeocodeError(Exception):
    """Nominatim answered with something unexpected (blocked? changed format?)."""


# --- pure helpers (no network) ---------------------------------------------

def address_queries(address: str, kommun: str) -> list[str]:
    """Search texts to try, best first: the tidied full address, then just the street.

    Landlords type addresses in odd ways ('Artillerigatan 30 .', 'Merkuriusgatan 2 A',
    'Distansgatan 66-68'). The street-only fallback gives an approximate spot when
    OpenStreetMap does not know the exact house number.
    """
    full = re.sub(r"\s*\.+\s*$", "", address.strip())              # 'Artillerigatan 30 .' -> 'Artillerigatan 30'
    full = re.sub(r"(\d+)\s*-\s*\d+$", r"\1", full)                 # '66-68' -> '66'
    full = re.sub(r"(\d+)\s+([A-Za-z])$", r"\1\2", full)            # '2 A' -> '2A'
    street = re.sub(r"\s+\d+\s*[A-Za-z]?$", "", full)               # drop the house number
    queries = [f"{full}, {kommun}"]
    if street and street != full:
        queries.append(f"{street}, {kommun}")
    return queries


def parse_result(data):
    """Nominatim's answer -> (lat, lon), or None if nothing was found."""
    if not isinstance(data, list):
        raise GeocodeError("answer is not a list of places")
    if not data:
        return None
    try:
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, TypeError, ValueError):
        raise GeocodeError("first place has no usable lat/lon") from None
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        return None  # not in Sweden, so not our address
    return lat, lon


# --- fetching --------------------------------------------------------------

class Geocoder:
    """One request at a time, with a pause between requests and a clear User-Agent."""

    def __init__(self, pause=PAUSE_SECONDS):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = None

    def _search(self, text: str):
        if self._last_request is not None:
            wait = self.pause - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        params = {"q": text, "format": "jsonv2", "countrycodes": "se", "limit": 1}
        try:
            response = self.session.get(SEARCH_URL, params=params, timeout=30)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return parse_result(response.json())

    def lookup(self, address: str, kommun: str):
        """(lat, lon) for the address, or None if none of the searches found it.

        Raises requests.RequestException or GeocodeError if Nominatim is
        unreachable or answers strangely, so the caller can stop for this run.
        """
        for text in address_queries(address, kommun):
            found = self._search(text)
            if found:
                return found
        return None
