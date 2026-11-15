import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

from munich_delivery import DeliveryOutcome, DeliveryQueue
from munich_radar import Scanner, _database
from test_munich_radar import NOW, offer, ref


class RecordingSender:
    def __init__(self, outcome=DeliveryOutcome.SENT):
        self.outcome = outcome
        self.attempts = []

    def send(self, url, message, before_submit):
        if not before_submit():
            return DeliveryOutcome.SKIPPED
        self.attempts.append((url, message))
        return self.outcome


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "radar.sqlite3"
        self.db = _database(self.path)
        self.addCleanup(self.db.close)

    def draft(self, listing, done=0):
        with self.db:
            self.db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'veröffentlicht', 'Hi, ich bin 28.', ?)",
                            (listing.id, listing.url, listing.published_at.timestamp(), done))

    def run_delivery(self, queue, listing, max_age=timedelta(hours=24)):
        return queue.deliver(self.db, listing.id, listing.url, "Hi!", listing.published_at, max_age)

    def test_expiry_is_rechecked_before_click_and_send_window_cannot_exceed_24h(self):
        listing = offer(1, 24)
        self.draft(listing)
        times = iter((NOW, NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=1)))
        sender = RecordingSender()
        result = self.run_delivery(DeliveryQueue(sender, clock=lambda: next(times)), listing, timedelta(hours=72))
        self.assertIsNone(result)
        self.assertFalse(sender.attempts)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery").fetchone(), (0,))

    def test_unknown_result_is_not_retried_even_after_restart(self):
        listing = offer(1, 1)
        self.draft(listing)
        sender = RecordingSender(DeliveryOutcome.UNCONFIRMED)
        queue = DeliveryQueue(sender, clock=lambda: NOW)
        self.run_delivery(queue, listing)
        self.assertTrue(queue.paused)
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("unconfirmed",))
        with closing(_database(self.path)) as restarted_db:
            restarted = DeliveryQueue(sender, clock=lambda: NOW)
            restarted.deliver(restarted_db, listing.id, listing.url, "Hi!", listing.published_at, timedelta(days=1))
        self.assertEqual(len(sender.attempts), 1)

    def test_preflight_block_pauses_sending_and_creates_no_attempt(self):
        listing = offer(1, 1)
        self.draft(listing)
        sender = Mock()
        sender.send.return_value = DeliveryOutcome.BLOCKED
        queue = DeliveryQueue(sender, clock=lambda: NOW)
        self.run_delivery(queue, listing)
        self.assertTrue(queue.paused)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery").fetchone(), (0,))

    def test_scanner_sends_newest_then_leaves_other_drafts_and_respects_manual_done(self):
        older, newest, done, stale, nonmatch = offer(1, 5), offer(2, 1), offer(3, 0), offer(4, 25), offer(5, 2)
        self.draft(done, done=1)
        by_id = {item.id: item for item in (older, newest, done, stale, nonmatch)}
        source, router = Mock(), Mock()
        source.candidates.return_value = [ref(item) for item in by_id.values()]
        source.details.side_effect = lambda candidate: by_id[candidate.id]
        router.qualifies.side_effect = lambda address, now: address != nonmatch.address
        sender = RecordingSender()
        queue = DeliveryQueue(sender, limit=1, clock=lambda: NOW)
        scanner = Scanner(source, router, "Hi, ich bin 28.", "none", timedelta(hours=24), 12, delivery=queue)
        counts = scanner.scan(self.db, NOW, {})
        self.assertEqual(counts["Bewerbung gesendet"], 1)
        self.assertEqual(sender.attempts, [(newest.url, "Hi, ich bin 28.")])
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("sent",))
        self.assertEqual(self.db.execute("SELECT id FROM drafts WHERE done = 0").fetchall(), [(older.id,)])


if __name__ == "__main__":
    unittest.main()
