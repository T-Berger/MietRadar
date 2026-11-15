from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard_data import load_dashboard
from munich_radar import _database


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "worker #1.sqlite3"

    def test_loader_does_not_create_database_and_reports_invalid_database(self):
        self.assertEqual(load_dashboard(self.path).status, "missing")
        self.assertFalse(self.path.exists())
        self.path.write_text("Invalid SQLite database")
        self.assertEqual(load_dashboard(self.path).status, "unavailable")

    def test_loads_current_matches_and_status_from_worker_schema(self):
        recent = (NOW - timedelta(hours=1)).timestamp()
        older = (NOW - timedelta(hours=23)).timestamp()
        expired = (NOW - timedelta(hours=25)).timestamp()
        future = (NOW + timedelta(hours=1)).timestamp()
        with closing(_database(self.path)) as db, db:
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)", [
                ("12", "https://www.wg-gesucht.de/12.html", recent, "Veröffentlicht", "Hallo", 0),
                ("ka-55", "https://www.kleinanzeigen.de/s-anzeige/wohnung/55-203-650", older, "Suchalarm", None, 1),
                ("13", "https://www.wg-gesucht.de/13.html", expired, "Veröffentlicht", "Hallo", 0),
                ("14", "https://www.wg-gesucht.de/14.html", future, "Veröffentlicht", "Hallo", 0),
            ])
            db.execute("INSERT INTO delivery VALUES (?, ?, ?, ?)", ("ka-55", "sent", recent, recent))
            db.execute("INSERT INTO outgoing_checks VALUES (?, ?, ?, ?, ?, ?)",
                       ("12", "application", "checksum", 0, "Termin klären", recent))
            db.execute("INSERT INTO klein_review VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("ka-44", "https://www.kleinanzeigen.de/s-anzeige/wohnung/44-203-650",
                        "Zimmer", "", 1, recent, "Suchalarm", "Adresse fehlt"))
            db.execute("INSERT INTO klein_reply_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("reply-1", "ka-55", "https://www.kleinanzeigen.de/m-nachrichten.html#test",
                        "Danke", "", expired, 0, "draft", None))
            db.execute("INSERT INTO klein_activity VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("reply:1", "reply", "ka-55", "javascript:alert(1)", "Antwort", "Besichtigung", recent, None))

        original = self.path.read_bytes()
        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.status, "ready")
        self.assertEqual([(row.id, row.platform, row.state, row.message) for row in data.matches], [
            ("12", "WG-Gesucht", "held", "Hallo"), ("ka-55", "Kleinanzeigen", "sent", None),
        ])
        self.assertEqual(data.matches[0].review_reason, "Termin klären")
        self.assertEqual(data.address_checks[0].title, "Zimmer")
        self.assertEqual(data.replies[0].url, "https://www.kleinanzeigen.de/m-nachrichten.html#test")
        self.assertIsNone(data.activity[0].url)
        self.assertEqual(self.path.read_bytes(), original)

    def test_older_schema_without_delivery_keeps_drafts_visible(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("CREATE TABLE delivery (id TEXT)")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                       ("12", "https://www.wg-gesucht.de/12.html?sample=1", NOW.timestamp(), "Suchalarm", "Hallo", 0))

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.status, "ready")
        self.assertEqual(data.matches[0].state, "draft")
        self.assertEqual(data.matches[0].url, "https://www.wg-gesucht.de/12.html")
        self.assertEqual(data.address_checks, ())
        self.assertIsNone(data.wg_scan)

    def test_shows_latest_public_inventory_count_even_without_matches(self):
        current = NOW.timestamp()
        earlier = (NOW - timedelta(hours=1)).timestamp()
        with closing(_database(self.path)) as db, db:
            db.execute("INSERT INTO wg_inventory_scan (id, scope, scanned_at) VALUES (1, 'Munich', ?)", (current,))
            db.executemany("""INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at)
                              VALUES (?, 'https://www.wg-gesucht.de/test.html', ?, ?)""", [
                ("10", earlier, earlier), ("11", earlier, current), ("12", current, current),
            ])

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.matches, ())
        self.assertEqual(data.wg_scan, (current, 2))
