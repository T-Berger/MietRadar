from __future__ import annotations

import base64
import hashlib
import stat
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from selenium.common.exceptions import TimeoutException

from munich_delivery import DeliveryOutcome
from wg_mac_worker import (
    Relay, RelayError, RelayTemporaryError, UnconfirmedDelivery, _check_browser, _inbox_updates, _poll_jobs,
    _read_inbox_in_temporary_tab, _write_attachment, process_next, read_token,
)
from wg_reply_inbox import WGInboxEntry, WGInboxError
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

    def test_repeated_uploads_use_distinct_generic_names_for_the_approved_pdf(self):
        attachment = {
            "name": "Bewerberin Änne Muster (vertraulich).pdf",
            "digest": hashlib.sha256(PDF).hexdigest(),
            "content": base64.b64encode(PDF).decode("ascii"),
        }
        with tempfile.TemporaryDirectory() as directory:
            first = _write_attachment(attachment, Path(directory))
            second = _write_attachment(attachment, Path(directory))

            self.assertNotEqual(first, second)
            for staged in (first, second):
                self.assertRegex(staged.name, r"^Bonitaetsnachweis_[0-9a-f]{16}\.pdf$")
                self.assertEqual(staged.read_bytes(), PDF)

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

    def test_unconfirmed_delivery_can_only_continue_after_railway_records_outcome(self):
        def send(_url, _message, claim, **_kwargs):
            self.assertTrue(claim())
            return DeliveryOutcome.UNCONFIRMED

        self.browser.send.side_effect = send
        self.relay.post.side_effect = [
            self.next_job, self.prepared, {"claimed": True}, {"recorded": True},
        ]
        with self.assertRaises(UnconfirmedDelivery):
            process_next(self.relay, self.browser, self.source)
        self.assertEqual(self.relay.post.call_args.args[1], {**JOB, "outcome": "unconfirmed"})
        self.relay.post.side_effect = [
            self.next_job, self.prepared, {"claimed": True}, {"recorded": False},
        ]
        with self.assertRaises(RelayError) as error:
            process_next(self.relay, self.browser, self.source)
        self.assertNotIsInstance(error.exception, UnconfirmedDelivery)

    def test_visible_worker_checks_inbox_in_a_new_tab_before_polling_again(self):
        driver = self.browser._browser.return_value
        with (patch("wg_mac_worker.process_next") as process,
              patch("wg_mac_worker._check_browser") as check,
              self.assertLogs("wg_mac_worker", level="WARNING")):
            def next_poll(*_args):
                if process.call_count == 1:
                    raise UnconfirmedDelivery("Versand unbestätigt")
                driver.switch_to.new_window.assert_called_once_with("tab")
                check.assert_called_once_with(self.browser, manual=True, keep_waiting=True)
                raise KeyboardInterrupt

            process.side_effect = next_poll
            with self.assertRaises(KeyboardInterrupt):
                _poll_jobs(self.relay, self.browser, visible=True)
        self.assertEqual(process.call_count, 2)
        driver.close.assert_not_called()

    def test_headless_worker_stops_after_unconfirmed_delivery(self):
        with patch("wg_mac_worker.process_next", side_effect=UnconfirmedDelivery("Versand unbestätigt")) as process:
            with self.assertRaises(UnconfirmedDelivery):
                _poll_jobs(self.relay, self.browser, visible=False)
        process.assert_called_once()
        self.browser._browser.assert_not_called()

    def test_inbox_read_restores_work_tab_without_touching_an_unconfirmed_tab(self):
        driver = self.browser._browser.return_value
        driver.current_window_handle = "work"
        driver.window_handles = ["work", "unconfirmed"]

        def new_window(_kind):
            driver.window_handles.append("inbox")
            driver.current_window_handle = "inbox"

        def switch_window(window):
            driver.current_window_handle = window

        def close_current():
            self.assertEqual(driver.current_window_handle, "inbox")
            driver.window_handles.remove("inbox")

        driver.switch_to.new_window.side_effect = new_window
        driver.switch_to.window.side_effect = switch_window
        driver.close.side_effect = close_current
        with patch("wg_mac_worker.recent_wg_messages", side_effect=WGInboxError("Postfach nicht erreichbar")):
            with self.assertRaises(WGInboxError):
                _read_inbox_in_temporary_tab(self.browser)
        self.assertEqual(driver.current_window_handle, "work")
        self.assertEqual(driver.window_handles, ["work", "unconfirmed"])
        self.browser.send.assert_not_called()

    def test_inbox_payload_contains_no_url_and_distinguishes_outgoing_messages(self):
        replies = (WGInboxEntry("2", "https://www.wg-gesucht.de/", "3", "Sonntag passt."),
                   WGInboxEntry("4", "https://www.wg-gesucht.de/", "5", None))
        with patch("wg_mac_worker._read_inbox_in_temporary_tab", return_value=replies):
            self.assertEqual(_inbox_updates(self.browser), [
                {"entries": [{"conversation_id": "2", "message_id": "3", "text": "Sonntag passt."}]},
                {"entries": [{"conversation_id": "4", "message_id": "5", "text": None}]},
            ])

    def test_worker_prioritizes_delivery_and_later_refreshes_inbox_capability(self):
        events = []

        def next_job(*_args):
            events.append("job")
            if len(events) == 1:
                return True  # An application is ready before the inbox becomes available.
            if len(events) < 4:
                return False
            raise KeyboardInterrupt

        def read_inbox(_sender):
            events.append("read inbox")
            return [{"entries": []}]

        self.relay.post.side_effect = [{"ok": True, "inbox": True}, {"ok": True}]
        with (patch("wg_mac_worker.process_next", side_effect=next_job),
              patch("wg_mac_worker._inbox_updates", side_effect=read_inbox),
              patch("wg_mac_worker.time.monotonic", side_effect=[0, 0, 301, 301]),
              patch("wg_mac_worker.time.sleep"), self.assertLogs("wg_mac_worker", level="INFO") as logs):
            with self.assertRaises(KeyboardInterrupt):
                _poll_jobs(self.relay, self.browser, visible=True, inbox_enabled=False)
        self.assertEqual(events[:4], ["job", "job", "job", "read inbox"])
        self.assertEqual(self.relay.post.call_args_list, [call("status", {}), call("inbox", {"entries": []})])
        self.assertIn("Postfach mit Railway abgeglichen", " ".join(logs.output))

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

    def test_visible_run_keeps_waiting_in_the_same_browser_until_login_completes(self):
        self.browser.startup_problem.return_value = None
        driver = self.browser._browser.return_value
        with (patch("wg_mac_worker._inbox_state", return_value=("login", [])),
              patch("wg_mac_worker.WebDriverWait") as waiter, patch("builtins.print")):
            waiter.return_value.until.side_effect = [TimeoutException(), "ready"]
            _check_browser(self.browser, manual=True, keep_waiting=True)
        self.assertEqual(waiter.return_value.until.call_count, 2)
        driver.get.assert_called_once_with("https://www.wg-gesucht.de/nachrichten.html")
        self.browser._browser.assert_called_once()
        self.browser.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
