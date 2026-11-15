"""Track listing IDs across successful public WG-Gesucht searches."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from wg_source import ListingRef


FIRST_SEEN_BASIS = "zuerst gesehen"


@dataclass(frozen=True)
class Observation:
    first_seen_at: float
    previous_scan_at: float | None
    details_checked_at: float | None

    def recent_first_seen(self, cutoff: datetime) -> datetime | None:
        if self.previous_scan_at is None or self.previous_scan_at < cutoff.timestamp():
            return None
        return datetime.fromtimestamp(self.first_seen_at, timezone.utc)


def install_observations(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wg_inventory_scan (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            scope TEXT NOT NULL,
            scanned_at REAL
        );
        CREATE TABLE IF NOT EXISTS wg_observations (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            previous_scan_at REAL,
            details_checked_at REAL,
            published_at REAL,
            published_day TEXT
        );
        CREATE INDEX IF NOT EXISTS wg_observations_last_seen ON wg_observations(last_seen_at);
    """)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not any(column[1] == "published_day" for column in db.execute("PRAGMA table_info(wg_observations)")):
            db.execute("ALTER TABLE wg_observations ADD COLUMN published_day TEXT")


def record_inventory(
    db: sqlite3.Connection, refs: Sequence[ListingRef], scope: str, now: datetime, cutoff: datetime,
) -> dict[str, Observation]:
    """Commit a complete result snapshot before reviewing individual detail pages."""
    stamp = now.timestamp()
    with db:
        # Take the write lock before reading the prior snapshot, in case a manual scan is also running.
        db.execute("INSERT OR IGNORE INTO wg_inventory_scan(id, scope) VALUES (1, ?)", (scope,))
        prior_scope, last_poll = db.execute("SELECT scope, scanned_at FROM wg_inventory_scan WHERE id = 1").fetchone()
        if last_poll is not None and stamp < last_poll:
            return {}
        previous = (
            last_poll if prior_scope == scope and last_poll is not None and cutoff.timestamp() <= last_poll < stamp
            else None
        )
        db.executemany("""
            INSERT INTO wg_observations(id, url, first_seen_at, last_seen_at, previous_scan_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET url = excluded.url, last_seen_at = excluded.last_seen_at
        """, ((ref.id, ref.url, stamp, stamp, previous) for ref in refs))
        db.execute("UPDATE wg_inventory_scan SET scope = ?, scanned_at = ? WHERE id = 1", (scope, stamp))
        rows = db.execute("""
            SELECT id, first_seen_at, previous_scan_at, details_checked_at
            FROM wg_observations WHERE last_seen_at = ?
        """, (stamp,)).fetchall()
    return {listing_id: Observation(*values) for listing_id, *values in rows}


def record_details(
    db: sqlite3.Connection, ref: ListingRef, now: datetime, published_at: datetime | None,
    published_day: date | None = None,
) -> tuple[datetime | None, date | None]:
    published = published_at.timestamp() if published_at is not None else None
    day = published_day.isoformat() if published_day is not None else None
    stamp = now.timestamp()
    with db:
        if published is not None or day is not None:
            # Search alerts can yield publication evidence without a public inventory snapshot.
            db.execute("""INSERT OR IGNORE INTO wg_observations (id, url, first_seen_at, last_seen_at)
                          VALUES (?, ?, ?, ?)""", (ref.id, ref.url, stamp, stamp))
        row = db.execute("""
            UPDATE wg_observations
            SET details_checked_at = MAX(COALESCE(details_checked_at, 0), ?),
                published_at = CASE
                    WHEN ? IS NULL THEN published_at
                    WHEN published_at IS NULL THEN ?
                    ELSE MIN(published_at, ?)
                END,
                published_day = COALESCE(MIN(published_day, ?), published_day, ?)
            WHERE id = ? RETURNING published_at, published_day
        """, (stamp, published, published, published, day, day, ref.id)).fetchone()
    if row is None:
        return published_at, published_day
    timestamp, day = row
    return (datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else None,
            date.fromisoformat(day) if day is not None else None)
