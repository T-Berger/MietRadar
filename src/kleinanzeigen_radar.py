"""Turn Kleinanzeigen saved-search emails into reviewed Munich drafts."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from kleinanzeigen_source import extract_listings, normalize_munich_address
from listing_profiles import ROUTE_FIT, record_listing_location, record_listing_profile, record_listing_route
from llm_personalizer import personalise_message
from munich_delivery import DeliveryQueue, save_application_draft
from munich_tiers import remove_secondary_offer, save_secondary_offer
from munich_transit import MUNICH_TIME, PRIMARY_TIERS, TransitRouter
from rental_availability import AVAILABILITY_POLICY_VERSION, availability_rejection, record_listing_availability
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
_SECONDARY_STATIONS = {"b": "Ostbahnhof", "c": "Hbf"}


def _remove_candidates(db: sqlite3.Connection, listing_id: str) -> None:
    db.execute("DELETE FROM klein_review WHERE id = ?", (listing_id,))
    db.execute("DELETE FROM klein_origins WHERE id = ?", (listing_id,))
    remove_secondary_offer(db, listing_id)
    record_listing_location(db, listing_id, None)


@dataclass
class KleinanzeigenRadar:
    router: TransitRouter
    template: str
    provider: str
    age: int = 28
    age_gap: int = 7
    delivery: DeliveryQueue | None = None
    outgoing_provider: str = "none"
    render_signature: str | None = None

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
        today = now.astimezone(MUNICH_TIME).date()
        items = inbox.recent_items(cutoff, limit, extract_listings)
        for listing, received_at in items:
            record_listing_profile(db, listing)
            draft = db.execute("""SELECT d.fresh_at, d.done, s.state, o.address, d.basis, d.message, r.signature,
                p.route_tier
                FROM drafts d LEFT JOIN delivery s ON s.id = d.id LEFT JOIN klein_origins o ON o.id = d.id
                LEFT JOIN draft_render_signatures r ON r.id = d.id
                LEFT JOIN listing_profiles p ON p.id = d.id WHERE d.id = ?
            """, (listing.id,)).fetchone()
            refresh = bool(draft and self.render_signature and draft[5] and draft[6] != self.render_signature)
            stamp = listing.published_at or received_at
            basis = "veröffentlicht" if listing.published_at else "Suchalarm erhalten"
            if stamp.tzinfo is None or stamp > now:
                continue
            if draft and not (draft[1] or draft[2]) and stamp.timestamp() < draft[0]:
                with db:
                    db.execute("UPDATE drafts SET fresh_at = ?, basis = ? WHERE id = ?", (stamp.timestamp(), basis, listing.id))
            if draft and draft[0] < stamp.timestamp():
                stamp, basis = datetime.fromtimestamp(draft[0], timezone.utc), draft[4]
            existing = db.execute("SELECT fresh_at, basis, reason FROM klein_review WHERE id = ?", (listing.id,)).fetchone()
            if existing and existing[0] < stamp.timestamp():
                stamp, basis = datetime.fromtimestamp(existing[0], timezone.utc), existing[1]
            secondary = db.execute("""SELECT fresh_at, basis, tier, station, minutes FROM munich_secondary_offers
                                    WHERE id = ?""", (listing.id,)).fetchone()
            if secondary and secondary[0] < stamp.timestamp():
                stamp, basis = datetime.fromtimestamp(secondary[0], timezone.utc), secondary[1]
            if not cutoff <= stamp <= now:
                if secondary and stamp.timestamp() < secondary[0]:
                    _, _, tier, station, minutes = secondary
                    with db:
                        save_secondary_offer(db, listing.id, listing.url, listing.title, stamp, basis, tier, station, minutes)
                continue
            availability = availability_rejection(
                listing.title, listing.description, today,
                available_from=listing.available_from, available_until=listing.available_until,
            )
            with db:
                record_listing_availability(db, listing.id, availability)
                if availability:
                    _remove_candidates(db, listing.id)
            if availability:
                counts[f"Kleinanzeigen: {availability}"] += 1
                continue
            address_changed = bool(draft and draft[3] and listing.address
                                   and draft[3].casefold() != listing.address.casefold())
            if address_changed:
                # A previously saved origin must not be reused after the ad supplies a different address.
                with db:
                    db.execute("DELETE FROM klein_origins WHERE id = ?", (listing.id,))
                    record_listing_location(db, listing.id, None)
            if listing.id in retry_after or (draft and (
                draft[1] or draft[2] or (draft[7] is not None and not (refresh or address_changed)
                                        and (not self.delivery or self.delivery.paused))
            )):
                continue
            reason = "Wohnungsgesuch" if _WANTED_AD.search(listing.title) else rejection_reason(
                listing.title, listing.description, wanted=listing.wanted or "", wg_type=listing.wg_type or "",
                resident_ages=listing.resident_ages or "", age=self.age, age_gap=self.age_gap,
            )
            if reason:
                record_listing_profile(db, listing, fit="unclear", reason=f"Suchfilter: {reason}.")
                counts[f"Kleinanzeigen: {reason}"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    _remove_candidates(db, listing.id)
                continue

            address = listing.address or (draft[3] if draft else None)
            tier, minutes = self.router.classify(address, now) if address else (None, None)
            fit, fit_reason = ROUTE_FIT.get(tier, ("unclear", "Die ÖPNV-Verbindung konnte nicht geprüft werden."
                                                  if address else "Die genaue Adresse fehlt."))
            mapped = tier in PRIMARY_TIERS or tier in _SECONDARY_STATIONS
            record_listing_profile(db, replace(listing, address=address), fit=fit, reason=fit_reason,
                                   provider=self.provider if tier != "outside" else "none",
                                   route_tier=tier, route_minutes=minutes,
                                   coordinates=self.router.coordinates(address) if mapped else None)
            if tier in PRIMARY_TIERS:
                checked_routes.add(listing.id)
                if refresh:
                    if self._save(db, listing.id, listing.url, listing.description, listing.is_wg,
                                  stamp.timestamp(), basis, address, now, title=listing.title, previous_message=draft[5]):
                        counts["Kleinanzeigen-Entwürfe aktualisiert"] += 1
                    retry_after[listing.id] = now + timedelta(minutes=1)
                elif draft:
                    with db:
                        self._save_origin(db, listing.id, address)
                        remove_secondary_offer(db, listing.id)
                elif self._save(db, listing.id, listing.url, listing.description, listing.is_wg,
                                stamp.timestamp(), basis, address, now, title=listing.title):
                    counts["neue Kleinanzeigen-Entwürfe"] += 1
                continue
            if tier in _SECONDARY_STATIONS and minutes is not None:
                self._save_secondary_offer(db, listing.id, listing.url, listing.title, stamp, basis, tier, minutes)
                if secondary is None or secondary[2] != tier:
                    counts[f"neue Kleinanzeigen-Angebote in Liste {tier.upper()}"] += 1
                continue
            if tier == "outside":
                counts["Kleinanzeigen: ÖPNV außerhalb der Suchgrenzen"] += 1
                retry_after[listing.id] = now + (now - cutoff)
                with db:
                    _remove_candidates(db, listing.id)
                continue

            if draft or secondary:
                # Keep earlier results when a route cannot be rechecked or the alert omits the manual address.
                if address:
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
        self._backfill_locations(db, now, cutoff, limit)
        return counts

    def _backfill_locations(self, db: sqlite3.Connection, now: datetime, cutoff: datetime, limit: int) -> None:
        # Older primary drafts already have the exact address used for their route check.
        rows = db.execute("""
            SELECT d.id, o.address FROM drafts d
            JOIN klein_origins o ON o.id = d.id
            JOIN listing_profiles p ON p.id = d.id
            JOIN listing_availability a ON a.id = d.id AND a.reason IS NULL AND a.policy_version = ?
            WHERE d.done = 0 AND d.message IS NOT NULL AND d.fresh_at BETWEEN ? AND ?
              AND p.route_tier IN ('a*', 'a') AND (p.latitude IS NULL OR p.longitude IS NULL)
              AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers m WHERE m.id = d.id)
            ORDER BY d.fresh_at DESC LIMIT ?
        """, (AVAILABILITY_POLICY_VERSION, cutoff.timestamp(), now.timestamp(), min(limit, 5))).fetchall()
        for listing_id, address in rows:
            if (coordinates := self.router.coordinates(address)) is not None:
                with db:
                    record_listing_location(db, listing_id, coordinates, address=address)

    def _send_ready(self, db, now, cutoff, checked_routes, retry_after) -> Counter:
        counts: Counter = Counter()
        send_window = min(now - cutoff, timedelta(hours=24))
        rows = db.execute("""SELECT d.id, d.url, d.message, d.fresh_at, o.address FROM drafts d
            JOIN klein_origins o ON o.id = d.id
            JOIN listing_availability a ON a.id = d.id AND a.reason IS NULL AND a.policy_version = ?
            LEFT JOIN delivery s ON s.id = d.id
            WHERE d.done = 0 AND d.message IS NOT NULL AND d.fresh_at BETWEEN ? AND ? AND s.id IS NULL
              AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers o2 WHERE o2.id = d.id)
            ORDER BY d.fresh_at DESC, d.id DESC
        """, (AVAILABILITY_POLICY_VERSION, (now - send_window).timestamp(), now.timestamp()))
        for listing_id, url, message, fresh_at, address in rows.fetchall():
            if self.delivery.paused:
                break
            if listing_id in retry_after:
                continue
            if listing_id not in checked_routes:
                tier, minutes = self.router.classify(address, now)
                coordinates = self.router.coordinates(address) if tier in PRIMARY_TIERS else None
                with db:
                    record_listing_route(db, listing_id, tier, minutes)
                    if coordinates is not None or tier == "outside":
                        record_listing_location(db, listing_id, coordinates, address=address)
                if tier not in PRIMARY_TIERS:
                    continue
            result = self.delivery.deliver(db, listing_id, url, message, datetime.fromtimestamp(fresh_at, timezone.utc), send_window)
            if result:
                counts[f"Kleinanzeigen: {result}"] += 1
        return counts

    @staticmethod
    def _save_origin(db: sqlite3.Connection, listing_id: str, address: str) -> None:
        db.execute("INSERT OR REPLACE INTO klein_origins VALUES (?, ?)", (listing_id, address))

    @staticmethod
    def _save_secondary_offer(
        db: sqlite3.Connection, listing_id: str, url: str, title: str, fresh_at: datetime, basis: str, tier: str, minutes: int,
    ) -> None:
        with db:
            save_secondary_offer(db, listing_id, url, title, fresh_at, basis, tier, _SECONDARY_STATIONS[tier], minutes)
            db.execute("DELETE FROM klein_review WHERE id = ?", (listing_id,))
            db.execute("DELETE FROM klein_origins WHERE id = ?", (listing_id,))

    def check_address(
        self, db: sqlite3.Connection, listing_id: str, address: str, now: datetime, cutoff: datetime,
    ) -> tuple[bool, str]:
        origin = normalize_munich_address(address)
        if not origin:
            return False, "Bitte eine veröffentlichte Münchner Adresse mit Straße, Hausnummer und PLZ angeben."
        row = db.execute("""
            SELECT url, title, excerpt, is_wg, fresh_at, basis FROM klein_review WHERE id = ? AND fresh_at BETWEEN ? AND ?
        """, (listing_id, cutoff.timestamp(), now.timestamp())).fetchone()
        if row is None:
            return False, "Kein aktueller Kleinanzeigen-Treffer mit dieser ID zur Ortsprüfung vorhanden."
        url, title, excerpt, is_wg, fresh_at, basis = row
        prior = db.execute("SELECT reason FROM listing_availability WHERE id = ?", (listing_id,)).fetchone()
        availability = (prior and prior[0]) or availability_rejection(title, excerpt, now.astimezone(MUNICH_TIME).date())
        with db:
            record_listing_availability(db, listing_id, availability)
            if availability:
                _remove_candidates(db, listing_id)
        if availability:
            return False, availability
        tier, minutes = self.router.classify(origin, now)
        fit, reason = ROUTE_FIT.get(tier, ("unclear", "Die ÖPNV-Verbindung konnte nicht geprüft werden."))
        mapped = tier in PRIMARY_TIERS or tier in _SECONDARY_STATIONS
        coordinates = self.router.coordinates(origin) if mapped else None
        with db:
            db.execute("UPDATE listing_profiles SET fit = ?, fit_reason = ? WHERE id = ?", (fit, reason, listing_id))
            record_listing_route(db, listing_id, tier, minutes)
            if coordinates is not None or tier == "outside":
                record_listing_location(db, listing_id, coordinates, address=origin)
        if tier in _SECONDARY_STATIONS and minutes is not None:
            self._save_secondary_offer(db, listing_id, url, title, datetime.fromtimestamp(fresh_at, timezone.utc),
                                       basis, tier, minutes)
            return True, f"In Liste {tier.upper()} gespeichert. Ab {_SECONDARY_STATIONS[tier]} dauert es {minutes} Minuten mit dem ÖPNV."
        if tier not in PRIMARY_TIERS:
            return False, "Die ÖPNV-Fahrt konnte nicht geprüft werden." if tier is None else "Keine passende ÖPNV-Verbindung gefunden."
        saved = self._save(db, listing_id, url, excerpt, bool(is_wg), fresh_at, basis, origin, now, title=title)
        return saved, "Entwurf gespeichert. Der Versandstatus steht unter delivery." if saved else "Der Treffer wurde bereits verarbeitet."

    def _save(
        self, db: sqlite3.Connection, listing_id: str, url: str, excerpt: str, is_wg: bool, fresh_at: float, basis: str,
        address: str, now: datetime, *, title: str = "", previous_message: str | None = None,
    ) -> bool:
        message = personalise_message(
            self.template, excerpt, listing_url=url, provider=self.provider, listing_type="wg" if is_wg else "wohnung",
            listing_title=title,
        )
        with db:
            saved = save_application_draft(
                db, listing_id, url, fresh_at, basis, message, context=excerpt,
                provider=self.outgoing_provider, now=now, signature=self.render_signature, previous_message=previous_message,
            )
            db.execute("DELETE FROM klein_review WHERE id = ?", (listing_id,))
            remove_secondary_offer(db, listing_id)
            if saved:
                self._save_origin(db, listing_id, address)
        return saved
