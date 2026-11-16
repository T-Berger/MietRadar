from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from dashboard_data import filter_listing_providers, load_dashboard, map_listings, weekend_dates
from listing_profiles import record_listing_location, record_listing_profile, record_listing_route
from munich_radar import _database
from rental_availability import AVAILABILITY_POLICY_VERSION, record_listing_availability
from wg_source import Listing


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
                       ("12", "application", sha256(b"Hallo").hexdigest(), 0, "Termin klären", recent))
            db.execute("INSERT INTO klein_review VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("ka-44", "https://www.kleinanzeigen.de/s-anzeige/wohnung/44-203-650",
                        "Zimmer", "", 1, recent, "Suchalarm", "Adresse fehlt"))
            for listing_id in ("12", "13", "14", "ka-44"):
                record_listing_availability(db, listing_id, None)
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
        self.assertEqual(data.matches[0].judge_reason, "Termin klären")
        self.assertIs(data.matches[0].judge_approved, False)
        self.assertEqual(data.address_checks[0].title, "Zimmer")
        self.assertEqual(data.replies[0].url, "https://www.kleinanzeigen.de/m-nachrichten.html#test")
        self.assertIsNone(data.activity[0].url)
        self.assertEqual(self.path.read_bytes(), original)

    def test_shows_exact_queued_text_verdict_separately_from_browser_delay(self):
        now = NOW.timestamp()
        message = "Hallo"
        digest = sha256(message.encode()).hexdigest()
        previous_digest = sha256(b"Previous message").hexdigest()
        with closing(_database(self.path)) as db, db:
            for listing_id, check_digest in (("12", digest), ("13", previous_digest)):
                record_listing_availability(db, listing_id, None)
                db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                           (listing_id, f"https://www.wg-gesucht.de/{listing_id}.html",
                            now, "Veröffentlicht", message, 0))
                db.execute("INSERT INTO application_reviews VALUES (?, ?, 'queued', ?, ?, NULL)",
                           (listing_id, digest, "Versandbrowser nicht verfügbar.", now))
                db.execute("INSERT INTO outgoing_checks VALUES (?, 'application', ?, 0, ?, ?)",
                           (listing_id, check_digest, "Ein persönlicher Bezug fehlt.", now))

        matches = {row.id: row for row in load_dashboard(self.path, now=NOW).matches}
        for row in matches.values():
            self.assertEqual(row.state, "queued")
            self.assertEqual(row.review_reason, "Versandbrowser nicht verfügbar.")
            self.assertEqual(row.message, message)
        self.assertIs(matches["12"].judge_approved, False)
        self.assertEqual(matches["12"].judge_reason, "Ein persönlicher Bezug fehlt.")
        self.assertIsNone(matches["13"].judge_approved)
        self.assertIsNone(matches["13"].judge_reason)

    def test_legacy_check_schema_does_not_claim_a_judgment_for_exact_text(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("CREATE TABLE outgoing_checks (id TEXT, kind TEXT, approved INTEGER, reason TEXT)")
            db.execute("INSERT INTO drafts VALUES ('12', ?, ?, 'Veröffentlicht', 'Hallo', 0)",
                       ("https://www.wg-gesucht.de/12.html", NOW.timestamp()))
            db.execute("INSERT INTO outgoing_checks VALUES ('12', 'application', 0, 'Termin klären')")

        row, = load_dashboard(self.path, now=NOW).matches
        self.assertEqual(row.state, "held")
        self.assertEqual(row.review_reason, "Termin klären")
        self.assertIsNone(row.judge_approved)
        self.assertIsNone(row.judge_reason)

    def test_older_schema_without_delivery_keeps_drafts_visible(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("CREATE TABLE delivery (id TEXT)")
            db.execute("CREATE TABLE listing_availability (id TEXT)")
            db.execute("INSERT INTO listing_availability VALUES ('12')")
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
        self.assertIsNone(data.listing_profiles["12"].route_tier)
        self.assertIsNone(data.listing_profiles["12"].route_minutes)
        self.assertIsNone(data.listing_profiles["12"].latitude)
        self.assertIsNone(data.listing_profiles["12"].longitude)
        self.assertFalse(data.listing_profiles["12"].is_commercial)
        self.assertFalse(filter_listing_providers(data, "commercial").matches)
        self.assertIsNone(data.wg_scan)

    def test_previous_availability_schema_filters_only_recorded_exclusions(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("CREATE TABLE listing_availability (id TEXT PRIMARY KEY, reason TEXT)")
            db.execute("INSERT INTO listing_availability VALUES ('12', 'Einzug erst nächstes Jahr')")
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'Veröffentlicht', 'Hallo', 0)", [
                (listing_id, f"https://www.wg-gesucht.de/{listing_id}.html", NOW.timestamp())
                for listing_id in ("12", "13")
            ])
        self.assertEqual([row.id for row in load_dashboard(self.path, now=NOW).matches], ["13"])

    def test_requires_current_availability_in_active_cards_without_losing_delivery_status(self):
        now = NOW.timestamp()
        with closing(_database(self.path)) as db, db:
            for listing_id, reason in [
                ("20", "Einzug erst nächstes Jahr"),
                ("21", None),
                ("24", "Zwischenmiete unter vier Monaten"),
                ("25", "Zwischenmiete unter vier Monaten"),
                ("ka-28", "Einzug erst nächstes Jahr"),
                ("ka-29", None),
                ("31", "Zwischenmiete unter vier Monaten"),
                ("32", None),
            ]:
                record_listing_availability(db, listing_id, reason)
            db.executemany("INSERT INTO listing_availability (id, reason, policy_version) VALUES (?, NULL, ?)", [
                (listing_id, AVAILABILITY_POLICY_VERSION - 1) for listing_id in ("26", "ka-30", "33")
            ])
            db.executemany("INSERT INTO drafts VALUES (?, 'https://www.wg-gesucht.de/test.html', ?, ?, ?, ?)", [
                ("20", now, "veröffentlicht", "Entwurf bleibt gespeichert", 0),
                ("21", now, "veröffentlicht", "Geprüft", 0),
                ("22", now, "veröffentlicht", "Noch nicht geprüft", 0),
                ("23", now, "veröffentlicht", None, 1),
                ("24", now, "veröffentlicht", "Status unklar", 0),
                ("25", now, "veröffentlicht", "Kontakt nicht verfügbar", 0),
                ("26", now, "veröffentlicht", "Vorher als verfügbar geprüft", 0),
            ])
            db.executemany("INSERT INTO delivery VALUES (?, ?, ?, ?)", [
                ("23", "sent", now, now),
                ("24", "unconfirmed", now, now),
                ("25", "unavailable", now, None),
            ])
            db.executemany("""INSERT INTO klein_review (id, url, title, excerpt, is_wg, fresh_at, basis, reason)
                              VALUES (?, 'https://www.kleinanzeigen.de/test', 'WG-Zimmer', '', 1, ?, 'veröffentlicht', 'Adresse fehlt')""",
                           [(f"ka-{listing_id}", now) for listing_id in (28, 29, 30, 34)])
            db.executemany("""INSERT INTO munich_secondary_offers
                VALUES (?, 'https://www.wg-gesucht.de/test.html', 'WG-Zimmer', ?, 'veröffentlicht', 'b', 'Ostbahnhof', 45)""",
                           [(str(listing_id), now) for listing_id in (31, 32, 33, 34)])

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(data.status, "ready")
        self.assertEqual([(row.id, row.state) for row in data.matches],
                         [("24", "unconfirmed"), ("23", "sent"), ("21", "draft")])
        self.assertEqual([row.id for row in data.address_checks], ["ka-29"])
        self.assertEqual([row.id for row in data.secondary_offers], ["32"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT message FROM drafts WHERE id = '20'").fetchone(),
                             ("Entwurf bleibt gespeichert",))
            self.assertEqual(db.execute("SELECT message FROM drafts WHERE id = '26'").fetchone(),
                             ("Vorher als verfügbar geprüft",))

    def test_hides_availability_exclusions_in_all_dated_listing_sources(self):
        now = NOW.timestamp()
        day = NOW.date()
        with closing(_database(self.path)) as db, db:
            for listing_id, reason in [
                ("10", "Einzug erst nächstes Jahr"),
                ("11", None),
                ("12", "Einzug erst nächstes Jahr"),
                ("13", None),
                ("14", "Zwischenmiete unter vier Monaten"),
                ("15", None),
                ("ka-16", "Einzug erst nächstes Jahr"),
                ("ka-17", None),
            ]:
                record_listing_availability(db, listing_id, reason)
            db.executemany("INSERT INTO listing_availability (id, reason, policy_version) VALUES (?, NULL, ?)", [
                ("18", AVAILABILITY_POLICY_VERSION - 1),
                ("ka-20", AVAILABILITY_POLICY_VERSION - 1),
            ])
            db.executemany("""INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at, published_at, published_day)
                              VALUES (?, 'https://www.wg-gesucht.de/test.html', ?, ?, ?, ?)""", [
                ("10", now, now, now, None), ("11", now, now, now, None),
                ("12", now, now, None, day.isoformat()), ("13", now, now, None, day.isoformat()),
                ("14", now, now, None, None), ("15", now, now, None, None),
                ("18", now, now, now, None), ("19", now, now, None, day.isoformat()),
            ])
            db.executemany("""INSERT INTO drafts
                              VALUES (?, 'https://www.kleinanzeigen.de/test', ?, 'veröffentlicht', 'Hallo', 0)""",
                           [(f"ka-{listing_id}", now) for listing_id in (16, 17, 20, 21)])

        posted = load_dashboard(self.path, now=NOW, posting_window=(day, day))
        self.assertEqual({row.id for row in posted.dated_listings}, {"11", "13", "ka-17"})
        first_seen = load_dashboard(self.path, now=NOW, posting_window=(day, day), date_basis="first_seen")
        self.assertEqual([row.id for row in first_seen.dated_listings], ["15"])

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
            for listing_id in ("12", "ka-77"):
                record_listing_availability(db, listing_id, None)
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
            record_listing_route(db, "12", "a*", 20)

        data = load_dashboard(self.path, now=NOW)
        self.assertEqual(set(data.listing_profiles), {"12", "ka-77"})
        profile = data.listing_profiles["12"]
        self.assertEqual((profile.title, profile.rent_eur, profile.rent_kind, profile.area_sqm),
                         ("Zimmer am Park", 780.5, "warm", 18.5))
        self.assertEqual((profile.summary, profile.fit, profile.fit_reason),
                         ("Das Zimmer ist möbliert.", "strong", "Nah am Ostbahnhof."))
        self.assertEqual(profile.photo_urls, (photo_url,))
        self.assertEqual((profile.route_tier, profile.route_minutes), ("a*", 20))
        self.assertIsNone(data.listing_profiles["ka-77"].rent_eur)
        self.assertEqual(data.listing_profiles["ka-77"].photo_urls, ())

    def test_map_uses_verified_current_tiers_and_follows_the_selected_date(self):
        today = NOW.timestamp()
        friday = (NOW - timedelta(days=2)).timestamp()
        url = "https://www.wg-gesucht.de/12.html"
        klein_url = "https://www.kleinanzeigen.de/s-anzeige/zimmer/77-199-650"
        with closing(_database(self.path)) as db, db:
            for listing_id in ("1", "2", "3", "5", "ka-77"):
                record_listing_availability(db, listing_id, None)
            record_listing_availability(db, "4", "keine Mindestmietdauer")
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'veröffentlicht', 'Hallo', 0)", [
                ("1", url, today), ("2", url, today), ("4", url, today),
            ])
            db.execute("INSERT INTO delivery VALUES ('4', 'sent', ?, ?)", (today, today))
            db.executemany("INSERT INTO munich_secondary_offers VALUES (?, ?, ?, ?, 'veröffentlicht', ?, ?, ?)", [
                ("3", url, "Zimmer", friday, "c", "Hbf", 39),
                ("ka-77", klein_url, "Zimmer", today, "b", "Ostbahnhof", 41),
            ])
            db.executemany("""
                INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at, published_at)
                VALUES (?, ?, ?, ?, ?)
            """, [(listing_id, url, friday, friday, friday) for listing_id in ("3", "5")])
            db.executemany("""
                INSERT INTO listing_profiles (id, title, route_tier, route_minutes, latitude, longitude)
                VALUES (?, 'Zimmer', ?, ?, ?, ?)
            """, [
                ("1", "a*", 19, 48.12, 11.58), ("2", "a", None, 12.54, 11.55),
                ("3", None, None, 48.15, 11.53), ("4", "a", None, 48.15, 11.55),
                ("5", "a*", 12, 48.123, 11.54), ("ka-77", None, None, 48.14, 11.65),
            ])

        data = load_dashboard(self.path, now=NOW, posting_window=(date(2026, 11, 13), date(2026, 11, 13)))
        self.assertIn("4", {row.id for row in data.matches})
        current = {item.id: item for item in map_listings(data)}
        self.assertEqual(set(current), {"1", "2", "ka-77"})
        self.assertEqual((current["1"].tier, current["1"].minutes, current["1"].latitude), ("a*", 19, 48.12))
        self.assertEqual((current["2"].latitude, current["2"].longitude), (None, None))
        self.assertEqual((current["ka-77"].tier, current["ka-77"].station, current["ka-77"].platform),
                         ("b", "Ostbahnhof", "Kleinanzeigen"))
        historical, = map_listings(data, date_filtered=True)
        self.assertEqual((historical.id, historical.tier, historical.station, historical.longitude),
                         ("3", "c", "Hauptbahnhof", 11.53))

    def test_commercial_filter_covers_current_dated_and_map_views(self):
        stamp = NOW.timestamp()
        with closing(_database(self.path)) as db, db:
            for listing_id in ("1", "2", "3"):
                url = f"https://www.wg-gesucht.de/{listing_id}.html"
                record_listing_availability(db, listing_id, None)
                if listing_id == "3":
                    db.execute("INSERT INTO munich_secondary_offers VALUES (?, ?, 'Zimmer', ?, ?, 'b', 'Ostbahnhof', 43)",
                               (listing_id, url, stamp, "veröffentlicht"))
                    db.execute("""INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at, published_at)
                                  VALUES (?, ?, ?, ?, ?)""", (listing_id, url, stamp, stamp, stamp))
                else:
                    db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'veröffentlicht', 'Hallo', 0)",
                               (listing_id, url, stamp))
                listing = Listing(listing_id, url, "Zimmer", "", NOW, "Beispielstraße 1, 80331 München")
                record_listing_profile(db, replace(listing, is_commercial=listing_id != "2"),
                                       route_tier="a" if listing_id != "3" else "b",
                                       coordinates=(48.12, 11.55))
                # A later alert without provider evidence must not erase a previously confirmed marker.
                record_listing_profile(db, listing)

        data = load_dashboard(self.path, now=NOW, posting_window=(NOW.date(), NOW.date()))
        commercial = filter_listing_providers(data, "commercial")
        self.assertEqual({row.id for row in commercial.matches}, {"1"})
        self.assertEqual({row.id for row in commercial.secondary_offers}, {"3"})
        self.assertEqual({row.id for row in commercial.dated_listings}, {"1", "3"})
        self.assertEqual({(row.id, row.is_commercial) for row in map_listings(commercial)}, {("1", True), ("3", True)})
        self.assertEqual({row.id for row in map_listings(commercial, date_filtered=True)}, {"1", "3"})
        other = filter_listing_providers(data, "other")
        self.assertEqual({(row.id, row.is_commercial) for row in map_listings(other)}, {("2", False)})

    def test_address_change_invalidates_a_saved_map_point(self):
        with closing(_database(self.path)) as db:
            listing = Listing("1", "https://www.wg-gesucht.de/1.html", "Zimmer", "", NOW,
                              "Beispielstraße 1, 80331 München")
            new_listing = Listing("1", listing.url, listing.title, "", NOW, "Beispielstraße 2, 80331 München")
            record_listing_profile(db, listing, route_tier="a*", route_minutes=15, coordinates=(48.12, 11.55))
            record_listing_location(db, "1", (48.12, float("inf")))
            self.assertEqual(db.execute("SELECT latitude, longitude FROM listing_profiles").fetchone(), (48.12, 11.55))
            record_listing_profile(db, new_listing)
            self.assertEqual(db.execute("SELECT latitude, longitude FROM listing_profiles").fetchone(), (None, None))

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
            for listing_id in ("old", "first-minute", "last-minute", "monday", "day-only", "sighting", "fallback"):
                record_listing_availability(db, listing_id, None)
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
