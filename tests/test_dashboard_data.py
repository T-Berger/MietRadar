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

from dashboard_data import (
    confirm_application_delivery, filter_listing_providers, load_dashboard, map_listings, set_listing_hidden, weekend_dates,
)
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

    def test_keeps_confirmed_applications_independent_of_offer_filters(self):
        now = NOW.timestamp()
        older = (NOW - timedelta(days=90)).timestamp()
        wg_url = "https://www.wg-gesucht.de/12.html"
        klein_url = "https://www.kleinanzeigen.de/s-anzeige/wohnung/55-203-650"
        with closing(_database(self.path)) as db, db:
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'Veröffentlicht', NULL, 1)", [
                ("12", f"{wg_url}?ref=search", older), ("ka-55", klein_url, now),
                ("13", "https://www.wg-gesucht.de/13.html", now),
                ("14", "https://www.wg-gesucht.de/14.html", now),
                ("15", "https://www.wg-gesucht.de/15.html", now),
            ])
            db.executemany("INSERT INTO delivery VALUES (?, ?, ?, ?)", [
                ("12", "sent", older, older), ("ka-55", "sent", now, now),
                ("13", "already_contacted", now, None), ("14", "unconfirmed", now, now),
                ("16", "sent", older - 100, older - 100),
            ])
            db.execute("INSERT INTO application_reviews VALUES ('15', 'hash', 'queued', NULL, ?, NULL)", (now,))
            db.execute("INSERT INTO listing_profiles(id, title) VALUES ('12', 'Zimmer am Park')")
            db.execute("INSERT INTO listing_availability(id, reason) VALUES ('12', 'Anzeige nicht mehr verfügbar')")
            db.execute("""INSERT INTO wg_observations(id, url, first_seen_at, last_seen_at)
                          VALUES ('16', 'https://www.wg-gesucht.de/16.html', ?, ?)""", (older, older))
            db.execute("""INSERT INTO klein_reply_drafts VALUES ('reply', 'ka-56', ?, 'Danke', '', ?, 1, 'sent', ?)""",
                       ("https://www.kleinanzeigen.de/m-nachrichten.html#conversation", now, now))
        set_listing_hidden(self.path, "12", True, title="Zimmer am Park", url=wg_url)

        data = load_dashboard(self.path, now=NOW, posting_window=(NOW.date(), NOW.date()))
        data = filter_listing_providers(data, "commercial")
        self.assertEqual([(row.id, row.platform, row.url, row.title, row.sent_at) for row in data.sent_applications], [
            ("ka-55", "Kleinanzeigen", klein_url, None, now),
            ("12", "WG-Gesucht", wg_url, "Zimmer am Park", older),
            ("16", "WG-Gesucht", "https://www.wg-gesucht.de/16.html", None, older - 100),
        ])

    def test_manually_confirms_only_a_matching_unclear_wg_delivery(self):
        attempted = (NOW - timedelta(hours=2)).timestamp()
        with closing(_database(self.path)) as db, db:
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'Veröffentlicht', 'Hallo', 0)", [
                ("12", "https://www.wg-gesucht.de/12.html", attempted),
                ("13", "https://www.wg-gesucht.de/14.html", attempted),
                ("14", "https://www.wg-gesucht.de/14.html", attempted),
                ("ka-55", "https://www.kleinanzeigen.de/s-anzeige/wohnung/55-203-650", attempted),
            ])
            db.executemany("INSERT INTO delivery VALUES (?, ?, ?, ?)", [
                ("12", "unconfirmed", attempted, attempted),
                ("13", "unconfirmed", attempted, attempted),
                ("ka-55", "unconfirmed", attempted, attempted),
            ])

        self.assertTrue(confirm_application_delivery(self.path, "12", now=NOW))
        for listing_id in ("12", "13", "14", "ka-55", "99"):
            self.assertFalse(confirm_application_delivery(self.path, listing_id, now=NOW + timedelta(minutes=1)))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state, recorded_at, attempted_at FROM delivery WHERE id = '12'").fetchone(),
                             ("sent", NOW.timestamp(), attempted))
            self.assertEqual(db.execute("SELECT done, message FROM drafts WHERE id = '12'").fetchone(), (1, "Hallo"))
        data = load_dashboard(self.path, now=NOW)
        self.assertEqual([(row.id, row.sent_at) for row in data.sent_applications], [("12", NOW.timestamp())])

    def test_shows_queue_history_independent_of_listing_filters(self):
        now = NOW.timestamp()
        older = (NOW - timedelta(days=3)).timestamp()
        text = "Hallo, ich würde mir das Zimmer gern ansehen."
        digest = sha256(text.encode()).hexdigest()
        url = "https://www.wg-gesucht.de/12.html"
        with closing(_database(self.path)) as db, db:
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'Veröffentlicht', ?, ?)", [
                ("12", url + "?ref=search", older, text, 0),
                ("13", "https://www.wg-gesucht.de/13.html", older, None, 0),
                ("14", "https://www.wg-gesucht.de/14.html", older, None, 1),
                ("15", "https://www.wg-gesucht.de/15.html", older, text, 0),
                ("16", "https://www.wg-gesucht.de/16.html", older, None, 1),
                ("17", "https://www.wg-gesucht.de/17.html", older, None, 1),
            ])
            db.executemany("INSERT INTO application_reviews VALUES (?, ?, ?, ?, ?, ?)", [
                ("12", digest, "queued", "Der Mac antwortet gerade nicht.", older, "pdf-digest"),
                ("13", digest, "failed", "Versandzeitfenster abgelaufen.", now - 4, None),
                ("14", digest, "queued", None, older, None),
                ("15", digest, "queued", "Überholter Grund", older, None),
                ("16", digest, "queued", None, older, None),
            ])
            db.executemany("INSERT INTO delivery VALUES (?, ?, ?, NULL)", [
                ("14", "sent", now), ("15", "unavailable", now - 2),
                ("16", "already_contacted", now - 3), ("17", "already_contacted", now - 1),
            ])
            db.execute("INSERT INTO outgoing_checks VALUES ('12', 'application', ?, 1, 'Text geprüft.', ?)",
                       (digest, now))
            db.execute("INSERT INTO listing_profiles (id, title) VALUES ('12', 'Zimmer am Park')")
        set_listing_hidden(self.path, "12", True, title="Zimmer am Park", url=url)

        original = self.path.read_bytes()
        data = filter_listing_providers(load_dashboard(self.path, now=NOW), "commercial")
        queue = data.application_queue
        self.assertEqual([(row.application.id, row.application.state, row.updated_at) for row in queue], [
            ("12", "queued", older), ("15", "unavailable", now - 2),
            ("16", "already_contacted", now - 3), ("13", "failed", now - 4),
        ])
        pending = queue[0].application
        self.assertEqual((queue[0].title, pending.url, pending.message, pending.attachment_digest),
                         ("Zimmer am Park", url, text, "pdf-digest"))
        self.assertEqual((pending.review_reason, pending.judge_approved, pending.judge_reason),
                         ("Der Mac antwortet gerade nicht.", True, "Text geprüft."))
        self.assertIsNone(queue[1].application.review_reason)
        self.assertEqual(queue[-1].application.review_reason, "Versandzeitfenster abgelaufen.")
        self.assertIsNone(queue[-1].application.message)
        self.assertEqual([row.id for row in data.sent_applications], ["14"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_queue_distinguishes_active_mac_claims_from_unconfirmed_attempts(self):
        now = NOW.timestamp()
        text = "Hallo, ich interessiere mich für das Zimmer."
        with closing(_database(self.path)) as db, db:
            db.execute("""CREATE TABLE wg_relay_preparations (
                id TEXT PRIMARY KEY, revision TEXT, expires_at REAL, claimed_at REAL, outcome TEXT
            )""")
            db.executemany("INSERT INTO drafts VALUES (?, ?, ?, 'Veröffentlicht', ?, 0)", [
                ("12", "https://www.wg-gesucht.de/12.html", now - 600, text),
                ("13", "https://www.wg-gesucht.de/99.html", now - 600, text),
            ])
            db.executemany("INSERT INTO application_reviews VALUES (?, ?, 'queued', NULL, ?, NULL)", [
                ("12", sha256(text.encode()).hexdigest(), now - 60), ("13", sha256(b"Previous").hexdigest(), now - 65),
            ])
            db.executemany("INSERT INTO delivery VALUES (?, 'unconfirmed', ?, ?)", [
                ("12", now - 15, now - 15), ("13", now - 20, now - 20), ("ka-55", now - 10, now - 10),
            ])
            db.executemany("INSERT INTO wg_relay_preparations VALUES (?, 'revision', ?, ?, ?)", [
                ("12", now + 10, now - 15, None), ("13", now + 10, now - 20, "UNCONFIRMED"),
            ])
            db.execute("""INSERT INTO wg_observations(id, url, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)""",
                       ("13", "https://www.wg-gesucht.de/13.html", now - 600, now - 600))

        queue = load_dashboard(self.path, now=NOW).application_queue
        self.assertEqual([(item.application.id, item.relay_claimed) for item in queue], [("12", True), ("13", False)])
        self.assertEqual(queue[0].application.message, text)
        self.assertEqual(queue[1].application.url, "https://www.wg-gesucht.de/13.html")
        self.assertIsNone(queue[1].application.message)
        self.assertFalse(load_dashboard(self.path, now=NOW + timedelta(seconds=11)).application_queue[0].relay_claimed)

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
        self.assertEqual(data.listing_profiles["12"].recipient_role, "unknown")
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
                role = "roommate" if listing_id == "3" else "manager"
                record_listing_profile(db, replace(listing, is_commercial=listing_id != "2", recipient_role=role),
                                       route_tier="a" if listing_id != "3" else "b",
                                       coordinates=(48.12, 11.55))
                # An alert without provider or recipient evidence preserves confirmed markers.
                record_listing_profile(db, listing)

        data = load_dashboard(self.path, now=NOW, posting_window=(NOW.date(), NOW.date()))
        commercial = filter_listing_providers(data, "commercial")
        self.assertEqual({row.id for row in commercial.matches}, {"1"})
        self.assertEqual({row.id for row in commercial.secondary_offers}, {"3"})
        self.assertEqual({row.id for row in commercial.dated_listings}, {"1", "3"})
        self.assertEqual({(row.id, row.is_commercial) for row in map_listings(commercial)}, {("1", True), ("3", True)})
        self.assertEqual({(row.id, row.recipient_role) for row in map_listings(commercial)},
                         {("1", "manager"), ("3", "roommate")})
        self.assertEqual({row.id for row in map_listings(commercial, date_filtered=True)}, {"1", "3"})
        other = filter_listing_providers(data, "other")
        self.assertEqual({(row.id, row.is_commercial) for row in map_listings(other)}, {("2", False)})
        self.assertEqual(other.listing_profiles["2"].recipient_role, "manager")

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
        self.assertIsNone(postings.matches[0].published_at)
        self.assertEqual(postings.matches[0].first_seen_at, saturday_start)
        last_minute, day_only, first_minute, fallback = postings.dated_listings
        self.assertEqual((last_minute.published_at, last_minute.first_seen_at), (monday_start - 1, monday_start - 1))
        self.assertEqual((day_only.published_at, day_only.published_day), (None, sunday))
        self.assertEqual(first_minute.published_at, saturday_start)
        self.assertEqual(fallback.published_at, saturday_start)

        sightings = load_dashboard(self.path, now=monday, posting_window=(saturday, sunday), date_basis="first_seen")
        self.assertEqual([(row.id, row.day) for row in sightings.dated_listings], [("sighting", saturday)])
        self.assertEqual(sightings.dated_listings[0].first_seen_at, saturday_start)
        self.assertIsNone(sightings.dated_listings[0].published_at)

    def test_current_match_dates_distinguish_publication_first_scan_and_alert(self):
        current = NOW.timestamp()
        first_seen = (NOW - timedelta(hours=2)).timestamp()
        published = (NOW - timedelta(hours=4)).timestamp()
        rows = [
            ("10", "https://www.wg-gesucht.de/10.html", "Veröffentlicht"),
            ("11", "https://www.wg-gesucht.de/11.html", "Veröffentlicht"),
            ("12", "https://www.wg-gesucht.de/12.html", "zuerst gesehen"),
            ("ka-13", "https://www.kleinanzeigen.de/s-anzeige/zimmer/13-199-650", "Suchalarm erhalten"),
        ]
        with closing(_database(self.path)) as db, db:
            for listing_id, url, basis in rows:
                record_listing_availability(db, listing_id, None)
                db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, 'Hallo', 0)", (listing_id, url, current, basis))
            db.execute("""INSERT INTO wg_observations (id, url, first_seen_at, last_seen_at, published_at)
                          VALUES ('10', ?, ?, ?, ?)""", (rows[0][1], first_seen, current, published))

        matches = {row.id: row for row in load_dashboard(self.path, now=NOW).matches}
        self.assertEqual({item: (row.published_at, row.first_seen_at) for item, row in matches.items()}, {
            "10": (published, first_seen), "11": (current, None), "12": (None, current), "ka-13": (None, None),
        })

    def test_dated_secondary_offers_include_published_listings_without_inventory(self):
        friday = date(2026, 11, 13)
        published = (NOW - timedelta(days=2)).timestamp()
        with closing(_database(self.path)) as db, db:
            for listing_id, basis, reason in (
                ("ka-11", "Veröffentlicht", None),
                ("ka-12", "Veröffentlicht", None),
                ("ka-13", "Veröffentlicht", "Anzeige nicht mehr verfügbar"),
                ("ka-14", "Suchalarm erhalten", None),
            ):
                url = f"https://www.kleinanzeigen.de/s-anzeige/zimmer/{listing_id[3:]}-199-650"
                record_listing_availability(db, listing_id, reason)
                db.execute("INSERT INTO munich_secondary_offers VALUES (?, ?, 'Zimmer', ?, ?, 'b', 'Ostbahnhof', 42)",
                           (listing_id, url, published, basis))
        set_listing_hidden(self.path, "ka-12", True, title="Zimmer",
                           url="https://www.kleinanzeigen.de/s-anzeige/zimmer/12-199-650")

        data = load_dashboard(self.path, now=NOW, posting_window=(friday, friday))
        self.assertEqual(data.secondary_offers, ())
        self.assertEqual([(row.id, row.day, row.published_at) for row in data.dated_listings],
                         [("ka-11", friday, published)])
        self.assertEqual([(row.id, row.tier, row.published_at) for row in data.dated_secondary_offers],
                         [("ka-11", "b", published)])

    def test_published_drafts_remain_searchable_with_an_old_schema(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                       ("12", "https://www.wg-gesucht.de/12.html", NOW.timestamp(), "Veröffentlicht", None, 1))

        data = load_dashboard(self.path, now=NOW, posting_window=weekend_dates(NOW.date()))
        self.assertEqual([(row.id, row.day) for row in data.dated_listings], [("12", NOW.date())])

    def test_oldest_first_queries_beyond_the_latest_hundred_postings(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE drafts (id TEXT, url TEXT, fresh_at REAL, basis TEXT, message TEXT, done INTEGER)")
            db.executemany("INSERT INTO drafts VALUES (?, 'https://www.wg-gesucht.de/test.html', ?, 'Veröffentlicht', NULL, 1)",
                           [(str(index), NOW.timestamp() - 101 + index) for index in range(101)])

        window = (NOW.date(), NOW.date())
        latest = load_dashboard(self.path, now=NOW, posting_window=window).dated_listings
        oldest = load_dashboard(self.path, now=NOW, posting_window=window, newest_first=False).dated_listings
        self.assertEqual((latest[0].id, latest[-1].id), ("100", "1"))
        self.assertEqual((oldest[0].id, oldest[-1].id), ("0", "99"))

    def test_historical_matches_are_filtered_before_the_result_limit(self):
        url = "https://www.wg-gesucht.de/test.html"
        first_seen = NOW.timestamp() - 102
        sightings = [(str(index), url, first_seen + index, NOW.timestamp()) for index in range(102)]
        with closing(_database(self.path)) as db, db:
            db.executemany("INSERT INTO wg_observations(id, url, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                           sightings)
            db.executemany("INSERT INTO listing_availability(id, policy_version) VALUES (?, ?)",
                           [(row[0], AVAILABILITY_POLICY_VERSION) for row in sightings])
            db.execute("INSERT INTO drafts VALUES ('0', ?, ?, 'zuerst gesehen', 'Hallo', 0)", (url, first_seen))
            db.execute("""INSERT INTO munich_secondary_offers(id, url, title, fresh_at, basis, tier, station, minutes)
                          VALUES ('1', ?, 'Zimmer', ?, 'zuerst gesehen', 'b', 'Ostbahnhof', 40)""",
                       (url, first_seen + 1))

        data = load_dashboard(self.path, now=NOW, posting_window=(NOW.date(), NOW.date()),
                              date_basis="first_seen", only_matches=True)
        self.assertEqual([row.id for row in data.dated_listings], ["1", "0"])
        self.assertEqual([row.id for row in data.dated_secondary_offers], ["1"])
