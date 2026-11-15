"""Poll Munich listings and queue fresh drafts for manual review and sending."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from llm_personalizer import personalise_message
from munich_transit import TransitRouter
from wg_alerts import AlertsError, AlertsInbox
from wg_preferences import rejection_reason
from wg_source import DEFAULT_SEARCH_URLS, WGListingGone, WGSource, WGSourceError

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

    def scan(self, db: sqlite3.Connection, now: datetime, retry_after: dict[str, datetime]) -> Counter:
        cutoff = now - self.max_age
        with db:
            # Keep IDs for deduplication, but remove expired personal drafts.
            db.execute("UPDATE drafts SET message = NULL WHERE message IS NOT NULL AND fresh_at < ?",
                       (cutoff.timestamp(),))
        for listing_id, retry in list(retry_after.items()):
            if retry <= now:
                del retry_after[listing_id]
        if self.alerts:
            refs = [(alert.ref, alert.received_at) for alert in self.alerts.recent(cutoff, self.limit)]
        else:
            refs = [(ref, None) for ref in self.source.candidates()]

        counts: Counter = Counter()
        eligible = []
        for ref, received_at in refs:
            if ref.id in retry_after or db.execute("SELECT 1 FROM drafts WHERE id = ?", (ref.id,)).fetchone():
                continue
            try:
                listing = self.source.details(ref)
            except WGListingGone:
                counts["nicht mehr verfügbar"] += 1
                retry_after[ref.id] = now + self.max_age
                continue

            fresh_at = listing.published_at if listing.published_at is not None else received_at
            if fresh_at is None or fresh_at.tzinfo is None:
                counts["kein Veröffentlichungsdatum"] += 1
                retry_after[ref.id] = now + timedelta(minutes=15)
                continue
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
                counts[reason] += 1
                retry_after[ref.id] = now + self.max_age
                continue
            eligible.append((fresh_at, listing, "veröffentlicht" if listing.published_at else "Suchalarm erhalten"))

        for fresh_at, listing, basis in sorted(eligible, key=lambda item: item[0], reverse=True):
            if not listing.address:
                counts["keine prüfbare Adresse"] += 1
                retry_after[listing.id] = now + timedelta(minutes=30)
                continue
            match = self.router.qualifies(listing.address, now)
            if match is not True:
                counts["ÖPNV über 30 Minuten" if match is False else "ÖPNV nicht prüfbar"] += 1
                retry_after[listing.id] = now + (self.max_age if match is False else timedelta(minutes=10))
                continue
            message = personalise_message(
                self.template, listing.description, listing_url=listing.url, provider=self.provider,
            )
            with db:
                saved = db.execute(
                    "INSERT OR IGNORE INTO drafts(id, url, fresh_at, basis, message) VALUES (?, ?, ?, ?, ?)",
                    (listing.id, listing.url, fresh_at.timestamp(), basis, message),
                )
            counts["neue Entwürfe"] += saved.rowcount
        return counts


def _scanner(max_age: timedelta) -> Scanner:
    mode = os.getenv("MUNICH_SOURCE", "public").strip().lower()
    if mode not in {"public", "alerts"}:
        raise ValueError("MUNICH_SOURCE muss public oder alerts sein")
    alerts = None
    if mode == "alerts":
        alerts = AlertsInbox(
            _required("MUNICH_IMAP_HOST"), _required("MUNICH_IMAP_USER"), _required("MUNICH_IMAP_PASSWORD"),
            _required("MUNICH_IMAP_FOLDER"), _required("MUNICH_ALERT_SENDER"),
        )
    search_urls = tuple(url.strip() for url in os.getenv("MUNICH_SEARCH_URLS", "").split(",") if url.strip())
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

    limit = _number("MUNICH_MAX_CANDIDATES", 12, 1, 40)
    return Scanner(
        WGSource(search_urls or DEFAULT_SEARCH_URLS, limit), TransitRouter(_required("GOOGLE_MAPS_API_KEY")),
        template, provider, max_age, limit, alerts,
        _number("MUNICH_APPLICANT_AGE", 28, 18, 100), _number("MUNICH_MAX_AGE_GAP", 7, 0, 100),
    )


def main() -> int:
    load_dotenv(ROOT / "config" / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("watch", "scan", "drafts", "done"))
    parser.add_argument("id", nargs="?", help="Anzeigennummer für drafts/done")
    args = parser.parse_args()
    if (args.command == "done" and not args.id) or (args.id and args.command in {"watch", "scan"}):
        parser.error("done braucht eine ID; watch und scan nehmen keine ID an")
    try:
        max_age = timedelta(hours=_number("MUNICH_MAX_AGE_HOURS", 24, 1, 72))
        data_dir = Path(os.getenv("MUNICH_DATA_DIR") or ROOT / "data")
        with closing(_database(data_dir / "munich_radar.sqlite3")) as db:
            if args.command == "done":
                with db:
                    updated = db.execute("UPDATE drafts SET done = 1, message = NULL WHERE id = ?", (args.id,))
                print("Erledigt." if updated.rowcount else "Anzeige nicht gefunden.")
                return 0 if updated.rowcount else 1
            if args.command == "drafts":
                cutoff = datetime.now(timezone.utc) - max_age
                query = "SELECT id, url, fresh_at, basis, message FROM drafts WHERE done = 0 AND message IS NOT NULL AND fresh_at >= ?"
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

            scanner = _scanner(max_age)
            interval = _number("MUNICH_POLL_SECONDS", 300, 60, 3600)
            delay = interval
            retry_after: dict[str, datetime] = {}
            while True:
                try:
                    counts = scanner.scan(db, datetime.now(timezone.utc), retry_after)
                    LOG.info("Scan fertig: %s", ", ".join(f"{n} {key}" for key, n in counts.items()) or "nichts Neues")
                    delay = interval
                    if args.command == "scan":
                        return 0
                except (WGSourceError, AlertsError) as exc:
                    LOG.warning("Scan pausiert: %s", exc)
                    if args.command == "scan":
                        return 1
                    delay = min(max(600, delay * 2), 3600)
                time.sleep(delay)
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("Start fehlgeschlagen: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
