"""Poll Munich listings, draft an application, and optionally send it."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from kleinanzeigen_activity import EmailNotifier, install_activity, record_activity, record_replies
from kleinanzeigen_radar import KleinanzeigenRadar, install_klein_review
from kleinanzeigen_reply_queue import ReplyDispatcher, install_reply_queue
from listing_photos import LEGACY_PHOTO_GALLERY_LIMIT, PHOTO_GALLERY_VERSION
from listing_profiles import (
    ROUTE_FIT, install_listing_profiles, record_listing_location, record_listing_profile, record_listing_route,
)
from llm_personalizer import (
    applicant_intro_from_template, application_render_signature, personalise_message,
    revise_first_message_draft, with_applicant_intro,
)
from munich_delivery import (
    DeliveryQueue, FirstMessageReview, application_recipient_role, install_schema, review_outgoing,
    save_application_draft,
)
from munich_tiers import remove_secondary_offer, save_secondary_offer, secondary_freshness
from munich_transit import PRIMARY_TIERS, TransitRouter
from rental_availability import AVAILABILITY_POLICY_VERSION, availability_rejection, record_listing_availability
from reviewed_applications import (
    ReviewedApplication, fail_application_review, get_application_attachment, pending_application_reviews,
    reject_unavailable_approval, set_application_wait_reason,
)
from wg_alerts import AlertsError, AlertsInbox
from wg_first_message import applicant_answers, viewing_availability, viewing_slot_labels
from wg_observations import FIRST_SEEN_BASIS, install_observations, record_details, record_inventory
from wg_preferences import rejection_reason
from wg_reply_drafts import install_wg_reply_drafts, sync_wg_reply_drafts
from wg_source import (
    DEFAULT_SEARCH_URLS, Listing, ListingRef, RecipientRole, WGAccessBlocked, WGListingGone, WGSource, WGSourceError,
)

if TYPE_CHECKING:
    from wg_reply_inbox import WGInboxEntry

ROOT = Path(__file__).resolve().parent.parent
BERLIN = ZoneInfo("Europe/Berlin")
LOG = logging.getLogger("munich_radar")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} fehlt")
    return value


def _number(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} muss eine ganze Zahl sein") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} muss zwischen {minimum} und {maximum} liegen")
    return value


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    if not path.exists():
        path.touch(mode=0o600, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA secure_delete = ON")
    db.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            fresh_at REAL NOT NULL,
            basis TEXT NOT NULL,
            message TEXT,
            done INTEGER NOT NULL DEFAULT 0
        )
    """)
    install_schema(db)
    install_klein_review(db)
    install_activity(db)
    install_reply_queue(db)
    install_wg_reply_drafts(db)
    install_observations(db)
    install_listing_profiles(db)
    return db


