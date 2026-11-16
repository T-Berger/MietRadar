"""Maintain one open reply draft per WG-Gesucht conversation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kleinanzeigen_judge import judge_reply
from outgoing_judge import judge_outgoing
from wg_reply_response import draft_wg_reply

if TYPE_CHECKING:
    from wg_reply_inbox import WGInboxEntry


def install_wg_reply_drafts(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS wg_reply_drafts (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, url TEXT NOT NULL,
            received_at REAL NOT NULL, label TEXT NOT NULL, summary TEXT NOT NULL,
            message TEXT, state TEXT NOT NULL, review_reason TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS wg_reply_inbox_status (
            id INTEGER PRIMARY KEY CHECK (id = 1), checked_at REAL NOT NULL, available INTEGER NOT NULL
        )
    """)


def sync_wg_reply_drafts(
    db: sqlite3.Connection, entries: tuple[WGInboxEntry, ...] | None, now: datetime,
    *, provider: str, outgoing_provider: str,
) -> int:
    """Record the latest verified reply or remove a draft once the user answers."""
    cutoff = (now - timedelta(days=30)).timestamp()
    with db:
        db.execute("DELETE FROM wg_reply_drafts WHERE received_at < ?", (cutoff,))
        db.execute("INSERT OR REPLACE INTO wg_reply_inbox_status VALUES (1, ?, ?)",
                   (now.timestamp(), entries is not None))
    if entries is None:
        return 0

    created = 0
    for entry in entries:
        if entry.text is None:
            with db:
                db.execute("DELETE FROM wg_reply_drafts WHERE id = ?", (entry.conversation_id,))
            continue
        if db.execute("SELECT 1 FROM wg_reply_drafts WHERE id = ? AND message_id = ?",
                      (entry.conversation_id, entry.message_id)).fetchone():
            continue
        outcome = judge_reply(entry.text, provider=provider)
        message = draft_wg_reply(entry.text, outcome, provider=provider)
        review = judge_outgoing(message, kind="reply", context=entry.text, provider=outgoing_provider) if message else None
        state = "draft" if review and review.approved else "held"
        reason = review.reason if review else "Bitte lies die Nachricht im WG-Gesucht-Postfach."
        with db:
            db.execute("""
                INSERT OR REPLACE INTO wg_reply_drafts
                    (id, message_id, url, received_at, label, summary, message, state, review_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (entry.conversation_id, entry.message_id, entry.url, now.timestamp(), outcome.label, outcome.summary,
                  message, state, reason))
        created += 1 if message else 0
    return created
