import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from selenium.common.exceptions import NoSuchElementException, SessionNotCreatedException, TimeoutException
from selenium.webdriver.common.by import By

from wg_delivery import BrowserSender, DeliveryOutcome


PUBLIC = "https://www.wg-gesucht.de/wg-zimmer-in-Muenchen.10123456.html"
CONTACT = "https://www.wg-gesucht.de/nachricht-senden/wg-zimmer-in-Muenchen.10123456.html"


def element(label="", href=None):
    item = Mock(text=label)
    item.is_displayed.return_value = True
    item.is_enabled.return_value = True
    item.get_attribute.side_effect = lambda attribute: href if attribute == "href" else None
    return item


class BrowserStartupTest(unittest.TestCase):
    def test_sandbox_probe_only_reports_the_known_failure(self):
        def fail_probe(command, *, stderr, **_kwargs):
            self.assertEqual(command[-1], "about:blank")
            self.assertNotIn("--no-sandbox", command)
            self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in command))
            stderr.write(b"Failed to move to new namespace: Operation not permitted\nprivate-driver-output")
            stderr.flush()
            process = MagicMock()
            process.__enter__.return_value.wait.return_value = 1
            return process

        with TemporaryDirectory() as directory, patch("wg_delivery.sys.platform", "linux"), \
                patch.dict("wg_delivery.os.environ", {"CHROME_BIN": "/usr/bin/chromium"}), \
                patch("wg_delivery.webdriver.Chrome", side_effect=SessionNotCreatedException("private-exception")), \
                patch("wg_delivery.subprocess.Popen", side_effect=fail_probe):
            sender = BrowserSender(Path(directory) / "profile")
            reason = sender.startup_problem()

        self.assertEqual(reason, "Chromium kann seine Sandbox in dieser Laufzeitumgebung nicht starten.")

    def test_successful_start_does_not_probe_or_visit_a_page(self):
        with TemporaryDirectory() as directory, patch("wg_delivery.webdriver.Chrome") as chrome, \
                patch("wg_delivery.subprocess.Popen") as probe:
            sender = BrowserSender(Path(directory) / "profile")
            self.assertIsNone(sender.startup_problem())
            chrome.return_value.get.assert_not_called()
            probe.assert_not_called()
            sender.close()


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.sender = BrowserSender(Path("unused-profile"))
        self.driver = Mock(current_url=PUBLIC, title="WG-Gesucht")
        self.sender._driver = self.driver
        self.link = element("Nachricht senden", CONTACT)
        self.link.click.side_effect = lambda: setattr(self.driver, "current_url", CONTACT)
        self.area = element()
        self.area.send_keys.side_effect = self.compose
        self.area.find_element.side_effect = NoSuchElementException()
        self.submit = element("Senden")
        self.file_input = None
        self.upload_error = None
        self.upload_preview = None
        self.attached_name = None
        self.reported_attachment = False
        self.timestamp = element("Heute, 10:00")
        self.confirmed = False
        self.receipt = None
        self.new_conversation = element()
        self.blocked = False
        self.driver.find_elements.side_effect = self.find_elements
        self.wait = Mock()
        self.wait.until.side_effect = self.evaluate
        self.web_wait = patch("wg_delivery.WebDriverWait", return_value=self.wait)
        self.web_wait.start()
        self.addCleanup(self.web_wait.stop)

    def compose(self, text):
        self.area.get_property.return_value = text

    def evaluate(self, predicate):
        if value := predicate(self.driver):
            return value
        raise TimeoutException()

    def find_elements(self, by, selector):
        if by == By.XPATH:
            return [element(self.attached_name)] if self.confirmed and self.reported_attachment else []
        assert by == By.CSS_SELECTOR
        if "captcha" in selector:
            return [element()] if self.blocked else []
        if selector == "a[href*='/nachricht-senden/']":
            return [self.link]
        if selector == "#message_timestamp":
            return [self.timestamp] if self.confirmed else []
        if selector == "#message_successfully_sent":
            return [self.receipt] if self.receipt else []
        if selector == "#start_new_conversation":
            return [self.new_conversation] if self.new_conversation else []
        if selector == "textarea#message_input":
            return [self.area]
        if selector == "#messenger_form input#file_input[type='file']":
            return [self.file_input] if self.file_input else []
        if selector == "#attachments_modal_content #validation-error-placeholder":
            return [self.upload_error] if self.upload_error else []
        if selector == "#messenger_form .message_input_wrapper.pre_attached_file":
            return [self.upload_preview] if self.upload_preview else []
        if selector.startswith("button[data-ng-click"):
            return [self.submit]
        return []

    def test_checks_eligibility_immediately_before_click_and_confirms_delivery(self):
        call_order = []
        def compose(text):
            self.compose(text)
            call_order.append("compose")
        self.area.send_keys.side_effect = compose

        def claim():
            call_order.append("claim")
            return True

        def click():
            call_order.append("click")
            self.confirmed = True

        self.submit.click.side_effect = click
        self.assertIs(self.sender.send(PUBLIC, "Hallo, ich bin interessiert.", claim), DeliveryOutcome.SENT)
        self.assertEqual(call_order, ["compose", "claim", "click"])

    def test_expired_or_claimed_elsewhere_does_not_click(self):
        self.assertIs(self.sender.send(PUBLIC, "Hallo", lambda: False), DeliveryOutcome.SKIPPED)
        self.submit.click.assert_not_called()

    def test_changed_browser_text_is_never_submitted(self):
        self.area.send_keys.side_effect = lambda text: self.compose(text[:4])
        claim = Mock()
        self.assertIs(self.sender.send(PUBLIC, "Hallo zusammen!", claim), DeliveryOutcome.BLOCKED)
        claim.assert_not_called()
        self.submit.click.assert_not_called()

    def test_already_contacted_listing_does_not_compose_or_click(self):
        self.confirmed = True
        claim = Mock()
        self.assertIs(self.sender.send(PUBLIC, "Hallo", claim), DeliveryOutcome.ALREADY_CONTACTED)
        self.area.send_keys.assert_not_called()
        self.submit.click.assert_not_called()
        claim.assert_not_called()

    def test_unknown_delivery_is_not_recorded_as_success(self):
        self.assertIs(self.sender.send(PUBLIC, "Hallo", lambda: True), DeliveryOutcome.UNCONFIRMED)
        self.submit.click.assert_called_once_with()

    def test_security_prompt_blocks_before_claim(self):
        self.blocked = True
        claim = Mock()
        self.assertIs(self.sender.send(PUBLIC, "Hallo", claim), DeliveryOutcome.BLOCKED)
        self.link.click.assert_not_called()
        self.submit.click.assert_not_called()
        claim.assert_not_called()

    def test_different_listing_contact_is_ignored(self):
        self.link.get_attribute.return_value = None
        self.link.get_attribute.side_effect = lambda name: CONTACT.replace("10123456", "10123457")
        claim = Mock()
        self.assertIs(self.sender.send(PUBLIC, "Hallo", claim), DeliveryOutcome.UNAVAILABLE)
        self.link.click.assert_not_called()
        claim.assert_not_called()

    def prepare_attachment(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "Nachweis.pdf"
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.file_input = element()
        self.file_input.get_attribute.side_effect = lambda name: "application/pdf" if name == "accept" else None
        self.file_input.send_keys.side_effect = lambda _path: self.show_upload_preview(path.name)
        self.attached_name = path.name
        return path

    def show_upload_preview(self, filename):
        preview = element()
        filename_element = element(filename)
        children = {
            (By.CSS_SELECTOR, 'input[name="message_type"][value="attached_file"]'): [element()],
            (By.CSS_SELECTOR, "b"): [filename_element],
        }
        preview.find_elements.side_effect = lambda by, selector: children.get((by, selector), [])
        self.upload_preview = preview

    def test_reserves_before_upload_and_checks_file_in_sent_conversation(self):
        attachment = self.prepare_attachment()
        calls = []

        def select_file(path):
            calls.append(("attach", path))
            self.show_upload_preview(attachment.name)

        self.file_input.send_keys.side_effect = select_file

        def reserve():
            calls.append(("reserve", None))
            return True

        def click():
            calls.append(("send", None))
            self.confirmed = self.reported_attachment = True

        self.submit.click.side_effect = click
        result = self.sender.send(PUBLIC, "Hallo", reserve, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.SENT)
        self.assertEqual(calls, [("reserve", None), ("attach", str(attachment.resolve())), ("send", None)])

    def test_revoked_approval_keeps_the_pdf_with_the_worker(self):
        attachment = self.prepare_attachment()
        self.assertIs(self.sender.send(PUBLIC, "Hallo", lambda: False, attachment=attachment), DeliveryOutcome.SKIPPED)
        self.file_input.send_keys.assert_not_called()
        self.submit.click.assert_not_called()

    def test_native_file_selection_without_upload_preview_does_not_send(self):
        attachment = self.prepare_attachment()

        def select_file(_path):
            self.driver.execute_script.return_value = [[attachment.name, attachment.stat().st_size]]

        self.file_input.send_keys.side_effect = select_file
        result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.UNCONFIRMED)
        self.file_input.send_keys.assert_called_once_with(str(attachment.resolve()))
        self.submit.click.assert_not_called()

    def test_upload_preview_with_a_different_filename_does_not_send(self):
        attachment = self.prepare_attachment()
        self.file_input.send_keys.side_effect = lambda _path: self.show_upload_preview("Anderer Nachweis.pdf")
        result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.UNCONFIRMED)
        self.submit.click.assert_not_called()

    def test_hidden_upload_error_blocks_submission_and_logs_safe_reason(self):
        attachment = self.prepare_attachment()
        error = element()
        error.is_displayed.return_value = False
        error.get_property.return_value = "Eine Datei mit demselben Namen ist in Ihrer Bibliothek bereits vorhanden."
        self.file_input.send_keys.side_effect = lambda _path: setattr(self, "upload_error", error)
        with self.assertLogs("wg_delivery", level="INFO") as logs:
            result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.UNCONFIRMED)
        self.assertIn("bereits vorhandenen Dateinamen", logs.output[0])
        self.submit.click.assert_not_called()

    def test_confirmation_requires_the_attachment_as_well_as_the_message(self):
        attachment = self.prepare_attachment()
        self.submit.click.side_effect = lambda: setattr(self, "confirmed", True)
        result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.UNCONFIRMED)
        self.submit.click.assert_called_once_with()

    def test_confirms_approved_pdf_with_success_receipt_and_hidden_composer(self):
        attachment = self.prepare_attachment()

        def click():
            self.receipt = element("Deine Nachricht wurde erfolgreich gesendet")
            self.new_conversation.is_displayed.return_value = False
            self.upload_preview = None

        self.submit.click.side_effect = click
        result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.SENT)
        self.submit.click.assert_called_once_with()

    def test_success_receipt_with_visible_composer_is_unconfirmed(self):
        attachment = self.prepare_attachment()
        self.submit.click.side_effect = lambda: setattr(self, "receipt", element("Nachricht gesendet"))
        result = self.sender.send(PUBLIC, "Hallo", lambda: True, attachment=attachment)
        self.assertIs(result, DeliveryOutcome.UNCONFIRMED)
        self.submit.click.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
