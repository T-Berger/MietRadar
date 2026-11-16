"""Persist WG-Gesucht draft edits and approve the exact message and attachment."""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wg_source import ListingRef


class ApplicationReviewError(ValueError):
    """A draft could not be changed or approved."""


@dataclass(frozen=True)
class ApplicationAttachmentInfo:
    name: str
    digest: str


@dataclass(frozen=True)
class ApplicationAttachment(ApplicationAttachmentInfo):
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class ReviewedApplication:
    id: str
    url: str
    message: str
    fresh_at: datetime
    message_hash: str
    updated_at: float
    attachment_digest: str | None = None


MAX_APPLICATION_ATTACHMENT_BYTES = 5 * 1024 * 1024
_EXPIRED = "Die Anzeige liegt außerhalb des Versandzeitfensters."
_ATTACHMENT_CHANGED = "Der PDF-Anhang wurde geändert. Bitte prüfe die Bewerbung erneut."
_MAX_SEND_AGE = timedelta(hours=24)
_PENDING = """
    SELECT d.id, d.url, d.message, d.fresh_at, r.message_hash, r.updated_at, r.attachment_digest
    FROM application_reviews r JOIN drafts d ON d.id = r.id
    WHERE r.state = 'queued' AND d.done = 0 AND d.message IS NOT NULL
      AND d.fresh_at BETWEEN ? AND ?
      AND r.attachment_digest IS (SELECT digest FROM application_attachment WHERE id = 1)
      AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = d.id)
      AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers WHERE id = d.id)
"""
_EXACT_APPROVAL = """
    WHERE id = ? AND state = 'queued' AND message_hash = ? AND updated_at = ? AND attachment_digest IS ?
      AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = application_reviews.id)
"""


def install_application_reviews(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS application_reviews (
            id TEXT PRIMARY KEY REFERENCES drafts(id),
            message_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('edited', 'queued', 'failed')),
            reason TEXT,
            updated_at REAL NOT NULL,
            attachment_digest TEXT
        )
    """)
    if "attachment_digest" not in {column[1] for column in db.execute("PRAGMA table_info(application_reviews)")}:
        db.execute("ALTER TABLE application_reviews ADD COLUMN attachment_digest TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS application_attachment (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            digest TEXT NOT NULL,
            content BLOB NOT NULL
        )
    """)


def _hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _wg_listing(listing_id: str, url: str) -> ListingRef | None:
    ref = ListingRef.from_url(url)
    return ref if ref and ref.id == listing_id else None


@contextmanager
def _locked_db(
    db_path: Path | str,
    *,
    error: str = "MietRadar konnte den Entwurf gerade nicht speichern. Bitte lade ihn neu.",
) -> Iterator[sqlite3.Connection]:
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db, db:
            db.execute("PRAGMA secure_delete = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise ApplicationReviewError(error) from exc


def _current_attachment(db: sqlite3.Connection) -> ApplicationAttachmentInfo | None:
    row = db.execute("SELECT name, digest FROM application_attachment WHERE id = 1").fetchone()
    return ApplicationAttachmentInfo(*row) if row else None


def load_application_attachment(db_path: Path | str) -> ApplicationAttachmentInfo | None:
    """Read the current filename and digest without fetching the document."""
    try:
        path = Path(db_path)
        if not path.exists():
            return None
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            installed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'application_attachment' AND type = 'table'"
            ).fetchone()
            if not installed:
                return None
            return _current_attachment(db)
    except (OSError, sqlite3.Error) as exc:
        raise ApplicationReviewError("MietRadar konnte den PDF-Anhang gerade nicht laden.") from exc


def _invalidate_changed_attachment(db: sqlite3.Connection, digest: str | None) -> None:
    db.execute("""
        UPDATE application_reviews SET state = 'failed', reason = ?, updated_at = ?
        WHERE state = 'queued' AND attachment_digest IS NOT ?
          AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = application_reviews.id)
    """, (_ATTACHMENT_CHANGED, datetime.now(timezone.utc).timestamp(), digest))


