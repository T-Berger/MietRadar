"""Coordinate bounded, at-most-once application attempts for the Munich radar."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Protocol

from outgoing_judge import OutgoingJudgment, judge_outgoing


class DeliveryOutcome(Enum):
    SENT = auto()
    ALREADY_CONTACTED = auto()
    UNCONFIRMED = auto()
    UNAVAILABLE = auto()
    BLOCKED = auto()
    SKIPPED = auto()


class Sender(Protocol):
    def send(self, url: str, text: str, before_submit: Callable[[], bool]) -> DeliveryOutcome: ...


def install_schema(db: sqlite3.Connection) -> None:
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


def review_outgoing(db, item_id, message, *, kind, context="", provider="none", now):
    verdict = judge_outgoing(message, kind=kind, context=context, provider=provider)
    with db:
        _record_outgoing(db, item_id, message, kind, verdict, now)
    return verdict


def save_application_draft(
    db: sqlite3.Connection, listing_id: str, url: str, fresh_at: float, basis: str, message: str,
    *, context: str, provider: str, now: datetime,
) -> bool:
    """Review the draft, then save both records in the caller's transaction."""
    verdict = judge_outgoing(message, kind="application", context=context, provider=provider)
    saved = db.execute(
        "INSERT OR IGNORE INTO drafts(id, url, fresh_at, basis, message) VALUES (?, ?, ?, ?, ?)",
        (listing_id, url, fresh_at, basis, message),
    )
    if saved.rowcount:
        _record_outgoing(db, listing_id, message, "application", verdict, now)
    return bool(saved.rowcount)


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

    def _eligible(self, db: sqlite3.Connection, listing_id: str, fresh_at: datetime, max_age: timedelta) -> bool:
        now = self.clock()
        # Keep the hard 24h ceiling for external delivery even when the draft
        # window has been configured to retain older listings for review.
        cutoff = now - min(max_age, timedelta(hours=24))
        if not cutoff <= fresh_at <= now:
            return False
        info = db.execute(
            """SELECT d.fresh_at FROM drafts d LEFT JOIN delivery s ON s.id = d.id
               WHERE d.id = ? AND d.done = 0 AND d.message IS NOT NULL AND s.id IS NULL
                 AND d.basis != 'zuerst gesehen'""",
            (listing_id,),
        ).fetchone()
        if info is None or not cutoff.timestamp() <= info[0] <= now.timestamp():
            return False
        last_day = (now - timedelta(days=1)).timestamp()
        attempts = db.execute("""SELECT
            (SELECT COUNT(*) FROM delivery WHERE attempted_at >= ?) +
            (SELECT COUNT(*) FROM klein_reply_drafts WHERE attempted_at >= ?)
        """, (last_day, last_day)).fetchone()[0]
        return attempts < self.limit

    def deliver(
        self, db: sqlite3.Connection, listing_id: str, url: str, message: str,
        fresh_at: datetime, max_age: timedelta,
        *, context: str = "",
    ) -> str | None:
        if self.paused or not self._eligible(db, listing_id, fresh_at, max_age):
            return None
        if not review_outgoing(db, listing_id, message, kind="application", context=context,
                               provider=self.judge_provider, now=self.clock()).approved:
            return "Bewerbung vom Prüfer zurückgehalten"

        reserved = False

        def reserve() -> bool:
            nonlocal reserved
            with db:
                # Serialize the final quota and listing checks with the write.
                db.execute("BEGIN IMMEDIATE")
                if not self._eligible(db, listing_id, fresh_at, max_age):
                    return False
                stamp = self.clock().timestamp()
                db.execute("INSERT INTO delivery VALUES (?, 'unconfirmed', ?, ?)", (listing_id, stamp, stamp))
            reserved = True
            return True

        outcome = self.sender.send(url, message, reserve)
        stamp = self.clock().timestamp()
        if outcome == DeliveryOutcome.BLOCKED:
            self.paused = True
            return "Versand pausiert: Anmeldung, Prüfung oder Kontaktformular blockiert"
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
