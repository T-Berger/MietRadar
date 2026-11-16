from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dashboard_data import load_dashboard, weekend_dates
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
            db.execute("""CREATE TABLE listing_profiles (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, rent_eur REAL, rent_kind TEXT,
                area_sqm REAL, summary TEXT, fit TEXT, fit_reason TEXT
            )""")
            db.execute("INSERT INTO listing_profiles (id, title) VALUES ('12', 'Zimmer am Park')")

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.status, "ready")
        self.assertEqual(data.matches[0].state, "draft")
        self.assertEqual(data.matches[0].url, "https://www.wg-gesucht.de/12.html")
        self.assertEqual(data.address_checks, ())
        self.assertEqual(data.secondary_offers, ())
        self.assertEqual(data.listing_profiles["12"].title, "Zimmer am Park")
        self.assertEqual(data.listing_profiles["12"].photo_urls, ())
        self.assertIsNone(data.wg_scan)

    def test_wg_reply_drafts_merge_with_kleinanzeigen_and_keep_only_safe_conversation_links(self):
        now = NOW.timestamp()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE klein_reply_drafts (id TEXT, listing_id TEXT, url TEXT, message TEXT, "
                       "received_at REAL, state TEXT)")
            db.execute("CREATE TABLE wg_reply_drafts (id TEXT, message_id TEXT, url TEXT, received_at REAL, label TEXT, "
                       "summary TEXT, message TEXT, state TEXT, review_reason TEXT)")
            db.execute("CREATE TABLE wg_reply_inbox_status (id INTEGER, checked_at REAL, available INTEGER)")
            db.execute("INSERT INTO wg_reply_inbox_status VALUES (1, ?, 0)", (now,))
            db.execute("INSERT INTO klein_reply_drafts VALUES (?, ?, ?, ?, ?, ?)",
                       ("ka:example", "ka-55", "https://www.kleinanzeigen.de/m-nachrichten.html#example",
                        "Danke!", now - 1, "draft"))
            db.executemany("INSERT INTO wg_reply_drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                ("123", "789", "https://www.wg-gesucht.de/nachricht.html?nachrichten-id=123&list=1#some-id",
                 now, "Besichtigung", "Ein Termin wurde angeboten.", "Danke für die Einladung!", "draft", None),
                ("456", "987", "https://www.wg-gesucht.de/nachricht.html?nachrichten-id=456&nachrichten-id=987",
                 now - 3, "Vorsicht", "Die Nachricht bittet um Vorauszahlung.", None, "held", "Verlauf prüfen"),
                ("789", "654", "https://www.wg-gesucht.de/nachricht.html?nachrichten-id=789",
                 now - 31 * 86400, "Absage", "Das Zimmer ist vergeben.", "Danke!", "draft", None),
            ])

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual([row.platform for row in data.replies], ["WG-Gesucht", "Kleinanzeigen", "WG-Gesucht"])
        self.assertEqual(data.replies[0].url, "https://www.wg-gesucht.de/nachricht.html?nachrichten-id=123")
        self.assertEqual((data.replies[0].label, data.replies[0].summary, data.replies[0].message),
                         ("Besichtigung", "Ein Termin wurde angeboten.", "Danke für die Einladung!"))
        self.assertEqual(data.replies[2].url, "https://www.wg-gesucht.de/nachrichten.html")
        self.assertEqual(data.replies[2].review_reason, "Verlauf prüfen")
        self.assertEqual(data.wg_inbox_status, (now, False))

    def test_current_secondary_offers_are_separate_from_primary_matches(self):
        recent = (NOW - timedelta(hours=1)).timestamp()
        older = (NOW - timedelta(hours=23)).timestamp()
        expired = (NOW - timedelta(hours=25)).timestamp()
        future = (NOW + timedelta(hours=1)).timestamp()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)", [
                ("12", "https://www.wg-gesucht.de/12.html", recent, "veröffentlicht", "Older primary draft", 0),
                ("13", "https://www.wg-gesucht.de/13.html", recent, "veröffentlicht", "Current primary draft", 0),
            ])
            db.execute("""CREATE TABLE munich_secondary_offers (
                id TEXT PRIMARY KEY, url TEXT, title TEXT, fresh_at REAL, basis TEXT,
                tier TEXT, station TEXT, minutes INTEGER
            )""")
            db.executemany("INSERT INTO munich_secondary_offers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                ("12", "https://www.wg-gesucht.de/12.html?tracking=1", "WG-Zimmer", recent,
                 "zuerst gesehen", "b", "Ostbahnhof", 45),
                ("ka-77", "https://www.kleinanzeigen.de/s-anzeige/wohnung/77-203-650", "Wohnung", older,
                 "veröffentlicht", "c", "Hbf", 28),
                ("13", "https://www.wg-gesucht.de/13.html", "WG-Zimmer", expired,
                 "veröffentlicht", "b", "Ostbahnhof", 36),
                ("14", "https://www.wg-gesucht.de/14.html", "WG-Zimmer", future,
                 "veröffentlicht", "b", "Ostbahnhof", 36),
                ("15", "https://www.wg-gesucht.de/15.html", "WG-Zimmer", recent,
                 "veröffentlicht", "b", "Ostbahnhof", 46),
            ])

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.status, "ready")
        self.assertEqual([row.id for row in data.matches], ["13"])
        offers = [(row.id, row.platform, row.tier, row.station, row.minutes) for row in data.secondary_offers]
        self.assertEqual(offers, [("12", "WG-Gesucht", "b", "Ostbahnhof", 45),
                                  ("ka-77", "Kleinanzeigen", "c", "Hbf", 28)])
        self.assertEqual(data.secondary_offers[0].url, "https://www.wg-gesucht.de/12.html")

    def test_loads_listing_profiles_for_visible_cards_only(self):
        photo_url = "https://img.wg-gesucht.de/media/up/2026/43/" + "a" * 64 + "_179317855502694671775502672029.sized.JPEG"
        unrelated_photo = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/16/00000000-0000-4000-8000-000000000000?rule=%24_59.AUTO"
        with closing(_database(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS listing_profiles (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, rent_eur REAL, rent_kind TEXT,
                area_sqm REAL, summary TEXT, fit TEXT, fit_reason TEXT
            )""")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                       ("12", "https://www.wg-gesucht.de/12.html", NOW.timestamp(), "veröffentlicht", "Hallo", 0))
            db.execute("INSERT INTO munich_secondary_offers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("ka-77", "https://www.kleinanzeigen.de/s-anzeige/wohnung/77-203-650", "Wohnung",
                        NOW.timestamp(), "zuerst gesehen", "b", "Ostbahnhof", 44))
            db.executemany("""INSERT INTO listing_profiles
                (id, title, rent_eur, rent_kind, area_sqm, summary, fit, fit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", [
                ("12", "Zimmer am Park", 780.5, "warm", 18.5, "Das Zimmer ist möbliert.", "strong", "Nah am Ostbahnhof."),
                ("ka-77", "Zwei Zimmer", None, None, 44, None, "unclear", "Miete offen."),
                ("999", "Unbekannte Anzeige", None, None, None, None, None, None),
            ])
            photos = json.dumps([photo_url, unrelated_photo, "javascript:alert(1)", photo_url])
            db.execute("UPDATE listing_profiles SET photo_urls = ? WHERE id = '12'", (photos,))
            db.execute("UPDATE listing_profiles SET photo_urls = '[invalid' WHERE id = 'ka-77'")

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(set(data.listing_profiles), {"12", "ka-77"})
        profile = data.listing_profiles["12"]
        self.assertEqual((profile.title, profile.rent_eur, profile.rent_kind, profile.area_sqm),
                         ("Zimmer am Park", 780.5, "warm", 18.5))
        self.assertEqual((profile.summary, profile.fit, profile.fit_reason),
                         ("Das Zimmer ist möbliert.", "strong", "Nah am Ostbahnhof."))
        self.assertEqual(profile.photo_urls, (photo_url,))
        self.assertIsNone(data.listing_profiles["ka-77"].rent_eur)
        self.assertEqual(data.listing_profiles["ka-77"].photo_urls, ())

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

    def test_weekend_postings_and_first_sightings_use_separate_berlin_dates(self):
        monday = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        saturday, sunday = weekend_dates(monday.date())
        self.assertEqual((saturday, sunday), (date(2026, 11, 14), date(2026, 11, 15)))
        self.assertEqual(weekend_dates(saturday), (saturday, sunday))
        saturday_start = datetime(2026, 11, 13, 23, tzinfo=timezone.utc).timestamp()
        monday_start = datetime(2026, 11, 15, 23, tzinfo=timezone.utc).timestamp()

        with closing(_database(self.path)) as db, db:
            if "published_day" not in {row[1] for row in db.execute("PRAGMA table_info(wg_observations)")}:
                db.execute("ALTER TABLE wg_observations ADD COLUMN published_day TEXT")
            db.executemany("""INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at, published_at, published_day)
                              VALUES (?, 'https://www.wg-gesucht.de/test.html', ?, ?, ?, ?)""", [
                ("old", saturday_start, monday_start, saturday_start - 1, None),
                ("first-minute", saturday_start, monday_start, saturday_start, None),
                ("last-minute", monday_start - 1, monday_start, monday_start - 1, None),
                ("monday", monday_start, monday_start, monday_start, None),
                ("day-only", saturday_start, monday_start, None, sunday.isoformat()),
                ("sighting", saturday_start, monday_start, None, None),
                ("fallback", saturday_start, monday_start, None, None),
            ])
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)", [
                ("first-minute", "https://www.wg-gesucht.de/12.html", saturday_start, "veröffentlicht", "Stale draft", 0),
                ("sighting", "https://www.wg-gesucht.de/13.html", saturday_start, "zuerst gesehen", "Stale draft", 0),
                ("fallback", "https://www.wg-gesucht.de/14.html", saturday_start, "veröffentlicht", "Stale draft", 0),
                ("day-only", "https://www.wg-gesucht.de/15.html", monday_start - 1, "zuerst gesehen", "Draft", 0),
            ])

        postings = load_dashboard(self.path, now=monday, posting_window=(saturday, sunday))
        self.assertEqual([(row.id, row.day, row.matched) for row in postings.dated_listings], [
            ("last-minute", sunday, False), ("day-only", sunday, True),
            ("first-minute", saturday, True), ("fallback", saturday, True),
        ])
        self.assertEqual([row.id for row in postings.matches], ["day-only"])
        self.assertEqual(postings.matches[0].published_day, sunday)

        sightings = load_dashboard(self.path, now=monday, posting_window=(saturday, sunday), date_basis="first_seen")
        self.assertEqual([(row.id, row.day) for row in sightings.dated_listings], [("sighting", saturday)])

    def test_published_drafts_remain_searchable_with_an_old_schema(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                       ("12", "https://www.wg-gesucht.de/12.html", NOW.timestamp(), "Veröffentlicht", None, 1))

        data = load_dashboard(self.path, now=NOW, posting_window=weekend_dates(NOW.date()))
        self.assertEqual([(row.id, row.day) for row in data.dated_listings], [("12", NOW.date())])
