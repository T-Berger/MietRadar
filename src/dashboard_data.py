"""Read dashboard data and save listing visibility in the MietRadar database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from rental_availability import AVAILABILITY_POLICY_VERSION

from listing_photos import listing_photo_urls
from listing_profiles import valid_listing_coordinates

if TYPE_CHECKING:
    from wg_source import RecipientRole


_TABLE_COLUMNS = {
    "drafts": {"id", "url", "fresh_at", "basis", "message", "done"},
    "delivery": {"id", "state"},
    "application_reviews": {"id", "state", "reason"},
    "outgoing_checks": {"id", "kind", "approved", "reason"},
    "klein_review": {"id", "url", "title", "fresh_at", "basis", "reason"},
    "klein_reply_drafts": {"id", "listing_id", "url", "message", "received_at", "state"},
    "wg_reply_drafts": {"id", "url", "message", "received_at", "state", "label", "summary", "review_reason"},
    "wg_reply_inbox_status": {"id", "checked_at", "available"},
    "klein_activity": {"kind", "listing_id", "url", "subject", "summary", "occurred_at"},
    "wg_inventory_scan": {"id", "scanned_at"},
    "wg_observations": {"last_seen_at"},
    "munich_secondary_offers": {"id", "url", "title", "fresh_at", "basis", "tier", "station", "minutes"},
    "listing_availability": {"id", "reason"},
    "listing_profiles": {"id", "title", "rent_eur", "rent_kind", "area_sqm", "summary", "fit", "fit_reason"},
    "dashboard_hidden_listings": {"id", "url", "title", "hidden_at"},
}
_PLATFORM_HOSTS = {"www.wg-gesucht.de", "wg-gesucht.de", "www.kleinanzeigen.de", "kleinanzeigen.de"}
WG_REPLY_INBOX = "https://www.wg-gesucht.de/nachrichten.html"
RESULT_LIMIT = 100
BERLIN = ZoneInfo("Europe/Berlin")
DateBasis = Literal["published", "first_seen"]
ProviderFilter = Literal["all", "commercial", "other"]


@dataclass(frozen=True)
class ListingProfile:
    title: str
    rent_eur: float | None
    rent_kind: str | None
    area_sqm: float | None
    summary: str | None
    fit: str | None
    fit_reason: str | None
    photo_urls: tuple[str, ...] = ()
    route_tier: str | None = None
    route_minutes: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    map_eligible: bool = True
    is_commercial: bool = False
    recipient_role: RecipientRole = "unknown"


@dataclass(frozen=True)
class Match:
    id: str
    platform: str
    url: str | None
    fresh_at: float
    basis: str
    message: str | None
    state: str
    review_reason: str | None
    published_day: date | None = None
    attachment_digest: str | None = None
    judge_approved: bool | None = None
    judge_reason: str | None = None


@dataclass(frozen=True)
class DatedListing:
    id: str
    platform: str
    url: str | None
    day: date
    matched: bool


@dataclass(frozen=True)
class SecondaryListing:
    id: str
    platform: str
    url: str | None
    title: str
    fresh_at: float
    basis: str
    tier: str
    station: str
    minutes: int


@dataclass(frozen=True)
class MapListing:
    id: str
    url: str | None
    platform: str
    title: str
    tier: str
    minutes: int | None
    station: str | None
    latitude: float | None
    longitude: float | None
    rent_eur: float | None
    rent_kind: str | None
    area_sqm: float | None
    is_commercial: bool = False
    recipient_role: RecipientRole = "unknown"


@dataclass(frozen=True)
class AddressCheck:
    id: str
    url: str | None
    title: str
    fresh_at: float
    basis: str
    reason: str


@dataclass(frozen=True)
class HiddenListing:
    id: str
    platform: str
    url: str | None
    title: str
    hidden_at: float


@dataclass(frozen=True)
class SentApplication:
    id: str
    platform: str
    url: str | None
    title: str | None
    sent_at: float


@dataclass(frozen=True)
class Reply:
    id: str
    listing_id: str | None
    url: str | None
    message: str | None
    received_at: float
    state: str
    review_reason: str | None
    platform: str = "Kleinanzeigen"
    label: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class Activity:
    kind: str
    listing_id: str | None
    url: str | None
    subject: str
    summary: str
    occurred_at: float


@dataclass(frozen=True)
class DashboardData:
    status: Literal["missing", "unavailable", "ready"]
    matches: tuple[Match, ...] = ()
    address_checks: tuple[AddressCheck, ...] = ()
    replies: tuple[Reply, ...] = ()
    activity: tuple[Activity, ...] = ()
    wg_scan: tuple[float, int] | None = None
    dated_listings: tuple[DatedListing, ...] = ()
    secondary_offers: tuple[SecondaryListing, ...] = ()
    listing_profiles: dict[str, ListingProfile] = field(default_factory=dict)
    application_reviews_available: bool = False
    wg_inbox_status: tuple[float, bool] | None = None
    dated_secondary_offers: tuple[SecondaryListing, ...] = ()
    hidden_listings: tuple[HiddenListing, ...] = ()
    sent_applications: tuple[SentApplication, ...] = ()


def filter_listing_providers(data: DashboardData, provider: ProviderFilter) -> DashboardData:
    if provider == "all":
        return data

    def includes(listing_id: str) -> bool:
        profile = data.listing_profiles.get(listing_id)
        return (provider == "commercial") == bool(profile and profile.is_commercial)

    return replace(
        data,
        matches=tuple(row for row in data.matches if includes(row.id)),
        address_checks=tuple(row for row in data.address_checks if includes(row.id)),
        dated_listings=tuple(row for row in data.dated_listings if includes(row.id)),
        secondary_offers=tuple(row for row in data.secondary_offers if includes(row.id)),
        dated_secondary_offers=tuple(row for row in data.dated_secondary_offers if includes(row.id)),
    )


def map_listings(data: DashboardData, *, date_filtered: bool = False) -> tuple[MapListing, ...]:
    """Return eligible A*–C listings, including those still waiting for a verified map point."""
    secondary_rows = (*data.secondary_offers, *data.dated_secondary_offers) if date_filtered else data.secondary_offers
    secondary_by_id = {row.id: row for row in secondary_rows if row.tier in {"b", "c"}}
    candidates = (data.dated_listings if date_filtered else
                  sorted((*data.matches, *data.secondary_offers), key=lambda row: row.fresh_at, reverse=True))
    listings = {}
    for row in candidates:
        profile = data.listing_profiles.get(row.id)
        if (profile and not profile.map_eligible) or (isinstance(row, Match) and row.state == "unavailable"):
            continue
        if secondary := secondary_by_id.get(row.id):
            title, tier, minutes = secondary.title, secondary.tier, secondary.minutes
            station = "Ostbahnhof" if tier == "b" else "Hauptbahnhof"
        elif profile and profile.route_tier in {"a*", "a"} and (
            isinstance(row, Match) or (isinstance(row, DatedListing) and row.matched)
        ):
            title, tier, minutes = profile.title, profile.route_tier, profile.route_minutes
            station = None
        else:
            continue
        point = valid_listing_coordinates(profile.latitude, profile.longitude) if profile else None
        latitude, longitude = point or (None, None)
        listings[row.id] = MapListing(
            row.id, row.url, row.platform, title or "Mietangebot", tier, minutes, station, latitude, longitude,
            profile.rent_eur if profile else None, profile.rent_kind if profile else None,
            profile.area_sqm if profile else None, bool(profile and profile.is_commercial),
            profile.recipient_role if profile else "unknown",
        )
    return tuple(listings.values())


def _platform_url(url: str | None) -> str | None:
    if not url or any(character.isspace() or ord(character) < 32 for character in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() not in _PLATFORM_HOSTS:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))


def set_listing_hidden(
    db_path: Path | str, listing_id: str, hidden: bool, *, title: str = "Mietangebot", url: str | None = None,
) -> None:
    """Hide a listing by its stable ID or restore it across dashboard visits."""
    with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=rw", uri=True, timeout=2)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_hidden_listings (
                id TEXT PRIMARY KEY, url TEXT, title TEXT NOT NULL, hidden_at REAL NOT NULL
            )
        """)
        if hidden:
            db.execute("""
                INSERT INTO dashboard_hidden_listings(id, url, title, hidden_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
            """, (listing_id, _platform_url(url), title or "Mietangebot", datetime.now(timezone.utc).timestamp()))
        else:
            db.execute("DELETE FROM dashboard_hidden_listings WHERE id = ?", (listing_id,))


