"""Coordinate bounded, at-most-once application attempts for the Munich radar."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from munich_tiers import install_secondary_offers
from outgoing_judge import OutgoingJudgment, judge_outgoing
from rental_availability import AVAILABILITY_POLICY_VERSION
from reviewed_applications import (
    ApplicationAttachment, install_application_reviews, reject_unavailable_approval, review_is_pending,
)
from wg_credit_proof import CREDIT_PROOF_REQUIRED_REASON, record_credit_proof_requirement
from wg_first_message import viewing_slot_labels
from wg_source import RecipientRole


class DeliveryOutcome(Enum):
    SENT = auto()
    ALREADY_CONTACTED = auto()
    UNCONFIRMED = auto()
    UNAVAILABLE = auto()
    CREDIT_PROOF_REQUIRED = auto()
    BLOCKED = auto()
    SKIPPED = auto()


class Sender(Protocol):
    def send(self, url: str, text: str, before_submit: Callable[[], bool]) -> DeliveryOutcome: ...


def application_recipient_role(
    detected: str | None, stored: str | None = None, *, commercial: bool = False,
) -> RecipientRole:
    for role in (detected, stored):
        if role == "manager" or role == "roommate":
            return role
    return "manager" if commercial else "unknown"


@dataclass(frozen=True)
class FirstMessageReview:
    listing_title: str
    applicant_intro: str
    viewing_availability: tuple[datetime, ...]
    applicant_answers: dict[str, str]
    user_reviewed: bool = False
    commercial: bool = False
    recipient_role: RecipientRole | None = None

    def slots(self, now: datetime) -> tuple[str, ...]:
        return viewing_slot_labels(self.viewing_availability, now)

    def judge(self, message: str, *, context: str, provider: str, now: datetime) -> OutgoingJudgment:
        return judge_outgoing(
            message, context=context, provider=provider, first_message=True, listing_title=self.listing_title,
            applicant_intro=self.applicant_intro, viewing_slots=self.slots(now),
            applicant_answers=self.applicant_answers, user_reviewed=self.user_reviewed, commercial=self.commercial,
            recipient_role=self.recipient_role,
        )


def install_schema(db: sqlite3.Connection) -> None:
    install_secondary_offers(db)
    install_application_reviews(db)
    db.execute("""CREATE TABLE IF NOT EXISTS draft_render_signatures (
        id TEXT PRIMARY KEY REFERENCES drafts(id), signature TEXT NOT NULL
    )""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS delivery (
            id TEXT PRIMARY KEY REFERENCES drafts(id),
            state TEXT NOT NULL,
            recorded_at REAL NOT NULL,
            attempted_at REAL
        )
    """)
    db.execute("""CREATE TABLE IF NOT EXISTS outgoing_checks (
        id TEXT NOT NULL, kind TEXT NOT NULL, message_hash TEXT NOT NULL,
        approved INTEGER NOT NULL, reason TEXT NOT NULL, checked_at REAL NOT NULL,
        PRIMARY KEY (id, kind)
    )""")


def _record_outgoing(
    db: sqlite3.Connection, item_id: str, message: str, kind: str, verdict: OutgoingJudgment, now: datetime,
) -> None:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    db.execute("INSERT OR REPLACE INTO outgoing_checks VALUES (?, ?, ?, ?, ?, ?)",
               (item_id, kind, digest, verdict.approved, verdict.reason, now.timestamp()))


def review_outgoing(db, item_id, message, *, kind, context="", provider="none", now, first_message=None):
    verdict = (first_message.judge(message, context=context, provider=provider, now=now) if first_message else
               judge_outgoing(message, kind=kind, context=context, provider=provider))
    with db:
        _record_outgoing(db, item_id, message, kind, verdict, now)
    return verdict


def save_application_draft(
    db: sqlite3.Connection, listing_id: str, url: str, fresh_at: float, basis: str, message: str,
    *, context: str, provider: str, now: datetime, signature: str | None = None, previous_message: str | None = None,
    first_message: FirstMessageReview | None = None,
) -> bool:
    """Review the draft, then save both records in the caller's transaction."""
    verdict = (first_message.judge(message, context=context, provider=provider, now=now) if first_message else
               judge_outgoing(message, kind="application", context=context, provider=provider))
    if previous_message is None:
        saved = db.execute(
            "INSERT OR IGNORE INTO drafts(id, url, fresh_at, basis, message) VALUES (?, ?, ?, ?, ?)",
            (listing_id, url, fresh_at, basis, message),
        )
    else:
        saved = db.execute("""
            UPDATE drafts SET message = ? WHERE id = ? AND done = 0 AND message = ?
              AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = drafts.id)
              AND NOT EXISTS (SELECT 1 FROM application_reviews WHERE id = drafts.id)
        """, (message, listing_id, previous_message))
    if saved.rowcount:
        _record_outgoing(db, listing_id, message, "application", verdict, now)
        if signature:
            db.execute("INSERT OR REPLACE INTO draft_render_signatures VALUES (?, ?)", (listing_id, signature))
    return bool(saved.rowcount)


def within_send_limit(db: sqlite3.Connection, now: datetime, limit: int) -> bool:
    last_day = (now - timedelta(days=1)).timestamp()
    attempts = db.execute("""SELECT
        (SELECT COUNT(*) FROM delivery WHERE attempted_at >= ?) +
        (SELECT COUNT(*) FROM klein_reply_drafts WHERE attempted_at >= ?)
    """, (last_day, last_day)).fetchone()[0]
    return attempts < limit


