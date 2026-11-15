"""Read a private dashboard snapshot from the MietRadar worker database."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


_TABLE_COLUMNS = {
    "drafts": {"id", "url", "fresh_at", "basis", "message", "done"},
    "delivery": {"id", "state"},
    "outgoing_checks": {"id", "kind", "approved", "reason"},
    "klein_review": {"id", "url", "title", "fresh_at", "basis", "reason"},
    "klein_reply_drafts": {"id", "listing_id", "url", "message", "received_at", "state"},
    "klein_activity": {"kind", "listing_id", "url", "subject", "summary", "occurred_at"},
    "wg_inventory_scan": {"id", "scanned_at"},
    "wg_observations": {"last_seen_at"},
}
_PLATFORM_HOSTS = {"www.wg-gesucht.de", "wg-gesucht.de", "www.kleinanzeigen.de", "kleinanzeigen.de"}
RESULT_LIMIT = 100
BERLIN = ZoneInfo("Europe/Berlin")
DateBasis = Literal["published", "first_seen"]


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


@dataclass(frozen=True)
class DatedListing:
    id: str
    platform: str
    url: str | None
    day: date
    matched: bool


@dataclass(frozen=True)
class AddressCheck:
    id: str
    url: str | None
    title: str
    fresh_at: float
    basis: str
    reason: str


@dataclass(frozen=True)
class Reply:
    id: str
    listing_id: str | None
    url: str | None
    message: str | None
    received_at: float
    state: str
    review_reason: str | None


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


def _platform_url(url: str) -> str | None:
    if not url or any(character.isspace() or ord(character) < 32 for character in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() not in _PLATFORM_HOSTS:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))


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


def _matches(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Match, ...]:
    if "drafts" not in tables:
        return ()
    delivery_join = "LEFT JOIN delivery s ON s.id = d.id" if "delivery" in tables else ""
    state = "s.state" if delivery_join else "NULL"
    check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'application'" if "outgoing_checks" in tables else ""
    reason = "c.reason" if check_join else "NULL"
    approved = "c.approved" if check_join else "NULL"
    observations = _columns(db, "wg_observations") if "wg_observations" in tables else set()
    observation_join = "LEFT JOIN wg_observations o ON o.id = d.id" if {"id", "published_day"} <= observations else ""
    day = "o.published_day" if observation_join else "NULL"
    rows = db.execute(f"""
        SELECT d.id, d.url, d.fresh_at, d.basis, d.message, {state}, {reason}, {approved}, {day}
        FROM drafts d {delivery_join} {check_join} {observation_join}
        WHERE d.fresh_at BETWEEN ? AND ?
          AND ((d.done = 0 AND d.message IS NOT NULL) OR {state} IS NOT NULL)
        ORDER BY d.fresh_at DESC, d.id DESC LIMIT ?
    """, (cutoff, until, RESULT_LIMIT))
    return tuple(
        Match(listing_id, "Kleinanzeigen" if listing_id.startswith("ka-") else "WG-Gesucht",
              _platform_url(url), stamp, basis, message if state is None else None,
              state or ("held" if approved == 0 else "draft"), reason, _parse_day(day_string))
        for listing_id, url, stamp, basis, message, state, reason, approved, day_string in rows
    )


def _address_checks(db: sqlite3.Connection, cutoff: float, until: float) -> tuple[AddressCheck, ...]:
    rows = db.execute("""
        SELECT id, url, title, fresh_at, basis, reason FROM klein_review
        WHERE fresh_at BETWEEN ? AND ? ORDER BY fresh_at DESC, id DESC LIMIT ?
    """, (cutoff, until, RESULT_LIMIT))
    return tuple(AddressCheck(item, _platform_url(url), title, stamp, basis, reason)
                 for item, url, title, stamp, basis, reason in rows)


def _replies(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Reply, ...]:
    check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'reply'" if "outgoing_checks" in tables else ""
    reason = "c.reason" if check_join else "NULL"
    rows = db.execute(f"""
        SELECT d.id, d.listing_id, d.url, d.message, d.received_at, d.state, {reason}
        FROM klein_reply_drafts d {check_join}
        WHERE d.received_at BETWEEN ? AND ? ORDER BY d.received_at DESC, d.id DESC LIMIT ?
    """, (cutoff, until, RESULT_LIMIT))
    return tuple(Reply(item, listing_id, _platform_url(url), message if state in {"draft", "held"} else None,
                       stamp, state, reason)
                 for item, listing_id, url, message, stamp, state, reason in rows)


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
            AND o.published_at IS NULL {unknown_day} {unknown_draft}
            ORDER BY o.first_seen_at DESC, o.id DESC LIMIT ?
        """, timestamp_params)
        return tuple(DatedListing(item, "WG-Gesucht", _platform_url(url), datetime.fromtimestamp(stamp, BERLIN).date(), bool(is_match))
                     for item, url, stamp, is_match in rows)

    candidates: dict[str, tuple[float, DatedListing]] = {}
    if has_observations:
        for item, url, stamp, is_match in db.execute(f"""
            SELECT o.id, o.url, o.published_at, {matched}
            FROM wg_observations o WHERE o.published_at >= ? AND o.published_at < ? AND o.published_at <= ?
            ORDER BY o.published_at DESC, o.id DESC LIMIT ?
        """, timestamp_params):
            day = datetime.fromtimestamp(stamp, BERLIN).date()
            candidates[item] = (stamp, DatedListing(item, "WG-Gesucht", _platform_url(url), day, bool(is_match)))

        if has_observation_day:
            for item, url, day_string, is_match in db.execute(f"""
                SELECT o.id, o.url, o.published_day, {matched}
                FROM wg_observations o WHERE o.published_at IS NULL AND o.published_day BETWEEN ? AND ?
                ORDER BY o.published_day DESC, o.id DESC LIMIT ?
            """, (start_day.isoformat(), end_day.isoformat(), RESULT_LIMIT)):
                if day := _parse_day(day_string):
                    candidates[item] = (0, DatedListing(item, "WG-Gesucht", _platform_url(url), day, bool(is_match)))

    if has_drafts:
        fallback = ""
        if has_observations:
            day_clause = " OR o.published_day IS NOT NULL" if has_observation_day else ""
            fallback = f"""AND NOT EXISTS (SELECT 1 FROM wg_observations o WHERE o.id = d.id
                            AND (o.published_at IS NOT NULL{day_clause}))"""
        for item, url, stamp in db.execute(f"""
            SELECT d.id, d.url, d.fresh_at FROM drafts d
            WHERE lower(d.basis) = 'veröffentlicht' AND d.fresh_at >= ? AND d.fresh_at < ? AND d.fresh_at <= ?
            {fallback} ORDER BY d.fresh_at DESC, d.id DESC LIMIT ?
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
    """Read current matches and up to 30 days of replies and activity."""
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
            return DashboardData(
                "ready",
                matches=_matches(db, tables, cutoff, until),
                address_checks=_address_checks(db, cutoff, until) if "klein_review" in tables else (),
                replies=_replies(db, tables, history_cutoff, until) if "klein_reply_drafts" in tables else (),
                activity=_activity(db, history_cutoff, until) if "klein_activity" in tables else (),
                wg_scan=_wg_scan(db, tables),
                dated_listings=_dated_listings(db, tables, posting_window, now, date_basis) if posting_window else (),
            )
    except (OSError, sqlite3.Error):
        return DashboardData("unavailable")
