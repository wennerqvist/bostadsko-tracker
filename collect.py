"""Entry point: collect listings, save them, and export site/listings.json.

Usage:  python collect.py [--no-alert]
"""

import argparse
import json
import sys
from pathlib import Path

import requests

import store
from collectors import boplats

EXPORT_PATH = Path(__file__).parent / "site" / "listings.json"


def export_json(conn, path=EXPORT_PATH) -> int:
    """Write all active listings (with their latest applicant count) as JSON."""
    rows = store.export_rows(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect apartment listings.")
    parser.add_argument("--no-alert", action="store_true",
                        help="skip Telegram alerts (there are none yet, so this changes nothing today)")
    parser.parse_args()

    conn = store.connect()
    now = store.now_iso()

    try:
        listings = boplats.collect(store.known_ids(conn, "boplats"))
    except (boplats.PageChanged, requests.RequestException) as error:
        print(f"Boplats failed, database left untouched: {error}", file=sys.stderr)
        return 1

    with conn:  # one transaction: everything is saved, or nothing is
        new = sum(store.upsert_listing(conn, item, now) for item in listings)
        for item in listings:
            store.snapshot(conn, item["id"], now, applicants=item.get("applicants"))
        closed = store.close_unseen(conn, "boplats", now)

    exported = export_json(conn)
    print(f"Done: {new} new, {len(listings) - new} already known, {closed} closed. "
          f"{exported} active listings written to {EXPORT_PATH.relative_to(Path(__file__).parent)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
