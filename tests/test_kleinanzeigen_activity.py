import io
import json
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from kleinanzeigen_activity import EmailNotifier, record_activity, record_replies
from kleinanzeigen_replies import KleinanzeigenReply
from munich_radar import _database
from test_kleinanzeigen_radar import klein_offer
from test_munich_radar import NOW


class KleinanzeigenActivityTest(unittest.TestCase):
    def test_only_known_listing_replies_are_judged_once_and_sent_events_are_labeled(self):
        listing = klein_offer(1)
        unknown = klein_offer(2)
        known_reply = KleinanzeigenReply("id1", listing.id, listing.url, "Kannst du am Freitag zur Besichtigung kommen?")
        unknown_reply = KleinanzeigenReply("id2", unknown.id, unknown.url, "Ist die Kamera noch da?")
        inbox = Mock()
        inbox.recent_items.return_value = [(known_reply, NOW), (unknown_reply, NOW)]
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "test.sqlite3")) as db:
            with db:
                db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'Suchalarm erhalten', NULL, 1)",
                           (listing.id, listing.url, (NOW - timedelta(hours=2)).timestamp()))
                db.execute("INSERT INTO delivery VALUES (?, 'sent', ?, ?)", (listing.id, NOW.timestamp(), NOW.timestamp()))
            record_activity(db, NOW)
            record_activity(db, NOW)
            self.assertEqual(record_replies(db, inbox, "none", NOW), 1)
            self.assertEqual(record_replies(db, inbox, "none", NOW), 0)
            kinds = db.execute("SELECT kind, subject FROM klein_activity ORDER BY kind").fetchall()
            self.assertEqual(kinds, [("delivery", "Kleinanzeigen-Bewerbung gesendet"), ("reply", "Kleinanzeigen-Antwort · Besichtigung")])

    def test_notifier_retries_same_event_key_and_sends_only_to_configured_user(self):
        listing = klein_offer(1)
        notifier = EmailNotifier("test-not-a-key", "radar@example.com", "me@example.com")
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "test.sqlite3")) as db:
            with db:
                db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'Suchalarm erhalten', 'Hallo', 0)",
                           (listing.id, listing.url, NOW.timestamp()))
            record_activity(db, NOW)
            with patch("kleinanzeigen_activity.urlopen", side_effect=[URLError("temporary"), io.BytesIO(b'{"id":"accepted"}')]) as fetch:
                self.assertEqual(notifier.flush(db, NOW), (0, False))
                self.assertEqual(notifier.flush(db, NOW), (1, True))
                self.assertEqual(notifier.flush(db, NOW), (0, True))
            requests = [item.args[0] for item in fetch.call_args_list]
            self.assertEqual(requests[0].get_header("Idempotency-key"), requests[1].get_header("Idempotency-key"))
            self.assertEqual(json.loads(requests[1].data)["to"], ["me@example.com"])


if __name__ == "__main__":
    unittest.main()