def _hidden_listings(db: sqlite3.Connection, tables: set[str]) -> tuple[HiddenListing, ...]:
    if "dashboard_hidden_listings" not in tables:
        return ()
    rows = db.execute("SELECT id, url, title, hidden_at FROM dashboard_hidden_listings ORDER BY hidden_at DESC, id DESC")
    return tuple(HiddenListing(listing_id, "Kleinanzeigen" if listing_id.startswith("ka-") else "WG-Gesucht",
                               _platform_url(url), title, hidden_at)
                 for listing_id, url, title, hidden_at in rows)


def _wg_reply_url(url: str) -> str:
    if not url or any(character.isspace() or ord(character) < 32 for character in url):
        return WG_REPLY_INBOX
    try:
        parts = urlsplit(url)
        ids = parse_qs(parts.query, keep_blank_values=True, max_num_fields=16).get("nachrichten-id", [])
    except ValueError:
        return WG_REPLY_INBOX
    if (parts.scheme != "https" or parts.netloc.lower() not in {"www.wg-gesucht.de", "wg-gesucht.de"}
            or parts.path != "/nachricht.html" or len(ids) != 1):
        return WG_REPLY_INBOX
    conversation_id = ids[0]
    if not (1 <= len(conversation_id) <= 20 and conversation_id.isascii() and conversation_id.isdecimal()):
        return WG_REPLY_INBOX
    return f"https://www.wg-gesucht.de/nachricht.html?nachrichten-id={conversation_id}"


