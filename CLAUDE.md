# Bostadskö Tracker

## Who you're working with
Jakob is not a developer by trade and has been learning Claude Code for a
couple of months. Explain things simply and briefly. Define jargon the first
time you use it. Before writing code, say in 2–3 plain sentences what you're
about to do and why. After creating or changing a file, explain what it does
in 2–3 sentences. Prefer the simplest tool that works over the cleverest.
Never add a dependency, service or abstraction without saying why in one line.

## What this project is
A personal tool that collects apartment listings from boplats.se and homeq.se
into one SQLite database, shows them on a map of Göteborg with a chance score
based on Jakob's queue time, and sends Telegram alerts. One user (Jakob),
zero monthly cost, no server. Full plan: PLAN.md.

## Stack (don't change without discussing)
- Python 3.11+, `requests`, `beautifulsoup4`, stdlib `sqlite3`. No frameworks.
- Data: `data/listings.db` (SQLite), exported to `site/listings.json`.
- Frontend: `site/index.html` + plain JS, Leaflet + OpenStreetMap tiles.
  No build step, no npm, no React.
- Hosting: GitHub Pages from `site/`. Scheduler: GitHub Actions, every 3 h.
- Alerts: Telegram bot via `requests`. Token in a GitHub secret, never in code.

## Layout
- `collect.py` — entry point: run both collectors, snapshot, score, export, alert
- `collectors/boplats.py`, `collectors/homeq.py` — one function each: fetch → list of dicts in the unified schema
- `store.py` — SQLite schema + upsert + snapshot
- `score.py` — bucket logic (see PLAN.md "How your chances are estimated")
- `notify.py` — Telegram
- `me.json` — queue dates and criteria (gitignored copy `me.local.json` for real values)
- `site/` — the web page
- `tests/` — saved HTML/JSON samples from both sites + parser tests

## Rules
- Be a polite visitor: one request at a time, 1–2 s pause, descriptive User-Agent,
  never re-fetch a listing detail page already stored. Poll no more than every 3 h.
- Never auto-apply to listings. Never store passwords or session cookies.
- Parsers must be tested against saved sample pages in `tests/samples/`, so a
  site redesign breaks a test, not the nightly run.
- Swedish number formats in the UI (7 200 kr, 52,5 m²). Dates as YYYY-MM-DD.
- Commit after every working step with a one-line message in plain English.

## Commands
- `python collect.py` — full run (fetch, store, score, export, alert)
- `python collect.py --no-alert` — same without Telegram
- `python -m pytest` — tests
- `python -m http.server -d site 8000` — view the map at localhost:8000
