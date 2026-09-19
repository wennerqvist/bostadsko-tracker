"""SQLite storage: schema, saving listings, snapshots, and reading them back."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).parent / "data" / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                 TEXT PRIMARY KEY,   -- 'boplats:<id>' or 'homeq:<id>'
    source             TEXT NOT NULL,      -- 'boplats' / 'homeq'
    url                TEXT,
    address            TEXT,
    area               TEXT,               -- stadsdel as the site names it
    kommun             TEXT,
    lat                REAL,
    lon                REAL,
    rent_sek           INTEGER,            -- per month
    size_m2            REAL,
    rooms              REAL,               -- 1.5 allowed
    floor              INTEGER,            -- NULL if unknown
    published          TEXT,               -- YYYY-MM-DD, when the site published it
    move_in            TEXT,               -- YYYY-MM-DD
    deadline           TEXT,               -- YYYY-MM-DD, last application date
    landlord           TEXT,
    allocation         TEXT,               -- queue / queue_guidance / first_come / lottery / points_landlord
    winners_queue_days INTEGER,            -- Boplats "kötid för liknande", in days
    is_short_lease     INTEGER,            -- 1 = time-limited lease, 0 = not, NULL = unknown
    first_seen        TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active'   -- 'active' / 'closed'
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id          TEXT NOT NULL REFERENCES listings(id),
    taken_at            TEXT NOT NULL,
    applicants          INTEGER,
    points_needed_top10 INTEGER
);
CREATE INDEX IF NOT EXISTS snapshots_by_listing ON snapshots(listing_id, taken_at);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id        TEXT NOT NULL REFERENCES listings(id),
    applied_at        TEXT,
    my_days_at_apply  INTEGER,
    ahead_of_me       INTEGER,
    outcome           TEXT DEFAULT 'pending',  -- pending / viewing / offered / declined / lost
    winner_queue_days INTEGER,
    notes             TEXT
);
"""

# Columns a collector may provide. Anything else in a listing dict is ignored.
LISTING_FIELDS = [
    "url", "address", "area", "kommun", "lat", "lon", "rent_sek", "size_m2",
    "rooms", "floor", "published", "move_in", "deadline", "landlord",
    "allocation", "winners_queue_days", "is_short_lease",
]

# Columns added after the first version. CREATE TABLE IF NOT EXISTS leaves an
# existing database alone, so connect() adds any of these that are missing.
ADDED_COLUMNS = {"is_short_lease": "INTEGER"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    """Open the database (creating the file and tables on first use)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    for column, kind in ADDED_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {kind}")
    return conn


def known_ids(conn, source: str) -> set[str]:
    """Ids we already have, so collectors can skip fetching their detail pages."""
    rows = conn.execute("SELECT id FROM listings WHERE source = ?", (source,))
    return {row["id"] for row in rows}


def upsert_listing(conn, listing: dict, now: str) -> bool:
    """Insert a new listing, or just refresh last_seen on one we already have.

    Returns True if the listing was new. An existing row is never overwritten
    with the (thinner) data from a search card, only touched to say "still here".
    """
    exists = conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing["id"],)).fetchone()
    if exists:
        conn.execute(
            "UPDATE listings SET last_seen = ?, status = 'active' WHERE id = ?",
            (now, listing["id"]),
        )
        return False

    columns = ["id", "source", "first_seen", "last_seen", "status"] + LISTING_FIELDS
    values = [listing["id"], listing["source"], now, now, "active"]
    values += [listing.get(field) for field in LISTING_FIELDS]
    marks = ", ".join("?" for _ in columns)
    conn.execute(f"INSERT INTO listings ({', '.join(columns)}) VALUES ({marks})", values)
    return True


def missing_coordinates(conn) -> list[sqlite3.Row]:
    """Active listings that have an address and kommun but no coordinates yet."""
    return conn.execute(
        "SELECT id, address, kommun FROM listings"
        " WHERE status = 'active' AND lat IS NULL AND address IS NOT NULL AND kommun IS NOT NULL"
        " ORDER BY id"
    ).fetchall()


def set_coordinates(conn, listing_id: str, lat: float, lon: float):
    """Fill in coordinates found after the listing was first saved."""
    conn.execute("UPDATE listings SET lat = ?, lon = ? WHERE id = ?", (lat, lon, listing_id))


def snapshot(conn, listing_id: str, taken_at: str, applicants=None, points_needed_top10=None):
    """Record the numbers that change over time, one row per listing per run."""
    conn.execute(
        "INSERT INTO snapshots (listing_id, taken_at, applicants, points_needed_top10)"
        " VALUES (?, ?, ?, ?)",
        (listing_id, taken_at, applicants, points_needed_top10),
    )


def close_unseen(conn, source: str, now: str) -> int:
    """Mark active listings of this source that were not seen in this run as closed."""
    cur = conn.execute(
        "UPDATE listings SET status = 'closed' WHERE source = ? AND status = 'active' AND last_seen < ?",
        (source, now),
    )
    return cur.rowcount


def export_rows(conn) -> list[dict]:
    """Active listings joined with their latest known applicant count."""
    rows = conn.execute(
        """
        SELECT l.*,
               (SELECT s.applicants FROM snapshots s
                 WHERE s.listing_id = l.id AND s.applicants IS NOT NULL
                 ORDER BY s.taken_at DESC LIMIT 1) AS applicants,
               (SELECT s.points_needed_top10 FROM snapshots s
                 WHERE s.listing_id = l.id AND s.points_needed_top10 IS NOT NULL
                 ORDER BY s.taken_at DESC LIMIT 1) AS points_needed_top10
        FROM listings l
        WHERE l.status = 'active'
        ORDER BY l.deadline, l.id
        """
    )
    return [dict(row) for row in rows]
