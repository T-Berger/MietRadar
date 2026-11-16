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
from munich_tiers import save_secondary_offer
from rental_availability import record_listing_availability
from test_kleinanzeigen_radar import klein_offer
from test_munich_radar import NOW, offer


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
            self.assertEqual(kinds, [("delivery", "Kleinanzeigen-Bewerbung gesendet"),
                                     ("match", "Neuer Kleinanzeigen-Treffer"),
                                     ("reply", "Kleinanzeigen-Antwort · Besichtigung")])

    def test_records_fresh_primary_matches_once_even_when_already_sent(self):
        wg_draft, wg_sent = offer(1, 3), offer(2, 3)
        klein_draft, klein_sent = klein_offer(3), klein_offer(4)
        old, secondary, unavailable, unconfirmed = (offer(i, 2) for i in range(5, 9))
        fresh = NOW - timedelta(hours=3)
        with tempfile.TemporaryDirectory() as directory, closing(_database(Path(directory) / "test.sqlite3")) as db:
            with db:
                db.executemany("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)", [
                    (wg_draft.id, wg_draft.url, (NOW - timedelta(hours=23, minutes=30)).timestamp(),
                     "zuerst gesehen", "Hallo", 0),
                    (wg_sent.id, wg_sent.url, fresh.timestamp(), "veröffentlicht", None, 1),
                    (klein_draft.id, klein_draft.url, fresh.timestamp(), "Suchalarm erhalten", "Hallo", 0),
                    (klein_sent.id, klein_sent.url, fresh.timestamp(), "Suchalarm erhalten", None, 1),
                    (old.id, old.url, (NOW - timedelta(hours=25)).timestamp(), "veröffentlicht", "Hallo", 0),
                    (secondary.id, secondary.url, fresh.timestamp(), "veröffentlicht", "Hallo", 0),
                    (unavailable.id, unavailable.url, fresh.timestamp(), "veröffentlicht", "Hallo", 0),
                    (unconfirmed.id, unconfirmed.url, fresh.timestamp(), "veröffentlicht", "Hallo", 0),
                ])
                db.executemany("INSERT INTO delivery VALUES (?, ?, ?, ?)", [
                    (wg_sent.id, "sent", NOW.timestamp(), NOW.timestamp()),
                    (klein_sent.id, "sent", NOW.timestamp(), NOW.timestamp()),
                    (unconfirmed.id, "unconfirmed", NOW.timestamp(), NOW.timestamp()),
                ])
                save_secondary_offer(db, secondary.id, secondary.url, secondary.title, fresh,
                                     "veröffentlicht", "b", "Ostbahnhof", 40)
                record_listing_availability(db, unavailable.id, "Anzeige nicht mehr verfügbar")
            record_activity(db, NOW)
            record_activity(db, NOW)
            rows = db.execute("SELECT listing_id, subject, summary FROM klein_activity WHERE kind = 'match'").fetchall()
            matches = {listing_id: (subject, summary) for listing_id, subject, summary in rows}
            self.assertEqual(len(rows), 4)
            self.assertEqual(db.execute("SELECT occurred_at FROM klein_activity WHERE id = ?",
                                        (f"match:{wg_draft.id}",)).fetchone(), (NOW.timestamp(),))
            for listing in (wg_draft, wg_sent, klein_draft, klein_sent):
                with self.subTest(listing=listing.id):
                    platform = "Kleinanzeigen" if listing.id.startswith("ka-") else "WG-Gesucht"
                    subject, summary = matches[listing.id]
                    self.assertEqual(subject, f"Neuer {platform}-Treffer")
                    self.assertIn("bereits gesendet" if listing in (wg_sent, klein_sent) else "Bewerbungsentwurf", summary)

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
            self.assertEqual(requests[1].get_header("User-agent"), "MietRadar/1.0")
            self.assertEqual(json.loads(requests[1].data)["to"], ["me@example.com"])


if __name__ == "__main__":
    unittest.main()
