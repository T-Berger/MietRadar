"""Turn Kleinanzeigen saved-search emails into reviewed Munich drafts."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kleinanzeigen_source import extract_listings, normalize_munich_address
from llm_personalizer import personalise_message
from munich_delivery import DeliveryQueue
from munich_transit import TransitRouter
from wg_alerts import AlertsInbox
from wg_preferences import rejection_reason


def install_klein_review(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS klein_review (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            is_wg INTEGER NOT NULL,
            fresh_at REAL NOT NULL,
            basis TEXT NOT NULL,
            reason TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS klein_origins (
            id TEXT PRIMARY KEY REFERENCES drafts(id),
            address TEXT NOT NULL
        )
    """)


_WANTED_AD = re.compile(
    r"\b(?:wohnungsgesuch|mietgesuch|such(?:e|en|t) (?:ein(?:e|en)? )?(?:wohnung|zimmer|wg-zimmer)|"
    r"(?:wohnung|zimmer|wg-zimmer) gesucht)\b", re.I,
)


@dataclass
class KleinanzeigenRadar:
    router: TransitRouter
    template: str
    provider: str
    age: int = 28
    age_gap: int = 7
    delivery: DeliveryQueue | None = None

    def scan(
        self, db: sqlite3.Connection, inbox: AlertsInbox, now: datetime, cutoff: datetime,
        limit: int, retry_after: dict[str, datetime],
    ) -> Counter:
        with db:
            db.execute("DELETE FROM klein_review WHERE fresh_at < ?", (cutoff.timestamp(),))
            db.execute("""DELETE FROM klein_origins WHERE id NOT IN (
                SELECT id FROM drafts WHERE done = 0 AND fresh_at >= ? AND message IS NOT NULL
            )""", ((now - min(now - cutoff, timedelta(days=1))).timestamp(),))
        counts: Counter = Counter()
        checked_routes = set()
        items = inbox.recent_items(cutoff, limit, extract_listings)
        for listing, received_at in items:
            if listing.id in retry_after:
                continue
            draft = db.execute("""SELECT d.fresh_at, d.done, s.state, o.address FROM drafts d
                LEFT JOIN delivery s ON s.id = d.id LEFT JOIN klein_origins o ON o.id = d.id WHERE d.id = ?
            """, (listing.id,)).fetchone()
            if draft and (not self.delivery or self.delivery.paused or draft[1] or draft[2]):
                continue
            stamp = listing.published_at or received_at
            basis = "veröffentlicht" if listing.published_at else "Suchalarm erhalten"
            if stamp.tzinfo is None or stamp > now:
                continue
            if draft and stamp.timestamp() < draft[0]:
                with db:
                    db.execute("UPDATE drafts SET fresh_at = ?, basis = ? WHERE id = ?", (stamp.timestamp(), basis, listing.id))
            existing = db.execute("SELECT fresh_at, basis, reason FROM klein_review WHERE id = ?", (listing.id,)).fetchone()
            if existing and existing[0] < stamp.timestamp():
                stamp, basis = datetime.fromtimestamp(existing[0], timezone.utc), existing[1]
            if not cutoff <= stamp <= now:
                continue
            reason = "Wohnungsgesuch" if _WANTED_AD.search(listing.title) else rejection_reason(
                listing.title, listing.description, wanted=listing.wanted or "", wg_type=listing.wg_type or "",
                resident_ages=listing.resident_ages or "", age=self.age, age_gap=self.age_gap,
            )
            if reason:
                counts[f"Kleinanzeigen: {reason}"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    db.execute("DELETE FROM klein_review WHERE id = ?", (listing.id,))
                    db.execute("DELETE FROM klein_origins WHERE id = ?", (listing.id,))
                continue

            address = listing.address or (draft[3] if draft else None)
            match = self.router.qualifies(address, now) if address else None
            if match is True:
                checked_routes.add(listing.id)
                if draft:
                    with db:
                        self._save_origin(db, listing.id, address)
                elif self._save(db, listing.id, listing.url, listing.description, listing.is_wg, stamp.timestamp(), basis, address):
                    counts["neue Kleinanzeigen-Entwürfe"] += 1
                continue
            if match is False:
                counts["Kleinanzeigen: keine ÖPNV-Verbindung bis 30 Minuten"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    db.execute("DELETE FROM klein_review WHERE id = ?", (listing.id,))
                    db.execute("DELETE FROM klein_origins WHERE id = ?", (listing.id,))
                continue

            if draft:
                # Unknown routes stay eligible for a fresh check on the next poll.
                retry_after[listing.id] = now + timedelta(minutes=10)
                continue
            pending_reason = "ÖPNV nicht prüfbar" if listing.address else "genaue Adresse fehlt"
            if listing.address:
                retry_after[listing.id] = now + timedelta(minutes=10)
            if existing is None or existing[2] != pending_reason:
                counts["Kleinanzeigen zur Ortsprüfung"] += 1
            with db:
                db.execute("""
                    INSERT INTO klein_review VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title, excerpt = excluded.excerpt, is_wg = excluded.is_wg,
                        fresh_at = excluded.fresh_at, basis = excluded.basis, reason = excluded.reason
                """, (listing.id, listing.url, listing.title[:240], listing.description[:1200], listing.is_wg,
                      stamp.timestamp(), basis, pending_reason))
        if self.delivery and not self.delivery.paused:
            counts.update(self._send_ready(db, now, cutoff, checked_routes, retry_after))
        return counts

    def _send_ready(self, db, now, cutoff, checked_routes, retry_after) -> Counter:
        counts: Counter = Counter()
        send_window = min(now - cutoff, timedelta(hours=24))
        rows = db.execute("""SELECT d.id, d.url, d.message, d.fresh_at, o.address FROM drafts d
            JOIN klein_origins o ON o.id = d.id LEFT JOIN delivery s ON s.id = d.id
            WHERE d.done = 0 AND d.message IS NOT NULL AND d.fresh_at BETWEEN ? AND ? AND s.id IS NULL
            ORDER BY d.fresh_at DESC, d.id DESC
        """, ((now - send_window).timestamp(), now.timestamp()))
        for listing_id, url, message, fresh_at, address in rows.fetchall():
            if self.delivery.paused:
                break
            if listing_id in retry_after:
                continue
            if listing_id not in checked_routes and self.router.qualifies(address, now) is not True:
                continue
            result = self.delivery.deliver(db, listing_id, url, message, datetime.fromtimestamp(fresh_at, timezone.utc), send_window)
            if result:
                counts[f"Kleinanzeigen: {result}"] += 1
        return counts

    @staticmethod
    def _save_origin(db: sqlite3.Connection, listing_id: str, address: str) -> None:
        db.execute("INSERT OR REPLACE INTO klein_origins VALUES (?, ?)", (listing_id, address))

    def check_address(
        self, db: sqlite3.Connection, listing_id: str, address: str, now: datetime, cutoff: datetime,
    ) -> tuple[bool, str]:
        origin = normalize_munich_address(address)
        if not origin:
            return False, "Bitte eine veröffentlichte Münchner Adresse mit Straße, Hausnummer und PLZ angeben."
        row = db.execute("""
            SELECT url, excerpt, is_wg, fresh_at, basis FROM klein_review WHERE id = ? AND fresh_at BETWEEN ? AND ?
        """, (listing_id, cutoff.timestamp(), now.timestamp())).fetchone()
        if row is None:
            return False, "Kein aktueller Kleinanzeigen-Treffer mit dieser ID zur Ortsprüfung vorhanden."
        url, excerpt, is_wg, fresh_at, basis = row
        match = self.router.qualifies(origin, now)
        if match is not True:
            return False, "Die ÖPNV-Fahrt konnte nicht geprüft werden." if match is None else "Keine ÖPNV-Verbindung innerhalb von 30 Minuten gefunden."
        saved = self._save(db, listing_id, url, excerpt, bool(is_wg), fresh_at, basis, origin)
        return saved, "Entwurf gespeichert. Der Versandstatus steht unter delivery." if saved else "Der Treffer wurde bereits verarbeitet."

    def _save(
        self, db: sqlite3.Connection, listing_id: str, url: str, excerpt: str, is_wg: bool, fresh_at: float, basis: str,
        address: str,
    ) -> bool:
        message = personalise_message(
            self.template, excerpt, listing_url=url, provider=self.provider, listing_type="wg" if is_wg else "wohnung",
        )
        with db:
            saved = db.execute(
                "INSERT OR IGNORE INTO drafts(id, url, fresh_at, basis, message) VALUES (?, ?, ?, ?, ?)",
                (listing_id, url, fresh_at, basis, message),
            )
            db.execute("DELETE FROM klein_review WHERE id = ?", (listing_id,))
            if saved.rowcount:
                self._save_origin(db, listing_id, address)
        return bool(saved.rowcount)
