import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from munich_radar import _database
from munich_tiers import save_secondary_offer
from reviewed_applications import (
    ApplicationReviewError,
    cancel_application_review,
    fail_application_review,
    pending_application_reviews,
    review_application,
    review_is_pending,
)


NOW = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
ID = "101"
URL = "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.101.html"
ORIGINAL = "Hallo, ich interessiere mich für euer WG-Zimmer."
EDITED = "Hi zusammen,\n\nich bin Alex und würde gern bei euch vorbeischauen.\n"


class ReviewedApplicationsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "radar.sqlite3"
        self.db = _database(self.path)
        self.addCleanup(self.db.close)
        with self.db:
            self.db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'zuerst gesehen', ?, 0)",
                            (ID, URL, NOW.timestamp(), ORIGINAL))

    def review(self, expected=ORIGINAL, message=EDITED, *, approve=True, now=NOW):
        return review_application(self.path, ID, expected, message, approve=approve, now=now, max_age=timedelta(days=3))

    def pending(self, now=NOW):
        return pending_application_reviews(self.db, now, timedelta(days=3))

    def test_one_click_saves_and_enqueues_exact_text_idempotently(self):
        with self.db:
            self.db.execute("INSERT INTO outgoing_checks VALUES (?, 'application', 'old', 1, 'ok', ?)",
                            (ID, NOW.timestamp()))
        self.assertEqual(self.review(), "queued")
        self.assertEqual(self.review(now=NOW + timedelta(minutes=1)), "queued")
        entry, = self.pending()
        self.assertEqual((entry.id, entry.url, entry.message, entry.updated_at), (ID, URL, EDITED, NOW.timestamp()))
        self.assertIsNone(self.db.execute("SELECT 1 FROM outgoing_checks WHERE id = ?", (ID,)).fetchone())
        self.assertTrue(review_is_pending(self.db, ID, EDITED, NOW, timedelta(days=1)))
        self.assertFalse(review_is_pending(self.db, ID, EDITED.strip(), NOW, timedelta(days=1)))

    def test_cancellation_disarms_queue_and_stale_draft_cannot_overwrite_edits(self):
        self.review()
        with self.assertRaises(ApplicationReviewError):
            self.review(EDITED, "Ein anderer Text", approve=False)
        cancel_application_review(self.path, ID, EDITED, now=NOW + timedelta(minutes=1))
        with self.assertRaises(ApplicationReviewError):
            self.review()
        self.assertIsNone(self.db.execute("SELECT 1 FROM application_reviews WHERE state = 'queued'").fetchone())
        self.assertEqual(self.db.execute("SELECT message FROM drafts WHERE id = ?", (ID,)).fetchone(), (EDITED,))

    def test_worker_failure_does_not_overwrite_a_later_approval(self):
        self.review()
        old, = self.pending()
        cancel_application_review(self.path, ID, EDITED, now=NOW + timedelta(minutes=1))
        self.review(EDITED, now=NOW + timedelta(minutes=2))
        fail_application_review(self.db, old, "Anzeige nicht erreichbar.", NOW + timedelta(minutes=3))
        self.assertEqual(len(self.pending(NOW + timedelta(minutes=3))), 1)

    def test_approval_caps_age_at_24_hours_and_expired_message_is_never_pending(self):
        self.review()
        self.assertEqual(len(self.pending(NOW + timedelta(days=1))), 1)
        expired_at = NOW + timedelta(days=1, seconds=1)
        self.assertEqual(self.pending(expired_at), ())
        self.assertEqual(self.db.execute("SELECT state FROM application_reviews").fetchone(), ("failed",))
        with self.assertRaisesRegex(ApplicationReviewError, "Versandzeitfenster"):
            self.review(EDITED, now=expired_at)

    def test_secondary_offer_and_delivery_reservation_block_changes(self):
        with self.db:
            save_secondary_offer(self.db, ID, URL, "WG-Zimmer", NOW, "zuerst gesehen", "b", "Ostbahnhof", 37)
        with self.assertRaises(ApplicationReviewError):
            self.review()
        with self.db:
            self.db.execute("DELETE FROM munich_secondary_offers WHERE id = ?", (ID,))
        self.review()
        with self.db:
            self.db.execute("INSERT INTO delivery VALUES (?, 'unconfirmed', ?, ?)", (ID, NOW.timestamp(), NOW.timestamp()))
        self.assertEqual(self.pending(), ())
        with self.assertRaises(ApplicationReviewError):
            cancel_application_review(self.path, ID, EDITED, now=NOW)

    def test_listing_url_must_match_the_wg_gesucht_id(self):
        for wrong_url in (URL.replace(".101.", ".102."), URL.replace("wg-gesucht.de", "kleinanzeigen.de")):
            with self.subTest(wrong_url=wrong_url):
                with self.db:
                    self.db.execute("UPDATE drafts SET url = ? WHERE id = ?", (wrong_url, ID))
                with self.assertRaisesRegex(ApplicationReviewError, "nicht mehr für diese Aktion verfügbar"):
                    self.review()


if __name__ == "__main__":
    unittest.main()
