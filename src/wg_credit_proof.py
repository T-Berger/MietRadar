"""Remember when a WG-Gesucht contact form requires a separate credit report."""

import sqlite3
from datetime import datetime


CREDIT_PROOF_REQUIRED_REASON = (
    "WG-Gesucht verlangt eine aktuelle Bonitätsauskunft. Die SCHUFA-Datenkopie reicht hier nicht."
)


def install_listing_credit_proof(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS listing_credit_proof (
            id TEXT PRIMARY KEY,
            checked_at REAL NOT NULL,
            required INTEGER NOT NULL CHECK (required IN (0, 1))
        )
    """)


def record_credit_proof_requirement(db: sqlite3.Connection, listing_id: str, now: datetime) -> None:
    db.execute("""
        INSERT INTO listing_credit_proof (id, checked_at, required) VALUES (?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET checked_at = excluded.checked_at, required = excluded.required
        WHERE listing_credit_proof.checked_at <= excluded.checked_at
    """, (listing_id, now.timestamp()))


def record_credit_proof_request(db: sqlite3.Connection, listing_id: str, now: datetime, *, requested: bool) -> None:
    """A public request may change, but it cannot override a confirmed rejection."""
    if requested:
        db.execute("""
            INSERT INTO listing_credit_proof (id, checked_at, required) VALUES (?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET checked_at = excluded.checked_at
            WHERE listing_credit_proof.required = 0 AND listing_credit_proof.checked_at <= excluded.checked_at
        """, (listing_id, now.timestamp()))
    else:
        db.execute("DELETE FROM listing_credit_proof WHERE id = ? AND required = 0 AND checked_at <= ?",
                   (listing_id, now.timestamp()))
