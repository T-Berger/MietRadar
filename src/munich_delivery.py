"""Coordinate bounded, at-most-once application attempts for the Munich radar."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Protocol


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


class DeliveryQueue:
    def __init__(
        self,
        sender: Sender,
        limit: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.sender = sender
        self.limit = limit
        self.clock = clock
        self.paused = False

    def _eligible(self, db: sqlite3.Connection, listing_id: str, fresh_at: datetime, max_age: timedelta) -> bool:
        now = self.clock()
        # Keep the hard 24h ceiling for external delivery even when the draft
        # window has been configured to retain older listings for review.
        cutoff = now - min(max_age, timedelta(hours=24))
        if not cutoff <= fresh_at <= now:
            return False
        info = db.execute(
            """SELECT d.fresh_at FROM drafts d LEFT JOIN delivery s ON s.id = d.id
               WHERE d.id = ? AND d.done = 0 AND d.message IS NOT NULL AND s.id IS NULL""",
            (listing_id,),
        ).fetchone()
        if info is None or not cutoff.timestamp() <= info[0] <= now.timestamp():
            return False
        attempts = db.execute("SELECT COUNT(*) FROM delivery WHERE attempted_at >= ?",
                              ((now - timedelta(days=1)).timestamp(),)).fetchone()[0]
        return attempts < self.limit

    def deliver(
        self, db: sqlite3.Connection, listing_id: str, url: str, message: str,
        fresh_at: datetime, max_age: timedelta,
    ) -> str | None:
        if self.paused or not self._eligible(db, listing_id, fresh_at, max_age):
            return None

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
