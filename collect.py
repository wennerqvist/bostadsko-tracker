"""Entry point: collect listings, save them, and export site/listings.json.

Usage:  python collect.py [--no-alert]
"""

import argparse
import json
import sys
from pathlib import Path

import requests

import geocode
import store
from collectors import boplats, homeq

EXPORT_PATH = Path(__file__).parent / "site" / "listings.json"


def export_json(conn, path=EXPORT_PATH) -> int:
    """Write all active listings (with their latest applicant count) as JSON."""
    rows = store.export_rows(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def save(conn, source: str, listings: list[dict], now: str, snapshots=True) -> tuple[int, int]:
    """Save one source's listings in one transaction (everything, or nothing).

    Returns (new, closed). Listings of this source that are no longer on the
    site are marked closed.
    """
    with conn:
        new = sum(store.upsert_listing(conn, item, now) for item in listings)
        if snapshots:
            for item in listings:
                store.snapshot(conn, item["id"], now, applicants=item.get("applicants"))
        closed = store.close_unseen(conn, source, now)
    return new, closed


def add_coordinates(conn, geocoder) -> tuple[int, int]:
    """Look up coordinates for listings that lack them. Returns (found, not found).

    Each found address is saved at once, so it is never looked up again. If the
    lookup service is unreachable we stop for this run; the rest is tried next time.
    """
    found = missing = 0
    todo = store.missing_coordinates(conn)
    if todo:
        print(f"Coordinates: looking up {len(todo)} addresses (about {round(len(todo) * 1.5 / 60)} min)...")
    for row in todo:
        try:
            result = geocoder.lookup(row["address"], row["kommun"])
        except (requests.RequestException, geocode.GeocodeError) as error:
            print(f"Coordinates: stopped early, will continue next run: {error}", file=sys.stderr)
            break
        if result:
            with conn:
                store.set_coordinates(conn, row["id"], *result)
            found += 1
        else:
            missing += 1
    return found, missing


def main(argv=None, db_path=store.DEFAULT_DB, export_path=EXPORT_PATH, geocoder=None) -> int:
    parser = argparse.ArgumentParser(description="Collect apartment listings.")
    parser.add_argument("--no-alert", action="store_true",
                        help="skip Telegram alerts (there are none yet, so this changes nothing today)")
    parser.parse_args(argv)

    conn = store.connect(db_path)
    now = store.now_iso()

    # (name, source id, how to fetch, errors that mean "this site failed", save snapshots?)
    sources = [
        ("Boplats", "boplats", lambda: boplats.collect(store.known_ids(conn, "boplats")),
         (boplats.PageChanged, requests.RequestException), True),
        # HomeQ gives no applicant counts, so a snapshot row would hold nothing.
        ("HomeQ", "homeq", homeq.collect,
         (homeq.HomeQError, requests.RequestException), False),
    ]

    failed = []
    for name, source, fetch, errors, snapshots in sources:
        try:
            listings = fetch()
        except errors as error:
            # Nothing is saved or closed for this source; its old listings stay as they were.
            print(f"{name} failed, its listings left untouched: {error}", file=sys.stderr)
            failed.append(name)
            continue
        new, closed = save(conn, source, listings, now, snapshots)
        print(f"{name}: {new} new, {len(listings) - new} already known, {closed} closed.")

    found, missing = add_coordinates(conn, geocoder or geocode.Geocoder())
    if found or missing:
        print(f"Coordinates: {found} found, {missing} not found.")

    exported = export_json(conn, export_path)
    print(f"Done: {exported} active listings written to {Path(export_path).name}.")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
