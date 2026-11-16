from __future__ import annotations

import base64
import hashlib
import stat
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from munich_delivery import DeliveryOutcome
from wg_mac_worker import Relay, RelayError, RelayTemporaryError, process_next, read_token
from wg_source import Listing, WGAccessBlocked, WGListingGone


URL = "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.123456.html"
APPROVED = "Hallo, ich interessiere mich für das Zimmer."
JOB = {"id": "123456", "revision": "1" * 64}
PDF = b"%PDF-1.4\nDummy\n%%EOF\n"


class MacWorkerTests(unittest.TestCase):
    def setUp(self):
        self.relay = Mock()
        self.browser = Mock()
        self.source = Mock()
        self.source.details.return_value = Listing(
            JOB["id"], URL, "WG-Zimmer", "Ein ruhiges Zimmer.",
            datetime(2026, 11, 16, 13, tzinfo=timezone.utc), "Teststraße 1, 80331 München",
            available_from=date(2026, 12, 1),
        )
        self.next_job = {"job": {**JOB, "url": URL}}
        self.prepared = {"ready": True, "message": APPROVED, "attachment": None}

    def test_only_submits_after_claim_and_cleans_up_approved_pdf(self):
        self.prepared["attachment"] = {
            "name": "Bonität.pdf",
            "digest": hashlib.sha256(PDF).hexdigest(),
            "content": base64.b64encode(PDF).decode("ascii"),
        }
        self.relay.post.side_effect = [
            self.next_job, self.prepared, {"claimed": True}, {"recorded": True},
        ]
        delivered = []

        def send(url, message, claim, *, attachment):
            self.assertEqual((url, message), (URL, APPROVED))
            self.assertEqual(attachment.read_bytes(), PDF)
            self.assertEqual(stat.S_IMODE(attachment.stat().st_mode), 0o600)
            self.assertTrue(claim())
            delivered.append(attachment)
            return DeliveryOutcome.SENT

        self.browser.send.side_effect = send
        self.assertTrue(process_next(self.relay, self.browser, self.source))
        self.assertFalse(delivered[0].exists())
        endpoints = [call.args[0] for call in self.relay.post.call_args_list]
        self.assertEqual(endpoints, ["next", "prepare", "claim", "finish"])
        self.assertEqual(
            self.relay.post.call_args_list[1].args[1]["listing"]["available_from"], "2026-12-01",
        )
        self.assertEqual(self.relay.post.call_args.args[1], {**JOB, "outcome": "sent"})

    def test_foreign_or_mismatched_job_url_never_reaches_the_browser(self):
        for url in (URL.replace("www.wg-gesucht.de", "example.example"), URL.replace("123456", "987654")):
            with self.subTest(url=url):
                self.relay.post.return_value = {"job": {**JOB, "url": url}}
                with self.assertRaises(RelayError):
                    process_next(self.relay, self.browser, self.source)
        self.source.details.assert_not_called()
        self.browser.send.assert_not_called()

    def test_tampered_pdf_never_reaches_the_browser(self):
        self.prepared["attachment"] = {
            "name": "Bonität.pdf", "digest": "0" * 64,
            "content": base64.b64encode(PDF).decode("ascii"),
        }
        self.relay.post.side_effect = [self.next_job, self.prepared]
        with self.assertRaisesRegex(RelayError, "PDF-Anhang"):
            process_next(self.relay, self.browser, self.source)
        self.browser.send.assert_not_called()

    def test_interrupted_claim_never_permits_submission_or_overwrites_status(self):
        self.relay.post.side_effect = [
            self.next_job, self.prepared, RelayTemporaryError("Verbindung verloren"),
        ]

        def send(_url, _message, claim, **_kwargs):
            self.assertFalse(claim())
            return DeliveryOutcome.SKIPPED

        self.browser.send.side_effect = send
        with self.assertRaisesRegex(RelayError, "Versandreservierung"):
            process_next(self.relay, self.browser, self.source)
        self.assertEqual([call.args[0] for call in self.relay.post.call_args_list], ["next", "prepare", "claim"])

    def test_withdrawn_approval_prevents_submission(self):
        self.relay.post.side_effect = [
            self.next_job, self.prepared, {"claimed": False}, {"recorded": False},
        ]

        def send(_url, _message, claim, **_kwargs):
            self.assertFalse(claim())
            return DeliveryOutcome.SKIPPED

        self.browser.send.side_effect = send
        self.assertTrue(process_next(self.relay, self.browser, self.source))
        self.assertEqual(self.relay.post.call_args.args[1], {**JOB, "outcome": "skipped"})

    def test_only_confirmed_gone_listing_is_reported_unavailable(self):
        for error, outcome in ((WGListingGone("gone"), "unavailable"), (WGAccessBlocked("blocked"), "skipped")):
            with self.subTest(outcome=outcome):
                self.relay.post.side_effect = [self.next_job, {"recorded": True}]
                self.source.details.side_effect = error
                if outcome == "unavailable":
                    self.assertTrue(process_next(self.relay, self.browser, self.source))
                else:
                    with self.assertRaises(RelayTemporaryError):
                        process_next(self.relay, self.browser, self.source)
                self.relay.post.assert_called_with("finish", {**JOB, "outcome": outcome})
        self.browser.send.assert_not_called()

    def test_token_file_is_private_and_endpoint_uses_https(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "token"
            filename.write_text("a" * 43)
            filename.chmod(0o644)
            with self.assertRaises(RelayError):
                read_token(filename)
            filename.chmod(0o600)
            self.assertEqual(read_token(filename), "a" * 43)
        for url in ("http://mietradar.example", "https://mietradar.example/api", "https://user@mietradar.example"):
            with self.subTest(url=url), self.assertRaises(RelayError):
                Relay(url, "a" * 43)


if __name__ == "__main__":
    unittest.main()
