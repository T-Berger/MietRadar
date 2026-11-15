"""Send a rental application through Kleinanzeigen's visible contact form."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from kleinanzeigen_source import _reference
from munich_delivery import DeliveryOutcome
from wg_delivery import BrowserSender, _label, _xpath_text


_INBOX_PATH = "/m-nachrichten.html"
_HOSTS = {"www.kleinanzeigen.de", "kleinanzeigen.de"}
_CONTACT = "nachricht schreiben"
_SEND = "nachricht senden"
_INBOX_LINK = "zu meinen nachrichten"
_INTERACTIVE = "button, a[href], input[type='submit'], [role='button']"
_LOGIN_OR_VERIFICATION = (
    "input[type='password'], input[autocomplete='one-time-code'], input[name='otp'], "
    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe'], "
    "iframe[src*='challenges.cloudflare.com'], iframe[title='reCAPTCHA'], iframe[title*='hCaptcha']"
)
_BLOCKED_TEXT = (
    "access denied", "zugriff verweigert", "überprüfung", "captcha", "just a moment", "sicherheitsprüfung",
)
_THREAD = "[role='log'], [aria-label='Nachrichtenverlauf'], [aria-label='Unterhaltung']"
_OUTGOING = (
    "[data-direction='outgoing'], [data-message-direction='outgoing'], "
    "[aria-label='Gesendete Nachricht'], [aria-label='Eigene Nachricht']"
)


def _page(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc.lower() in _HOSTS


def _same_listing(driver, url: str) -> bool:
    current = _reference(driver.current_url)
    return current is not None and current.url == url


def _is_inbox(url: str) -> bool:
    return _page(url) and urlsplit(url).path == _INBOX_PATH


def _visible(scope, selector: str):
    return [element for element in scope.find_elements(By.CSS_SELECTOR, selector) if element.is_displayed()]


def _enabled(element) -> bool:
    return element.is_enabled() and element.get_attribute("aria-disabled") != "true"


def _control(scope, label: str, *, require_enabled: bool = True):
    matching = [item for item in _visible(scope, _INTERACTIVE)
                if _label(item) == label and (not require_enabled or _enabled(item))]
    return matching[0] if len(matching) == 1 else None


def _on_site_control(control, attribute: str) -> bool:
    target = control.get_attribute(attribute)
    return not target or _page(target)


def _has_optional_contact_data(scope) -> bool:
    fields = _visible(scope, "input[type='tel'], input[type='email'], input[name*='phone' i], input[name*='email' i]")
    # A reusable phone/email field may include information from an unrelated
    # interaction. Only the submitted message is configured by MietRadar.
    return any(item.get_attribute("required") or item.get_attribute("value") for item in fields)


def _timestamp_after(element, submitted_at: datetime) -> bool:
    for stamp in _visible(element, "time[datetime]"):
        try:
            value = datetime.fromisoformat((stamp.get_attribute("datetime") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None and value >= submitted_at:
            return True
    return False


class KleinanzeigenBrowserSender(BrowserSender):
    """Use a browser profile that the user authenticated manually."""

    @staticmethod
    def _blocked(driver) -> bool:
        return (
            not _page(driver.current_url)
            or urlsplit(driver.current_url).path == "/m-einloggen.html"
            or any(text in driver.title.casefold() for text in _BLOCKED_TEXT)
            or bool(_visible(driver, _LOGIN_OR_VERIFICATION))
        )

    @staticmethod
    def _unavailable(driver) -> bool:
        return any(_label(heading).startswith(("gelöscht", "die gewünschte anzeige ist nicht mehr verfügbar"))
                   for heading in _visible(driver, "h1, [role='alert']"))

    def _find_contact(self, driver, url: str):
        if self._blocked(driver):
            return DeliveryOutcome.BLOCKED
        if self._unavailable(driver):
            return DeliveryOutcome.UNAVAILABLE
        if not _same_listing(driver, url):
            return DeliveryOutcome.BLOCKED
        return _control(driver, _CONTACT)

    def _find_composer(self, driver, url: str):
        if self._blocked(driver) or not _same_listing(driver, url):
            return DeliveryOutcome.BLOCKED
        if self._unavailable(driver):
            return DeliveryOutcome.UNAVAILABLE

        matching = []
        for area in _visible(driver, "textarea"):
            if not area.is_enabled() or area.get_attribute("readonly"):
                continue
            for scope in area.find_elements(By.XPATH, "./ancestor::*[self::form or self::dialog or @role='dialog']"):
                if submit := _control(scope, _SEND, require_enabled=False):
                    if _on_site_control(scope, "action") and _on_site_control(submit, "formaction"):
                        matching.append((area, submit, scope))
                    break
        if len(matching) == 1:
            area, submit, scope = matching[0]
            return DeliveryOutcome.BLOCKED if _has_optional_contact_data(scope) else (area, submit)
        return None

    def _confirmed(self, driver, url: str, text: str, submitted_at: datetime) -> bool:
        if self._blocked(driver) or not _is_inbox(driver.current_url):
            return False

        exact = _xpath_text(" ".join(text.split()))
        for thread in _visible(driver, _THREAD):
            # The conversation must visibly identify the same listing. A copy
            # of the text in an inbox preview or another thread does not count.
            if not any((reference := _reference(link.get_attribute("href") or "")) and reference.url == url
                       for link in _visible(thread, "a[href]")):
                continue
            for message in _visible(thread, _OUTGOING):
                matching_body = any(item.is_displayed() for item in message.find_elements(
                    By.XPATH, f".//*[not(self::time) and not(self::textarea) and normalize-space(.)={exact}]",
                ))
                if matching_body and _timestamp_after(message, submitted_at):
                    return True
        return False

    def send(self, url: str, text: str, before_submit: Callable[[], bool]) -> DeliveryOutcome:
        listing = _reference(url)
        if listing is None or not text.strip():
            return DeliveryOutcome.UNAVAILABLE
        claimed = False
        try:
            driver = self._browser()
            wait = WebDriverWait(driver, 12)
            driver.get(listing.url)
            contact = wait.until(lambda browser: self._find_contact(browser, listing.url))
            if isinstance(contact, DeliveryOutcome):
                return contact
            if not _on_site_control(contact, "href"):
                return DeliveryOutcome.BLOCKED
            contact.click()

            composer = wait.until(lambda browser: self._find_composer(browser, listing.url))
            if isinstance(composer, DeliveryOutcome):
                return composer
            area, _ = composer
            area.clear()
            area.send_keys(text)
            def filled_composer(browser):
                current = self._find_composer(browser, listing.url)
                if isinstance(current, DeliveryOutcome):
                    return current
                if current and current[0].get_attribute("value") == text and _enabled(current[1]):
                    return current
                return None

            composer = wait.until(filled_composer)
            if isinstance(composer, DeliveryOutcome):
                return composer
            if not before_submit():
                return DeliveryOutcome.SKIPPED

            claimed = True
            submitted_at = datetime.now(timezone.utc).replace(microsecond=0)
            composer[1].click()

            def inbox_or_link(browser):
                if self._blocked(browser):
                    return DeliveryOutcome.BLOCKED
                if _is_inbox(browser.current_url):
                    return True
                return _control(browser, _INBOX_LINK) if _same_listing(browser, listing.url) else None

            inbox = wait.until(inbox_or_link)
            if inbox is DeliveryOutcome.BLOCKED:
                return DeliveryOutcome.UNCONFIRMED
            if inbox is not True:
                if not _is_inbox(inbox.get_attribute("href") or ""):
                    return DeliveryOutcome.UNCONFIRMED
                inbox.click()
            wait.until(lambda browser: self._blocked(browser) or self._confirmed(browser, listing.url, text, submitted_at))
            return DeliveryOutcome.SENT if self._confirmed(driver, listing.url, text, submitted_at) else DeliveryOutcome.UNCONFIRMED
        except (OSError, WebDriverException):
            return DeliveryOutcome.UNCONFIRMED if claimed else DeliveryOutcome.BLOCKED
