"""Read the latest messages in a normal, authenticated WG-Gesucht browser."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from wg_delivery import BrowserSender


_ORIGIN = "https://www.wg-gesucht.de"
_INBOX_PATH = "/nachrichten.html"
_THREAD_PATH = "/nachricht.html"
_THREAD_LINK = ".conversation_list_item .latest_message a.link-conversation-list"
_MESSAGE = "#messages_container .message[id^='last_message_id_']"
_MESSAGE_TEXT = ".message_content.message_text"
_LOGIN = "#pre_session_email, #login_form #login_password"
_MESSAGE_ID = re.compile(r"last_message_id_([0-9]{1,20})")
_NUMERIC = re.compile(r"[0-9]{1,20}")
_EMPTY = re.compile(r"\b(?:keine (?:vorhandenen )?nachrichten|no messages)\b", re.I)


class WGInboxError(RuntimeError):
    """The browser could not confirm that the inbox was accessible."""


@dataclass(frozen=True)
class WGInboxEntry:
    conversation_id: str
    url: str
    message_id: str
    text: str | None


def _at_wg_path(url: str, path: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc.casefold() == "www.wg-gesucht.de" and parts.path == path


def _thread_id(url: str) -> str | None:
    if not _at_wg_path(url, _THREAD_PATH):
        return None
    ids = parse_qs(urlsplit(url).query, keep_blank_values=True).get("nachrichten-id", [])
    return ids[0] if len(ids) == 1 and _NUMERIC.fullmatch(ids[0]) else None


def _thread_url(conversation_id: str, message_id: str | None = None) -> str:
    anchor = f"#last_message_id_{message_id}" if message_id else ""
    return f"{_ORIGIN}{_THREAD_PATH}?nachrichten-id={conversation_id}&list=1{anchor}"


def _visible(scope, selector: str):
    return [element for element in scope.find_elements("css selector", selector) if element.is_displayed()]


def _inbox_state(sender, driver):
    if sender._blocked(driver):
        return "blocked", []
    if _visible(driver, _LOGIN):
        return "login", []
    if not _at_wg_path(driver.current_url, _INBOX_PATH):
        return None
    links = _visible(driver, _THREAD_LINK)
    if links or any(_EMPTY.search(body.text) for body in _visible(driver, "body")):
        return "ready", links
    return None


def _latest_message(sender, driver, conversation_id: str) -> WGInboxEntry | None:
    if sender._blocked(driver) or _visible(driver, _LOGIN):
        raise WGInboxError("Das WG-Gesucht-Postfach braucht eine Anmeldung oder Bestätigung.")
    if _thread_id(driver.current_url) != conversation_id:
        return None
    messages = _visible(driver, _MESSAGE)
    if not messages or not (identifier := _MESSAGE_ID.fullmatch(messages[-1].get_attribute("id") or "")):
        return None
    latest = messages[-1]
    message_id = identifier[1]
    url = _thread_url(conversation_id, message_id)
    if "my_message" in (latest.get_attribute("class") or "").split():
        return WGInboxEntry(conversation_id, url, message_id, None)
    bodies = _visible(latest, _MESSAGE_TEXT)
    if len(bodies) != 1 or not (text := bodies[0].text.strip()):
        return None
    return WGInboxEntry(conversation_id, url, message_id, text[:6000])


def recent_wg_messages(sender: BrowserSender, *, limit: int = 10) -> tuple[WGInboxEntry, ...]:
    """Read at most one latest message per thread without using the reply field."""
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait

    if limit <= 0:
        return ()
    try:
        driver = sender._browser()
        wait = WebDriverWait(driver, 8)
        driver.get(f"{_ORIGIN}{_INBOX_PATH}")
        state, links = wait.until(lambda browser: _inbox_state(sender, browser))
        if state == "login" and sender._login(driver, wait):
            driver.get(f"{_ORIGIN}{_INBOX_PATH}")
            state, links = wait.until(lambda browser: _inbox_state(sender, browser))
        if state != "ready":
            raise WGInboxError("Das WG-Gesucht-Postfach braucht eine Anmeldung oder Bestätigung.")

        threads = dict.fromkeys(conversation_id for link in links
                                if (conversation_id := _thread_id(link.get_attribute("href") or "")))
        entries = []
        for conversation_id in list(threads)[:min(limit, 20)]:
            driver.get(_thread_url(conversation_id))
            try:
                entries.append(wait.until(lambda browser: _latest_message(sender, browser, conversation_id)))
            except TimeoutException:
                continue  # A thread can be unavailable while the rest of the inbox still works.
        return tuple(entries)
    except (OSError, WebDriverException):
        raise WGInboxError("Das WG-Gesucht-Postfach konnte gerade nicht gelesen werden.") from None
