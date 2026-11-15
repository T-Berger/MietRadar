import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from munich_radar import Scanner, _database
from wg_alerts import AlertsInbox, ListingAlert, extract_refs
from wg_preferences import rejection_reason
from wg_source import Listing, ListingRef, WGSourceError


NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def offer(i, age, *, description="Ein Zimmer mit Balkon.", address=None):
    url = f"https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.1234{i}.html"
    return Listing(str(12340 + i), url, "WG-Zimmer", description,
                   NOW - timedelta(hours=age) if age is not None else None,
                   address or f"Musterstraße {i}, 81667 München")


def ref(listing):
    return ListingRef(listing.id, listing.url)


class PreferencesTest(unittest.TestCase):
    def test_explicit_exclusions(self):
        for description, wanted, resident_ages, expected in (
            ("Vermietung nur an Studierende.", "", "", "nur Studierende"),
            ("Die Studienbescheinigung ist erforderlich.", "", "", "nur Studierende"),
            ("Nur für berufstätige Frauen.", "", "", "nur Frauen"),
            ("", "Frau zwischen 18 und 25 Jahren", "", "nur Frauen"),
            ("Wir sind beide 20 und 26 Jahre alt.", "", "", "Alter der Mitbewohner außerhalb der Spanne"),
            ("", "", "26 bis 36 Jahre", "Alter der Mitbewohner außerhalb der Spanne"),
        ):
            with self.subTest(description=description, wanted=wanted, resident_ages=resident_ages):
                self.assertEqual(rejection_reason("Zimmer", description, wanted=wanted, resident_ages=resident_ages), expected)

    def test_mixed_households_and_inexact_age_statements_are_allowed(self):
        for description in (
            "Wir sind nur Frauen. Das Geschlecht ist uns egal.",
            "Die WG ist für Studierende und Berufstätige.",
            "Nur für Studierende oder Azubis.",
            "Wir suchen eine Mitbewohnerin oder einen Mitbewohner.",
            "Vor 40 Jahren gebaut. Gesucht: 18 bis 40 Jahre.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(rejection_reason("Zimmer", description, resident_ages="21 bis 35 Jahre"))


class ScannerTest(unittest.TestCase):
    def test_only_fresh_eligible_transit_matches_are_queued_in_order_and_deduplicated(self):
        older, stale, fresh, unknown, excluded, no_route = (
            offer(1, 8), offer(2, 25), offer(3, 1), offer(4, None),
            offer(5, 1, description="Nur für Studierende."), offer(6, 2),
        )
        listing_by_id = {item.id: item for item in (older, stale, fresh, unknown, excluded, no_route)}
        source = Mock()
        source.inventory_scope = "munich"
        source.candidates.return_value = [ref(item) for item in listing_by_id.values()]
        source.details.side_effect = lambda candidate: listing_by_id[candidate.id]
        router = Mock()
        router.classify.side_effect = lambda address, now: (None, None) if address == no_route.address else ("a", None)
        scanner = Scanner(source, router, "Hi {name},\n\nich bin 28.\n\n{LLM_TEXT}\n\nViele Grüße", "none", timedelta(days=1), 12)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.sqlite3"
            with closing(_database(path)) as db:
                counts = scanner.scan(db, NOW, {})
                self.assertEqual(counts["neue Entwürfe"], 2)
                self.assertEqual([call.args[0] for call in router.classify.call_args_list],
                                 [fresh.address, no_route.address, older.address])
                queued = db.execute("SELECT id, message FROM drafts ORDER BY fresh_at DESC").fetchall()
                self.assertEqual([row[0] for row in queued], [fresh.id, older.id])
                self.assertNotIn("{LLM_TEXT}", queued[0][1])
            with closing(_database(path)) as db:
                self.assertEqual(scanner.scan(db, NOW, {})["neue Entwürfe"], 0)
                scanner.scan(db, NOW + timedelta(days=2), {})
                self.assertEqual(db.execute("SELECT COUNT(*) FROM drafts WHERE message IS NOT NULL").fetchone()[0], 0)

    def test_alert_timestamp_is_distinct_and_cannot_override_publication(self):
        unknown, stale = offer(1, None), offer(2, 36)
        alerts, source, router = Mock(), Mock(), Mock()
        alerts.recent.return_value = [ListingAlert(ref(item), NOW - timedelta(minutes=10)) for item in (unknown, stale)]
        source.details.side_effect = [unknown, stale]
        router.classify.return_value = ("a", None)
        scanner = Scanner(source, router, "Hi!", "none", timedelta(days=1), 12, alerts)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            scanner.scan(db, NOW, {})
            self.assertEqual(db.execute("SELECT id, basis FROM drafts").fetchall(), [(unknown.id, "Suchalarm erhalten")])
        source.candidates.assert_not_called()
        router.classify.assert_called_once()

    def test_snapshots_cover_all_ids_and_first_observed_matches_remain_manual(self):
        baseline, beyond_budget, new, stale = offer(1, None), offer(2, None), offer(3, None), offer(4, 36)
        source, router, delivery = Mock(), Mock(), Mock()
        source.inventory_scope = "munich"
        source.candidates.side_effect = [
            [ref(baseline), ref(beyond_budget)],
            [ref(baseline), ref(new)],
            [ref(beyond_budget), ref(stale)],
        ]
        source.details.side_effect = [baseline, new, stale]
        router.classify.return_value = ("a", None)
        scanner = Scanner(source, router, "Hi!", "none", timedelta(hours=24), 1, delivery=delivery)
        later = NOW + timedelta(hours=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.sqlite3"
            with closing(_database(path)) as db:
                counts = scanner.scan(db, NOW, {})
                self.assertEqual(counts["WG-Gesucht-Angebote beobachtet"], 2)
                source.details.assert_called_once_with(ref(baseline))
                self.assertEqual(db.execute("SELECT count(*) FROM drafts").fetchone(), (0,))
                self.assertEqual(db.execute("SELECT id FROM wg_observations ORDER BY id").fetchall(),
                                 [(baseline.id,), (beyond_budget.id,)])
            with closing(_database(path)) as db:
                scanner.scan(db, later, {})
                self.assertEqual(db.execute("SELECT id, fresh_at, basis FROM drafts").fetchall(),
                                 [(new.id, later.timestamp(), "zuerst gesehen")])
                scanner.scan(db, later + timedelta(hours=1), {})
                self.assertEqual(db.execute("SELECT id FROM drafts").fetchall(), [(new.id,)])
                self.assertEqual(db.execute("SELECT first_seen_at, last_seen_at, previous_scan_at FROM wg_observations WHERE id = ?",
                                            (beyond_budget.id,)).fetchone(),
                                 (NOW.timestamp(), (later + timedelta(hours=1)).timestamp(), None))
                self.assertEqual(db.execute("SELECT published_at FROM wg_observations WHERE id = ?", (stale.id,)).fetchone(),
                                 (stale.published_at.timestamp(),))
        delivery.deliver.assert_not_called()

    def test_failed_inventory_does_not_advance_the_baseline_and_a_long_gap_resets_it(self):
        baseline, during_gap, new = offer(1, None), offer(2, None), offer(3, None)
        source, router = Mock(), Mock()
        source.inventory_scope = "munich"
        source.candidates.side_effect = [[ref(baseline)], WGSourceError("unavailable"), [ref(during_gap)], [ref(new)]]
        source.details.side_effect = [baseline, during_gap, new]
        router.classify.return_value = ("a", None)
        scanner = Scanner(source, router, "Hi!", "none", timedelta(hours=24), 12)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            scanner.scan(db, NOW, {})
            with self.assertRaises(WGSourceError):
                scanner.scan(db, NOW + timedelta(hours=1), {})
            self.assertEqual(db.execute("SELECT scanned_at FROM wg_inventory_scan WHERE id = 1").fetchone(), (NOW.timestamp(),))
            scanner.scan(db, NOW + timedelta(hours=25), {})
            self.assertEqual(db.execute("SELECT count(*) FROM drafts").fetchone(), (0,))
            scanner.scan(db, NOW + timedelta(hours=26), {})
            self.assertEqual(db.execute("SELECT id FROM drafts").fetchall(), [(new.id,)])

    def test_records_calendar_day_without_drafting_definitely_old_listing(self):
        old = replace(offer(1, None), published_day=date(2026, 11, 12))
        source, router = Mock(), Mock()
        source.inventory_scope = "munich"
        source.candidates.side_effect = [[], [ref(old)]]
        source.details.return_value = old
        scanner = Scanner(source, router, "Hi!", "none", timedelta(hours=24), 12)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            scanner.scan(db, NOW, {})
            scanner.scan(db, NOW + timedelta(hours=1), {})
            self.assertEqual(db.execute("SELECT published_at, published_day FROM wg_observations").fetchone(),
                             (None, "2026-11-12"))
            self.assertEqual(db.execute("SELECT count(*) FROM drafts").fetchone(), (0,))
        router.classify.assert_not_called()

    def test_calendar_day_from_an_alert_never_qualifies_for_automatic_delivery(self):
        known_day = replace(offer(1, None), published_day=date(2026, 11, 14))
        alerts, source, router, delivery = Mock(), Mock(), Mock(), Mock()
        alerts.recent.return_value = [ListingAlert(ref(known_day), NOW - timedelta(minutes=5))]
        source.details.side_effect = [known_day, replace(known_day, published_day=None)]
        router.classify.return_value = ("a", None)
        delivery.paused = False
        scanner = Scanner(source, router, "Hi!", "none", timedelta(hours=72), 12, alerts, delivery=delivery)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            scanner.scan(db, NOW, {})
            scanner.scan(db, NOW + timedelta(hours=1), {})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM drafts").fetchone(), (1,))
            self.assertEqual(db.execute("SELECT published_at, published_day FROM wg_observations").fetchone(),
                             (None, "2026-11-14"))
        delivery.deliver.assert_not_called()

    def test_secondary_travel_matches_are_saved_without_applications(self):
        east, central = offer(1, 1), offer(2, 2)
        source, router, delivery = Mock(), Mock(), Mock()
        source.inventory_scope = "munich"
        source.candidates.return_value = [ref(east), ref(central)]
        source.details.side_effect = [east, central]
        router.classify.side_effect = [("b", 38), ("c", 45)]
        scanner = Scanner(source, router, "Hi!", "none", timedelta(hours=24), 12, delivery=delivery)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            scanner.scan(db, NOW, {})
            self.assertEqual(db.execute("SELECT tier, station, minutes FROM munich_secondary_offers ORDER BY id").fetchall(),
                             [("b", "Ostbahnhof", 38), ("c", "Hbf", 45)])
            self.assertEqual(db.execute("SELECT count(*) FROM drafts").fetchone(), (0,))
        delivery.deliver.assert_not_called()


class AlertParsingTest(unittest.TestCase):
    def test_extracts_only_canonical_listing_links_from_configured_sender(self):
        listing = offer(1, None)
        email = EmailMessage()
        email["From"] = "WG-Gesucht <alerts@wg-gesucht.de>"
        email.set_content(f"Neues Angebot: {listing.url}?tracking=yes")
        email.add_alternative(
            f'<a href="{listing.url}">Inserat</a><a href="https://example.com/12341.html">Fremd</a>'
            '<a href="https://www.wg-gesucht.de/nachricht-senden.html">Ignorieren</a>', subtype="html",
        )
        self.assertEqual(extract_refs(email.as_bytes(), "alerts@wg-gesucht.de"), [ref(listing)])
        self.assertEqual(extract_refs(email.as_bytes(), "other@wg-gesucht.de"), [])

    def test_poll_uses_mailbox_receive_time_and_does_not_mark_alerts_read(self):
        listing = offer(1, None)
        email = EmailMessage()
        email["From"] = "alerts@wg-gesucht.de"
        email["Date"] = "Fri, 13 Nov 2026 12:00:00 +0000"
        email.set_content(listing.url)
        mailbox = MagicMock()
        mailbox.__enter__.return_value = mailbox
        mailbox.select.return_value = ("OK", [])
        mailbox.uid.side_effect = [
            ("OK", [b"1 2"]),
            ("OK", [(b'2 (INTERNALDATE "15-Nov-2026 11:55:00 +0200" BODY[] {100}', email.as_bytes())]),
            ("OK", [(b'1 (BODY[] {100}', email.as_bytes()), b' INTERNALDATE "15-Nov-2026 11:30:00 +0200")']),
        ]
        inbox = AlertsInbox("imap.example.com", "user", "secret", "Alerts", "alerts@wg-gesucht.de")
        with patch("wg_alerts.imaplib.IMAP4_SSL", return_value=mailbox):
            self.assertEqual(inbox.recent(NOW - timedelta(days=1), 10),
                             [ListingAlert(ref(listing), NOW - timedelta(minutes=30))])
        mailbox.select.assert_called_once_with('"Alerts"', readonly=True)
        self.assertIn("BODY.PEEK", mailbox.uid.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()
