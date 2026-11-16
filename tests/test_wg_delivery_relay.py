import base64
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from munich_radar import Scanner, _database
from outgoing_judge import OutgoingJudgment
from reviewed_applications import cancel_application_review, review_application, save_application_attachment
from test_munich_radar import NOW, offer
from wg_delivery_relay import PREPARATION_AGE, ReviewedDeliveryRelay, _RelayServer


TOKEN = "a1" * 32
MESSAGE = "Hallo zusammen,\n\nich bin Sam. Kann ich vorbeikommen?\n\nViele Grüße\nSam"
PDF = b"%PDF-1.4\nEin Testanhang\n%%EOF\n"


class ReviewedDeliveryRelayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "radar.sqlite3"
        self.db = _database(self.path)
        self.addCleanup(self.db.close)
        self.now = NOW
        self.listing = offer(1, 1)
        self.scanner = Mock(spec=Scanner)
        self.scanner.max_age = timedelta(hours=24)
        self.scanner.outgoing_provider = "mistral"
        self.scanner.validate_reviewed_listing.return_value = True
        self.relay = self.new_relay()

    def new_relay(self):
        return ReviewedDeliveryRelay(self.path, self.scanner, TOKEN, 3, clock=lambda: self.now)

    def draft(self, *, approve=True, attachment=False):
        with self.db:
            self.db.execute("INSERT INTO drafts VALUES (?, ?, ?, 'veröffentlicht', ?, 0)",
                            (self.listing.id, self.listing.url, self.listing.published_at.timestamp(), MESSAGE))
        document = save_application_attachment(self.path, filename="Test.pdf", content=PDF) if attachment else None
        if approve:
            review_application(self.path, self.listing.id, MESSAGE, MESSAGE, approve=True, now=self.now,
                               max_age=self.scanner.max_age, attachment_digest=document.digest if document else None)
        return document

    def job(self):
        result = self.relay.dispatch("/v1/next", {})["job"]
        return {key: result[key] for key in ("id", "revision")} if result else None

    def public_listing(self):
        return {
            "title": self.listing.title, "description": self.listing.description, "address": self.listing.address,
            "wanted": None, "wg_type": None, "resident_ages": None,
            "published_at": self.listing.published_at.isoformat(), "published_day": None,
            "available_from": None, "available_until": None,
        }

    def prepare(self, job):
        with patch("munich_delivery.judge_outgoing", return_value=OutgoingJudgment(True, "ok")) as judge:
            result = self.relay.dispatch("/v1/prepare", {**job, "listing": self.public_listing()})
        return result, judge

    def test_authentication_and_next_never_return_unapproved_message_text(self):
        self.draft(approve=False)
        server = _RelayServer(("127.0.0.1", 0), self.relay)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(f"{url}/v1/status", data=b"{}"), timeout=2)
        self.assertEqual(error.exception.code, 401)
        with closing(error.exception):
            self.assertEqual(json.load(error.exception), {"error": "unauthorized"})
        with urlopen(Request(f"{url}/v1/next", data=b"{}", headers={"Authorization": f"Bearer {TOKEN}"}), timeout=2) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(json.load(response), {"job": None})

    def test_prepared_payload_is_exact_and_only_one_claim_can_win(self):
        document = self.draft(attachment=True)
        next_result = self.relay.dispatch("/v1/next", {})["job"]
        self.assertEqual(set(next_result), {"id", "url", "revision"})
        self.assertEqual(next_result["url"], self.listing.url)
        job = {key: next_result[key] for key in ("id", "revision")}
        self.assertEqual(self.relay.dispatch("/v1/claim", job), {"claimed": False})
        result, judge = self.prepare(job)
        self.assertEqual(result, {
            "ready": True, "message": MESSAGE,
            "attachment": {"name": "Test.pdf", "digest": document.digest, "content": base64.b64encode(PDF).decode()},
        })
        self.assertEqual(judge.call_args.args, (MESSAGE,))
        self.assertEqual(judge.call_args.kwargs["provider"], "mistral")
        self.assertTrue(judge.call_args.kwargs["user_reviewed"])
        self.scanner.validate_reviewed_listing.assert_called_once()
        rival = self.new_relay()
        start = Barrier(2)

        def claim(relay):
            start.wait(timeout=5)
            return relay.dispatch("/v1/claim", job)["claimed"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim, (self.relay, rival))), [False, True])
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("unconfirmed",))
        result = {"recorded": True}
        done = {**job, "outcome": "sent"}
        self.assertEqual(self.relay.dispatch("/v1/finish", done), result)
        self.assertEqual(rival.dispatch("/v1/finish", done), result)
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("sent",))
        self.assertEqual(self.db.execute("SELECT done, message FROM drafts").fetchone(), (1, None))
        self.assertIsNone(self.job())

    def test_replacing_approved_attachment_invalidates_claim_after_prepare(self):
        self.draft(attachment=True)
        job = self.job()
        self.assertTrue(self.prepare(job)[0]["ready"])
        save_application_attachment(self.path, filename="New.pdf", content=PDF.replace(b"Ein", b"Zwei"))
        self.assertEqual(self.relay.dispatch("/v1/claim", job), {"claimed": False})
        self.assertEqual(self.relay.dispatch("/v1/prepare", {**job, "listing": self.public_listing()}), {"ready": False})
        self.assertIsNone(self.db.execute("SELECT state FROM delivery").fetchone())

    def test_withdrawal_and_expiry_prevent_claiming_an_old_preparation(self):
        self.draft()
        job = self.job()
        self.assertTrue(self.prepare(job)[0]["ready"])
        self.now += PREPARATION_AGE + timedelta(seconds=1)
        self.assertEqual(self.relay.dispatch("/v1/claim", job), {"claimed": False})
        result, judge = self.prepare(job)
        self.assertTrue(result["ready"])
        judge.assert_called_once()
        cancel_application_review(self.path, self.listing.id, MESSAGE, now=self.now)
        self.assertEqual(self.relay.dispatch("/v1/claim", job), {"claimed": False})
        self.assertIsNone(self.db.execute("SELECT state FROM delivery").fetchone())

    def test_a_lost_confirmation_survives_a_relay_restart_without_resending(self):
        self.draft()
        job = self.job()
        self.assertTrue(self.prepare(job)[0]["ready"])
        self.assertEqual(self.relay.dispatch("/v1/claim", job), {"claimed": True})
        relay = self.new_relay()
        self.assertEqual(relay.dispatch("/v1/next", {}), {"job": None})
        self.assertEqual(relay.dispatch("/v1/claim", job), {"claimed": False})
        self.assertEqual(relay.dispatch("/v1/finish", {**job, "outcome": "unconfirmed"}), {"recorded": True})
        self.assertEqual(self.db.execute("SELECT state FROM delivery").fetchone(), ("unconfirmed",))
        self.assertEqual(self.db.execute("SELECT done, message FROM drafts").fetchone(), (0, MESSAGE))


if __name__ == "__main__":
    unittest.main()
