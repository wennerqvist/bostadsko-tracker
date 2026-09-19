"""Alert rules from alerts.json: which listings deserve a Telegram message.

Works like the filters on the web page: a value we do not know (None) never
rules a listing out, because we cannot know. That includes the chance bucket.
"""

import json
from pathlib import Path

import score

ALERTS_PATH = Path(__file__).parent / "alerts.json"

SOURCES = ("boplats", "homeq")
CHANCE_WORDS = {"hög chans": "likely", "möjlig": "possible"}  # what alerts.json says -> score.py's bucket
SEARCH_KEYS = (
    "name", "max_rent", "min_size", "min_rooms", "sources", "kommuner",
    "include_first_come", "include_lottery", "min_chance",
)


class AlertsError(Exception):
    """alerts.json holds something we cannot use."""


# --- reading alerts.json -----------------------------------------------------

def _number(value, where: str, key: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AlertsError(f"{where}: '{key}' must be a number or null, got {value!r}")
    return value


def _text_list(value, where: str, key: str, allowed=None):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AlertsError(f"{where}: '{key}' must be a list of text like [\"a\", \"b\"] or null")
    if allowed is not None:
        wrong = [item for item in value if item not in allowed]
        if wrong:
            raise AlertsError(f"{where}: '{key}' can only contain {', '.join(allowed)}; got {wrong[0]!r}")
    return value


def _bool(value, where: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise AlertsError(f"{where}: '{key}' must be true or false, got {value!r}")
    return value


def _check_search(raw, number: int) -> dict:
    """One entry of 'searches' -> a search dict with every key filled in."""
    if not isinstance(raw, dict):
        raise AlertsError(f"search {number} must be a {{...}} block")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AlertsError(f"search {number} needs a 'name' (some text)")
    where = f"search '{name}'"
    unknown = [key for key in raw if key not in SEARCH_KEYS]
    if unknown:  # a typo like "max_rnt" would otherwise silently do nothing
        raise AlertsError(f"{where}: unknown setting '{unknown[0]}'. Allowed: {', '.join(SEARCH_KEYS)}")

    chance = raw.get("min_chance")
    if chance is not None:
        if not isinstance(chance, str) or chance.strip().lower() not in CHANCE_WORDS:
            raise AlertsError(f"{where}: 'min_chance' must be \"hög chans\", \"möjlig\" or null, got {chance!r}")
        chance = CHANCE_WORDS[chance.strip().lower()]

    sources = _text_list(raw.get("sources"), where, "sources", allowed=SOURCES)
    return {
        "name": name.strip(),
        "max_rent": _number(raw.get("max_rent"), where, "max_rent"),
        "min_size": _number(raw.get("min_size"), where, "min_size"),
        "min_rooms": _number(raw.get("min_rooms"), where, "min_rooms"),
        "sources": tuple(SOURCES if sources is None else sources),
        "kommuner": _text_list(raw.get("kommuner"), where, "kommuner"),
        "include_first_come": _bool(raw.get("include_first_come", True), where, "include_first_come"),
        "include_lottery": _bool(raw.get("include_lottery", True), where, "include_lottery"),
        "min_chance": chance,  # a score.py bucket ('likely' / 'possible') or None
    }


def load_alerts(path=None) -> list[dict]:
    """The searches in alerts.json. A missing file gives no searches, so no alerts."""
    path = Path(path) if path else ALERTS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except ValueError as error:
        raise AlertsError(f"{path.name} is not valid JSON: {error}") from None
    if not isinstance(raw, dict):
        raise AlertsError(f"{path.name} must be a JSON object with a 'searches' list")
    searches = raw.get("searches", [])
    if not isinstance(searches, list):
        raise AlertsError(f"{path.name}: 'searches' must be a list")
    return [_check_search(item, number) for number, item in enumerate(searches, start=1)]


# --- does a listing match? ---------------------------------------------------

def matches(listing: dict, search: dict) -> bool:
    """True if this listing (a row from listings.json, with 'bucket') fits the search."""
    if listing.get("source") not in search["sources"]:
        return False
    allocation = listing.get("allocation")
    if allocation == "first_come" and not search["include_first_come"]:
        return False
    if allocation == "lottery" and not search["include_lottery"]:
        return False

    rent, size, rooms = listing.get("rent_sek"), listing.get("size_m2"), listing.get("rooms")
    if search["max_rent"] is not None and rent is not None and rent > search["max_rent"]:
        return False
    if search["min_size"] is not None and size is not None and size < search["min_size"]:
        return False
    if search["min_rooms"] is not None and rooms is not None and rooms < search["min_rooms"]:
        return False

    kommun = listing.get("kommun")
    if search["kommuner"] is not None and kommun:
        if kommun.casefold() not in {name.casefold() for name in search["kommuner"]}:
            return False

    bucket = listing.get("bucket")
    if search["min_chance"] and bucket in score.BUCKETS:
        if score.BUCKETS.index(bucket) < score.BUCKETS.index(search["min_chance"]):
            return False
    return True
