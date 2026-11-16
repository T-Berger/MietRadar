"""Let a paired Mac deliver exact, reviewed WG-Gesucht applications."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

from munich_delivery import DeliveryOutcome, FirstMessageReview, review_outgoing, within_send_limit
from munich_radar import ROOT, Scanner, _database, _number, _scanner, _switch
from reviewed_applications import (
    ReviewedApplication, fail_application_review, get_application_attachment, pending_application_reviews,
    review_is_pending, set_application_wait_reason,
)
from wg_source import Listing


LOG = logging.getLogger("wg_delivery_relay")
VALID_TOKEN = re.compile(r"(?:[a-f0-9]{64}|[A-Za-z0-9_-]{43})", re.ASCII)
REVISION = re.compile(r"[a-f0-9]{64}", re.ASCII)
LISTING_ID = re.compile(r"[0-9]{1,20}", re.ASCII)
MAX_REQUEST_BYTES = 256 * 1024
PREPARATION_AGE = timedelta(minutes=2)
RETRY_DELAY = timedelta(minutes=2)
REPLY_RETRY_DELAY = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_fields(body: object, expected: set[str]) -> dict:
    if not isinstance(body, dict) or body.keys() != expected:
        raise ValueError("Invalid request fields")
    return body


def _read_job(body: object, *fields: str) -> tuple[str, str]:
    job = _read_fields(body, {"id", "revision", *fields})
    listing_id, revision = job["id"], job["revision"]
    if (
        not isinstance(listing_id, str)
        or not LISTING_ID.fullmatch(listing_id)
        or not isinstance(revision, str)
        or not REVISION.fullmatch(revision)
    ):
        raise ValueError("Invalid approval identifier")
    return listing_id, revision


def _read_public_listing(body: object, item: ReviewedApplication) -> Listing:
    text_limits = {
        "title": 1024, "description": 65536, "address": 512, "wanted": 1024, "wg_type": 1024, "resident_ages": 1024,
    }
    days = {"published_day", "available_from", "available_until"}
    required = {"published_at", *text_limits, *days}
    optional = {"is_commercial"} if isinstance(body, dict) and "is_commercial" in body else set()
    fields = _read_fields(body, required | optional)
    is_commercial = fields.get("is_commercial", False)
    if type(is_commercial) is not bool:
        raise ValueError("Invalid public listing provider")
    values = {}
    for key, limit in text_limits.items():
        value = fields[key]
        if value is None and key not in {"title", "description"}:
            values[key] = None
        elif isinstance(value, str) and len(value) <= limit and "\x00" not in value:
            values[key] = value
        else:
            raise ValueError("Invalid public listing field")
    for key in days:
        value = fields[key]
        if value is not None and (not isinstance(value, str) or len(value) != 10):
            raise ValueError("Invalid listing date")
        values[key] = date.fromisoformat(value) if value else None
    value = fields["published_at"]
    if value is not None and (not isinstance(value, str) or len(value) > 40):
        raise ValueError("Invalid publication time")
    published_at = datetime.fromisoformat(value) if value else None
    if published_at is not None and published_at.tzinfo is None:
        raise ValueError("Publication time needs a time zone")
    return Listing(id=item.id, url=item.url, published_at=published_at, is_commercial=is_commercial, **values)


class ReviewedDeliveryRelay:
    def __init__(
        self, db_path: Path, scanner: Scanner, token: str, daily_limit: int,
        *, clock: Callable[[], datetime] = _utcnow,
    ):
        if not VALID_TOKEN.fullmatch(token):
            raise ValueError("MUNICH_WG_RELAY_TOKEN muss ein zufällig erzeugtes 256-Bit-Token sein.")
        self.db_path = db_path
        self.scanner = scanner
        self.secret = token.encode("ascii")
        self.authorization = b"Bearer " + self.secret
        self.daily_limit = daily_limit
        self.clock = clock
        self.lock = Lock()
        self.retry_after: dict[str, datetime] = {}
        with closing(_database(db_path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS wg_relay_preparations (
                    id TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    claimed_at REAL,
                    outcome TEXT
                )
            """)
            db.execute("DELETE FROM wg_relay_preparations WHERE expires_at < ?",
                       ((self.clock() - timedelta(days=2)).timestamp(),))

    def _revision(self, item: ReviewedApplication) -> str:
        value = json.dumps([
            item.id, item.url, item.message_hash, item.updated_at.hex(), item.attachment_digest,
        ], ensure_ascii=True, separators=(",", ":"))
        return hmac.digest(self.secret, value.encode("ascii"), "sha256").hex()

    def _item(self, db: sqlite3.Connection, listing_id: str, revision: str, now: datetime) -> ReviewedApplication | None:
        return next((
            item for item in pending_application_reviews(db, now, self.scanner.max_age)
            if item.id == listing_id and hmac.compare_digest(self._revision(item), revision)
        ), None)

    def _still_pending(self, db: sqlite3.Connection, item: ReviewedApplication, now: datetime) -> bool:
        if not review_is_pending(
            db, item.id, item.message, now, self.scanner.max_age, attachment_digest=item.attachment_digest,
        ):
            return False
        row = db.execute(
            "SELECT updated_at, message_hash FROM application_reviews WHERE id = ?", (item.id,),
        ).fetchone()
        return row == (item.updated_at, item.message_hash)

    @staticmethod
    def _preparation(db: sqlite3.Connection, listing_id: str, revision: str) -> tuple | None:
        return db.execute("""
            SELECT expires_at, claimed_at, outcome FROM wg_relay_preparations WHERE id = ? AND revision = ?
        """, (listing_id, revision)).fetchone()

    def _next(self, db: sqlite3.Connection, body: object) -> dict:
        _read_fields(body, set())
        now = self.clock()
        self.retry_after = {key: stamp for key, stamp in self.retry_after.items() if now < stamp}
        if within_send_limit(db, now, self.daily_limit):
            for item in pending_application_reviews(db, now, self.scanner.max_age):
                if item.id not in self.retry_after:
                    return {"job": {"id": item.id, "url": item.url, "revision": self._revision(item)}}
        return {"job": None}

    def _prepare(self, db: sqlite3.Connection, body: object) -> dict:
        listing_id, revision = _read_job(body, "listing")
        now = self.clock()
        item = self._item(db, listing_id, revision, now)
        if not item or listing_id in self.retry_after or not within_send_limit(db, now, self.daily_limit):
            return {"ready": False}
        listing = _read_public_listing(body["listing"], item)
        prior = self._preparation(db, listing_id, revision)
        reuse = prior and prior[0] > now.timestamp() and prior[1] is None and prior[2] is None
        if not reuse:
            if not self.scanner.validate_reviewed_listing(db, item, listing, now, self.retry_after):
                return {"ready": False}
            provider_type = db.execute("SELECT is_commercial FROM listing_profiles WHERE id = ?", (item.id,)).fetchone()
            judgment = review_outgoing(
                db, item.id, item.message, kind="application", context=listing.description,
                provider=self.scanner.outgoing_provider, now=self.clock(),
                first_message=FirstMessageReview(listing.title, "", (), {}, user_reviewed=True,
                                                commercial=listing.is_commercial or bool(provider_type and provider_type[0])),
            )
            if not judgment.approved:
                fail_application_review(db, item, judgment.reason, self.clock())
                return {"ready": False}
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if not self._still_pending(db, item, now) or not within_send_limit(db, now, self.daily_limit):
                return {"ready": False}
            if reuse and prior[0] <= now.timestamp():
                return {"ready": False}
            attachment = None
            if item.attachment_digest:
                document = get_application_attachment(db, item.attachment_digest)
                if document is None:
                    self.retry_after[item.id] = now + RETRY_DELAY
                    return {"ready": False}
                attachment = {
                    "name": document.name, "digest": document.digest,
                    "content": base64.b64encode(document.content).decode("ascii"),
                }
            if not reuse:
                db.execute("""
                    INSERT INTO wg_relay_preparations (id, revision, expires_at, claimed_at, outcome)
                    VALUES (?, ?, ?, NULL, NULL) ON CONFLICT(id) DO UPDATE SET
                        revision = excluded.revision, expires_at = excluded.expires_at, claimed_at = NULL, outcome = NULL
                """, (item.id, revision, (now + PREPARATION_AGE).timestamp()))
        set_application_wait_reason(db, item, None)
        return {"ready": True, "message": item.message, "attachment": attachment}

    def _claim(self, db: sqlite3.Connection, body: object) -> dict:
        listing_id, revision = _read_job(body)
        item = self._item(db, listing_id, revision, self.clock())
        if item is None:
            return {"claimed": False}
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            prep = self._preparation(db, listing_id, revision)
            if (
                prep is None or prep[1] is not None or prep[2] is not None or prep[0] <= now.timestamp()
                or not self._still_pending(db, item, now) or not within_send_limit(db, now, self.daily_limit)
            ):
                return {"claimed": False}
            stamp = now.timestamp()
            db.execute("INSERT INTO delivery VALUES (?, 'unconfirmed', ?, ?)", (listing_id, stamp, stamp))
            db.execute("UPDATE wg_relay_preparations SET claimed_at = ? WHERE id = ? AND revision = ?",
                       (stamp, listing_id, revision))
        return {"claimed": True}

    def _finish(self, db: sqlite3.Connection, body: object) -> dict:
        listing_id, revision = _read_job(body, "outcome")
        try:
            outcome = DeliveryOutcome[body["outcome"].upper()]
        except (AttributeError, KeyError):
            raise ValueError("Invalid outcome") from None
        now = self.clock()
        prep = self._preparation(db, listing_id, revision)
        if prep and prep[2] is not None:
            return {"recorded": prep[2] == outcome.name}
        if prep and prep[1] is not None:
            return {"recorded": self._finish_claim(db, listing_id, revision, prep[1], outcome, now)}

        item = self._item(db, listing_id, revision, now)
        if item is None:
            return {"recorded": False}
        if outcome in {DeliveryOutcome.BLOCKED, DeliveryOutcome.SKIPPED}:
            self.retry_after[item.id] = now + (REPLY_RETRY_DELAY if outcome == DeliveryOutcome.BLOCKED else RETRY_DELAY)
            reason = ("Der Versandbrowser auf dem Mac braucht eine Anmeldung oder Prüfung."
                      if outcome == DeliveryOutcome.BLOCKED else "Die Anzeige konnte am Mac gerade nicht geprüft werden.")
            set_application_wait_reason(db, item, reason)
            with db:
                db.execute("DELETE FROM wg_relay_preparations WHERE id = ? AND revision = ? AND claimed_at IS NULL",
                           (item.id, revision))
            return {"recorded": True}
        if outcome != DeliveryOutcome.UNAVAILABLE and (
            outcome != DeliveryOutcome.ALREADY_CONTACTED or prep is None or prep[0] <= now.timestamp()
        ):
            return {"recorded": False}
        return {"recorded": self._finish_without_claim(db, item, revision, outcome, now)}

    def _finish_claim(
        self, db: sqlite3.Connection, listing_id: str, revision: str, claimed_at: float,
        outcome: DeliveryOutcome, now: datetime,
    ) -> bool:
        if outcome not in {DeliveryOutcome.SENT, DeliveryOutcome.UNCONFIRMED, DeliveryOutcome.SKIPPED, DeliveryOutcome.BLOCKED}:
            return False
        with db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""
                UPDATE delivery SET state = ?, recorded_at = ?
                WHERE id = ? AND state = 'unconfirmed' AND attempted_at = ?
            """, ("sent" if outcome == DeliveryOutcome.SENT else "unconfirmed", now.timestamp(), listing_id, claimed_at))
            if not changed.rowcount:
                return False
            if outcome == DeliveryOutcome.SENT:
                db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (listing_id,))
            db.execute("UPDATE wg_relay_preparations SET outcome = ? WHERE id = ? AND revision = ?",
                       (outcome.name, listing_id, revision))
        return True

    def _finish_without_claim(
        self, db: sqlite3.Connection, item: ReviewedApplication, revision: str, outcome: DeliveryOutcome, now: datetime,
    ) -> bool:
        with db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if not self._still_pending(db, item, now):
                return False
            stamp = now.timestamp()
            db.execute("INSERT INTO delivery VALUES (?, ?, ?, NULL)", (item.id, outcome.name.lower(), stamp))
            if outcome == DeliveryOutcome.ALREADY_CONTACTED:
                db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (item.id,))
            db.execute("""
                INSERT INTO wg_relay_preparations (id, revision, expires_at, claimed_at, outcome)
                VALUES (?, ?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET
                    revision = excluded.revision, expires_at = excluded.expires_at, claimed_at = NULL,
                    outcome = excluded.outcome
            """, (item.id, revision, stamp, outcome.name))
        return True

    def dispatch(self, path: str, body: object) -> dict:
        if path == "/v1/status":
            _read_fields(body, set())
            return {"ok": True}
        handlers = {
            "/v1/next": self._next, "/v1/prepare": self._prepare, "/v1/claim": self._claim, "/v1/finish": self._finish,
        }
        with self.lock, closing(sqlite3.connect(self.db_path, timeout=5)) as db:
            db.execute("PRAGMA secure_delete = ON")
            return handlers[path](db, body)


class _RelayServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], relay: ReviewedDeliveryRelay):
        self.relay = relay
        super().__init__(address, _RelayHandler)

    def handle_error(self, request, client_address):
        LOG.warning("Eine Anfrage an den WG-Versand konnte nicht beantwortet werden.")


class _RelayHandler(BaseHTTPRequestHandler):
    server: _RelayServer

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    def _respond(self, status: HTTPStatus, body: dict) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        received = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(received, self.server.relay.authorization):
            self._respond(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if self.path not in {"/v1/status", "/v1/next", "/v1/prepare", "/v1/claim", "/v1/finish"}:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        length_headers = self.headers.get_all("Content-Length") or []
        if (len(length_headers) != 1 or not re.fullmatch(r"[0-9]{1,9}", length_headers[0], re.ASCII)
                or self.headers.get("Transfer-Encoding") is not None):
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "invalid_length"})
            return
        length = int(length_headers[0])
        if length > MAX_REQUEST_BYTES:
            self._respond(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
            return
        try:
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise ValueError("Incomplete request")
            result = self.server.relay.dispatch(self.path, json.loads(payload))
        except (ValueError, UnicodeError):
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
        except TimeoutError:
            self._respond(HTTPStatus.REQUEST_TIMEOUT, {"error": "request_timeout"})
        except Exception:
            LOG.warning("Der WG-Versand konnte eine Anfrage nicht verarbeiten.")
            self._respond(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "temporarily_unavailable"})
        else:
            self._respond(HTTPStatus.OK, result)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        if not _switch("MUNICH_REVIEWED_SEND", False):
            raise ValueError("Der WG-Versand vom Mac braucht MUNICH_REVIEWED_SEND=true.")
        max_age = timedelta(hours=_number("MUNICH_MAX_AGE_HOURS", 24, 1, 72))
        data_dir = Path(os.getenv("MUNICH_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
        relay = ReviewedDeliveryRelay(
            data_dir / "munich_radar.sqlite3", _scanner(max_age), os.getenv("MUNICH_WG_RELAY_TOKEN", ""),
            _number("MUNICH_DAILY_SEND_LIMIT", 3, 1, 5),
        )
        port = _number("MUNICH_WG_RELAY_PORT", 8765, 1, 65535)
        with _RelayServer(("127.0.0.1", port), relay) as server:
            LOG.info("Der WG-Versand vom Mac ist bereit.")
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        if isinstance(exc, ValueError):
            LOG.error("Der WG-Versand konnte nicht starten. %s", exc)
        else:
            LOG.error("Der WG-Versand konnte nicht starten. Prüfe Datenbank und Port.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
