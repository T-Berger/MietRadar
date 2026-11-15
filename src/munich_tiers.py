"""Store listings for the dashboard's B and C commute ratings."""

import sqlite3
from datetime import datetime


def install_secondary_offers(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS munich_secondary_offers (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            fresh_at REAL NOT NULL,
            basis TEXT NOT NULL,
            tier TEXT NOT NULL CHECK (tier IN ('b', 'c')),
            station TEXT NOT NULL CHECK (station IN ('Hbf', 'Ostbahnhof')),
            minutes INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 45)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS munich_secondary_offers_fresh ON munich_secondary_offers(fresh_at)")


def save_secondary_offer(
    db: sqlite3.Connection, listing_id: str, url: str, title: str, fresh_at: datetime,
    basis: str, tier: str, station: str, minutes: int,
) -> None:
    db.execute("""
        INSERT INTO munich_secondary_offers (id, url, title, fresh_at, basis, tier, station, minutes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            fresh_at = MIN(munich_secondary_offers.fresh_at, excluded.fresh_at),
            basis = CASE WHEN excluded.fresh_at < munich_secondary_offers.fresh_at THEN excluded.basis
                         ELSE munich_secondary_offers.basis END,
            tier = excluded.tier,
            station = excluded.station,
            minutes = excluded.minutes
    """, (listing_id, url, title[:240], fresh_at.timestamp(), basis, tier, station, minutes))


def remove_secondary_offer(db: sqlite3.Connection, listing_id: str) -> None:
    db.execute("DELETE FROM munich_secondary_offers WHERE id = ?", (listing_id,))


def secondary_freshness(db: sqlite3.Connection, listing_id: str) -> tuple[float, str] | None:
    return db.execute("SELECT fresh_at, basis FROM munich_secondary_offers WHERE id = ?", (listing_id,)).fetchone()
