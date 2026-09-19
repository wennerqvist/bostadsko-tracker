"""Entry point: collect listings, save them, export site/listings.json, and send alerts.

Usage:  python collect.py [--no-alert]

After saving and exporting, sends the Telegram alerts that are due (see alerts.json and notify.py).
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import alerts
import geocode
import notify
import score
import store
from collectors import boplats, homeq

EXPORT_PATH = Path(__file__).parent / "site" / "listings.json"
INSIGHT_MAX_AGE = timedelta(hours=20)  # a listing's HomeQ points figure is read again after this long


def scored_rows(conn, me, today) -> list[dict]:
    """All active listings with applicant count and chance bucket (and 'alerted_at', for the alerts)."""
    rows = store.export_rows(conn)
    score.add_scores(rows, me, today)
    return rows


def export_json(conn, path=EXPORT_PATH, me=None, today=None) -> int:
    """Write all active listings (with applicant count and chance bucket) as JSON.

    Next to it goes queue.json: your queue start dates, so the page can count days itself.
    """
    path = Path(path)
    me = me or score.load_me()
    rows = scored_rows(conn, me, today or date.today())
    rows = [{key: value for key, value in row.items() if key != "alerted_at"} for row in rows]  # the page has no use for it
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    path.with_name("queue.json").write_text(json.dumps(score.queue_json(me), indent=2), encoding="utf-8")
    return len(rows)


def save(conn, source: str, listings: list[dict], now: str, snapshots=True) -> tuple[int, int]:
    """Save one source's listings in one transaction (everything, or nothing).

    Returns (new, closed). Listings of this source that are no longer on the
    site are marked closed.
    """
    with conn:
        new = sum(store.upsert_listing(conn, item, now) for item in listings)
        for item in listings:
            if snapshots or "insight_frame" in item:  # HomeQ: only listings whose points were read this run
                store.snapshot(conn, item["id"], now, applicants=item.get("applicants"),
                               points_needed_top10=item.get("points_needed_top10"), frame=item.get("insight_frame"))
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


def main(argv=None, db_path=store.DEFAULT_DB, export_path=EXPORT_PATH, geocoder=None, me_path=None,
         alerts_path=None) -> int:
    parser = argparse.ArgumentParser(description="Collect apartment listings.")
    parser.add_argument("--no-alert", action="store_true", help="skip Telegram alerts")
    args = parser.parse_args(argv)

    try:
        me = score.load_me(me_path)  # check this first, so a typo in the dates stops us before any fetching
    except score.MeError as error:
        print(f"Cannot read your queue dates: {error}", file=sys.stderr)
        return 1
    if None in me.values():
        print("Queue dates are missing (fill in me.local.json): listings get no chance score.", file=sys.stderr)

    conn = store.connect(db_path)
    now = store.now_iso()

    insight_cutoff = (datetime.fromisoformat(now) - INSIGHT_MAX_AGE).isoformat(timespec="seconds")

    # (name, source id, how to fetch, errors that mean "this site failed", save snapshots for every listing?)
    sources = [
        ("Boplats", "boplats", lambda: boplats.collect(store.known_ids(conn, "boplats")),
         (boplats.PageChanged, requests.RequestException), True),
        # HomeQ gives no applicant counts; it only gets a snapshot row when its points figure was read.
        ("HomeQ", "homeq",
         lambda: homeq.collect(store.ids_with_allocation(conn, "homeq"),
                               last_insight=store.latest_insight_times(conn, "homeq"),
                               insight_cutoff=insight_cutoff),
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

    exported = export_json(conn, export_path, me)
    print(f"Done: {exported} active listings written to {Path(export_path).name}.")

    if not args.no_alert:
        # After the export, so a problem with alerts never stops the listings from being saved and shown.
        try:
            searches = alerts.load_alerts(alerts_path)
            settings = alerts.load_settings(alerts_path)
            rows = scored_rows(conn, me, date.today())
            print("Alerts:", notify.run_alerts(conn, rows, searches, settings, datetime.fromisoformat(now)))
        except (alerts.AlertsError, notify.NotifyError) as error:
            print(f"Alerts failed: {error}", file=sys.stderr)
            failed.append("alerts")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
