"""Verify the configured IMAP or Resend integration from the deployed service."""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from kleinanzeigen_activity import Activity, EmailNotifier, install_activity
from kleinanzeigen_replies import extract_replies
from kleinanzeigen_source import extract_listings
from wg_alerts import AlertsError, AlertsInbox


_MAILBOX = ("MUNICH_IMAP_HOST", "MUNICH_IMAP_USER", "MUNICH_IMAP_PASSWORD")
_ALERTS = (*_MAILBOX, "KLEINANZEIGEN_IMAP_FOLDER", "KLEINANZEIGEN_ALERT_SENDER")
_REPLIES = (*_MAILBOX, "KLEINANZEIGEN_REPLIES_IMAP_FOLDER", "KLEINANZEIGEN_REPLY_SENDER")
_EMAIL = ("RESEND_API_KEY", "MUNICH_NOTIFY_EMAIL_FROM", "MUNICH_NOTIFY_EMAIL_TO")


def _check_inbox(values: list[str], now: datetime, *, replies: bool) -> int:
    inbox = AlertsInbox(*values)
    if replies:
        cutoff, extract = now - timedelta(days=7), extract_replies
    else:
        cutoff, extract = now - timedelta(hours=24), extract_listings
    try:
        items = inbox.recent_items(cutoff, 20, extract)
    except AlertsError:
        print("IMAP check failed. Verify the server, app password, and IMAP folder.")
        return 1

    count = sum(received_at <= now for _, received_at in items)
    item_type = "reply" if replies else "rental listing"
    if not count:
        print(f"IMAP connected and searched the folder. No recent {item_type} could be parsed.")
        return 1
    print(f"IMAP verified. Parsed {count} recent {item_type}{'' if count == 1 else 's'}.")
    return 0


def _check_email(values: list[str], now: datetime) -> int:
    notifier = EmailNotifier(*values)
    with closing(sqlite3.connect(":memory:")) as db:
        install_activity(db)
        Activity(
            f"smoke:{uuid4().hex}", "test", None, "", "E-Mail-Test",
            "Die E-Mail-Benachrichtigungen von MietRadar funktionieren.", now.timestamp(),
        ).save(db)
        sent, accepted = notifier.flush(db, now)
    if accepted and sent == 1:
        print("Resend accepted the test notification. Check the configured inbox to verify delivery.")
        return 0
    print("Resend did not accept the test notification. Verify the API key, sender, and recipient.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "check", choices=("kleinanzeigen", "kleinanzeigen-replies", "email"),
        help="The email check sends one test notification to the configured recipient.",
    )
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env")

    variables = {"kleinanzeigen": _ALERTS, "kleinanzeigen-replies": _REPLIES, "email": _EMAIL}[args.check]
    values = [os.getenv(variable, "").strip() for variable in variables]
    missing = [variable for variable, value in zip(variables, values) if not value]
    if missing:
        print("Missing configuration: " + ", ".join(missing))
        return 2
    now = datetime.now(timezone.utc)
    if args.check == "email":
        return _check_email(values, now)
    return _check_inbox(values, now, replies=args.check == "kleinanzeigen-replies")


if __name__ == "__main__":
    raise SystemExit(main())
