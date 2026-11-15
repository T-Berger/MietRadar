import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from kleinanzeigen_delivery import KleinanzeigenBrowserSender
from munich_delivery import DeliveryOutcome


LISTING = "https://www.kleinanzeigen.de/s-anzeige/zimmer-in-muenchen/4212345678-199-6432"
OTHER_LISTING = LISTING.replace("4212345678", "4212345679")
INBOX = "https://www.kleinanzeigen.de/m-nachrichten.html"
TEXT = "Hallo, ich interessiere mich für das Zimmer."


def element(label="", **attributes):
    item = Mock(text=label)
    item.is_displayed.return_value = True
    item.is_enabled.return_value = True
    item.get_attribute.side_effect = attributes.get
    item.find_elements.return_value = []
    return item


class KleinanzeigenSenderTest(unittest.TestCase):
    def setUp(self):
        self.sender = KleinanzeigenBrowserSender(Path("unused-profile"))
        self.driver = Mock(current_url=LISTING, title="Zimmer in München")
        self.sender._driver = self.driver
        self.contact = element("Nachricht schreiben")
        self.scope = element()
        self.area = element()
        self.area.find_elements.return_value = [self.scope]
        self.submit = element("Nachricht senden")
        self.form_open = False
        self.contact.click.side_effect = self.open_composer
        self.scope.find_elements.side_effect = lambda by, selector: [self.submit] if selector.startswith("button,") else []
        self.draft = ""
        self.area.send_keys.side_effect = lambda text: setattr(self, "draft", text)
        self.area.get_attribute.side_effect = lambda name: self.draft if name == "value" else None
        self.submit.is_enabled.side_effect = lambda: bool(self.draft)
        self.driver.find_elements.side_effect = self.find_on_page
        self.wait = patch("kleinanzeigen_delivery.WebDriverWait", return_value=Mock(until=self.evaluate))
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def open_composer(self):
        self.form_open = True

    def evaluate(self, predicate):
        if value := predicate(self.driver):
            return value
        raise TimeoutException()

    def find_on_page(self, by, selector):
        self.assertEqual(by, By.CSS_SELECTOR)
        if selector.startswith("button,") and self.driver.current_url == LISTING:
            return [self.contact]
        if selector == "textarea" and self.form_open:
            return [self.area]
        return []

    def test_claim_happens_after_composition_and_before_single_click(self):
        def claim():
            self.assertEqual(self.draft, TEXT)
            self.submit.click.assert_not_called()
            return True

        self.submit.click.side_effect = lambda: setattr(self.driver, "current_url", INBOX)
        with patch.object(self.sender, "_confirmed", return_value=True):
            self.assertIs(self.sender.send(LISTING, TEXT, claim), DeliveryOutcome.SENT)
        self.submit.click.assert_called_once_with()

    def test_recheck_failure_does_not_submit(self):
        self.assertIs(self.sender.send(LISTING, TEXT, lambda: False), DeliveryOutcome.SKIPPED)
        self.submit.click.assert_not_called()

    def test_login_redirect_stops_before_reserving_application(self):
        self.contact.click.side_effect = lambda: setattr(
            self.driver, "current_url", "https://www.kleinanzeigen.de/m-einloggen.html",
        )
        claim = Mock()
        self.assertIs(self.sender.send(LISTING, TEXT, claim), DeliveryOutcome.BLOCKED)
        claim.assert_not_called()
        self.submit.click.assert_not_called()

    def test_different_listing_cannot_be_messaged(self):
        self.driver.get.side_effect = lambda _: setattr(self.driver, "current_url", OTHER_LISTING)
        claim = Mock()
        self.assertIs(self.sender.send(LISTING, TEXT, claim), DeliveryOutcome.BLOCKED)
        self.contact.click.assert_not_called()
        claim.assert_not_called()

    def test_prefilled_phone_blocks_the_application(self):
        private_field = element(value="089000000")
        self.scope.find_elements.side_effect = lambda by, selector: [self.submit] if selector.startswith("button,") else [private_field]
        claim = Mock()
        self.assertIs(self.sender.send(LISTING, TEXT, claim), DeliveryOutcome.BLOCKED)
        self.area.send_keys.assert_not_called()
        claim.assert_not_called()

    def test_click_without_a_confirmed_conversation_remains_uncertain(self):
        self.submit.click.side_effect = lambda: setattr(self.driver, "current_url", INBOX)
        self.assertIs(self.sender.send(LISTING, TEXT, lambda: True), DeliveryOutcome.UNCONFIRMED)
        self.submit.click.assert_called_once_with()

    def test_confirmation_requires_new_outgoing_text_and_correct_thread(self):
        self.driver.current_url = INBOX
        after = datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc)
        thread, outgoing, body = element(), element(), element(TEXT)
        link = element(href=LISTING)
        timestamp = element(datetime=after.isoformat())
        self.driver.find_elements.side_effect = lambda by, selector: [thread] if selector.startswith("[role='log']") else []
        thread.find_elements.side_effect = lambda by, selector: [link] if selector == "a[href]" else [outgoing]
        outgoing.find_elements.side_effect = lambda by, selector: [timestamp] if selector == "time[datetime]" else [body]

        self.assertTrue(self.sender._confirmed(self.driver, LISTING, TEXT, after))
        self.assertFalse(self.sender._confirmed(self.driver, OTHER_LISTING, TEXT, after))
        self.assertFalse(self.sender._confirmed(self.driver, LISTING, TEXT, after + timedelta(seconds=1)))
        outgoing.find_elements.side_effect = lambda by, selector: [timestamp] if selector == "time[datetime]" else []
        self.assertFalse(self.sender._confirmed(self.driver, LISTING, TEXT, after))


if __name__ == "__main__":
    unittest.main()
