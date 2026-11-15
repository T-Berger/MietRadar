"""Poll Munich listings, draft an application, and optionally send it."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from kleinanzeigen_activity import EmailNotifier, install_activity, record_activity, record_replies
from kleinanzeigen_radar import KleinanzeigenRadar, install_klein_review
from llm_personalizer import personalise_message
from munich_delivery import DeliveryQueue, install_schema
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
    install_schema(db)
    install_klein_review(db)
    install_activity(db)
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
    klein_alerts: AlertsInbox | None = None
    klein_delivery: DeliveryQueue | None = None

    def scan(self, db: sqlite3.Connection, now: datetime, retry_after: dict[str, datetime]) -> Counter:
        cutoff = now - self.max_age
        with db:
            # Keep IDs for deduplication, but remove expired personal drafts.
            db.execute("UPDATE drafts SET message = NULL WHERE message IS NOT NULL AND fresh_at < ?",
                       (cutoff.timestamp(),))
        for listing_id, retry in list(retry_after.items()):
            if retry <= now:
                del retry_after[listing_id]

        counts: Counter = Counter()
        if self.klein_alerts:
            klein = KleinanzeigenRadar(self.router, self.template, self.provider, self.age, self.age_gap, self.klein_delivery)
            try:
                counts.update(klein.scan(db, self.klein_alerts, now, cutoff, self.limit, retry_after))
            except AlertsError as exc:
                LOG.warning("Kleinanzeigen-Suchalarm nicht erreichbar: %s", exc)

        if self.alerts:
            refs = [(alert.ref, alert.received_at) for alert in self.alerts.recent(cutoff, self.limit)]
        else:
            refs = [(ref, None) for ref in self.source.candidates()]

        eligible = []
        for ref, received_at in refs:
            if ref.id in retry_after:
                continue
            draft = db.execute(
                """SELECT d.fresh_at, d.message, d.done, s.state FROM drafts d
                   LEFT JOIN delivery s ON s.id = d.id WHERE d.id = ?""", (ref.id,),
            ).fetchone()
            if draft and (not self.delivery or self.delivery.paused or not draft[1] or draft[2] or draft[3]):
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
            if draft:
                # A new alert may not extend an existing listing's window.
                fresh_at = min(fresh_at, datetime.fromtimestamp(draft[0], timezone.utc))
            eligible.append((fresh_at, listing, "veröffentlicht" if listing.published_at else "Suchalarm erhalten", draft))

        for fresh_at, listing, basis, draft in sorted(eligible, key=lambda item: item[0], reverse=True):
            if not listing.address:
                counts["keine prüfbare Adresse"] += 1
                retry_after[listing.id] = now + timedelta(minutes=30)
                continue
            match = self.router.qualifies(listing.address, now)
            if match is not True:
                counts["ÖPNV über 30 Minuten" if match is False else "ÖPNV nicht prüfbar"] += 1
                retry_after[listing.id] = now + (self.max_age if match is False else timedelta(minutes=10))
                continue
            if draft:
                message = draft[1]
            else:
                message = personalise_message(
                    self.template, listing.description, listing_url=listing.url, provider=self.provider,
                )
                with db:
                    saved = db.execute(
                        "INSERT OR IGNORE INTO drafts(id, url, fresh_at, basis, message) VALUES (?, ?, ?, ?, ?)",
                        (listing.id, listing.url, fresh_at.timestamp(), basis, message),
                    )
                counts["neue Entwürfe"] += saved.rowcount
                if not saved.rowcount:
                    continue
            if self.delivery:
                result = self.delivery.deliver(db, listing.id, listing.url, message, fresh_at, self.max_age)
                if result:
                    counts[result] += 1
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
    return template, provider


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

    limit = _number("MUNICH_MAX_CANDIDATES", 12, 1, 40)
    return Scanner(
        WGSource(search_urls or DEFAULT_SEARCH_URLS, limit), TransitRouter(_required("GOOGLE_MAPS_API_KEY")),
        template, provider, max_age, limit, alerts,
        _number("MUNICH_APPLICANT_AGE", 28, 18, 100), _number("MUNICH_MAX_AGE_GAP", 7, 0, 100),
        klein_alerts=klein_alerts,
    )


def main() -> int:
    load_dotenv(ROOT / "config" / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("watch", "scan", "drafts", "delivery", "done", "klein-review", "klein-address", "klein-activity"))
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
                klein = KleinanzeigenRadar(TransitRouter(_required("GOOGLE_MAPS_API_KEY")), template, provider)
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
            with ExitStack() as sessions:
                if _switch("MUNICH_AUTOSEND", False):
                    try:
                        from wg_delivery import BrowserSender
                    except ModuleNotFoundError as exc:
                        raise ValueError("Für den Versand: pip install -r requirements-send.txt") from exc
                    limit = _number("MUNICH_DAILY_SEND_LIMIT", 3, 1, 5)
                    sender = BrowserSender(
                        data_dir / "munich_browser",
                        headless=_switch("MUNICH_BROWSER_HEADLESS", True),
                        email=os.getenv("WG_EMAIL", "").strip(), password=os.getenv("WG_PASSWORD", ""),
                    )
                    sessions.callback(sender.close)
                    scanner.delivery = DeliveryQueue(sender, limit)
                if _switch("KLEINANZEIGEN_AUTOSEND", False):
                    if scanner.klein_alerts is None:
                        raise ValueError("Für Kleinanzeigen-Versand zuerst Kleinanzeigen-Suchalarme einrichten")
                    try:
                        from kleinanzeigen_delivery import KleinanzeigenBrowserSender
                    except ModuleNotFoundError as exc:
                        raise ValueError("Für den Versand: pip install -r requirements-send.txt") from exc
                    klein_sender = KleinanzeigenBrowserSender(
                        data_dir / "kleinanzeigen_browser", headless=_switch("KLEINANZEIGEN_BROWSER_HEADLESS", True),
                    )
                    sessions.callback(klein_sender.close)
                    scanner.klein_delivery = DeliveryQueue(klein_sender, _number("MUNICH_DAILY_SEND_LIMIT", 3, 1, 5))
                interval = _number("MUNICH_POLL_SECONDS", 300, 60, 3600)
                return _poll(scanner, db, interval, once=args.command == "scan",
                             reply_inbox=reply_inbox, judge_provider=judge_provider, notifier=notifier)
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
) -> int:
    delay = interval
    retry_after: dict[str, datetime] = {}
    while True:
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
        klein_paused = scanner.klein_delivery is not None and scanner.klein_delivery.paused
        record_activity(db, now, klein_paused)
        if reply_inbox:
            try:
                replied = record_replies(db, reply_inbox, judge_provider, now)
                if replied:
                    counts["Kleinanzeigen-Antworten bewertet"] += replied
            except AlertsError as exc:
                LOG.warning("Kleinanzeigen-Antworten nicht erreichbar: %s", exc)
                failed = True
        if notifier:
            sent, ok = notifier.flush(db, now)
            if sent:
                counts["Hinweise per E-Mail angenommen"] += sent
            if not ok:
                LOG.warning("E-Mail-Hinweise vorerst nicht angenommen. Einstellungen prüfen.")
                failed = True
        LOG.info("Scan fertig: %s", ", ".join(f"{n} {key}" for key, n in counts.items()) or "nichts Neues")
        if once:
            return int(failed or klein_paused or (scanner.delivery is not None and scanner.delivery.paused))
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
