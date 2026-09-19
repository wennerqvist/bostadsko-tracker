"""Your queue days and a chance bucket (likely / possible / unlikely) per listing.

The rules are in PLAN.md, "How your chances are estimated". A listing we cannot
judge gets no bucket (None) and a note saying why, never a guess.
"""

import json
from datetime import date
from pathlib import Path

ME_PATH = Path(__file__).parent / "me.json"                 # template, safe to commit
ME_LOCAL_PATH = Path(__file__).parent / "me.local.json"     # your real dates, gitignored
DATE_KEYS = ("boplats_registered", "homeq_verified")

BUCKETS = ["unlikely", "possible", "likely"]  # worst to best

LIKELY_RATIO = 1.10    # Boplats: your days vs. the winners' average
POSSIBLE_RATIO = 0.80
HOMEQ_POSSIBLE_RATIO = 0.90  # HomeQ: within 10 % below the points needed
FEW_APPLICANTS = 5     # this many or fewer, close to the deadline: one bucket up
MANY_APPLICANTS = 50   # this many or more: one bucket down
DEADLINE_DAYS = 3      # "close to the deadline"


class MeError(Exception):
    """me.json / me.local.json holds something we cannot use."""


# --- your queue dates --------------------------------------------------------

def load_me(path=None, today=None) -> dict:
    """{'boplats_registered': date | None, 'homeq_verified': date | None}.

    Reads me.local.json if it exists, else me.json. A missing file or a null
    date gives None (scores are then left out, not guessed).
    """
    path = Path(path) if path else (ME_LOCAL_PATH if ME_LOCAL_PATH.exists() else ME_PATH)
    today = today or date.today()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except ValueError as error:
        raise MeError(f"{path.name} is not valid JSON: {error}") from None
    if not isinstance(raw, dict):
        raise MeError(f"{path.name} must be a JSON object")

    me = {}
    for key in DATE_KEYS:
        value = raw.get(key)
        if value is None:
            me[key] = None
            continue
        try:
            me[key] = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise MeError(f"{path.name}: '{key}' must be a date like 2023-04-21, got {value!r}") from None
        if me[key] > today:
            raise MeError(f"{path.name}: '{key}' is in the future ({value})")
    return me


def queue_days(start, on) -> int | None:
    """Days in the queue on a given date (1 per day since signing up). None if no start date."""
    return None if start is None else (on - start).days


def queue_json(me: dict) -> dict:
    """What the web page needs to count days itself (it also projects forward)."""
    return {
        "boplats_start": me["boplats_registered"].isoformat() if me["boplats_registered"] else None,
        "homeq_start": me["homeq_verified"].isoformat() if me["homeq_verified"] else None,
    }


# --- chance buckets ----------------------------------------------------------

def _shift(bucket: str, steps: int) -> str:
    """Move a bucket up (+1) or down (-1), stopping at the ends."""
    return BUCKETS[max(0, min(len(BUCKETS) - 1, BUCKETS.index(bucket) + steps))]


def _n(value) -> str:
    """7200 -> '7 200' (Swedish thousands separator)."""
    return f"{value:,}".replace(",", " ")


def _score_boplats(listing, my_days, today):
    winners = listing.get("winners_queue_days")
    if not winners:
        return None, "Boplats visar ingen kötid för vinnarna av liknande lägenheter, så chansen kan inte räknas ut."

    ratio = my_days / winners
    bucket = "likely" if ratio >= LIKELY_RATIO else "possible" if ratio >= POSSIBLE_RATIO else "unlikely"
    note = f"Dina {_n(my_days)} dagar mot vinnarnas {_n(winners)} dagar ({round(ratio * 100)} %)."

    applicants = listing.get("applicants")
    if applicants is not None:
        try:
            days_left = (date.fromisoformat(listing["deadline"]) - today).days
        except (KeyError, TypeError, ValueError):  # no deadline, or one we cannot read
            days_left = None
        if applicants >= MANY_APPLICANTS:
            bucket = _shift(bucket, -1)
            note += f" {applicants} sökande: ett steg ner."
        elif applicants <= FEW_APPLICANTS and days_left is not None and days_left <= DEADLINE_DAYS:
            bucket = _shift(bucket, +1)
            note += f" Bara {applicants} sökande nära sista dagen: ett steg upp."
    return bucket, note


def _score_homeq(listing, my_days):
    needed = listing.get("points_needed_top10")
    if not needed:
        return None, "HomeQ visar ingen poänggräns för den här annonsen (än), så chansen kan inte räknas ut."

    bucket = "likely" if my_days >= needed else "possible" if my_days >= needed * HOMEQ_POSSIBLE_RATIO else "unlikely"
    note = f"Dina {_n(my_days)} poäng mot {_n(needed)} som krävs för topp 10."
    if listing.get("allocation") == "queue_guidance":
        bucket = _shift(bucket, -1)
        note += " Poängen är bara vägledande: ett steg ner."
    return bucket, note


def score_listing(listing: dict, days: dict, today: date):
    """(bucket, note) for one listing. `days` maps 'boplats'/'homeq' to your queue days."""
    allocation = listing.get("allocation")
    if allocation == "first_come":
        return "possible", "Först till kvarn: köpoängen spelar ingen roll, den som ansöker först vinner."
    if allocation == "lottery":
        applicants = listing.get("applicants")
        odds = f" Ungefär 1 chans på {applicants}." if applicants else ""
        return "possible", f"Lottning: din kötid spelar ingen roll.{odds}"
    if allocation == "points_landlord":
        return None, "Hyresvärden använder egna poäng, så chansen kan inte räknas ut."

    source = listing.get("source")
    my_days = days.get(source)
    if my_days is None:
        return None, "Ditt köstartdatum saknas i me.local.json."
    if source == "boplats" and allocation == "queue":
        return _score_boplats(listing, my_days, today)
    if source == "homeq" and allocation in ("queue", "queue_guidance"):
        return _score_homeq(listing, my_days)
    return None, "Tilldelningen är okänd, så chansen kan inte räknas ut."


def add_scores(rows: list[dict], me: dict, today: date) -> None:
    """Add 'bucket' and 'bucket_note' to every exported listing."""
    days = {
        "boplats": queue_days(me["boplats_registered"], today),
        "homeq": queue_days(me["homeq_verified"], today),
    }
    for row in rows:
        row["bucket"], row["bucket_note"] = score_listing(row, days, today)
