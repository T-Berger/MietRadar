import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from kleinanzeigen_activity import Activity, EmailNotifier, EmailOutcome, install_activity, record_activity, record_replies
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
            rejected = HTTPError("https://api.resend.com/emails", 429, "Rate limited", {}, io.BytesIO(b'{}'))
            with (patch("kleinanzeigen_activity.urlopen", side_effect=[rejected, io.BytesIO(b'{"id":"accepted"}')]) as fetch,
                  self.assertLogs("kleinanzeigen_activity")):
                self.assertEqual(notifier.flush(db, NOW), (0, False))
                self.assertEqual(notifier.flush(db, NOW), (1, True))
                self.assertEqual(notifier.flush(db, NOW), (0, True))
            requests = [item.args[0] for item in fetch.call_args_list]
            self.assertEqual(requests[0].get_header("Idempotency-key"), requests[1].get_header("Idempotency-key"))
            self.assertEqual(requests[1].get_header("User-agent"), "MietRadar/1.0")
            self.assertEqual(json.loads(requests[1].data)["to"], ["me@example.com"])

    def test_brevo_keeps_exact_alias_and_confirms_a_retried_email(self):
        sender = "sender@example.com"
        recipient = "me+resend@example.net"
        endpoint = "https://api.brevo.com/v3/smtp/email"
        notifier = EmailNotifier("example-key", sender, recipient, provider="brevo")
        duplicate = HTTPError(endpoint, 400, "Bad Request", {}, io.BytesIO(
            b'{"code":"duplicate_parameter","message":"Email for the idempotency key has already been processed"}'
        ))
        with closing(sqlite3.connect(":memory:")) as db:
            install_activity(db)
            Activity("match:1", "match", "1", "https://example.net/offer", "Neues Angebot",
                     "Ein Zimmer passt.", NOW.timestamp()).save(db)
            rejected = HTTPError(endpoint, 429, "Rate limited", {}, io.BytesIO(b'{}'))
            with (patch("kleinanzeigen_activity.urlopen", side_effect=[rejected, duplicate]) as fetch,
                  self.assertLogs("kleinanzeigen_activity")):
                self.assertEqual(notifier.flush(db, NOW), (0, False))
                self.assertEqual(notifier.flush(db, NOW), (1, True))
                self.assertEqual(notifier.flush(db, NOW), (0, True))
            first, second = [call.args[0] for call in fetch.call_args_list]
            payload = json.loads(second.data)
            self.assertEqual(second.full_url, endpoint)
            self.assertEqual(second.get_header("Api-key"), "example-key")
            self.assertEqual(json.loads(first.data)["headers"], payload["headers"])
            self.assertEqual(payload["sender"], {"name": "MietRadar", "email": sender})
            self.assertEqual(payload["to"], [{"email": recipient}])
            self.assertEqual(payload["textContent"], "Ein Zimmer passt.\n\nhttps://example.net/offer")

    def test_brevo_only_accepts_provider_message_id(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net", provider="brevo")
        with patch("kleinanzeigen_activity.urlopen", side_effect=[io.BytesIO(b'{"id":"wrong"}'),
                                                                io.BytesIO(b'{"messageId":"accepted"}')]):
            self.assertIs(notifier._send("match:1", "Neues Angebot", "Ein Zimmer passt.", ""), EmailOutcome.UNCERTAIN)
            self.assertIs(notifier._send("match:1", "Neues Angebot", "Ein Zimmer passt.", ""), EmailOutcome.ACCEPTED)

    def test_email_rejection_logs_only_safe_diagnostics(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net", provider="brevo")
        endpoint = "https://api.brevo.com/v3/smtp/email"
        for status, code, message, hint in (
            (401, "unauthorized", "Unrecognised IP address 192.0.2.10", "IP-Adresse in Brevo nicht zugelassen"),
            (403, "permission_denied", "Access denied for resource", "Rechte für transaktionale E-Mails"),
            (400, "invalid_parameter", "Sender is not verified", "Bestätigte Absenderadresse"),
            (401, "unauthorized", "Key not found", "API-Schlüssel und Zugriff"),
            (403, "private-provider-value", "Arbitrary provider message", "Einstellungen beim Anbieter prüfen"),
        ):
            with self.subTest(status=status, code=code):
                body = json.dumps({"code": code, "message": f"{message}: sender@example.com example-key"}).encode()
                error = HTTPError(endpoint, status, "Rejected", {}, io.BytesIO(body))
                with patch("kleinanzeigen_activity.urlopen", side_effect=error), self.assertLogs("kleinanzeigen_activity") as logs:
                    self.assertIs(notifier._send("match:1", "Neues Angebot", "Ein Zimmer passt.", ""), EmailOutcome.REJECTED)
                self.assertIn(f"Brevo hat die E-Mail abgelehnt (HTTP {status})", logs.output[0])
                self.assertIn(hint, logs.output[0])
                for private_value in (message, "sender@example.com", "example-key", "private-provider-value"):
                    self.assertNotIn(private_value, logs.output[0])

    def test_one_listing_email_prefers_the_match_and_leaves_real_replies_separate(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net")
        with closing(sqlite3.connect(":memory:")) as db:
            install_activity(db)
            for event_id, kind, listing_id in (
                ("review:ka-1", "review", "ka-1"), ("match:ka-1", "match", "ka-1"),
                ("delivery:sent:ka-1", "delivery", "ka-1"), ("reply:1", "reply", "ka-1"),
                ("match:1", "match", "1"),
            ):
                Activity(event_id, kind, listing_id, "", kind, "Text", NOW.timestamp()).save(db)
            with patch.object(EmailNotifier, "_send", return_value=EmailOutcome.ACCEPTED) as send:
                self.assertEqual(notifier.flush(db, NOW), (3, True))
                self.assertEqual(notifier.flush(db, NOW + timedelta(hours=1)), (0, True))
            self.assertEqual({call.args[0] for call in send.call_args_list}, {"match:ka-1", "match:1", "reply:1"})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM klein_activity").fetchone(), (5,))

    def test_previous_delivery_notice_still_blocks_after_activity_cleanup(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net")
        with closing(sqlite3.connect(":memory:")) as db:
            install_activity(db)
            earlier = (NOW - timedelta(days=28)).timestamp()
            Activity("delivery:sent:ka-1", "delivery", "ka-1", "", "Gesendet", "Text", earlier).save(db)
            with db:
                db.execute("UPDATE klein_activity SET emailed_at = ?", (earlier,))
            install_activity(db)
            with db:
                db.execute("DELETE FROM klein_activity")
            Activity("match:ka-1", "match", "ka-1", "https://example.net/new-slug", "Treffer", "Text",
                     NOW.timestamp()).save(db)
            with patch.object(EmailNotifier, "_send") as send:
                self.assertEqual(notifier.flush(db, NOW), (0, True))
            send.assert_not_called()

    def test_uncertain_provider_response_is_not_retried_either_in_this_run_or_later(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net", provider="brevo")
        with closing(sqlite3.connect(":memory:")) as db:
            install_activity(db)
            Activity("match:1", "match", "1", "", "Treffer", "Text", NOW.timestamp()).save(db)
            with (patch("kleinanzeigen_activity.urlopen", side_effect=URLError("timeout")) as fetch,
                  self.assertLogs("kleinanzeigen_activity")):
                self.assertEqual(notifier.flush(db, NOW), (0, False))
                self.assertEqual(notifier.flush(db, NOW + timedelta(hours=1)), (0, True))
            fetch.assert_called_once()
            self.assertEqual(db.execute("SELECT emailed_at FROM klein_activity").fetchone(), (None,))

    def test_other_worker_can_not_send_a_listing_with_an_in_flight_email(self):
        notifier = EmailNotifier("example-key", "sender@example.com", "me@example.net")
        in_flight = Event()
        allow_return = Event()

        def send(*args):
            in_flight.set()
            self.assertTrue(allow_return.wait(3))
            return EmailOutcome.ACCEPTED

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                install_activity(db)
                Activity("match:1", "match", "1", "", "Treffer", "Text", NOW.timestamp()).save(db)

            def flush():
                with closing(sqlite3.connect(path)) as db:
                    return notifier.flush(db, NOW)

            with patch.object(EmailNotifier, "_send", side_effect=send) as provider, ThreadPoolExecutor() as pool:
                first = pool.submit(flush)
                try:
                    self.assertTrue(in_flight.wait(3))
                    self.assertEqual(flush(), (0, True))
                finally:
                    allow_return.set()
                self.assertEqual(first.result(), (1, True))
            provider.assert_called_once()


if __name__ == "__main__":
    unittest.main()
