import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from kleinanzeigen_radar import KleinanzeigenRadar
from munich_delivery import DeliveryQueue
from munich_radar import _database
from test_kleinanzeigen_radar import klein_offer
from test_munich_delivery import RecordingSender
from test_munich_radar import NOW


class KleinanzeigenAutosendTest(unittest.TestCase):
    def test_sends_fresh_transit_matches_newest_first_with_24_hour_ceiling(self):
        older, newest, stale = klein_offer(1), klein_offer(2), klein_offer(3)
        unknown = klein_offer(4, address=None)
        no_route = klein_offer(5)
        inbox, router = Mock(), Mock()
        inbox.recent_items.return_value = [
            (older, NOW - timedelta(hours=3)), (stale, NOW - timedelta(hours=25)),
            (newest, NOW - timedelta(minutes=5)), (unknown, NOW - timedelta(minutes=4)),
            (no_route, NOW - timedelta(minutes=2)),
        ]
        router.classify.side_effect = lambda address, now: ("outside", None) if address == no_route.address else ("a", None)
        sender = RecordingSender()
        queue = DeliveryQueue(sender, clock=lambda: NOW)
        radar = KleinanzeigenRadar(router, "Hi, ich bin 28.", "none", delivery=queue)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            cutoff = NOW - timedelta(hours=48)
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            self.assertEqual(sender.attempts, [(newest.url, "Hi, ich bin 28."), (older.url, "Hi, ich bin 28.")])
            self.assertEqual(db.execute("SELECT id FROM klein_review").fetchall(), [(unknown.id,)])

    def test_manual_address_draft_gets_rechecked_before_attempt(self):
        listing = klein_offer(1, address=None)
        inbox, router = Mock(), Mock()
        inbox.recent_items.return_value = [(listing, NOW - timedelta(minutes=20))]
        sender = RecordingSender()
        radar = KleinanzeigenRadar(router, "Hallo!", "none", delivery=DeliveryQueue(sender, clock=lambda: NOW))
        cutoff = NOW - timedelta(days=1)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "radar.sqlite3")) as db:
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            router.classify.return_value = ("a", None)
            self.assertTrue(radar.check_address(db, listing.id, "Musterstraße 12, 81667 München", NOW, cutoff)[0])
            inbox.recent_items.return_value = []
            router.qualifies.return_value = False
            radar.scan(db, inbox, NOW, cutoff, 10, {})
            self.assertEqual(sender.attempts, [])
        router.qualifies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
