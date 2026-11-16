"""Send one application through WG-Gesucht's normal browser controls."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory, TemporaryFile
from urllib.parse import parse_qs, urlsplit

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, SessionNotCreatedException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from munich_delivery import DeliveryOutcome
from wg_source import ListingRef


_HOSTS = {"www.wg-gesucht.de", "wg-gesucht.de"}
_CONTACT_PREFIXES = ("/nachricht-senden/", "/en/nachricht-senden/")
_CONTACT_LABELS = {"nachricht senden", "send message"}
_SEND_LABELS = _CONTACT_LABELS | {"senden", "send"}
_PDF_INPUT = "#messenger_form input#file_input[type='file']"
_PENDING_ATTACHMENTS = "#messenger_form .message_input_wrapper.pre_attached_file"
_FORM_ERROR = "#messenger_form #validation-error-placeholder, #attachments_modal_content #validation-error-placeholder"
_UPLOAD_ERROR = "#attachments_modal_content #validation-error-placeholder"
_NEW_CONVERSATION = "#start_new_conversation"
_SENT_NOTICE = "#message_successfully_sent"
_VERIFICATION = (
    "#login_two_factor_authentication_form, #registration_verification_phone_number_form, "
    "#registration_verification_code_form, #sicherheit_bestaetigung, "
    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe'], "
    "iframe[title*='hCaptcha'], iframe[src*='challenges.cloudflare.com']"
)
_BLOCKED_TITLES = ("überprüfung", "access denied", "just a moment", "zugriff verweigert", "captcha")
_BROWSER_STARTUP = "Chromium startet nicht. Prüfe Browser, ChromeDriver und das Browserprofil."
_BROWSER_SANDBOX = "Chromium kann seine Sandbox in dieser Laufzeitumgebung nicht starten."
LOG = logging.getLogger(__name__)


class BrowserStartupError(WebDriverException):
    """A safe operator-facing error without WebDriver's diagnostic output."""


def _sandbox_startup_failed(chrome: str) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        with TemporaryDirectory(prefix="mietradar-browser-check-") as profile, TemporaryFile() as stderr:
            command = [
                chrome, "--headless=new", "--disable-dev-shm-usage", "--disable-background-networking",
                "--no-first-run", f"--user-data-dir={profile}", "--dump-dom", "about:blank",
            ]
            with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True) as process:
                try:
                    if process.wait(timeout=8) == 0:
                        return False
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return False
            end = stderr.seek(0, os.SEEK_END)
            stderr.seek(max(0, end - 16384))
            output = stderr.read().lower()
    except OSError:
        return False
    return (
        b"failed to move to new namespace" in output or b"no usable sandbox" in output
        or (b"sandbox/linux/services/credentials.cc" in output and b"permission denied" in output)
    )


