import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from dashboard_data import load_dashboard
from kleinanzeigen_radar import KleinanzeigenRadar
from munich_radar import Scanner, _database
from test_munich_radar import NOW, offer, ref


def klein_offer(number, **changes):
    return replace(
        offer(number, None), id=f"ka-{12340 + number}",
        url=f"https://www.kleinanzeigen.de/s-anzeige/wohnung/{12340 + number}-203-1234", **changes,
    )


class KleinanzeigenRadarTest(unittest.TestCase):
    def test_draft_mode_reviews_applications_from_both_sources(self):
        wg, klein = offer(1, 1), klein_offer(1)
        source, router, inbox = Mock(inventory_scope="munich"), Mock(), Mock()
        source.candidates.return_value = [ref(wg)]
        source.details.return_value = wg
        router.classify.return_value = ("a", None)
        inbox.recent_items.return_value = [(klein, NOW - timedelta(minutes=20))]
        scanner = Scanner(source, router, "Hallo, ich würde mir das Zimmer gerne ansehen!", "none",
                          timedelta(days=1), 10, klein_alerts=inbox, outgoing_provider="mistral")
        verdicts = ['{"approved": true, "reason": "ok"}', '{"approved": false, "reason": "bezug"}']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.sqlite3"
            with closing(_database(path)) as db, patch("outgoing_judge._generate_via_mistral", side_effect=verdicts) as judge:
                scanner.scan(db, NOW, {})
                self.assertEqual(judge.call_count, 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery").fetchone(), (0,))
            snapshot = load_dashboard(path, now=NOW)
            self.assertEqual([(match.id, match.state) for match in snapshot.matches],
                             [(klein.id, "draft"), (wg.id, "held")])

    def test_combined_scan_keeps_ids_separate_and_sends_only_wg_gesucht(self):
        wg = offer(1, 1)
        klein = klein_offer(1)
        source, router, inbox, delivery = Mock(inventory_scope="munich"), Mock(), Mock(), Mock()
        source.candidates.return_value = [ref(wg)]
        source.details.return_value = wg
        router.classify.return_value = ("a", None)
        inbox.recent_items.return_value = [(klein, NOW - timedelta(minutes=20))]
        scanner = Scanner(source, router, "Hallo!", "none", timedelta(days=1), 10,
                          delivery=delivery, klein_alerts=inbox)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            counts = scanner.scan(db, NOW, {})
            self.assertEqual(counts["neue Kleinanzeigen-Entwürfe"], 1)
            self.assertEqual(db.execute("SELECT id FROM drafts ORDER BY fresh_at DESC").fetchall(), [(klein.id,), (wg.id,)])
        delivery.deliver.assert_called_once()
        self.assertEqual(delivery.deliver.call_args.args[1], wg.id)

    def test_missing_address_stays_in_review_until_a_fresh_transit_match(self):
        pending = klein_offer(2, address=None)
        old = klein_offer(3, address=None)
        exclusive = klein_offer(4, address=None, title="Nur für Studierende")
        router, inbox = Mock(), Mock()
        inbox.recent_items.return_value = [
            (pending, NOW - timedelta(hours=1)), (old, NOW - timedelta(hours=25)),
            (exclusive, NOW - timedelta(minutes=10)),
        ]
        radar = KleinanzeigenRadar(router, "Hallo!", "none")
        cutoff = NOW - timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            router.classify.assert_not_called()
            self.assertEqual(db.execute("SELECT id FROM klein_review").fetchall(), [(pending.id,)])
            self.assertFalse(radar.check_address(db, pending.id, "81667 München", NOW, cutoff)[0])
            router.classify.return_value = ("a", None)
            self.assertTrue(radar.check_address(db, pending.id, "Musterstraße 12, 81667 München", NOW, cutoff)[0])
            self.assertEqual(db.execute("SELECT id, basis FROM drafts").fetchall(), [(pending.id, "Suchalarm erhalten")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM klein_review").fetchone()[0], 0)

    def test_secondary_offers_keep_original_freshness_without_a_draft_or_delivery(self):
        east, central = klein_offer(1), klein_offer(2)
        initial = NOW - timedelta(hours=2)
        recent = NOW - timedelta(minutes=30)
        router, inbox, delivery = Mock(), Mock(), Mock(paused=False)
        router.classify.side_effect = lambda address, _: ("b", 38) if address == east.address else ("c", 45)
        inbox.recent_items.side_effect = [[(east, initial), (central, recent)], [(east, recent)]]
        radar = KleinanzeigenRadar(router, "Hallo!", "none", delivery=delivery)
        cutoff = NOW - timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            with patch("kleinanzeigen_radar.personalise_message") as personalise:
                radar.scan(db, inbox, NOW, cutoff, 10, {})
                radar.scan(db, inbox, NOW, cutoff, 10, {})
                personalise.assert_not_called()
            rows = db.execute("""SELECT id, tier, station, minutes, fresh_at FROM munich_secondary_offers
                                 ORDER BY id""").fetchall()
            self.assertEqual(rows, [
                (east.id, "b", "Ostbahnhof", 38, initial.timestamp()), (central.id, "c", "Hbf", 45, recent.timestamp()),
            ])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM drafts").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM klein_origins").fetchone(), (0,))
        delivery.deliver.assert_not_called()

    def test_manual_secondary_offer_does_not_reopen_address_review_on_alert_replay(self):
        listing = klein_offer(1, address=None)
        router, inbox = Mock(), Mock()
        router.classify.return_value = ("b", 43)
        inbox.recent_items.return_value = [(listing, NOW - timedelta(minutes=30))]
        radar = KleinanzeigenRadar(router, "Hallo!", "none")
        cutoff = NOW - timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            saved, _ = radar.check_address(db, listing.id, "Musterstraße 12, 81667 München", NOW, cutoff)
            self.assertTrue(saved)
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            self.assertEqual(db.execute("SELECT id, tier, minutes FROM munich_secondary_offers").fetchall(),
                             [(listing.id, "b", 43)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM klein_review").fetchone(), (0,))
        router.classify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
