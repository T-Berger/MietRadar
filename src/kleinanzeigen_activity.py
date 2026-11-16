"""Save listing matches and Kleinanzeigen activity and optionally email them."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from kleinanzeigen_judge import judge_reply
from kleinanzeigen_reply_queue import save_reply_draft
from kleinanzeigen_replies import extract_replies
from wg_alerts import AlertsInbox


def install_activity(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS klein_activity (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            listing_id TEXT,
            url TEXT NOT NULL,
            subject TEXT NOT NULL,
            summary TEXT NOT NULL,
            occurred_at REAL NOT NULL,
            emailed_at REAL
        )
    """)


@dataclass(frozen=True)
class Activity:
    id: str
    kind: str
    listing_id: str | None
    url: str
    subject: str
    summary: str
    occurred_at: float

    def save(self, db: sqlite3.Connection) -> int:
        with db:
            return db.execute("""INSERT OR IGNORE INTO klein_activity
                (id, kind, listing_id, url, subject, summary, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (self.id, self.kind, self.listing_id, self.url, self.subject, self.summary, self.occurred_at)).rowcount


def record_activity(db: sqlite3.Connection, now: datetime, sending_paused: bool = False) -> None:
    cutoff = (now - timedelta(hours=24)).timestamp()
    with db:
        db.execute("DELETE FROM klein_activity WHERE occurred_at < ?", ((now - timedelta(days=30)).timestamp(),))
        db.execute("UPDATE klein_reply_drafts SET context = '', message = '' WHERE received_at < ? AND (context <> '' OR message <> '')",
                   ((now - timedelta(days=30)).timestamp(),))
        db.execute("""INSERT OR IGNORE INTO klein_activity (id, kind, listing_id, url, subject, summary, occurred_at)
            SELECT 'review:' || r.id, 'review', r.id, r.url, 'Kleinanzeigen-Adresse prüfen',
                   'Die ÖPNV-Verbindung ist noch nicht bestätigt. Der Treffer steht unter klein-review.', r.fresh_at
            FROM klein_review r WHERE r.fresh_at BETWEEN ? AND ?
        """, (cutoff, now.timestamp()))
        db.execute("""INSERT OR IGNORE INTO klein_activity (id, kind, listing_id, url, subject, summary, occurred_at)
            SELECT 'reply-delivery:' || state || ':' || id, 'delivery', listing_id, url,
                   CASE state WHEN 'sent' THEN 'Kleinanzeigen-Antwort gesendet' ELSE 'Antwortversand unklar' END,
                   CASE state WHEN 'sent' THEN 'Die geprüfte Antwort ist im Kleinanzeigen-Postfach sichtbar.'
                              ELSE 'Bitte prüfe den Verlauf. Der Radar wiederholt den Versuch nicht.' END, attempted_at
            FROM klein_reply_drafts WHERE attempted_at BETWEEN ? AND ? AND state IN ('sent', 'unconfirmed')
        """, (cutoff, now.timestamp()))
        db.execute("""INSERT OR IGNORE INTO klein_activity (id, kind, listing_id, url, subject, summary, occurred_at)
            SELECT 'match:' || d.id, 'match', d.id, d.url,
                   CASE WHEN d.id GLOB 'ka-*' THEN 'Neuer Kleinanzeigen-Treffer' ELSE 'Neuer WG-Gesucht-Treffer' END,
                   CASE WHEN s.state = 'sent' THEN 'Die ÖPNV-Verbindung passt. Die Bewerbung wurde bereits gesendet.'
                        ELSE 'Die ÖPNV-Verbindung passt. Ein deutscher Bewerbungsentwurf liegt bereit.' END, ?
            FROM drafts d LEFT JOIN delivery s ON s.id = d.id
            WHERE d.fresh_at BETWEEN ? AND ?
              AND ((d.done = 0 AND d.message IS NOT NULL AND s.id IS NULL) OR s.state = 'sent')
              AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers o WHERE o.id = d.id)
              AND NOT EXISTS (SELECT 1 FROM listing_availability a WHERE a.id = d.id AND a.reason IS NOT NULL)
        """, (now.timestamp(), cutoff, now.timestamp()))
        db.execute("""INSERT OR IGNORE INTO klein_activity (id, kind, listing_id, url, subject, summary, occurred_at)
            SELECT 'delivery:' || s.state || ':' || s.id, 'delivery', s.id, d.url,
                CASE s.state WHEN 'sent' THEN 'Kleinanzeigen-Bewerbung gesendet'
                             WHEN 'unconfirmed' THEN 'Kleinanzeigen-Versand unklar'
                             ELSE 'Kleinanzeigen-Kontakt bereits vorhanden' END,
                CASE s.state WHEN 'sent' THEN 'Die Nachricht ist im Kleinanzeigen-Postfach sichtbar.'
                             WHEN 'unconfirmed' THEN 'Bitte prüfe dein Kleinanzeigen-Postfach. Der Radar wiederholt den Versuch nicht.'
                             ELSE 'Zu dieser Anzeige gibt es bereits einen Nachrichtenverlauf.' END, s.recorded_at
            FROM delivery s JOIN drafts d ON d.id = s.id
            WHERE s.id GLOB 'ka-*' AND s.recorded_at BETWEEN ? AND ?
              AND s.state IN ('sent', 'unconfirmed', 'already_contacted')
        """, (cutoff, now.timestamp()))
    if sending_paused:
        Activity(
            f"paused:{now.date().isoformat()}", "delivery", None, "", "Kleinanzeigen-Versand pausiert",
            "Bitte prüfe die Anmeldung, das Kontaktformular oder einen unklaren Versand im Kleinanzeigen-Postfach.",
            now.timestamp(),
        ).save(db)


def record_replies(
    db: sqlite3.Connection, inbox: AlertsInbox, provider: str, now: datetime, *, outgoing_provider: str = "none",
) -> int:
    count = 0
    for reply, received in inbox.recent_items(now - timedelta(days=7), 20, extract_replies):
        event_id = f"reply:{reply.id}"
        if received > now or db.execute("SELECT 1 FROM klein_activity WHERE id = ?", (event_id,)).fetchone():
            continue
        if reply.listing_id and not db.execute("SELECT 1 FROM drafts WHERE id = ?", (reply.listing_id,)).fetchone():
            continue
        judgment = judge_reply(reply.text, provider=provider)
        save_reply_draft(db, reply, judgment, received, provider, outgoing_provider)
        summary = f"Priorität {judgment.urgency}. {judgment.summary}"
        count += Activity(
            event_id, "reply", reply.listing_id, reply.url,
            f"Kleinanzeigen-Antwort · {judgment.label}", summary, received.timestamp(),
        ).save(db)
    return count


@dataclass(frozen=True)
class EmailNotifier:
    api_key: str
    sender: str
    recipient: str
    provider: str = "resend"

    def flush(self, db: sqlite3.Connection, now: datetime) -> tuple[int, bool]:
        # Older items stay in the local log. Brevo's duplicate protection is shorter than Resend's 24h.
        pending = db.execute("""SELECT id, subject, summary, url FROM klein_activity
            WHERE emailed_at IS NULL AND occurred_at BETWEEN ? AND ? ORDER BY occurred_at DESC LIMIT 15
        """, ((now - timedelta(hours=23)).timestamp(), now.timestamp())).fetchall()
        sent = 0
        for event_id, subject, summary, url in pending:
            if not self._send(event_id, subject, summary, url):
                return sent, False
            with db:
                db.execute("UPDATE klein_activity SET emailed_at = ? WHERE id = ?", (now.timestamp(), event_id))
            sent += 1
        return sent, True

    def _send(self, event_id: str, subject: str, summary: str, url: str) -> bool:
        subject = f"MietRadar · {subject}"
        body = f"{summary}\n\n{url}".strip()
        headers = {"Content-Type": "application/json", "User-Agent": "MietRadar/1.0"}
        if self.provider == "brevo":
            endpoint = "https://api.brevo.com/v3/smtp/email"
            headers["api-key"] = self.api_key
            payload = {"sender": {"name": "MietRadar", "email": self.sender}, "to": [{"email": self.recipient}],
                       "subject": subject, "textContent": body,
                       "headers": {"idempotencyKey": str(uuid5(NAMESPACE_URL, f"mietradar/email/{event_id}"))}}
            response_id = "messageId"
        elif self.provider == "resend":
            endpoint = "https://api.resend.com/emails"
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["Idempotency-Key"] = "mietradar/" + hashlib.sha256(event_id.encode()).hexdigest()
            payload = {"from": self.sender, "to": [self.recipient], "subject": subject, "text": body}
            response_id = "id"
        else:
            raise ValueError(f"Unbekannter E-Mail-Anbieter: {self.provider}")

        request = Request(endpoint, method="POST", data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return bool(json.load(response).get(response_id))
        except HTTPError as error:
            with error:
                if self.provider != "brevo" or error.code != 400:
                    return False
                try:
                    details = json.loads(error.read(4096))
                    return (details.get("code") == "duplicate_parameter"
                            and "idempotency" in details.get("message", "").lower())
                except (OSError, ValueError, AttributeError):
                    return False
        except (URLError, OSError, HTTPException, ValueError, AttributeError):
            return False
