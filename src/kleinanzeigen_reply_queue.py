"""Keep reply drafts and send at most one judged acknowledgment per listing."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit

from kleinanzeigen_judge import ReplyJudgment, _redact
from kleinanzeigen_replies import KleinanzeigenReply, _inbox_link
from kleinanzeigen_response import automatic_response, draft_response
from munich_delivery import DeliveryOutcome, review_outgoing


class ReplySender(Protocol):
    def send_reply(self, url: str, listing_id: str, text: str, before_submit: Callable[[], bool]) -> DeliveryOutcome: ...


def install_reply_queue(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS klein_reply_drafts (
        id TEXT PRIMARY KEY, listing_id TEXT, url TEXT NOT NULL,
        message TEXT NOT NULL, context TEXT NOT NULL, received_at REAL NOT NULL,
        can_send INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'draft', attempted_at REAL
    )""")


def _exact_conversation(url: str) -> bool:
    return _inbox_link(url) == url and bool(urlsplit(url).fragment)


def save_reply_draft(
    db: sqlite3.Connection, reply: KleinanzeigenReply, judgment: ReplyJudgment,
    received: datetime, provider: str, outgoing_provider: str,
) -> None:
    auto = automatic_response(reply.text, judgment) if reply.listing_id and _exact_conversation(reply.url) else None
    message = auto or draft_response(reply.text, judgment, provider=provider)
    if not message:
        return
    verdict = review_outgoing(db, reply.id, message, kind="reply", context=reply.text,
                              provider=outgoing_provider, now=datetime.now(timezone.utc))
    # Retain only the redacted context needed to check a later outgoing attempt.
    context = _redact(reply.text)[:1800] if auto else ""
    with db:
        db.execute("""INSERT OR IGNORE INTO klein_reply_drafts
            (id, listing_id, url, message, context, received_at, can_send, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (reply.id, reply.listing_id, reply.url, message, context, received.timestamp(), bool(auto),
              "draft" if verdict.approved else "held"))


class ReplyDispatcher:
    def __init__(self, sender: ReplySender, provider: str, limit: int = 3,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.sender = sender
        self.provider = provider
        self.limit = limit
        self.clock = clock
        self.paused = False

    def run(self, db: sqlite3.Connection, now: datetime) -> int:
        if self.paused:
            return 0
        cutoff = (now - timedelta(hours=24)).timestamp()
        entries = db.execute("""SELECT id, listing_id, url, message, context FROM klein_reply_drafts
            WHERE can_send = 1 AND state IN ('draft', 'held') AND received_at BETWEEN ? AND ?
            ORDER BY received_at DESC, id DESC LIMIT 20
        """, (cutoff, now.timestamp())).fetchall()
        sent = 0
        for item_id, listing_id, url, message, context in entries:
            if not listing_id or not _exact_conversation(url) or not self._ready(db, item_id):
                continue
            verdict = review_outgoing(db, item_id, message, kind="reply", context=context,
                                      provider=self.provider, now=self.clock())
            if not verdict.approved:
                with db:
                    db.execute("UPDATE klein_reply_drafts SET state = 'held' WHERE id = ? AND attempted_at IS NULL", (item_id,))
                continue

            reserved = False

            def reserve() -> bool:
                nonlocal reserved
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if not self._ready(db, item_id):
                        return False
                    db.execute("UPDATE klein_reply_drafts SET state = 'unconfirmed', attempted_at = ? WHERE id = ?",
                               (self.clock().timestamp(), item_id))
                reserved = True
                return True

            outcome = self.sender.send_reply(url, listing_id, message, reserve)
            if reserved and outcome == DeliveryOutcome.SENT:
                with db:
                    db.execute("UPDATE klein_reply_drafts SET state = 'sent', context = '' WHERE id = ?", (item_id,))
                sent += 1
            elif reserved or outcome in {DeliveryOutcome.BLOCKED, DeliveryOutcome.UNCONFIRMED}:
                self.paused = True
                break
        return sent

    def _ready(self, db: sqlite3.Connection, item_id: str) -> bool:
        now = self.clock()
        cutoff = (now - timedelta(hours=24)).timestamp()
        eligible = db.execute("""SELECT 1 FROM klein_reply_drafts d
            WHERE d.id = ? AND d.can_send = 1 AND d.attempted_at IS NULL AND d.received_at BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM klein_reply_drafts other
                  WHERE other.listing_id = d.listing_id AND other.attempted_at IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM klein_activity a WHERE a.kind = 'reply'
                  AND (a.listing_id = d.listing_id OR a.url = d.url)
                  AND a.id != 'reply:' || d.id AND a.occurred_at >= d.received_at)
        """, (item_id, cutoff, now.timestamp())).fetchone()
        if not eligible:
            return False
        replies, applications = db.execute("""SELECT
            (SELECT COUNT(*) FROM klein_reply_drafts WHERE attempted_at >= ?),
            (SELECT COUNT(*) FROM delivery WHERE attempted_at >= ?)""", (cutoff, cutoff)).fetchone()
        return replies + applications < self.limit