def _wg_page(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc.lower() in _HOSTS


def _contact_id(url: str) -> str | None:
    if not _wg_page(url):
        return None
    path = urlsplit(url).path
    for prefix in _CONTACT_PREFIXES:
        if path.startswith(prefix):
            listing = ListingRef.from_url("/" + path.removeprefix(prefix))
            return listing.id if listing else None
    return None


def _visible(scope, selector: str):
    return next((item for item in scope.find_elements(By.CSS_SELECTOR, selector) if item.is_displayed()), None)


def _label(button) -> str:
    return " ".join((button.text or button.get_attribute("value") or "").split()).casefold()


def _xpath_text(value: str) -> str:
    parts = value.split("'")
    if len(parts) == 1:
        return f"'{value}'"
    arguments = ', "\'", '.join(f"'{part}'" for part in parts)
    return f"concat({arguments})"


class BrowserSender:
    def __init__(self, profile: Path, *, headless: bool = True, email: str = "", password: str = ""):
        self.profile = profile
        self.headless = headless
        self.email = email
        self.password = password
        self._driver = None
        self._login_attempted = False

    def _browser(self):
        if self._driver is not None:
            return self._driver
        chrome = os.getenv("CHROME_BIN") or which("chromium")
        try:
            self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            options = webdriver.ChromeOptions()
            options.page_load_strategy = "eager"
            options.add_argument(f"--user-data-dir={self.profile.resolve()}")
            options.add_argument("--lang=de-DE")
            options.add_argument("--window-size=1280,900")
            if sys.platform == "darwin":
                # Reopen the dedicated profile with the same Keychain as native Chrome.
                options.add_experimental_option("excludeSwitches", ["use-mock-keychain", "password-store"])
            if self.headless:
                options.add_argument("--headless=new")
                options.add_argument("--disable-dev-shm-usage")
            if chrome:
                options.binary_location = chrome
            executable = os.getenv("CHROMEDRIVER") or which("chromedriver")
            driver = webdriver.Chrome(options=options, service=Service(executable_path=executable))
            self._driver = driver
            driver.set_page_load_timeout(25)
            return driver
        except SessionNotCreatedException:
            if chrome and self.headless and _sandbox_startup_failed(chrome):
                raise BrowserStartupError(_BROWSER_SANDBOX) from None
            raise BrowserStartupError(_BROWSER_STARTUP) from None
        except (OSError, WebDriverException):
            self.close()
            raise BrowserStartupError(_BROWSER_STARTUP) from None

    def startup_problem(self) -> str | None:
        """Check whether the browser starts without opening the WG-Gesucht site."""
        try:
            self._browser()
        except BrowserStartupError as error:
            return error.msg
        return None

    def close(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except WebDriverException:
                pass
            finally:
                self._driver = None

    @staticmethod
    def _blocked(driver) -> bool:
        return (
            not _wg_page(driver.current_url)
            or any(title in driver.title.casefold() for title in _BLOCKED_TITLES)
            or _visible(driver, _VERIFICATION) is not None
        )

    def _composer_state(self, driver, listing: ListingRef):
        if self._blocked(driver):
            return DeliveryOutcome.BLOCKED
        if _visible(driver, "#pre_session_email, #login_form #login_password") is not None:
            return "login"
        if _contact_id(driver.current_url) != listing.id:
            return None
        if _visible(driver, "#message_timestamp") is not None or self._new_conversation_sent(driver):
            return DeliveryOutcome.ALREADY_CONTACTED
        return "ready" if _visible(driver, "textarea#message_input") is not None else None

    def _login(self, driver, wait: WebDriverWait):
        if self._login_attempted or not (self.email and self.password) or self._blocked(driver):
            return False
        self._login_attempted = True
        if email_input := _visible(driver, "#pre_session_form #pre_session_email"):
            form = driver.find_element(By.ID, "pre_session_form")
            next_button = _visible(form, "button, input[type='submit']")
            if next_button is None or _label(next_button) not in {"weiter", "continue"}:
                return False
            email_input.clear()
            email_input.send_keys(self.email)
            next_button.click()
            wait.until(lambda browser: self._blocked(browser) or _visible(browser, "#login_form #login_password"))
        if self._blocked(driver):
            return False
        password_input = _visible(driver, "#login_form #login_password")
        form = _visible(driver, "#login_form")
        if form is None or password_input is None:
            return False
        submit = _visible(form, "button[type='submit'], input[type='submit']")
        if submit is None or _label(submit) not in {"einloggen", "log in", "sign in"}:
            return False
        username = _visible(form, "input[name='login_email_username']:not([readonly]):not([type='hidden'])")
        if username is not None:
            username.clear()
            username.send_keys(self.email)
        password_input.send_keys(self.password)
        submit.click()
        wait.until(lambda browser: self._blocked(browser) or _visible(browser, "#login_form #login_password") is None)
        return not self._blocked(driver)

    def _contact(self, driver, wait: WebDriverWait, listing: ListingRef):
        if self._blocked(driver):
            return DeliveryOutcome.BLOCKED
        current = ListingRef.from_url(driver.current_url)
        if current is None or current.id != listing.id:
            return DeliveryOutcome.BLOCKED
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/nachricht-senden/']")
        contact = next((link for link in links if link.is_displayed() and _label(link) in _CONTACT_LABELS
                        and _contact_id(link.get_attribute("href") or "") == listing.id), None)
        if contact is None:
            return DeliveryOutcome.UNAVAILABLE
        contact.click()
        return wait.until(lambda browser: self._composer_state(browser, listing))

    @staticmethod
    def _submit_button(driver, area):
        selectors = "button[data-ng-click='submit()'], button[ng-click='submit()']"
        try:
            scope = area.find_element(By.XPATH, "./ancestor::form[1]")
            selectors += ", button[type='submit'], input[type='submit']"
        except NoSuchElementException:
            scope = driver
        return next((button for button in scope.find_elements(By.CSS_SELECTOR, selectors)
                     if button.is_displayed() and button.is_enabled() and _label(button) in _SEND_LABELS), None)

    @staticmethod
    def _attachment_input(driver):
        inputs = driver.find_elements(By.CSS_SELECTOR, _PDF_INPUT)
        if len(inputs) != 1 or not inputs[0].is_enabled():
            return None
        accepted_types = {value.strip().casefold() for value in (inputs[0].get_attribute("accept") or "").split(",")}
        return inputs[0] if accepted_types.intersection({"application/pdf", ".pdf"}) else None

    @staticmethod
    def _sent_attachment_label(driver, filename: str) -> bool:
        literal = _xpath_text(filename)
        return any(element.is_displayed() for element in driver.find_elements(
            By.XPATH, "//*[not(self::textarea) and not(ancestor::form[@id='messenger_form'])"
            f" and (normalize-space(.) = {literal} or @title = {literal})]",
        ))

    @staticmethod
    def _attachment_ready(driver, filename: str) -> bool:
        # WG-Gesucht uploads the file first and adds it to the pending message on success.
        panels = driver.find_elements(By.CSS_SELECTOR, _PENDING_ATTACHMENTS)
        if len(panels) != 1 or not panels[0].is_displayed():
            return False
        panel = panels[0]
        if not panel.find_elements(By.CSS_SELECTOR, 'input[name="message_type"][value="attached_file"]'):
            return False
        label = _visible(panel, "b")
        return label is not None and label.text == filename

    @staticmethod
    def _attachment_error(driver) -> str | None:
        # The site puts the upload error in its file picker, even when the modal is closed.
        for element in driver.find_elements(By.CSS_SELECTOR, _UPLOAD_ERROR):
            value = element.get_property("textContent")
            if not isinstance(value, str) or not value.strip():
                continue
            message = " ".join(value.casefold().split())
            if "demselben namen" in message or "same name" in message:
                return "WG-Gesucht meldet einen bereits vorhandenen Dateinamen"
            if "bibliothek" in message and "voll" in message or "library is full" in message:
                return "WG-Gesucht meldet eine volle Dateibibliothek"
            if "ungültiger dateiname" in message or "invalid file name" in message:
                return "WG-Gesucht lehnt den Dateinamen ab"
            return "WG-Gesucht meldet einen Fehler beim PDF-Upload"
        return None

    @staticmethod
    def _new_conversation_sent(driver) -> bool:
        return _visible(driver, _SENT_NOTICE) is not None and _visible(driver, _NEW_CONVERSATION) is None

    def _confirmed(self, driver, listing: ListingRef, message: str, attachment_name: str | None = None) -> bool:
        if self._blocked(driver):
            return False
        if _contact_id(driver.current_url) == listing.id and self._new_conversation_sent(driver):
            return True
        if _visible(driver, "#message_timestamp") is None:
            return False
        if attachment_name and not self._sent_attachment_label(driver, attachment_name):
            return False
        if _contact_id(driver.current_url) == listing.id:
            return True
        parts = urlsplit(driver.current_url)
        if parts.path not in {"/nachrichten.html", "/en/nachrichten.html"}:
            return False
        if not parse_qs(parts.query).get("nachrichten-id"):
            return False
        links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        if not any((ref := ListingRef.from_url(link.get_attribute("href") or "")) and ref.id == listing.id
                   for link in links if link.is_displayed()):
            return False
        literal = _xpath_text(" ".join(message.split()))
        return any(item.is_displayed() for item in driver.find_elements(
            By.XPATH, f"//*[not(self::textarea) and normalize-space(.) = {literal}]"
        ))

    def send(
        self, url: str, text: str, before_submit: Callable[[], bool], *, attachment: Path | None = None,
    ) -> DeliveryOutcome:
        listing = ListingRef.from_url(url) if _wg_page(url) else None
        if listing is None or not text.strip():
            return DeliveryOutcome.UNAVAILABLE
        claimed = False
        phase = "Versand reserviert"

        def unconfirmed() -> DeliveryOutcome:
            LOG.info("WG-Gesucht: Versand nicht bestätigt. Letzte Stufe: %s.", phase)
            return DeliveryOutcome.UNCONFIRMED

        try:
            if attachment is not None:
                attachment = attachment.resolve(strict=True)
                if attachment.suffix.casefold() != ".pdf" or not attachment.is_file() or attachment.stat().st_size <= 0:
                    return DeliveryOutcome.BLOCKED
            driver = self._browser()
            wait = WebDriverWait(driver, 12)
            driver.get(listing.url)
            state = self._contact(driver, wait, listing)
            if state == "login":
                if not self._login(driver, wait):
                    return DeliveryOutcome.BLOCKED
                driver.get(listing.url)
                state = self._contact(driver, wait, listing)
            if isinstance(state, DeliveryOutcome):
                return state
            if state != "ready" or _contact_id(driver.current_url) != listing.id:
                return DeliveryOutcome.BLOCKED
            area = _visible(driver, "textarea#message_input")
            if area is None or not area.is_enabled():
                return DeliveryOutcome.BLOCKED
            area.clear()
            area.send_keys(text)
            if area.get_property("value") != text:
                return DeliveryOutcome.BLOCKED
            submit = self._submit_button(driver, area)
            if submit is None or self._composer_state(driver, listing) != "ready":
                return DeliveryOutcome.BLOCKED
            if driver.find_elements(By.CSS_SELECTOR, _PENDING_ATTACHMENTS):
                return DeliveryOutcome.BLOCKED
            if attachment is not None:
                field = self._attachment_input(driver)
                if field is None or _visible(driver, _FORM_ERROR) is not None:
                    return DeliveryOutcome.BLOCKED
            if not before_submit():
                return DeliveryOutcome.SKIPPED
            claimed = True
            if attachment is not None:
                # Selecting the file may start an upload, so the approved send is reserved above.
                phase = "Anhang auswählen"
                field.send_keys(str(attachment))
                phase = "Anhang im Formular prüfen"

                def ready_after_upload(browser):
                    nonlocal phase
                    if reason := self._attachment_error(browser):
                        phase = reason
                        return DeliveryOutcome.UNCONFIRMED
                    if self._composer_state(browser, listing) != "ready" or _visible(browser, _FORM_ERROR) is not None:
                        return DeliveryOutcome.UNCONFIRMED
                    if self._attachment_ready(browser, attachment.name):
                        return self._submit_button(browser, area)
                    return None

                submit = WebDriverWait(driver, 45).until(ready_after_upload)
                if submit == DeliveryOutcome.UNCONFIRMED or area.get_property("value") != text:
                    return unconfirmed()
            phase = "Auf Senden klicken"
            submit.click()
            phase = "Klick auf Senden abgeschlossen; auf Bestätigung warten"
            attachment_name = attachment.name if attachment is not None else None
            wait.until(lambda browser: self._blocked(browser) or self._confirmed(browser, listing, text, attachment_name))
            return (DeliveryOutcome.SENT if self._confirmed(driver, listing, text, attachment_name)
                    else unconfirmed())
        except (TimeoutException, WebDriverException, OSError):
            return unconfirmed() if claimed else DeliveryOutcome.BLOCKED
