"""Read a private dashboard snapshot from the MietRadar worker database."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit


_TABLE_COLUMNS = {
    "drafts": {"id", "url", "fresh_at", "basis", "message", "done"},
    "delivery": {"id", "state"},
    "outgoing_checks": {"id", "kind", "approved", "reason"},
    "klein_review": {"id", "url", "title", "fresh_at", "basis", "reason"},
    "klein_reply_drafts": {"id", "listing_id", "url", "message", "received_at", "state"},
    "klein_activity": {"kind", "listing_id", "url", "subject", "summary", "occurred_at"},
}
_PLATFORM_HOSTS = {"www.wg-gesucht.de", "wg-gesucht.de", "www.kleinanzeigen.de", "kleinanzeigen.de"}
_MAX_ROWS = 100


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
        if table in present and required <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    }


def _matches(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Match, ...]:
    if "drafts" not in tables:
        return ()
    delivery_join = "LEFT JOIN delivery s ON s.id = d.id" if "delivery" in tables else ""
    state = "s.state" if delivery_join else "NULL"
    check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'application'" if "outgoing_checks" in tables else ""
    reason = "c.reason" if check_join else "NULL"
    approved = "c.approved" if check_join else "NULL"
    rows = db.execute(f"""
        SELECT d.id, d.url, d.fresh_at, d.basis, d.message, {state}, {reason}, {approved}
        FROM drafts d {delivery_join} {check_join}
        WHERE d.fresh_at BETWEEN ? AND ?
          AND ((d.done = 0 AND d.message IS NOT NULL) OR {state} IS NOT NULL)
        ORDER BY d.fresh_at DESC, d.id DESC LIMIT ?
    """, (cutoff, until, _MAX_ROWS))
    return tuple(
        Match(listing_id, "Kleinanzeigen" if listing_id.startswith("ka-") else "WG-Gesucht",
              _platform_url(url), stamp, basis, message if state is None else None,
              state or ("held" if approved == 0 else "draft"), reason)
        for listing_id, url, stamp, basis, message, state, reason, approved in rows
    )


def _address_checks(db: sqlite3.Connection, cutoff: float, until: float) -> tuple[AddressCheck, ...]:
    rows = db.execute("""
        SELECT id, url, title, fresh_at, basis, reason FROM klein_review
        WHERE fresh_at BETWEEN ? AND ? ORDER BY fresh_at DESC, id DESC LIMIT ?
    """, (cutoff, until, _MAX_ROWS))
    return tuple(AddressCheck(item, _platform_url(url), title, stamp, basis, reason)
                 for item, url, title, stamp, basis, reason in rows)


def _replies(db: sqlite3.Connection, tables: set[str], cutoff: float, until: float) -> tuple[Reply, ...]:
    check_join = "LEFT JOIN outgoing_checks c ON c.id = d.id AND c.kind = 'reply'" if "outgoing_checks" in tables else ""
    reason = "c.reason" if check_join else "NULL"
    rows = db.execute(f"""
        SELECT d.id, d.listing_id, d.url, d.message, d.received_at, d.state, {reason}
        FROM klein_reply_drafts d {check_join}
        WHERE d.received_at BETWEEN ? AND ? ORDER BY d.received_at DESC, d.id DESC LIMIT ?
    """, (cutoff, until, _MAX_ROWS))
    return tuple(Reply(item, listing_id, _platform_url(url), message if state in {"draft", "held"} else None,
                       stamp, state, reason)
                 for item, listing_id, url, message, stamp, state, reason in rows)


def _activity(db: sqlite3.Connection, cutoff: float, until: float) -> tuple[Activity, ...]:
    rows = db.execute("""
        SELECT kind, listing_id, url, subject, summary, occurred_at FROM klein_activity
        WHERE occurred_at BETWEEN ? AND ? ORDER BY occurred_at DESC LIMIT ?
    """, (cutoff, until, _MAX_ROWS))
    return tuple(Activity(kind, listing_id, _platform_url(url), subject, summary, stamp)
                 for kind, listing_id, url, subject, summary, stamp in rows)


def load_dashboard(
    db_path: Path | str, *, now: datetime | None = None, max_age_hours: int = 24,
) -> DashboardData:
    """Read current matches and up to 30 days of replies and activity."""
    if not 1 <= max_age_hours <= 72:
        raise ValueError("max_age_hours must be between 1 and 72")
    now = now if now is not None else datetime.now(timezone.utc)
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
            )
    except (OSError, sqlite3.Error):
        return DashboardData("unavailable")
