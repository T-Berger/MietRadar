import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from wg_reply_drafts import install_wg_reply_drafts, sync_wg_reply_drafts


class WGReplyDraftTests(unittest.TestCase):
    def test_keeps_one_draft_for_the_latest_incoming_message_until_answered(self):
        now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        install_wg_reply_drafts(db)
        incoming = SimpleNamespace(conversation_id="1234", message_id="4567", text="Passt dir morgen eine Besichtigung?",
                                   url="https://www.wg-gesucht.de/nachricht.html?nachrichten-id=1234")
        answered = SimpleNamespace(conversation_id=incoming.conversation_id, message_id="4568", text=None, url=incoming.url)
        options = {"provider": "none", "outgoing_provider": "none"}
        self.assertEqual(sync_wg_reply_drafts(db, (incoming,), now, **options), 1)
        state = db.execute("SELECT state, message FROM wg_reply_drafts WHERE id = ?", (incoming.conversation_id,)).fetchone()
        self.assertEqual(state[0], "draft")
        self.assertTrue(state[1])
        with patch("wg_reply_drafts.draft_wg_reply") as writer:
            self.assertEqual(sync_wg_reply_drafts(db, (incoming,), now + timedelta(minutes=2), **options), 0)
            writer.assert_not_called()
        sync_wg_reply_drafts(db, None, now, **options)
        self.assertEqual(db.execute("SELECT available FROM wg_reply_inbox_status").fetchone(), (0,))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM wg_reply_drafts").fetchone(), (1,))
        sync_wg_reply_drafts(db, (answered,), now + timedelta(minutes=3), **options)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM wg_reply_drafts").fetchone(), (0,))