@dataclass
class Scanner:
    source: WGSource
    router: TransitRouter
    template: str
    provider: str
    max_age: timedelta
    limit: int
    alerts: AlertsInbox | None = None
    age: int = 28
    age_gap: int = 7
    delivery: DeliveryQueue | None = None
    reviewed_delivery: DeliveryQueue | None = None
    klein_alerts: AlertsInbox | None = None
    klein_delivery: DeliveryQueue | None = None
    outgoing_provider: str = "none"
    render_signature: str | None = None
    applicant_intro: str = ""
    viewing_availability: tuple[datetime, ...] = ()
    applicant_answers: dict[str, str] = field(default_factory=dict)
    reviewed_startup_problem: Callable[[], str | None] | None = None
    _location_retry_after: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def add_intro_to_current_drafts(self, db: sqlite3.Connection, now: datetime) -> int:
        """Clean up recent unedited drafts using the cached listing title."""
        drafts = db.execute("""
            SELECT d.id, d.url, d.fresh_at, d.basis, d.message, COALESCE(p.title, '')
            FROM drafts d LEFT JOIN listing_profiles p ON p.id = d.id
            WHERE d.done = 0 AND d.fresh_at BETWEEN ? AND ? AND d.message IS NOT NULL AND d.id NOT LIKE 'ka-%'
              AND (p.recipient_role = 'roommate' OR (p.recipient_role IS NULL AND COALESCE(p.is_commercial, 0) = 0
                   AND NOT EXISTS (SELECT 1 FROM draft_render_signatures WHERE id = d.id)))
              AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = d.id)
              AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers WHERE id = d.id)
              AND NOT EXISTS (SELECT 1 FROM application_reviews WHERE id = d.id)
            ORDER BY d.fresh_at DESC LIMIT ?
        """, ((now - self.max_age).timestamp(), now.timestamp(), self.limit)).fetchall()
        updated = 0
        viewing_slots = viewing_slot_labels(self.viewing_availability, now)
        for listing_id, url, stamp, basis, previous, title in drafts:
            listing_type = "wg" if "/wg-zimmer-" in url else "wohnung"
            # Leave the optional reference empty when a WG draft uses model personalization.
            repair_title = "" if listing_type == "wg" and self.provider != "none" else title
            revised = revise_first_message_draft(
                previous, self.applicant_intro, listing_title=repair_title, listing_type=listing_type,
                viewing_slots=viewing_slots, template=self.template,
            )
            if revised == previous:
                continue
            with db:
                updated += save_application_draft(
                    db, listing_id, url, stamp, basis, revised,
                    context=title, provider=self.outgoing_provider, now=now, previous_message=previous,
                )
        return updated

    @staticmethod
    def _check_availability(db: sqlite3.Connection, listing: Listing, now: datetime) -> str | None:
        reason = availability_rejection(
            listing.title, listing.description, now.astimezone(BERLIN).date(),
            available_from=listing.available_from, available_until=listing.available_until,
        )
        with db:
            record_listing_availability(db, listing.id, reason)
            if reason:
                remove_secondary_offer(db, listing.id)
                reject_unavailable_approval(db, listing.id, reason, now)
                record_listing_location(db, listing.id, None)
        return reason

    def _recheck_while_browser_is_blocked(
        self, db: sqlite3.Connection, item: ReviewedApplication, now: datetime,
    ) -> str | None:
        """Check older offers and rejudge the approved text without starting a send."""
        needs_availability = not db.execute(
            "SELECT 1 FROM listing_availability WHERE id = ? AND policy_version = ?",
            (item.id, AVAILABILITY_POLICY_VERSION),
        ).fetchone()
        needs_judgment = self.outgoing_provider == "mistral" and not db.execute("""
            SELECT 1 FROM outgoing_checks
            WHERE id = ? AND kind = 'application' AND message_hash = ? AND checked_at >= ?
        """, (item.id, item.message_hash, item.updated_at)).fetchone()
        if not (needs_availability or needs_judgment):
            return None
        try:
            listing = self.source.details(ListingRef.from_url(item.url))
        except WGSourceError:
            return None
        if reason := self._check_availability(db, listing, now):
            return reason
        if needs_judgment:
            review_outgoing(
                db, item.id, item.message, kind="application", context=listing.description, provider=self.outgoing_provider,
                now=now, first_message=FirstMessageReview(listing.title, "", (), {}, user_reviewed=True,
                                                         recipient_role=self._recipient_role(db, listing)),
            )
        return None

    @staticmethod
    def _recipient_role(db: sqlite3.Connection, listing: Listing) -> RecipientRole:
        row = db.execute("SELECT recipient_role, is_commercial FROM listing_profiles WHERE id = ?",
                         (listing.id,)).fetchone()
        return application_recipient_role(listing.recipient_role, row[0] if row else None,
                                          commercial=listing.is_commercial or bool(row and row[1]))

    def validate_reviewed_listing(
        self, db: sqlite3.Connection, item: ReviewedApplication, listing: Listing, now: datetime,
        retry_after: dict[str, datetime],
    ) -> bool:
        """Recheck a queued application's current listing before handing it to a browser."""
        if item.id != listing.id:
            return False
        if self._check_availability(db, listing, now):
            return False
        cutoff = now - min(self.max_age, timedelta(hours=24))
        expired = (not cutoff <= listing.published_at <= now if listing.published_at is not None else
                   bool(listing.published_day and not cutoff.astimezone(BERLIN).date() <= listing.published_day
                        <= now.astimezone(BERLIN).date()))
        if expired:
            fail_application_review(db, item, "Die Anzeige liegt außerhalb des Versandzeitfensters.", now)
            return False
        if rejection_reason(
            listing.title, listing.description, wanted=listing.wanted or "", resident_ages=listing.resident_ages or "",
            wg_type=listing.wg_type or "", age=self.age, age_gap=self.age_gap,
        ):
            fail_application_review(db, item, "Die Anzeige entspricht inzwischen nicht mehr deinen Suchfiltern.", now)
            return False
        tier, minutes = self.router.classify(listing.address, now) if listing.address else (None, None)
        if tier is None:
            if not listing.address:
                with db:
                    record_listing_location(db, item.id, None)
            set_application_wait_reason(
                db, item, "Die ÖPNV-Fahrzeit konnte gerade nicht bestätigt werden. MietRadar prüft sie erneut.",
            )
            retry_after[item.id] = now + timedelta(minutes=5)
            return False
        if tier not in PRIMARY_TIERS:
            with db:
                record_listing_location(db, item.id, None)
                fail_application_review(db, item, "Die Fahrt dauert inzwischen länger als 30 Minuten.", now)
            return False
        coordinates = self.router.coordinates(listing.address)
        with db:
            record_listing_route(db, item.id, tier, minutes)
            if coordinates is not None:
                record_listing_location(db, item.id, coordinates, address=listing.address)
        return True

    def send_reviewed(self, db: sqlite3.Connection, now: datetime, retry_after: dict[str, datetime]) -> Counter:
        counts = Counter()
        if self.reviewed_delivery is None:
            return counts
        for listing_id in [key for key, deadline in retry_after.items() if deadline <= now]:
            del retry_after[listing_id]
        ready = [item for item in pending_application_reviews(db, now, self.max_age)
                 if item.id not in retry_after]
        if not ready:
            return counts
        retry_at = now + timedelta(minutes=5)
        startup_problem = (
            self.reviewed_startup_problem()
            if self.reviewed_startup_problem is not None and not self.reviewed_delivery.paused
            else None
        )
        for item in ready:
            if self.reviewed_delivery.paused:
                set_application_wait_reason(
                    db, item, "Versand ist pausiert. Prüfe den letzten Versandversuch im WG-Gesucht-Postfach.",
                )
                retry_after[item.id] = retry_at
                continue
            if startup_problem:
                if reason := self._recheck_while_browser_is_blocked(db, item, now):
                    counts[reason] += 1
                    continue
                set_application_wait_reason(db, item, startup_problem)
                retry_after[item.id] = retry_at
                counts["Freigaben warten auf Versandbrowser"] += 1
                continue
            ref = ListingRef.from_url(item.url)
            try:
                listing = self.source.details(ref)
            except WGListingGone:
                fail_application_review(db, item, "Die Anzeige ist nicht mehr verfügbar.", now)
                continue
            except WGSourceError:
                set_application_wait_reason(
                    db, item, "Die Anzeige konnte bei WG-Gesucht gerade nicht geprüft werden. MietRadar versucht es erneut.",
                )
                retry_after[item.id] = retry_at
                counts["Freigaben warten auf WG-Gesucht"] += 1
                continue
            if not self.validate_reviewed_listing(db, item, listing, now, retry_after):
                exclusion = db.execute("SELECT reason FROM listing_availability WHERE id = ?", (item.id,)).fetchone()
                if exclusion and exclusion[0]:
                    counts[exclusion[0]] += 1
                elif item.id in retry_after:
                    counts["Freigaben warten auf ÖPNV-Prüfung"] += 1
                continue
            attachment = get_application_attachment(db, item.attachment_digest) if item.attachment_digest else None
            if item.attachment_digest and attachment is None:
                fail_application_review(db, item, "Der freigegebene Anhang steht nicht mehr zur Verfügung.", now)
                continue
            set_application_wait_reason(db, item, None)
            result = self.reviewed_delivery.deliver(
                db, item.id, item.url, item.message, item.fresh_at, self.max_age, context=listing.description,
                first_message=FirstMessageReview(listing.title, "", (), {}, user_reviewed=True,
                                                recipient_role=self._recipient_role(db, listing)), attachment=attachment,
            )
            if result:
                counts[result] += 1
                if "zurückgehalten" in result or result.startswith("Versand pausiert"):
                    judgment = db.execute("SELECT reason FROM outgoing_checks WHERE id = ? AND kind = 'application'",
                                          (item.id,)).fetchone()
                    reason = judgment[0] if judgment and "zurückgehalten" in result else (
                        "Versand pausiert. Prüfe die WG-Gesucht-Anmeldung (WG_EMAIL und WG_PASSWORD), "
                        "den Browser und mögliche Bestätigungsabfragen der Plattform."
                    )
                    fail_application_review(db, item, reason, now)
            else:
                retry_after[item.id] = now + timedelta(minutes=1)
        return counts

    def scan(self, db: sqlite3.Connection, now: datetime, retry_after: dict[str, datetime]) -> Counter:
        cutoff = now - self.max_age
        viewing_slots = viewing_slot_labels(self.viewing_availability, now)
        wg_template = with_applicant_intro(self.template, self.applicant_intro, first_message=True)
        signatures = {
            role: self.render_signature and application_render_signature(
                wg_template, self.provider, first_message=True, viewing_slots=viewing_slots,
                applicant_answers=self.applicant_answers, recipient_role=role,
            ) for role in ("manager", "roommate", "unknown")
        }
        first_eligible_day = cutoff.astimezone(BERLIN).date()
        current_day = now.astimezone(BERLIN).date()
        with db:
            # Keep IDs for deduplication, but remove expired personal drafts.
            db.execute("UPDATE drafts SET message = NULL WHERE message IS NOT NULL AND fresh_at < ?",
                       (cutoff.timestamp(),))
        for listing_id, retry in list(retry_after.items()):
            if retry <= now:
                del retry_after[listing_id]
        for listing_id, retry in list(self._location_retry_after.items()):
            if retry <= now:
                del self._location_retry_after[listing_id]

        counts: Counter = Counter()
        if self.klein_alerts:
            klein = KleinanzeigenRadar(
                self.router, self.template, self.provider, self.age, self.age_gap, self.klein_delivery,
                outgoing_provider=self.outgoing_provider,
                render_signature=self.render_signature,
                applicant_intro=self.applicant_intro, applicant_answers=self.applicant_answers,
            )
            try:
                counts.update(klein.scan(db, self.klein_alerts, now, cutoff, self.limit, retry_after))
            except AlertsError as exc:
                LOG.warning("Kleinanzeigen-Suchalarm nicht erreichbar: %s", exc)

        observations = {}
        refs: list[tuple[ListingRef, datetime | None]] = []
        inventory_error = None
        try:
            if self.alerts:
                refs = [(alert.ref, alert.received_at) for alert in self.alerts.recent(cutoff, self.limit)]
            else:
                inventory = self.source.candidates()
                observations = record_inventory(db, inventory, self.source.inventory_scope, now, cutoff)
                counts["WG-Gesucht-Angebote beobachtet"] = len(inventory)
                candidates = sorted(
                    (ref for ref in inventory if ref.id in observations),
                    key=lambda ref: (
                        observations[ref.id].recent_first_seen(cutoff) is None,
                        observations[ref.id].details_checked_at or 0,
                    ),
                )
                refs = [(ref, None) for ref in candidates]
        except WGAccessBlocked:
            raise
        except (WGSourceError, AlertsError) as exc:
            inventory_error = exc

        refresh_refs = []
        if self.render_signature:
            # Keep part of the detail budget available for newly seen offers when slots change.
            refresh_limit = max(1, self.limit // 3)
            older_drafts = db.execute("""
                SELECT d.url FROM drafts d LEFT JOIN draft_render_signatures s ON s.id = d.id
                LEFT JOIN listing_profiles p ON p.id = d.id
                WHERE d.id NOT LIKE 'ka-%' AND d.done = 0 AND d.message IS NOT NULL
                  AND d.fresh_at BETWEEN ? AND ? AND COALESCE(s.signature, '') !=
                      CASE WHEN p.recipient_role = 'roommate' THEN ?
                           WHEN p.recipient_role = 'manager' OR p.is_commercial = 1 THEN ? ELSE ? END
                  AND NOT EXISTS (SELECT 1 FROM delivery WHERE id = d.id)
                  AND NOT EXISTS (SELECT 1 FROM application_reviews WHERE id = d.id)
                ORDER BY d.fresh_at DESC LIMIT ?
            """, (cutoff.timestamp(), now.timestamp(), signatures["roommate"], signatures["manager"],
                  signatures["unknown"], refresh_limit)).fetchall()
            refresh_refs = [(ref, None) for url, in older_drafts if (ref := ListingRef.from_url(url))]

        metadata_backfill = db.execute("""
            SELECT f.url,
                   p.photo_urls IS NULL OR (COALESCE(p.photo_gallery_version, 0) < ? AND
                       CASE WHEN json_valid(p.photo_urls) THEN json_array_length(p.photo_urls) = ? ELSE 0 END
                   ) AS photos_outdated,
                   MAX(f.check_route) AND p.route_tier IS NULL,
                   a.policy_version IS NOT ? AS availability_outdated,
                   (p.latitude IS NULL OR p.longitude IS NULL)
                       AND (MAX(f.is_secondary) OR p.route_tier IN ('a*', 'a')) AS location_missing
            FROM (
                SELECT id, url, fresh_at,
                       NOT EXISTS (SELECT 1 FROM munich_secondary_offers WHERE id = d.id) AS check_route,
                       0 AS is_secondary
                FROM drafts d WHERE done = 0 AND message IS NOT NULL
                UNION ALL
                SELECT id, url, fresh_at, 0, 1 FROM munich_secondary_offers
            ) f LEFT JOIN listing_profiles p ON p.id = f.id
                LEFT JOIN listing_availability a ON a.id = f.id
            WHERE f.id NOT LIKE 'ka-%' AND f.fresh_at BETWEEN ? AND ?
            GROUP BY f.id
            HAVING photos_outdated OR (MAX(f.check_route) AND p.route_tier IS NULL)
                OR availability_outdated OR (location_missing AND a.reason IS NULL)
            ORDER BY availability_outdated DESC, p.photo_urls IS NOT NULL, MAX(f.fresh_at) DESC
        """, (PHOTO_GALLERY_VERSION, LEGACY_PHOTO_GALLERY_LIMIT, AVAILABILITY_POLICY_VERSION,
              cutoff.timestamp(), now.timestamp()))
        metadata_limit = self.limit if inventory_error is not None else (self.limit + 1) // 2
        metadata_refs = []
        for url, photos, route, availability, location in metadata_backfill:
            ref = ListingRef.from_url(url)
            if ref is not None and ref.id not in retry_after:
                location = location and ref.id not in self._location_retry_after
                if not (photos or route or availability or location):
                    continue
                metadata_refs.append((ref, photos, route, availability, location))
                if len(metadata_refs) >= metadata_limit:
                    break
        metadata_backfill.close()
        photo_ids = {ref.id for ref, photos, _, _, _ in metadata_refs if photos}
        route_ids = {ref.id for ref, _, route, _, _ in metadata_refs if route}
        availability_ids = {ref.id for ref, _, _, availability, _ in metadata_refs if availability}
        location_ids = {ref.id for ref, _, _, _, location in metadata_refs if location}
        metadata_ids = {ref.id for ref, *_ in metadata_refs}
        original_ids = {ref.id for ref, _ in refs}
        refresh_ids = {ref.id for ref, _ in refresh_refs}
        queued_ids = original_ids.copy()
        for ref in [ref for ref, _ in refresh_refs] + [ref for ref, *_ in metadata_refs]:
            if ref.id not in queued_ids:
                refs.append((ref, None))
                queued_ids.add(ref.id)
        refs.sort(key=lambda row: (row[0].id not in availability_ids, row[0].id not in refresh_ids, row[0].id not in photo_ids,
                                   row[0].id not in original_ids))

        eligible = []
        detail_reads = 0
        detail_errors = 0
        detail_error = None
        for ref, received_at in refs:
            if ref.id in retry_after:
                continue
            draft = db.execute(
                """SELECT d.fresh_at, d.message, d.done, s.state, d.basis, r.signature, a.state, p.is_commercial,
                          p.recipient_role FROM drafts d
                   LEFT JOIN delivery s ON s.id = d.id LEFT JOIN draft_render_signatures r ON r.id = d.id
                   LEFT JOIN application_reviews a ON a.id = d.id
                   LEFT JOIN listing_profiles p ON p.id = d.id
                   WHERE d.id = ?""", (ref.id,),
            ).fetchone()
            cached_role = application_recipient_role(draft[8], commercial=bool(draft[7])) if draft else "unknown"
            expected_signature = signatures[cached_role]
            refresh_draft = bool(draft and not draft[6] and expected_signature and draft[5] != expected_signature)
            skip_draft = bool(draft and (not draft[1] or draft[2] or draft[3] or draft[6] or
                                       (not refresh_draft and (not self.delivery or self.delivery.paused))))
            if skip_draft and ref.id not in metadata_ids:
                continue
            if detail_reads >= self.limit:
                break
            detail_reads += 1
            try:
                listing = self.source.details(ref)
            except WGListingGone:
                reason = "Anzeige nicht mehr verfügbar"
                with db:
                    record_listing_availability(db, ref.id, reason)
                    remove_secondary_offer(db, ref.id)
                    reject_unavailable_approval(db, ref.id, reason, now)
                    record_listing_location(db, ref.id, None)
                    if ref.id in photo_ids:
                        db.execute("""
                            INSERT INTO listing_profiles(id, title, photo_urls, photo_gallery_version)
                            VALUES (?, '', '[]', ?)
                            ON CONFLICT(id) DO UPDATE SET
                                photo_urls = COALESCE(listing_profiles.photo_urls, excluded.photo_urls),
                                photo_gallery_version = excluded.photo_gallery_version
                        """, (ref.id, PHOTO_GALLERY_VERSION))
                counts[reason] += 1
                retry_after[ref.id] = now + self.max_age
                continue
            except WGAccessBlocked:
                raise
            except WGSourceError as exc:
                detail_error = exc
                detail_errors += 1
                counts["Inserate derzeit nicht abrufbar"] += 1
                retry_after[ref.id] = now + timedelta(minutes=5)
                continue
            if draft and draft[7] and not listing.is_commercial:
                listing = replace(listing, is_commercial=True)
            role = application_recipient_role(listing.recipient_role, draft[8] if draft else None,
                                             commercial=listing.is_commercial)
            listing = replace(listing, recipient_role=role) if role != listing.recipient_role else listing
            if draft and not (draft[2] or draft[3] or draft[6]) and draft[1] and self.render_signature:
                expected_signature = signatures[role]
                if draft[5] != expected_signature:
                    refresh_draft = True
                    skip_draft = False
            record_listing_profile(db, listing)
            if reason := self._check_availability(db, listing, now):
                counts[reason] += 1
                retry_after[listing.id] = now + self.max_age
                continue
            if not listing.address:
                with db:
                    record_listing_location(db, ref.id, None)
            if ref.id in photo_ids and listing.photo_urls:
                counts["Inserate mit Fotos ergänzt"] += 1
            if ref.id in metadata_ids and (skip_draft or (ref.id not in original_ids and not refresh_draft)):
                needs_location = ref.id in location_ids
                if ref.id in route_ids and listing.address:
                    tier, minutes = self.router.classify(listing.address, now)
                    if tier in PRIMARY_TIERS:
                        needs_location = True
                        with db:
                            record_listing_route(db, listing.id, tier, minutes)
                if needs_location:
                    self._location_retry_after[listing.id] = now + timedelta(hours=1)
                    if listing.address and (coordinates := self.router.coordinates(listing.address)):
                        with db:
                            record_listing_location(db, listing.id, coordinates, address=listing.address)
                continue

            published_at = listing.published_at
            published_day = listing.published_day
            observation = observations.get(ref.id)
            if published_at is None or published_at.tzinfo is not None:
                published_at, published_day = record_details(db, ref, now, published_at, published_day)
            if published_at is None and published_day is not None:
                counts["Veröffentlichungsdaten ohne Uhrzeit"] += 1
                if not first_eligible_day <= published_day <= current_day:
                    with db:
                        remove_secondary_offer(db, ref.id)
                    counts["außerhalb des Zeitfensters"] += 1
                    continue
            if published_at is not None:
                fresh_at, basis = published_at, "veröffentlicht"
            elif received_at is not None:
                fresh_at, basis = received_at, "Suchalarm erhalten"
            else:
                fresh_at = observation.recent_first_seen(cutoff) if observation else None
                basis = FIRST_SEEN_BASIS
            refresh_only = fresh_at is None and refresh_draft
            if refresh_only:
                fresh_at, basis = datetime.fromtimestamp(draft[0], timezone.utc), draft[4]
            if fresh_at is None or fresh_at.tzinfo is None:
                counts["kein Veröffentlichungsdatum"] += 1
                retry_after[ref.id] = now + timedelta(minutes=15)
                continue
            previous_secondary = secondary_freshness(db, ref.id)
            if previous_secondary is not None:
                if previous_secondary[0] < fresh_at.timestamp():
                    stamp, basis = previous_secondary
                    fresh_at = datetime.fromtimestamp(stamp, timezone.utc)
                elif fresh_at < cutoff:
                    with db:
                        db.execute("UPDATE munich_secondary_offers SET fresh_at = ?, basis = ? WHERE id = ?",
                                   (fresh_at.timestamp(), basis, ref.id))
            if not cutoff <= fresh_at <= now:
                counts["außerhalb des Zeitfensters"] += 1
                retry_after[ref.id] = now + self.max_age
                continue
            reason = rejection_reason(
                listing.title, listing.description,
                wanted=listing.wanted or "", wg_type=listing.wg_type or "",
                resident_ages=listing.resident_ages or "", age=self.age, age_gap=self.age_gap,
            )
            if reason:
                record_listing_profile(db, listing, fit="unclear", reason=f"Suchfilter: {reason}.")
                with db:
                    remove_secondary_offer(db, ref.id)
                    record_listing_location(db, ref.id, None)
                counts[reason] += 1
                retry_after[ref.id] = now + self.max_age
                continue
            if draft:
                # A new alert may not extend an existing listing's window.
                fresh_at = min(fresh_at, datetime.fromtimestamp(draft[0], timezone.utc))
            may_deliver = not refresh_only and basis != FIRST_SEEN_BASIS and (published_at is not None or published_day is None)
            eligible.append((fresh_at, listing, basis, draft, may_deliver, refresh_draft))

        for fresh_at, listing, basis, draft, may_deliver, refresh_draft in sorted(eligible, key=lambda item: item[0], reverse=True):
            if not listing.address:
                record_listing_profile(db, listing, fit="unclear", reason="Die genaue Adresse fehlt.")
                with db:
                    remove_secondary_offer(db, listing.id)
                counts["keine prüfbare Adresse"] += 1
                retry_after[listing.id] = now + timedelta(minutes=30)
                continue
            tier, minutes = self.router.classify(listing.address, now)
            fit, fit_reason = ROUTE_FIT.get(tier, ("unclear", "Die ÖPNV-Verbindung konnte nicht geprüft werden."))
            mapped = tier in PRIMARY_TIERS or tier in {"b", "c"}
            record_listing_profile(db, listing, fit=fit, reason=fit_reason,
                                   provider=self.provider if mapped else "none",
                                   route_tier=tier, route_minutes=minutes,
                                   coordinates=self.router.coordinates(listing.address) if mapped else None)
            if tier in {"b", "c"} and minutes is not None:
                with db:
                    save_secondary_offer(db, listing.id, listing.url, listing.title, fresh_at, basis,
                                         tier, "Ostbahnhof" if tier == "b" else "Hbf", minutes)
                counts[f"{tier.upper()}-Tier-Treffer"] += 1
                continue
            if tier not in PRIMARY_TIERS:
                if tier == "outside":
                    with db:
                        remove_secondary_offer(db, listing.id)
                counts["ÖPNV über 45 Minuten" if tier == "outside" else "ÖPNV nicht prüfbar"] += 1
                retry_after[listing.id] = now + (self.max_age if tier == "outside" else timedelta(minutes=10))
                continue
            first_message = FirstMessageReview(listing.title, self.applicant_intro, self.viewing_availability,
                                               self.applicant_answers, recipient_role=listing.recipient_role)
            if draft and not refresh_draft:
                with db:
                    remove_secondary_offer(db, listing.id)
                message = draft[1]
            else:
                message = personalise_message(
                    wg_template, listing.description, listing_url=listing.url, listing_title=listing.title,
                    provider=self.provider, listing_type="wg" if listing.is_wg else "wohnung", first_message=True,
                    viewing_slots=viewing_slots, applicant_answers=self.applicant_answers,
                    applicant_intro=self.applicant_intro, recipient_role=listing.recipient_role,
                )
                with db:
                    saved = save_application_draft(
                        db, listing.id, listing.url, fresh_at.timestamp(), basis, message,
                        context=listing.description, provider=self.outgoing_provider, now=now,
                        signature=signatures[listing.recipient_role],
                        previous_message=draft[1] if draft else None,
                        first_message=first_message,
                    )
                    remove_secondary_offer(db, listing.id)
                if not saved:
                    continue
                counts["Entwürfe aktualisiert" if draft else "neue Entwürfe"] += 1
                if draft:
                    continue  # A refreshed personal draft first appears for review.
            if self.delivery and may_deliver:
                result = self.delivery.deliver(db, listing.id, listing.url, message, fresh_at, self.max_age,
                                               context=listing.description, first_message=first_message)
                if result:
                    counts[result] += 1
        if inventory_error is not None:
            raise inventory_error
        if detail_error is not None and detail_reads == detail_errors:
            raise detail_error
        return counts


def _inbox(folder_variable: str, sender_variable: str) -> AlertsInbox:
    return AlertsInbox(
        _required("MUNICH_IMAP_HOST"), _required("MUNICH_IMAP_USER"), _required("MUNICH_IMAP_PASSWORD"),
        _required(folder_variable), _required(sender_variable),
    )


def _reply_settings() -> tuple[AlertsInbox | None, str]:
    folder = "KLEINANZEIGEN_REPLIES_IMAP_FOLDER"
    sender = "KLEINANZEIGEN_REPLY_SENDER"
    if not (os.getenv(folder, "").strip() or os.getenv(sender, "").strip()):
        return None, "none"
    provider = os.getenv("KLEINANZEIGEN_JUDGE_PROVIDER", "none").strip().lower()
    if provider not in {"none", "mistral"}:
        raise ValueError("KLEINANZEIGEN_JUDGE_PROVIDER muss none oder mistral sein")
    if provider == "mistral":
        _required("MISTRAL_API_KEY")
    return _inbox(folder, sender), provider


def _email_notifier() -> EmailNotifier | None:
    if not (os.getenv("MUNICH_NOTIFY_EMAIL_TO", "").strip() or os.getenv("MUNICH_NOTIFY_EMAIL_FROM", "").strip()):
        return None
    return EmailNotifier(_required("RESEND_API_KEY"), _required("MUNICH_NOTIFY_EMAIL_FROM"), _required("MUNICH_NOTIFY_EMAIL_TO"))


def _outgoing_provider() -> str:
    provider = os.getenv("MUNICH_OUTGOING_JUDGE_PROVIDER", "none").strip().lower()
    if provider not in {"mistral", "none"}:
        raise ValueError("MUNICH_OUTGOING_JUDGE_PROVIDER muss mistral oder none sein (none nutzt lokale Regeln)")
    if provider == "mistral":
        _required("MISTRAL_API_KEY")
    return provider


def _draft_settings() -> tuple[str, str]:
    provider = os.getenv("MUNICH_LLM_PROVIDER", "none").strip().lower()
    if provider not in {"none", "mistral"}:
        raise ValueError("MUNICH_LLM_PROVIDER muss none oder mistral sein")
    if provider == "mistral":
        _required("MISTRAL_API_KEY")
    template = os.getenv("MUNICH_MESSAGE_TEMPLATE", "").strip()
    if not template:
        file = Path(os.getenv("MUNICH_MESSAGE_FILE", str(ROOT / "config" / "message.txt")))
        if not file.is_file():
            raise ValueError("Schreib deinen Bewerbungstext in MUNICH_MESSAGE_TEMPLATE oder config/message.txt")
        template = file.read_text(encoding="utf-8").strip()
    if not template.replace("{LLM_TEXT}", "").strip():
        raise ValueError("Die Bewerbungsvorlage braucht eigenen Text")
    if provider == "mistral" and "{LLM_TEXT}" not in template:
        raise ValueError("Für Mistral muss die Bewerbungsvorlage {LLM_TEXT} enthalten")
    return with_applicant_intro(template, os.getenv("MUNICH_APPLICANT_INTRO", "")), provider


def _scanner(max_age: timedelta) -> Scanner:
    mode = os.getenv("MUNICH_SOURCE", "public").strip().lower()
    if mode not in {"public", "alerts"}:
        raise ValueError("MUNICH_SOURCE muss public oder alerts sein")
    alerts = _inbox("MUNICH_IMAP_FOLDER", "MUNICH_ALERT_SENDER") if mode == "alerts" else None
    klein_alerts = None
    if os.getenv("KLEINANZEIGEN_IMAP_FOLDER", "").strip() or os.getenv("KLEINANZEIGEN_ALERT_SENDER", "").strip():
        klein_alerts = _inbox("KLEINANZEIGEN_IMAP_FOLDER", "KLEINANZEIGEN_ALERT_SENDER")
    search_urls = tuple(url.strip() for url in os.getenv("MUNICH_SEARCH_URLS", "").split(",") if url.strip())
    template, provider = _draft_settings()

    limit = _number("MUNICH_MAX_CANDIDATES", 24, 1, 40)
    pages = _number("MUNICH_WG_SEARCH_PAGES", 2, 1, 3)
    return Scanner(
        WGSource(search_urls or DEFAULT_SEARCH_URLS, pages), TransitRouter(os.getenv("MUNICH_TRANSIT_API_URL")),
        template, provider, max_age, limit, alerts,
        _number("MUNICH_APPLICANT_AGE", 28, 18, 100), _number("MUNICH_MAX_AGE_GAP", 7, 0, 100),
        klein_alerts=klein_alerts, outgoing_provider=_outgoing_provider(),
        render_signature=application_render_signature(template, provider),
        applicant_intro=os.getenv("MUNICH_APPLICANT_INTRO", "").strip() or applicant_intro_from_template(template),
        viewing_availability=viewing_availability(os.getenv("MUNICH_VIEWING_SLOTS", "")),
        applicant_answers=applicant_answers(os.getenv("MUNICH_APPLICANT_ANSWERS", "")),
    )


def main() -> int:
    load_dotenv(ROOT / "config" / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("watch", "scan", "drafts", "delivery", "done", "klein-review", "klein-address",
                                                "klein-activity", "klein-reply-drafts", "outgoing-checks"))
    parser.add_argument("id", nargs="?", help="Anzeigennummer, bei Kleinanzeigen mit ka- am Anfang")
    parser.add_argument("--address", help="Veröffentlichte Adresse für klein-address")
    args = parser.parse_args()
    if (args.command == "done" and not args.id) or (args.id and args.command in {"watch", "scan"}):
        parser.error("done braucht eine ID; watch und scan nehmen keine ID an")
    if args.command == "klein-address" and (not args.id or not args.id.startswith("ka-") or not args.address):
        parser.error("klein-address braucht eine Kleinanzeigen-ID mit ka- und --address")
    if args.address and args.command != "klein-address":
        parser.error("--address ist nur für klein-address gedacht")
    try:
        max_age = timedelta(hours=_number("MUNICH_MAX_AGE_HOURS", 24, 1, 72))
        data_dir = Path(os.getenv("MUNICH_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
        with closing(_database(data_dir / "munich_radar.sqlite3")) as db:
            if args.command == "outgoing-checks":
                query = "SELECT id, kind, approved, reason FROM outgoing_checks"
                rows = db.execute(query + (" WHERE id = ?" if args.id else "") + " ORDER BY checked_at DESC LIMIT 50",
                                  (args.id,) if args.id else ()).fetchall()
                for item_id, kind, approved, reason in rows:
                    print(f"{item_id} · {'Antwort' if kind == 'reply' else 'Bewerbung'} · {'freigegeben' if approved else 'zurückgehalten'}\n{reason}")
                if not rows:
                    print("Keine ausgehenden Nachrichten geprüft.")
                return 0
            if args.command == "klein-reply-drafts":
                query = """SELECT d.id, d.listing_id, d.url, d.message, d.state, c.reason
                           FROM klein_reply_drafts d LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'reply'"""
                rows = db.execute(query + (" WHERE d.id = ? OR d.listing_id = ?" if args.id else "")
                                  + " ORDER BY d.received_at DESC LIMIT 30", (args.id, args.id) if args.id else ()).fetchall()
                states = {"draft": "Entwurf", "held": "Zurückgehalten", "unconfirmed": "Versand unklar", "sent": "Gesendet"}
                for reply_id, listing_id, url, message, state, reason in rows:
                    print(f"{reply_id} · {listing_id or 'Kleinanzeigen'} · {states[state]}\n{url}\n{message}")
                    if reason:
                        print(reason)
                if not rows:
                    print("Keine Kleinanzeigen-Antwortentwürfe.")
                return 0
            if args.command == "klein-activity":
                query = "SELECT listing_id, url, subject, summary, occurred_at FROM klein_activity"
                params = ()
                if args.id:
                    query += " WHERE listing_id = ?"
                    params = (args.id,)
                rows = db.execute(query + " ORDER BY occurred_at DESC LIMIT 50", params).fetchall()
                for listing_id, url, subject, summary, stamp in rows:
                    when = datetime.fromtimestamp(stamp, BERLIN).strftime("%d.%m.%Y %H:%M %Z")
                    print(f"{subject} · {listing_id or 'Kleinanzeigen'} · {when}\n{summary}")
                    if url:
                        print(url)
                if not rows:
                    print("Keine Kleinanzeigen-Aktivität.")
                return 0
            if args.command == "klein-review":
                cutoff = datetime.now(timezone.utc) - max_age
                query = "SELECT id, url, title, fresh_at, basis, reason FROM klein_review WHERE fresh_at >= ?"
                params = (cutoff.timestamp(),)
                if args.id:
                    query += " AND id = ?"
                    params += (args.id,)
                rows = db.execute(query + " ORDER BY fresh_at DESC, id DESC", params).fetchall()
                for listing_id, url, title, stamp, basis, reason in rows:
                    when = datetime.fromtimestamp(stamp, BERLIN).strftime("%d.%m.%Y %H:%M %Z")
                    print(f"{listing_id} · {title or 'Kleinanzeigen'} · {reason} · {basis}: {when}\n{url}")
                if not rows:
                    print("Keine aktuellen Kleinanzeigen-Treffer zur Ortsprüfung.")
                return 0
            if args.command == "klein-address":
                now = datetime.now(timezone.utc)
                template, provider = _draft_settings()
                klein = KleinanzeigenRadar(
                    TransitRouter(os.getenv("MUNICH_TRANSIT_API_URL")), template, provider,
                    outgoing_provider=_outgoing_provider(),
                    render_signature=application_render_signature(template, provider),
                    applicant_intro=os.getenv("MUNICH_APPLICANT_INTRO", "").strip() or applicant_intro_from_template(template),
                    applicant_answers=applicant_answers(os.getenv("MUNICH_APPLICANT_ANSWERS", "")),
                )
                saved, message = klein.check_address(db, args.id, args.address, now, now - max_age)
                print(message)
                return 0 if saved else 1
            if args.command == "done":
                with db:
                    updated = db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (args.id,))
                print("Erledigt." if updated.rowcount else "Anzeige nicht gefunden.")
                return 0 if updated.rowcount else 1
            if args.command == "drafts":
                cutoff = datetime.now(timezone.utc) - max_age
                query = """SELECT id, url, fresh_at, basis, message FROM drafts
                           WHERE done = 0 AND message IS NOT NULL AND fresh_at >= ?
                           AND NOT EXISTS (SELECT 1 FROM munich_secondary_offers o WHERE o.id = drafts.id)
                           AND NOT EXISTS (SELECT 1 FROM delivery WHERE delivery.id = drafts.id AND state = 'unconfirmed')"""
                params: tuple = (cutoff.timestamp(),)
                if args.id:
                    query += " AND id = ?"
                    params += (args.id,)
                rows = db.execute(query + " ORDER BY fresh_at DESC, id DESC", params).fetchall()
                if not rows:
                    print("Keine aktuellen Entwürfe.")
                for listing_id, url, stamp, basis, message in rows:
                    when = datetime.fromtimestamp(stamp, BERLIN).strftime("%d.%m.%Y %H:%M %Z")
                    print(f"{listing_id} · {basis}: {when}\n{url}")
                    if args.id:
                        print(f"\n{message}")
                return 0
            if args.command == "delivery":
                query = """SELECT s.id, s.state, s.recorded_at, d.url FROM delivery s JOIN drafts d ON d.id = s.id"""
                labels = {"unconfirmed": "Unklar – bitte im Postfach der Plattform prüfen", "sent": "Gesendet",
                          "already_contacted": "Bereits kontaktiert", "unavailable": "Kontakt nicht verfügbar"}
                if args.id:
                    rows = db.execute(query + " WHERE s.id = ?", (args.id,)).fetchall()
                else:
                    rows = db.execute(query + " ORDER BY s.recorded_at DESC LIMIT 50").fetchall()
                for listing_id, state, stamp, url in rows:
                    when = datetime.fromtimestamp(stamp, BERLIN).strftime("%d.%m.%Y %H:%M %Z")
                    print(f"{listing_id} · {labels[state]} · {when}\n{url}")
                if not rows:
                    print("Keine Versanddaten.")
                return 0

            scanner = _scanner(max_age)
            reply_inbox, judge_provider = _reply_settings()
            notifier = _email_notifier()
            outgoing_provider = scanner.outgoing_provider
            reply_mode = os.getenv("KLEINANZEIGEN_REPLY_MODE", "draft").strip().lower()
            if reply_mode not in {"auto", "draft"}:
                raise ValueError("KLEINANZEIGEN_REPLY_MODE muss auto oder draft sein")
            if reply_mode == "auto" and reply_inbox is None:
                raise ValueError("Für automatische Antworten zuerst Kleinanzeigen-Antwortmails einrichten")
            limit = _number("MUNICH_DAILY_SEND_LIMIT", 3, 1, 5)
            reply_dispatcher = None
            wg_reply_reader = None
            with ExitStack() as sessions:
                reviewed_send = _switch("MUNICH_REVIEWED_SEND", False)
                auto_send = not reviewed_send and _switch("MUNICH_AUTOSEND", False)
                send_in_service = reviewed_send and not os.getenv("MUNICH_WG_RELAY_TOKEN", "").strip()
                wg_reply_drafts = _switch("MUNICH_WG_REPLY_DRAFTS", False)
                if send_in_service or auto_send or wg_reply_drafts:
                    try:
                        from wg_delivery import BrowserSender
                    except ModuleNotFoundError as exc:
                        raise ValueError("Für den Versand: pip install -r requirements-send.txt") from exc
                    sender = BrowserSender(
                        data_dir / "munich_browser",
                        headless=_switch("MUNICH_BROWSER_HEADLESS", True),
                        email=os.getenv("WG_EMAIL", "").strip(), password=os.getenv("WG_PASSWORD", ""),
                    )
                    sessions.callback(sender.close)
                    queue = DeliveryQueue(sender, limit, judge_provider=outgoing_provider)
                    if send_in_service:
                        scanner.reviewed_delivery = queue
                        scanner.reviewed_startup_problem = sender.startup_problem
                    elif auto_send:
                        scanner.delivery = queue
                    if wg_reply_drafts:
                        from wg_reply_inbox import WGInboxError, recent_wg_messages

                        def wg_reply_reader():
                            try:
                                return recent_wg_messages(sender)
                            except WGInboxError as exc:
                                LOG.warning("WG-Gesucht-Postfach nicht erreichbar: %s", exc)
                                return None
                klein_autosend = _switch("KLEINANZEIGEN_AUTOSEND", False)
                if klein_autosend or reply_mode == "auto":
                    if klein_autosend and scanner.klein_alerts is None:
                        raise ValueError("Für Kleinanzeigen-Versand zuerst Kleinanzeigen-Suchalarme einrichten")
                    try:
                        from kleinanzeigen_delivery import KleinanzeigenBrowserSender
                    except ModuleNotFoundError as exc:
                        raise ValueError("Für den Versand: pip install -r requirements-send.txt") from exc
                    klein_sender = KleinanzeigenBrowserSender(
                        data_dir / "kleinanzeigen_browser", headless=_switch("KLEINANZEIGEN_BROWSER_HEADLESS", True),
                    )
                    sessions.callback(klein_sender.close)
                    if klein_autosend:
                        scanner.klein_delivery = DeliveryQueue(klein_sender, limit, judge_provider=outgoing_provider)
                    if reply_mode == "auto":
                        reply_dispatcher = ReplyDispatcher(klein_sender, outgoing_provider, limit)
                interval = _number("MUNICH_POLL_SECONDS", 3600, 60, 3600)
                return _poll(scanner, db, interval, once=args.command == "scan",
                             reply_inbox=reply_inbox, judge_provider=judge_provider, notifier=notifier,
                             outgoing_provider=outgoing_provider, reply_dispatcher=reply_dispatcher,
                             wg_reply_reader=wg_reply_reader)
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("Start fehlgeschlagen: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0


def _switch(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} muss true oder false sein")
    return value == "true"


def _poll(
    scanner: Scanner, db: sqlite3.Connection, interval: int, *, once: bool,
    reply_inbox: AlertsInbox | None = None, judge_provider: str = "none", notifier: EmailNotifier | None = None,
    outgoing_provider: str = "none", reply_dispatcher: ReplyDispatcher | None = None,
    wg_reply_reader: Callable[[], tuple[WGInboxEntry, ...] | None] | None = None,
) -> int:
    delay = interval
    retry_after: dict[str, datetime] = {}
    review_retry_after: dict[str, datetime] = {}
    while True:
        started = time.monotonic()
        failed = False
        counts: Counter = Counter()
        try:
            counts.update(scanner.scan(db, datetime.now(timezone.utc), retry_after))
            delay = interval
        except (WGSourceError, AlertsError) as exc:
            LOG.warning("Scan pausiert: %s", exc)
            failed = True
            delay = min(max(600, delay * 2), 3600)
        now = datetime.now(timezone.utc)
        updated = scanner.add_intro_to_current_drafts(db, now)
        if updated:
            counts["gespeicherte Entwürfe sprachlich überarbeitet"] += updated
        counts.update(scanner.send_reviewed(db, now, review_retry_after))
        if wg_reply_reader is not None:
            drafted = sync_wg_reply_drafts(db, wg_reply_reader(), datetime.now(timezone.utc),
                                           provider=outgoing_provider, outgoing_provider=outgoing_provider)
            if drafted:
                counts["WG-Gesucht-Rückentwürfe"] += drafted
        klein_paused = scanner.klein_delivery is not None and scanner.klein_delivery.paused
        if reply_inbox:
            try:
                replied = record_replies(db, reply_inbox, judge_provider, now, outgoing_provider=outgoing_provider)
                if replied:
                    counts["Kleinanzeigen-Antworten bewertet"] += replied
                if reply_dispatcher and not klein_paused:
                    answers = reply_dispatcher.run(db, now)
                    if answers:
                        counts["Kleinanzeigen-Antworten gesendet"] += answers
            except AlertsError as exc:
                LOG.warning("Kleinanzeigen-Antworten nicht erreichbar: %s", exc)
                failed = True
        reply_paused = reply_dispatcher is not None and reply_dispatcher.paused
        now = datetime.now(timezone.utc)
        record_activity(db, now, klein_paused or reply_paused)
        if notifier:
            sent, ok = notifier.flush(db, now)
            if sent:
                counts["Hinweise per E-Mail angenommen"] += sent
            if not ok:
                LOG.warning("E-Mail-Hinweise vorerst nicht angenommen. Einstellungen prüfen.")
                failed = True
        LOG.info("Scan fertig: %s", ", ".join(f"{n} {key}" for key, n in counts.items()) or "nichts Neues")
        if once:
            return int(failed or klein_paused or reply_paused or
                       any(queue is not None and queue.paused for queue in (scanner.delivery, scanner.reviewed_delivery)))
        remaining = max(1, delay - (time.monotonic() - started))
        if scanner.reviewed_delivery is None:
            time.sleep(remaining)
            continue
        deadline = time.monotonic() + remaining
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(10, remaining))
            for outcome, count in scanner.send_reviewed(db, datetime.now(timezone.utc), review_retry_after).items():
                LOG.info("Freigegebene Bewerbungen: %s %s", count, outcome)


if __name__ == "__main__":
    raise SystemExit(main())