def _available_tables(db: sqlite3.Connection) -> set[str]:
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {
        table for table, required in _TABLE_COLUMNS.items()
        if table in present and required <= _columns(db, table)
    }


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def weekend_dates(today: date) -> tuple[date, date]:
    saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    return saturday, saturday + timedelta(days=1)


def _parse_day(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _availability_filter(db: sqlite3.Connection, tables: set[str], listing_id: str) -> str:
    if "listing_availability" not in tables:
        return "1"
    if "policy_version" in _columns(db, "listing_availability"):
        return f"""EXISTS (
            SELECT 1 FROM listing_availability availability
            WHERE availability.id = {listing_id} AND availability.reason IS NULL
              AND availability.policy_version = {AVAILABILITY_POLICY_VERSION}
        )"""
    return f"""NOT EXISTS (
        SELECT 1 FROM listing_availability availability
        WHERE availability.id = {listing_id} AND availability.reason IS NOT NULL
    )"""


def _not_hidden(tables: set[str], listing_id: str) -> str:
    if "dashboard_hidden_listings" not in tables:
        return "1"
    return f"NOT EXISTS (SELECT 1 FROM dashboard_hidden_listings hidden WHERE hidden.id = {listing_id})"


def _sent_applications(db: sqlite3.Connection, tables: set[str], until: float) -> tuple[SentApplication, ...]:
    if "delivery" not in tables or "recorded_at" not in _columns(db, "delivery"):
        return ()
    draft_join = "LEFT JOIN drafts d ON d.id = s.id" if "drafts" in tables else ""
    draft_url = "d.url" if draft_join else "NULL"
    observation_join = ""
    if "wg_observations" in tables and {"id", "url"} <= _columns(db, "wg_observations"):
        observation_join = "LEFT JOIN wg_observations o ON o.id = s.id"
    observation_url = "o.url" if observation_join else "NULL"
    profile_join = "LEFT JOIN listing_profiles p ON p.id = s.id" if "listing_profiles" in tables else ""
    title = "NULLIF(TRIM(p.title), '')" if profile_join else "NULL"
    rows = db.execute(f"""
        SELECT s.id, s.recorded_at, {draft_url}, {observation_url}, {title}
        FROM delivery s {draft_join} {observation_join} {profile_join}
        WHERE s.state = 'sent' AND s.recorded_at BETWEEN 0 AND ?
        ORDER BY s.recorded_at DESC, s.id DESC
    """, (until,))
    return tuple(SentApplication(listing_id, "Kleinanzeigen" if listing_id.startswith("ka-") else "WG-Gesucht",
                                 _platform_url(url) or _platform_url(observed_url), name, sent_at)
                 for listing_id, sent_at, url, observed_url, name in rows)


def _matches(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Match, ...]:
    if "drafts" not in tables:
        return ()
    delivery_join = "LEFT JOIN delivery s ON s.id = d.id" if "delivery" in tables else ""
    state = "s.state" if delivery_join else "NULL"
    check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'application'" if "outgoing_checks" in tables else ""
    has_check_hash = bool(check_join and "message_hash" in _columns(db, "outgoing_checks"))
    reason = "c.reason" if check_join else "NULL"
    approved = "c.approved" if check_join else "NULL"
    check_hash = "c.message_hash" if has_check_hash else "NULL"
    review_join = "LEFT JOIN application_reviews r ON r.id = d.id" if "application_reviews" in tables else ""
    review_state, review_reason = ("r.state", "r.reason") if review_join else ("NULL", "NULL")
    attachment_digest = ("r.attachment_digest" if review_join and
                         "attachment_digest" in _columns(db, "application_reviews") else "NULL")
    observations = _columns(db, "wg_observations") if "wg_observations" in tables else set()
    observation_join = "LEFT JOIN wg_observations o ON o.id = d.id" if {"id", "published_day"} <= observations else ""
    day = "o.published_day" if observation_join else "NULL"
    available = _availability_filter(db, tables, "d.id")
    secondary_filter = ""
    parameters = [cutoff, until]
    if "munich_secondary_offers" in tables:
        secondary_filter = """AND NOT EXISTS (
            SELECT 1 FROM munich_secondary_offers m
            WHERE m.id = d.id AND m.fresh_at BETWEEN ? AND ? AND m.minutes BETWEEN 1 AND 45
        )"""
        parameters.extend((cutoff, until))
    parameters.append(RESULT_LIMIT)
    rows = db.execute(f"""
        SELECT d.id, d.url, d.fresh_at, d.basis, d.message, {state}, {reason}, {approved}, {day},
               {review_state}, {review_reason}, {attachment_digest}, {check_hash}
        FROM drafts d {delivery_join} {check_join} {observation_join} {review_join}
        WHERE d.fresh_at BETWEEN ? AND ?
          AND ((d.done = 0 AND d.message IS NOT NULL) OR {state} IS NOT NULL)
          AND ({available} OR {state} IN ('sent', 'already_contacted', 'unconfirmed'))
          AND {_not_hidden(tables, 'd.id')}
          {secondary_filter}
        ORDER BY d.fresh_at DESC, d.id DESC LIMIT ?
    """, parameters)
    matches = []
    for (listing_id, url, stamp, basis, message, delivery, check_reason, approved, day_string,
         review, outcome_reason, attachment, message_hash) in rows:
        exact_check = (
            delivery is None and message is not None and approved in (0, 1)
            and message_hash == sha256(message.encode("utf-8")).hexdigest()
        )
        held = approved == 0 and (exact_check or not has_check_hash)
        reason = outcome_reason if review and delivery is None else None
        if delivery or (not review and not has_check_hash):
            reason = check_reason
        matches.append(Match(
            listing_id, "Kleinanzeigen" if listing_id.startswith("ka-") else "WG-Gesucht",
            _platform_url(url), stamp, basis, message if delivery is None else None,
            delivery or review or ("held" if held else "draft"), reason,
            _parse_day(day_string), attachment, bool(approved) if exact_check else None,
            check_reason if exact_check else None,
        ))
    return tuple(matches)


def _address_checks(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[AddressCheck, ...]:
    available = _availability_filter(db, tables, "r.id")
    rows = db.execute(f"""
        SELECT r.id, r.url, r.title, r.fresh_at, r.basis, r.reason FROM klein_review r
        WHERE r.fresh_at BETWEEN ? AND ? AND {available} AND {_not_hidden(tables, 'r.id')}
        ORDER BY r.fresh_at DESC, r.id DESC LIMIT ?
    """, (cutoff, until, RESULT_LIMIT))
    return tuple(AddressCheck(item, _platform_url(url), title, stamp, basis, reason)
                 for item, url, title, stamp, basis, reason in rows)


def _secondary_offers(
    db: sqlite3.Connection, tables: set[str], cutoff: float, until: float, *, ids: tuple[str, ...] | None = None,
) -> tuple[SecondaryListing, ...]:
    available = _availability_filter(db, tables, "m.id")
    if ids == ():
        return ()
    if ids is None:
        scope, params = "m.fresh_at BETWEEN ? AND ?", (cutoff, until)
    else:
        scope = f"m.fresh_at <= ? AND m.id IN ({', '.join('?' for _ in ids)})"
        params = (until, *ids)
    rows = db.execute(f"""
        SELECT m.id, m.url, m.title, m.fresh_at, m.basis, m.tier, m.station, m.minutes FROM munich_secondary_offers m
        WHERE {scope} AND m.minutes BETWEEN 1 AND 45 AND {available} AND {_not_hidden(tables, 'm.id')}
        ORDER BY m.fresh_at DESC, m.id DESC LIMIT ?
    """, (*params, RESULT_LIMIT))
    return tuple(
        SecondaryListing(item, "Kleinanzeigen" if item.startswith("ka-") else "WG-Gesucht",
                         _platform_url(url), title, stamp, basis, tier, station, minutes)
        for item, url, title, stamp, basis, tier, station, minutes in rows
    )


def _replies(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Reply, ...]:
    replies = []
    if "klein_reply_drafts" in tables:
        check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'reply'" if "outgoing_checks" in tables else ""
        reason = "c.reason" if check_join else "NULL"
        rows = db.execute(f"""
            SELECT d.id, d.listing_id, d.url, d.message, d.received_at, d.state, {reason}
            FROM klein_reply_drafts d {check_join}
            WHERE d.received_at BETWEEN ? AND ? ORDER BY d.received_at DESC, d.id DESC LIMIT ?
        """, (cutoff, until, RESULT_LIMIT))
        replies.extend(Reply(item, listing_id, _platform_url(url), message if state in {"draft", "held"} else None,
                             stamp, state, reason)
                       for item, listing_id, url, message, stamp, state, reason in rows)
    if "wg_reply_drafts" in tables:
        rows = db.execute("""
            SELECT id, url, message, received_at, state, review_reason, label, summary
            FROM wg_reply_drafts
            WHERE received_at BETWEEN ? AND ? ORDER BY received_at DESC, id DESC LIMIT ?
        """, (cutoff, until, RESULT_LIMIT))
        replies.extend(Reply(item, None, _wg_reply_url(url), message if state in {"draft", "held"} else None,
                             stamp, state, reason, "WG-Gesucht", label, summary)
                       for item, url, message, stamp, state, reason, label, summary in rows)
    return tuple(sorted(replies, key=lambda row: (row.received_at, row.id), reverse=True)[:RESULT_LIMIT])


def _wg_inbox_status(db: sqlite3.Connection, tables: set[str]) -> tuple[float, bool] | None:
    if "wg_reply_inbox_status" not in tables:
        return None
    row = db.execute("SELECT checked_at, available FROM wg_reply_inbox_status WHERE id = 1").fetchone()
    return (row[0], bool(row[1])) if row else None


def _activity(db: sqlite3.Connection, cutoff: float, until: float) -> tuple[Activity, ...]:
    rows = db.execute("""
        SELECT kind, listing_id, url, subject, summary, occurred_at FROM klein_activity
        WHERE occurred_at BETWEEN ? AND ? ORDER BY occurred_at DESC LIMIT ?
    """, (cutoff, until, RESULT_LIMIT))
    return tuple(Activity(kind, listing_id, _platform_url(url), subject, summary, stamp)
                 for kind, listing_id, url, subject, summary, stamp in rows)


def _wg_scan(db: sqlite3.Connection, tables: set[str]) -> tuple[float, int] | None:
    if not {"wg_inventory_scan", "wg_observations"} <= tables:
        return None
    return db.execute("""
        SELECT s.scanned_at, (SELECT COUNT(*) FROM wg_observations WHERE last_seen_at = s.scanned_at)
        FROM wg_inventory_scan s WHERE s.id = 1 AND s.scanned_at IS NOT NULL
    """).fetchone()


def _profiles(db: sqlite3.Connection, tables: set[str], ids: set[str]) -> dict[str, ListingProfile]:
    if "listing_profiles" not in tables or not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    columns = _columns(db, "listing_profiles")
    photo_column = "photo_urls" if "photo_urls" in columns else "NULL"
    route_columns = "route_tier, route_minutes" if {"route_tier", "route_minutes"} <= columns else "NULL, NULL"
    location_columns = "latitude, longitude" if {"latitude", "longitude"} <= columns else "NULL, NULL"
    commercial_column = "is_commercial" if "is_commercial" in columns else "NULL"
    role_column = "recipient_role" if "recipient_role" in columns else "NULL"
    available = _availability_filter(db, tables, "listing_profiles.id")
    rows = db.execute(f"""
        SELECT id, title, rent_eur, rent_kind, area_sqm, summary, fit, fit_reason, {photo_column}, {route_columns},
               {location_columns}, {available}, {commercial_column}, {role_column}
        FROM listing_profiles WHERE id IN ({placeholders})
    """, tuple(ids))
    profiles = {}
    for (listing_id, title, rent_eur, rent_kind, area_sqm, summary, fit, fit_reason, photos_json, tier, minutes,
         latitude, longitude, map_eligible, is_commercial, role) in rows:
        try:
            photos = json.loads(photos_json) if photos_json else []
        except (ValueError, TypeError):
            photos = []
        platform = "ka" if listing_id.startswith("ka-") else "wg"
        photo_urls = listing_photo_urls(photos, platform=platform) if isinstance(photos, list) else ()
        latitude, longitude = valid_listing_coordinates(latitude, longitude) or (None, None)
        profiles[listing_id] = ListingProfile(
            title, rent_eur, rent_kind, area_sqm, summary, fit, fit_reason, photo_urls, tier, minutes,
            latitude, longitude, bool(map_eligible), is_commercial == 1,
            role if role in ("manager", "roommate") else "unknown",
        )
    return profiles


def _dated_listings(
    db: sqlite3.Connection, tables: set[str], window: tuple[date, date], now: datetime, basis: DateBasis,
) -> tuple[DatedListing, ...]:
    start_day, end_day = window
    end_day = min(end_day, now.astimezone(BERLIN).date())
    if start_day > end_day:
        return ()

    start = datetime.combine(start_day, time.min, BERLIN).timestamp()
    stop = datetime.combine(end_day + timedelta(days=1), time.min, BERLIN).timestamp()
    timestamp_params = (start, stop, now.timestamp(), RESULT_LIMIT)
    has_drafts = "drafts" in tables
    observation_columns = _columns(db, "wg_observations") if "wg_observations" in tables else set()
    has_observations = {"id", "url", "published_at"} <= observation_columns
    has_observation_day = "published_day" in observation_columns
    matched = "EXISTS (SELECT 1 FROM drafts d WHERE d.id = o.id)" if has_drafts else "0"
    observations_filter = _availability_filter(db, tables, "o.id")
    visible_observation = _not_hidden(tables, "o.id")

    if basis == "first_seen":
        if not has_observations or "first_seen_at" not in observation_columns:
            return ()
        unknown_day = "AND o.published_day IS NULL" if has_observation_day else ""
        unknown_draft = """
            AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.id = o.id AND lower(d.basis) = 'veröffentlicht')
        """ if has_drafts else ""
        rows = db.execute(f"""
            SELECT o.id, o.url, o.first_seen_at, {matched}
            FROM wg_observations o
            WHERE o.first_seen_at >= ? AND o.first_seen_at < ? AND o.first_seen_at <= ?
            AND o.published_at IS NULL AND {observations_filter} AND {visible_observation} {unknown_day} {unknown_draft}
            ORDER BY o.first_seen_at DESC, o.id DESC LIMIT ?
        """, timestamp_params)
        return tuple(DatedListing(item, "WG-Gesucht", _platform_url(url), datetime.fromtimestamp(stamp, BERLIN).date(), bool(is_match))
                     for item, url, stamp, is_match in rows)

    candidates: dict[str, tuple[float, DatedListing]] = {}
    if has_observations:
        for item, url, stamp, is_match in db.execute(f"""
            SELECT o.id, o.url, o.published_at, {matched}
            FROM wg_observations o WHERE o.published_at >= ? AND o.published_at < ? AND o.published_at <= ?
            AND {observations_filter} AND {visible_observation}
            ORDER BY o.published_at DESC, o.id DESC LIMIT ?
        """, timestamp_params):
            day = datetime.fromtimestamp(stamp, BERLIN).date()
            candidates[item] = (stamp, DatedListing(item, "WG-Gesucht", _platform_url(url), day, bool(is_match)))

        if has_observation_day:
            for item, url, day_string, is_match in db.execute(f"""
                SELECT o.id, o.url, o.published_day, {matched}
                FROM wg_observations o WHERE o.published_at IS NULL AND o.published_day BETWEEN ? AND ?
                AND {observations_filter} AND {visible_observation}
                ORDER BY o.published_day DESC, o.id DESC LIMIT ?
            """, (start_day.isoformat(), end_day.isoformat(), RESULT_LIMIT)):
                if day := _parse_day(day_string):
                    candidates[item] = (0, DatedListing(item, "WG-Gesucht", _platform_url(url), day, bool(is_match)))

    if has_drafts:
        fallback = ""
        drafts_filter = _availability_filter(db, tables, "d.id")
        if has_observations:
            day_clause = " OR o.published_day IS NOT NULL" if has_observation_day else ""
            fallback = f"""AND NOT EXISTS (SELECT 1 FROM wg_observations o WHERE o.id = d.id
                            AND (o.published_at IS NOT NULL{day_clause}))"""
        for item, url, stamp in db.execute(f"""
            SELECT d.id, d.url, d.fresh_at FROM drafts d
            WHERE lower(d.basis) = 'veröffentlicht' AND d.fresh_at >= ? AND d.fresh_at < ? AND d.fresh_at <= ?
            AND {drafts_filter} AND {_not_hidden(tables, 'd.id')} {fallback}
            ORDER BY d.fresh_at DESC, d.id DESC LIMIT ?
        """, timestamp_params):
            day = datetime.fromtimestamp(stamp, BERLIN).date()
            platform = "Kleinanzeigen" if item.startswith("ka-") else "WG-Gesucht"
            candidates[item] = (stamp, DatedListing(item, platform, _platform_url(url), day, True))

    ordered = sorted(candidates.values(), key=lambda row: (row[1].day, row[0], row[1].id), reverse=True)
    return tuple(listing for _, listing in ordered[:RESULT_LIMIT])


def load_dashboard(
    db_path: Path | str, *, now: datetime | None = None, max_age_hours: int = 24,
    posting_window: tuple[date, date] | None = None, date_basis: DateBasis = "published",
) -> DashboardData:
    """Read current matches, confirmed applications, and recent replies and activity."""
    if not 1 <= max_age_hours <= 72:
        raise ValueError("max_age_hours must be between 1 and 72")
    now = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=max_age_hours)).timestamp()
    history_cutoff = (now - timedelta(days=30)).timestamp()
    until = now.timestamp()
    path = Path(db_path)

    try:
        if not path.is_file():
            return DashboardData("missing")
        # URI read-only mode keeps the dashboard from creating a database if the worker has not started.
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            tables = _available_tables(db)
            matches = _matches(db, tables, cutoff, until)
            address_checks = _address_checks(db, tables, cutoff, until) if "klein_review" in tables else ()
            dated_listings = _dated_listings(db, tables, posting_window, now, date_basis) if posting_window else ()
            secondary_offers = _secondary_offers(db, tables, cutoff, until) if "munich_secondary_offers" in tables else ()
            dated_secondary_offers = _secondary_offers(db, tables, cutoff, until, ids=tuple(
                row.id for row in dated_listings
            )) if dated_listings and "munich_secondary_offers" in tables else ()
            visible_ids = {row.id for group in (matches, address_checks, dated_listings, secondary_offers) for row in group}
            return DashboardData(
                "ready",
                matches=matches,
                address_checks=address_checks,
                replies=_replies(db, tables, history_cutoff, until),
                activity=_activity(db, history_cutoff, until) if "klein_activity" in tables else (),
                wg_scan=_wg_scan(db, tables),
                dated_listings=dated_listings,
                secondary_offers=secondary_offers,
                listing_profiles=_profiles(db, tables, visible_ids),
                application_reviews_available="application_reviews" in tables,
                wg_inbox_status=_wg_inbox_status(db, tables),
                dated_secondary_offers=dated_secondary_offers,
                hidden_listings=_hidden_listings(db, tables),
                sent_applications=_sent_applications(db, tables, until),
            )
    except (OSError, sqlite3.Error):
        return DashboardData("unavailable")
