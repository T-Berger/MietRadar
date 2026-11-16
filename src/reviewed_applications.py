"""Persist WG-Gesucht draft edits and approval of the exact message to send."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wg_source import ListingRef


class ApplicationReviewError(ValueError):
    """A draft could not be changed or approved."""


@dataclass(frozen=True)
class ReviewedApplication:
    id: str
    url: str
    message: str
    fresh_at: datetime
    message_hash: str
    updated_at: float


_EXPIRED = "Die Anzeige liegt außerhalb des Versandzeitfensters."
_MAX_SEND_AGE = timedelta(hours=24)
_PENDING = """
    SELECT d.id, d.url, d.message, d.fresh_at, r.message_hash, r.updated_at
    FROM application_reviews r JOIN drafts d ON d.id = r.id
    WHERE r.state = 'queued' AND d.done = 0 AND d.message IS NOT NULL
      AND d.fresh_at BETWEEN ? AND ?
      AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = d.id)
      AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers WHERE id = d.id)
"""


def install_application_reviews(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS application_reviews (
            id TEXT PRIMARY KEY REFERENCES drafts(id),
            message_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('edited', 'queued', 'failed')),
            reason TEXT,
            updated_at REAL NOT NULL
        )
    """)


def _hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _wg_listing(listing_id: str, url: str) -> ListingRef | None:
    ref = ListingRef.from_url(url)
    return ref if ref and ref.id == listing_id else None


@contextmanager
def _locked_db(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise ApplicationReviewError("MietRadar konnte den Entwurf gerade nicht speichern. Bitte lade ihn neu.") from exc


def _current_draft(
    db: sqlite3.Connection, listing_id: str,
) -> tuple[float, str, str | None, str | None]:
    row = db.execute("""
        SELECT d.url, d.fresh_at, d.message, r.state, r.message_hash
        FROM drafts d LEFT JOIN application_reviews r ON r.id = d.id
        WHERE d.id = ? AND d.done = 0 AND d.message IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = d.id)
          AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers WHERE id = d.id)
    """, (listing_id,)).fetchone()
    if row is None or not _wg_listing(listing_id, row[0]):
        raise ApplicationReviewError("Der Entwurf ist nicht mehr für diese Aktion verfügbar.")
    return row[1:]


def _check_revision(current_message: str, expected_message: str) -> None:
    if current_message != expected_message:
        raise ApplicationReviewError("Der Entwurf wurde inzwischen geändert. Bitte lade ihn neu und prüfe die aktuelle Fassung.")


def review_application(
    db_path: Path | str, listing_id: str, expected_message: str, message: str, *,
    approve: bool, now: datetime, max_age: timedelta,
) -> str:
    """Save an edit or save and approve the submitted text in one transaction."""
    if not message.strip() or len(message) > 6000:
        raise ApplicationReviewError("Die Bewerbung braucht einen Text mit höchstens 6000 Zeichen.")
    state = "queued" if approve else "edited"
    age = min(max_age, _MAX_SEND_AGE) if approve else max_age
    with _locked_db(db_path) as db:
        fresh_at, current_message, previous_state, previous_hash = _current_draft(db, listing_id)
        if not (now - age).timestamp() <= fresh_at <= now.timestamp():
            raise ApplicationReviewError(_EXPIRED if approve else "Der Entwurf ist inzwischen abgelaufen.")
        digest = _hash(message)
        if approve and previous_state == "queued" and current_message == message and previous_hash == digest:
            return "queued"
        _check_revision(current_message, expected_message)
        if previous_state == "queued":
            raise ApplicationReviewError("Zieh zuerst die Versandfreigabe zurück, um den Text zu ändern.")
        if message != expected_message:
            db.execute("UPDATE drafts SET message = ? WHERE id = ?", (message, listing_id))
            db.execute("DELETE FROM outgoing_checks WHERE id = ? AND kind = 'application'", (listing_id,))
        db.execute("""
            INSERT INTO application_reviews (id, message_hash, state, reason, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET message_hash = excluded.message_hash,
                state = excluded.state, reason = NULL, updated_at = excluded.updated_at
        """, (listing_id, digest, state, now.timestamp()))
    return state


def cancel_application_review(db_path: Path | str, listing_id: str, expected_message: str, *, now: datetime) -> None:
    """Withdraw a queued approval while leaving the current text available for editing."""
    with _locked_db(db_path) as db:
        _, current_message, state, _ = _current_draft(db, listing_id)
        _check_revision(current_message, expected_message)
        if state == "edited":
            return
        if state != "queued":
            raise ApplicationReviewError("Für diesen Entwurf liegt keine Versandfreigabe vor.")
        db.execute("UPDATE application_reviews SET state = 'edited', reason = NULL, updated_at = ? WHERE id = ?",
                   (now.timestamp(), listing_id))


def pending_application_reviews(
    db: sqlite3.Connection, now: datetime, max_age: timedelta,
) -> tuple[ReviewedApplication, ...]:
    cutoff = (now - min(max_age, _MAX_SEND_AGE)).timestamp()
    stamp = now.timestamp()
    with db:
        db.execute("""
            UPDATE application_reviews SET state = 'failed', reason = ?, updated_at = ?
            WHERE state = 'queued' AND id IN (SELECT id FROM drafts WHERE fresh_at < ?)
              AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = application_reviews.id)
        """, (_EXPIRED, stamp, cutoff))
        rows = db.execute(_PENDING + " ORDER BY d.fresh_at DESC, r.updated_at, d.id", (cutoff, stamp)).fetchall()
    pending = []
    for listing_id, url, message, fresh_at, message_hash, updated_at in rows:
        ref = _wg_listing(listing_id, url)
        if ref is not None and message_hash == _hash(message):
            pending.append(ReviewedApplication(listing_id, ref.url, message,
                                               datetime.fromtimestamp(fresh_at, timezone.utc), message_hash, updated_at))
    return tuple(pending)


def review_is_pending(
    db: sqlite3.Connection, listing_id: str, message: str, now: datetime, max_age: timedelta,
) -> bool:
    """Check the exact approval again within the transaction reserving the send."""
    cutoff = (now - min(max_age, _MAX_SEND_AGE)).timestamp()
    row = db.execute(_PENDING + " AND d.id = ? AND d.message = ?", (cutoff, now.timestamp(), listing_id, message)).fetchone()
    return bool(row and _wg_listing(listing_id, row[1]) and row[4] == _hash(message))


def fail_application_review(db: sqlite3.Connection, row: ReviewedApplication, reason: str, now: datetime) -> None:
    """Record a worker failure only for the approval it actually checked."""
    with db:
        db.execute("""
            UPDATE application_reviews SET state = 'failed', reason = ?, updated_at = ?
            WHERE id = ? AND state = 'queued' AND message_hash = ? AND updated_at = ?
              AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = application_reviews.id)
        """, (reason, now.timestamp(), row.id, row.message_hash, row.updated_at))
