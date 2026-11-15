"""Turn Kleinanzeigen saved-search emails into reviewed Munich drafts."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kleinanzeigen_source import extract_listings, normalize_munich_address
from llm_personalizer import personalise_message
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


@dataclass
class KleinanzeigenRadar:
    router: TransitRouter
    template: str
    provider: str
    age: int = 28
    age_gap: int = 7

    def scan(
        self, db: sqlite3.Connection, inbox: AlertsInbox, now: datetime, cutoff: datetime,
        limit: int, retry_after: dict[str, datetime],
    ) -> Counter:
        with db:
            db.execute("DELETE FROM klein_review WHERE fresh_at < ?", (cutoff.timestamp(),))
        counts: Counter = Counter()
        items = inbox.recent_items(cutoff, limit, extract_listings)
        for listing, received_at in items:
            if listing.id in retry_after or db.execute("SELECT 1 FROM drafts WHERE id = ?", (listing.id,)).fetchone():
                continue
            stamp = listing.published_at or received_at
            basis = "veröffentlicht" if listing.published_at else "Suchalarm erhalten"
            if stamp.tzinfo is None or not cutoff <= stamp <= now:
                continue
            existing = db.execute("SELECT fresh_at, basis, reason FROM klein_review WHERE id = ?", (listing.id,)).fetchone()
            if existing and existing[0] < stamp.timestamp():
                stamp, basis = datetime.fromtimestamp(existing[0], timezone.utc), existing[1]
            if not cutoff <= stamp <= now:
                continue
            reason = rejection_reason(
                listing.title, listing.description, wanted=listing.wanted or "", wg_type=listing.wg_type or "",
                resident_ages=listing.resident_ages or "", age=self.age, age_gap=self.age_gap,
            )
            if reason:
                counts[f"Kleinanzeigen: {reason}"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    db.execute("DELETE FROM klein_review WHERE id = ?", (listing.id,))
                continue

            match = self.router.qualifies(listing.address, now) if listing.address else None
            if match is True:
                if self._save(db, listing.id, listing.url, listing.description, listing.is_wg, stamp.timestamp(), basis):
                    counts["neue Kleinanzeigen-Entwürfe"] += 1
                continue
            if match is False:
                counts["Kleinanzeigen: keine ÖPNV-Verbindung bis 30 Minuten"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    db.execute("DELETE FROM klein_review WHERE id = ?", (listing.id,))
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
        return counts

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
        saved = self._save(db, listing_id, url, excerpt, bool(is_wg), fresh_at, basis)
        return saved, "Entwurf gespeichert. Nachricht über Kleinanzeigen verschicken." if saved else "Der Treffer wurde bereits verarbeitet."

    def _save(
        self, db: sqlite3.Connection, listing_id: str, url: str, excerpt: str, is_wg: bool, fresh_at: float, basis: str,
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
        return bool(saved.rowcount)
