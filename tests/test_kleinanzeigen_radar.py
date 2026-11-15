import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from kleinanzeigen_radar import KleinanzeigenRadar
from munich_radar import Scanner, _database
from test_munich_radar import NOW, offer, ref


def klein_offer(number, **changes):
    return replace(
        offer(number, None), id=f"ka-{12340 + number}",
        url=f"https://www.kleinanzeigen.de/s-anzeige/wohnung/{12340 + number}-203-1234", **changes,
    )


class KleinanzeigenRadarTest(unittest.TestCase):
    def test_combined_scan_keeps_ids_separate_and_sends_only_wg_gesucht(self):
        wg = offer(1, 1)
        klein = klein_offer(1)
        source, router, inbox, delivery = Mock(inventory_scope="munich"), Mock(), Mock(), Mock()
        source.candidates.return_value = [ref(wg)]
        source.details.return_value = wg
        router.qualifies.return_value = True
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
            router.qualifies.assert_not_called()
            self.assertEqual(db.execute("SELECT id FROM klein_review").fetchall(), [(pending.id,)])
            self.assertFalse(radar.check_address(db, pending.id, "81667 München", NOW, cutoff)[0])
            router.qualifies.return_value = True
            self.assertTrue(radar.check_address(db, pending.id, "Musterstraße 12, 81667 München", NOW, cutoff)[0])
            self.assertEqual(db.execute("SELECT id, basis FROM drafts").fetchall(), [(pending.id, "Suchalarm erhalten")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM klein_review").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
