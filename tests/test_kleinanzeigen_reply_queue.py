import sqlite3
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from kleinanzeigen_judge import ReplyJudgment
from kleinanzeigen_activity import Activity, install_activity
from kleinanzeigen_replies import KleinanzeigenReply
from kleinanzeigen_reply_queue import ReplyDispatcher, install_reply_queue, save_reply_draft
from munich_delivery import DeliveryOutcome, install_schema
from outgoing_judge import OutgoingJudgment
from test_munich_radar import NOW


class ReplyQueueTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        install_schema(self.db)
        install_activity(self.db)
        install_reply_queue(self.db)
        self.inbox = "https://www.kleinanzeigen.de/m-nachrichten.html#thread-1234"

    def save(self, ident, minutes=5, url=None):
        save_reply_draft(self.db, KleinanzeigenReply(ident, "ka-1234", url or self.inbox,
                                                    "Kannst du morgen zur Besichtigung kommen?"),
                         ReplyJudgment("Besichtigung", "hoch", "Termin abstimmen"),
                         NOW - timedelta(minutes=minutes), "none", "none")

    def test_automatic_mode_rechecks_judge_and_reserves_only_one_reply_per_listing(self):
        self.save("first", minutes=10)
        self.save("newest")
        sender = Mock()

        def send(url, listing_id, message, reserve):
            self.assertEqual((url, listing_id), (self.inbox, "ka-1234"))
            self.assertTrue(reserve())
            self.assertIn("Danke für die Einladung", message)
            return DeliveryOutcome.SENT

        sender.send_reply.side_effect = send
        dispatcher = ReplyDispatcher(sender, "mistral", clock=lambda: NOW)
        with patch("munich_delivery.judge_outgoing", return_value=OutgoingJudgment(False, "Abgelehnt")):
            self.assertEqual(dispatcher.run(self.db, NOW), 0)
        sender.send_reply.assert_not_called()
        with patch("munich_delivery.judge_outgoing", return_value=OutgoingJudgment(True, "Freigegeben")):
            self.assertEqual(dispatcher.run(self.db, NOW), 1)
            self.assertEqual(dispatcher.run(self.db, NOW), 0)
        self.assertEqual(self.db.execute("SELECT id FROM klein_reply_drafts WHERE state = 'sent'").fetchall(), [("newest",)])

    def test_old_replies_and_generic_inbox_links_remain_manual_drafts(self):
        self.save("old", minutes=1500)
        self.save("draft", url="https://www.kleinanzeigen.de/m-nachrichten.html")
        sender = Mock()
        self.assertEqual(ReplyDispatcher(sender, "none", clock=lambda: NOW).run(self.db, NOW), 0)
        sender.send_reply.assert_not_called()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM klein_reply_drafts WHERE state = 'draft'").fetchone(), (2,))

    def test_later_message_without_a_draft_blocks_the_previous_invitation(self):
        self.save("invite")
        Activity("reply:later", "reply", None, self.inbox, "Absage", "Schon vergeben", NOW.timestamp()).save(self.db)
        sender = Mock()
        self.assertEqual(ReplyDispatcher(sender, "none", clock=lambda: NOW).run(self.db, NOW), 0)
        sender.send_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