class DeliveryQueue:
    def __init__(
        self,
        sender: Sender,
        limit: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        *, judge_provider: str = "none",
    ) -> None:
        self.sender = sender
        self.limit = limit
        self.clock = clock
        self.paused = False
        self.judge_provider = judge_provider

    def _eligible(self, db: sqlite3.Connection, listing_id: str, fresh_at: datetime, max_age: timedelta,
                  *, reviewed_message: str | None = None, attachment_digest: str | None = None) -> bool:
        now = self.clock()
        # Keep the hard 24h ceiling for external delivery even when the draft
        # window has been configured to retain older listings for review.
        cutoff = now - min(max_age, timedelta(hours=24))
        if not cutoff <= fresh_at <= now:
            return False
        if reviewed_message is not None:
            if not review_is_pending(db, listing_id, reviewed_message, now, max_age,
                                     attachment_digest=attachment_digest):
                return False
        else:
            info = db.execute(
                """SELECT d.fresh_at FROM drafts d LEFT JOIN delivery s ON s.id = d.id
                   WHERE d.id = ? AND d.done = 0 AND d.message IS NOT NULL AND s.id IS NULL
                     AND d.basis != 'zuerst gesehen'
                     AND NOT EXISTS (SELECT 1 FROM application_reviews r WHERE r.id = d.id)
                     AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers o WHERE o.id = d.id)
                     AND NOT EXISTS (SELECT 1 FROM listing_availability a WHERE a.id = d.id AND a.reason IS NOT NULL)
                     AND NOT EXISTS (SELECT 1 FROM listing_credit_proof p WHERE p.id = d.id AND p.required = 1)
                     AND (d.id NOT LIKE 'ka-%' OR EXISTS (
                         SELECT 1 FROM listing_availability a WHERE a.id = d.id AND a.policy_version = ?
                     ))""",
                (listing_id, AVAILABILITY_POLICY_VERSION),
            ).fetchone()
            if info is None or not cutoff.timestamp() <= info[0] <= now.timestamp():
                return False
        return within_send_limit(db, now, self.limit)

    def deliver(
        self, db: sqlite3.Connection, listing_id: str, url: str, message: str,
        fresh_at: datetime, max_age: timedelta,
        *, context: str = "", first_message: FirstMessageReview | None = None,
        attachment: ApplicationAttachment | None = None,
    ) -> str | None:
        user_reviewed = bool(first_message and first_message.user_reviewed)
        if attachment is not None and not user_reviewed:
            return None
        reviewed_message = message if user_reviewed else None
        attachment_digest = attachment.digest if attachment is not None else None
        if self.paused or not self._eligible(db, listing_id, fresh_at, max_age, reviewed_message=reviewed_message,
                                             attachment_digest=attachment_digest):
            return None
        checked_at = self.clock()
        if not review_outgoing(db, listing_id, message, kind="application", context=context,
                               provider=self.judge_provider, now=checked_at, first_message=first_message).approved:
            return "Bewerbung vom Prüfer zurückgehalten"

        reserved = False

        def reserve() -> bool:
            nonlocal reserved
            with db:
                # Serialize the final quota and listing checks with the write.
                db.execute("BEGIN IMMEDIATE")
                if not self._eligible(db, listing_id, fresh_at, max_age, reviewed_message=reviewed_message,
                                      attachment_digest=attachment_digest):
                    return False
                submit_at = self.clock()
                if first_message and not user_reviewed and first_message.slots(checked_at) != first_message.slots(submit_at):
                    return False
                stamp = submit_at.timestamp()
                db.execute("INSERT INTO delivery VALUES (?, 'unconfirmed', ?, ?)", (listing_id, stamp, stamp))
            reserved = True
            return True

        if attachment is None:
            outcome = self.sender.send(url, message, reserve)
        else:
            with TemporaryDirectory(prefix="mietradar-attachment-") as workspace:
                document = Path(workspace) / Path(attachment.name).name
                document.write_bytes(attachment.content)
                outcome = self.sender.send(url, message, reserve, attachment=document)
        stamp = self.clock().timestamp()
        if outcome == DeliveryOutcome.BLOCKED:
            self.paused = True
            return "Versand pausiert: Anmeldung, Prüfung oder Kontaktformular blockiert"
        if outcome == DeliveryOutcome.CREDIT_PROOF_REQUIRED:
            with db:
                record_credit_proof_requirement(db, listing_id, self.clock())
                if reserved:
                    db.execute("UPDATE delivery SET state = 'credit_proof_required', recorded_at = ? WHERE id = ?",
                               (stamp, listing_id))
                else:
                    reject_unavailable_approval(db, listing_id, CREDIT_PROOF_REQUIRED_REASON, self.clock())
            return CREDIT_PROOF_REQUIRED_REASON
        if reserved:
            if outcome != DeliveryOutcome.SENT:
                # A click may have occurred. Keep the claim and halt this session
                # so an incompatible form cannot burn through the queue.
                self.paused = True
                return "Versand unklar: Bitte Nachrichten prüfen"
            with db:
                db.execute("UPDATE delivery SET state = 'sent', recorded_at = ? WHERE id = ?", (stamp, listing_id))
                db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (listing_id,))
            return "Bewerbung gesendet"

        if outcome in {DeliveryOutcome.ALREADY_CONTACTED, DeliveryOutcome.UNAVAILABLE}:
            state = "already_contacted" if outcome == DeliveryOutcome.ALREADY_CONTACTED else "unavailable"
            with db:
                db.execute("INSERT OR IGNORE INTO delivery VALUES (?, ?, ?, NULL)", (listing_id, state, stamp))
                if outcome == DeliveryOutcome.ALREADY_CONTACTED:
                    db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (listing_id,))
            return "bereits kontaktiert" if outcome == DeliveryOutcome.ALREADY_CONTACTED else "Kontakt nicht verfügbar"
        if outcome == DeliveryOutcome.UNCONFIRMED:
            self.paused = True
            return "Versand pausiert: Ergebnis nicht überprüfbar"
        return None