def save_application_attachment(db_path: Path | str, *, filename: str, content: bytes) -> ApplicationAttachmentInfo:
    """Store the private PDF and invalidate approvals of a different attachment state."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if (not name.lower().endswith(".pdf") or not 4 < len(name) <= 180
            or any(unicodedata.category(character).startswith("C") for character in name)
            or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]):
        raise ApplicationReviewError("Wähle eine gültige PDF-Datei aus.")
    if len(content) > MAX_APPLICATION_ATTACHMENT_BYTES:
        raise ApplicationReviewError("Der PDF-Anhang darf höchstens 5 MB groß sein.")
    digest = hashlib.sha256(content).hexdigest()
    with _locked_db(db_path, error="MietRadar konnte den PDF-Anhang gerade nicht speichern.") as db:
        current = _current_attachment(db)
        if current and current.digest == digest:
            return current
        db.execute("""
            INSERT INTO application_attachment (id, name, digest, content) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, digest = excluded.digest, content = excluded.content
        """, (name, digest, content))
        _invalidate_changed_attachment(db, digest)
    return ApplicationAttachmentInfo(name, digest)


def remove_application_attachment(db_path: Path | str) -> None:
    """Delete the configured PDF and invalidate approvals that included it."""
    with _locked_db(db_path, error="MietRadar konnte den PDF-Anhang gerade nicht löschen.") as db:
        if db.execute("DELETE FROM application_attachment WHERE id = 1").rowcount:
            _invalidate_changed_attachment(db, None)


def get_application_attachment(db: sqlite3.Connection, digest: str) -> ApplicationAttachment | None:
    """Take a byte snapshot of the approved PDF before opening the delivery composer."""
    row = db.execute(
        "SELECT name, digest, content FROM application_attachment WHERE id = 1 AND digest = ?", (digest,)
    ).fetchone()
    if row and hashlib.sha256(row[2]).hexdigest() == digest:
        return ApplicationAttachment(*row)
    return None


def _current_draft(
    db: sqlite3.Connection, listing_id: str,
) -> tuple[float, str, str | None, str | None, str | None]:
    row = db.execute("""
        SELECT d.url, d.fresh_at, d.message, r.state, r.message_hash, r.attachment_digest
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
    approve: bool, now: datetime, max_age: timedelta, attachment_digest: str | None = None,
) -> str:
    """Save an edit or approve the submitted text and PDF in one transaction."""
    if not message.strip() or len(message) > 6000:
        raise ApplicationReviewError("Die Bewerbung braucht einen Text mit höchstens 6000 Zeichen.")
    state = "queued" if approve else "edited"
    age = min(max_age, _MAX_SEND_AGE) if approve else max_age
    with _locked_db(db_path) as db:
        fresh_at, current_message, previous_state, previous_hash, previous_attachment = _current_draft(db, listing_id)
        if not (now - age).timestamp() <= fresh_at <= now.timestamp():
            raise ApplicationReviewError(_EXPIRED if approve else "Der Entwurf ist inzwischen abgelaufen.")
        if approve:
            current = _current_attachment(db)
            if attachment_digest != (current.digest if current else None):
                raise ApplicationReviewError(_ATTACHMENT_CHANGED)
        digest = _hash(message)
        if (approve and previous_state == "queued" and current_message == message
                and previous_hash == digest and previous_attachment == attachment_digest):
            return "queued"
        _check_revision(current_message, expected_message)
        if previous_state == "queued":
            raise ApplicationReviewError("Zieh zuerst die Versandfreigabe zurück, um den Text zu ändern.")
        if message != expected_message:
            db.execute("UPDATE drafts SET message = ? WHERE id = ?", (message, listing_id))
            db.execute("DELETE FROM outgoing_checks WHERE id = ? AND kind = 'application'", (listing_id,))
        db.execute("""
            INSERT INTO application_reviews (id, message_hash, state, reason, updated_at, attachment_digest)
            VALUES (?, ?, ?, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET message_hash = excluded.message_hash,
                state = excluded.state, reason = NULL, updated_at = excluded.updated_at,
                attachment_digest = excluded.attachment_digest
        """, (listing_id, digest, state, now.timestamp(), attachment_digest if approve else None))
    return state


def cancel_application_review(db_path: Path | str, listing_id: str, expected_message: str, *, now: datetime) -> None:
    """Withdraw a queued approval while leaving the current text available for editing."""
    with _locked_db(db_path) as db:
        _, current_message, state, _, _ = _current_draft(db, listing_id)
        _check_revision(current_message, expected_message)
        if state == "edited":
            return
        if state != "queued":
            raise ApplicationReviewError("Für diesen Entwurf liegt keine Versandfreigabe vor.")
        db.execute("""
            UPDATE application_reviews SET state = 'edited', reason = NULL, updated_at = ?, attachment_digest = NULL
            WHERE id = ?
        """, (now.timestamp(), listing_id))


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
    for listing_id, url, message, fresh_at, message_hash, updated_at, attachment_digest in rows:
        ref = _wg_listing(listing_id, url)
        if ref is not None and message_hash == _hash(message):
            pending.append(ReviewedApplication(listing_id, ref.url, message,
                                               datetime.fromtimestamp(fresh_at, timezone.utc), message_hash,
                                               updated_at, attachment_digest))
    return tuple(pending)


def review_is_pending(
    db: sqlite3.Connection, listing_id: str, message: str, now: datetime, max_age: timedelta, *,
    attachment_digest: str | None = None,
) -> bool:
    """Check the exact approval again within the transaction reserving the send."""
    cutoff = (now - min(max_age, _MAX_SEND_AGE)).timestamp()
    row = db.execute(_PENDING + " AND d.id = ? AND d.message = ? AND r.attachment_digest IS ?",
                     (cutoff, now.timestamp(), listing_id, message, attachment_digest)).fetchone()
    return bool(row and _wg_listing(listing_id, row[1]) and row[4] == _hash(message))


def set_application_wait_reason(db: sqlite3.Connection, row: ReviewedApplication, reason: str | None) -> None:
    """Show why the exact approval is waiting without changing its identity."""
    with db:
        db.execute("UPDATE application_reviews SET reason = ?" + _EXACT_APPROVAL + " AND reason IS NOT ?",
                   (reason, row.id, row.message_hash, row.updated_at, row.attachment_digest, reason))


def fail_application_review(db: sqlite3.Connection, row: ReviewedApplication, reason: str, now: datetime) -> None:
    """Record a worker failure only for the approval it actually checked."""
    with db:
        db.execute("UPDATE application_reviews SET state = 'failed', reason = ?, updated_at = ?" + _EXACT_APPROVAL,
                   (reason, now.timestamp(), row.id, row.message_hash, row.updated_at, row.attachment_digest))
