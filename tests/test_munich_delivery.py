import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from munich_delivery import DeliveryOutcome, DeliveryQueue, FirstMessageReview
from munich_radar import Scanner, _database
from munich_tiers import save_secondary_offer
from outgoing_judge import OutgoingJudgment
from test_munich_radar import NOW, offer, ref
from wg_first_message import viewing_slot_labels


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

    def test_first_seen_draft_is_never_sent_automatically(self):
        listing = offer(1, 1)
        self.draft(listing)
        with self.db:
            self.db.execute("UPDATE drafts SET basis = 'zuerst gesehen' WHERE id = ?", (listing.id,))
        sender = Mock()
        self.assertIsNone(self.run_delivery(DeliveryQueue(sender, clock=lambda: NOW), listing))
        sender.send.assert_not_called()

    def test_expiry_is_rechecked_before_click_and_send_window_cannot_exceed_24h(self):
        listing = offer(1, 24)
        self.draft(listing)
        times = iter((NOW, NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=1)))
        sender = RecordingSender()
        result = self.run_delivery(DeliveryQueue(sender, clock=lambda: next(times)), listing, timedelta(hours=72))
        self.assertIsNone(result)
        self.assertFalse(sender.attempts)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery").fetchone(), (0,))

    def test_demotion_to_secondary_before_click_prevents_delivery(self):
        listing = offer(1, 1)
        self.draft(listing)
        sender = Mock()

        def send(url, message, before_submit):
            with self.db:
                save_secondary_offer(self.db, listing.id, url, listing.title, listing.published_at,
                                     "veröffentlicht", "c", "Hbf", 45)
            self.assertFalse(before_submit())
            return DeliveryOutcome.SKIPPED

        sender.send.side_effect = send
        self.assertIsNone(self.run_delivery(DeliveryQueue(sender, clock=lambda: NOW), listing))
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
        newest = replace(newest, title="WG-Zimmer mit Balkon", is_wg=True)
        self.draft(done, done=1)
        by_id = {item.id: item for item in (older, newest, done, stale, nonmatch)}
        source, router = Mock(), Mock()
        source.inventory_scope = "munich"
        source.candidates.return_value = [ref(item) for item in by_id.values()]
        source.details.side_effect = lambda candidate: by_id[candidate.id]
        router.classify.side_effect = lambda address, now: ("outside", None) if address == nonmatch.address else ("a", None)
        sender = RecordingSender()
        queue = DeliveryQueue(sender, limit=1, clock=lambda: NOW)
        template = "Hi zusammen,\n\n{LLM_TEXT}\n\nKann ich zur Besichtigung vorbeikommen?\n\nLiebe Grüße\nSam"
        intro = "Ich bin Sam, 28, und arbeite als Architekt."
        availability = (NOW + timedelta(hours=5), NOW + timedelta(days=1, hours=6))
        scanner = Scanner(source, router, template, "none", timedelta(hours=24), 12, delivery=queue,
                          applicant_intro=intro, viewing_availability=availability)
        with patch("munich_delivery.judge_outgoing", return_value=OutgoingJudgment(True, "ok")) as judge:
            counts = scanner.scan(self.db, NOW, {})
        self.assertEqual(counts["Bewerbung gesendet"], 1)
        self.assertEqual(len(sender.attempts), 1)
        url, message = sender.attempts[0]
        self.assertEqual(url, newest.url)
        self.assertTrue(message.startswith(f"Hi zusammen,\n\n{intro}"))
        self.assertIn("Balkon", message)
        for slot in viewing_slot_labels(availability, NOW):
            self.assertIn(slot, message)
        newest_checks = [call for call in judge.call_args_list if call.kwargs.get("listing_title") == newest.title]
        self.assertEqual(len(newest_checks), 2)
        self.assertTrue(all(call.args == (message,) and call.kwargs["first_message"] for call in newest_checks))
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("sent",))
        self.assertEqual(self.db.execute("SELECT id FROM drafts WHERE done = 0").fetchall(), [(older.id,)])

    def test_viewing_slot_that_expires_after_judging_prevents_submit(self):
        listing = offer(1, 1)
        self.draft(listing)
        checked_at = NOW
        sender = Mock()

        def send(url, message, before_submit):
            nonlocal checked_at
            checked_at += timedelta(minutes=2)
            self.assertFalse(before_submit())
            return DeliveryOutcome.SKIPPED

        sender.send.side_effect = send
        queue = DeliveryQueue(sender, clock=lambda: checked_at)
        review = FirstMessageReview(listing.title, "", (NOW + timedelta(minutes=1),), {})
        with patch("munich_delivery.judge_outgoing", return_value=OutgoingJudgment(True, "ok")):
            queue.deliver(self.db, listing.id, listing.url, "Hi!", listing.published_at,
                          timedelta(hours=24), first_message=review)
        sender.send.assert_called_once()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()
