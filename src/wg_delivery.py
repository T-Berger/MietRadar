"""Send one application through WG-Gesucht's normal browser controls."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from shutil import which
from urllib.parse import parse_qs, urlsplit

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from munich_delivery import DeliveryOutcome
from wg_source import ListingRef


_HOSTS = {"www.wg-gesucht.de", "wg-gesucht.de"}
_CONTACT_PREFIXES = ("/nachricht-senden/", "/en/nachricht-senden/")
_CONTACT_LABELS = {"nachricht senden", "send message"}
_SEND_LABELS = _CONTACT_LABELS | {"senden", "send"}
_VERIFICATION = (
    "#login_two_factor_authentication_form, #registration_verification_phone_number_form, "
    "#registration_verification_code_form, #sicherheit_bestaetigung, "
    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='recaptcha/enterprise/bframe'], "
    "iframe[title*='hCaptcha'], iframe[src*='challenges.cloudflare.com']"
)
_BLOCKED_TITLES = ("überprüfung", "access denied", "just a moment", "zugriff verweigert", "captcha")


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
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "eager"
        options.add_argument(f"--user-data-dir={self.profile.resolve()}")
        options.add_argument("--lang=de-DE")
        options.add_argument("--window-size=1280,900")
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-dev-shm-usage")
        if chrome := os.getenv("CHROME_BIN") or which("chromium"):
            options.binary_location = chrome
        executable = os.getenv("CHROMEDRIVER") or which("chromedriver")
        driver = webdriver.Chrome(options=options, service=Service(executable_path=executable))
        self._driver = driver
        driver.set_page_load_timeout(25)
        return driver

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
        if _visible(driver, "#message_timestamp") is not None:
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

    def _confirmed(self, driver, listing: ListingRef, message: str) -> bool:
        if self._blocked(driver) or _visible(driver, "#message_timestamp") is None:
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

    def send(self, url: str, text: str, before_submit: Callable[[], bool]) -> DeliveryOutcome:
        listing = ListingRef.from_url(url) if _wg_page(url) else None
        if listing is None or not text.strip():
            return DeliveryOutcome.UNAVAILABLE
        claimed = False
        try:
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
            if not before_submit():
                return DeliveryOutcome.SKIPPED
            claimed = True
            submit.click()
            wait.until(lambda browser: self._blocked(browser) or self._confirmed(browser, listing, text))
            return DeliveryOutcome.SENT if self._confirmed(driver, listing, text) else DeliveryOutcome.UNCONFIRMED
        except (TimeoutException, WebDriverException, OSError):
            return DeliveryOutcome.UNCONFIRMED if claimed else DeliveryOutcome.BLOCKED
