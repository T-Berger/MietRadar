"""Send approved WG-Gesucht applications from a dedicated local Chrome profile."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import stat
import sys
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait

from munich_delivery import DeliveryOutcome
from reviewed_applications import MAX_APPLICATION_ATTACHMENT_BYTES
from wg_delivery import BrowserSender
from wg_reply_inbox import _inbox_state
from wg_source import BASE_URL, DEFAULT_SEARCH_URLS, Listing, ListingRef, WGListingGone, WGSource, WGSourceError


DEFAULT_STATE = Path.home() / "Library" / "Application Support" / "MietRadar WG"
DEFAULT_PROFILE = DEFAULT_STATE / "chrome"
DEFAULT_TOKEN_FILE = DEFAULT_STATE / "relay-token"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_REVISION = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ID = re.compile(r"[0-9]{1,20}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_MAX_RESPONSE = 8 * 1024 * 1024
_MAX_PREPARE_REQUEST = 256 * 1024
_LISTING_LIMITS = {"title": 1024, "description": 65_536, "address": 512, "wanted": 1024,
                   "wg_type": 1024, "resident_ages": 1024}
_REQUEST_TIMEOUT = 45
_POLL_SECONDS = 25
_RETRY_SECONDS = 60

LOG = logging.getLogger(__name__)


class RelayError(RuntimeError):
    """An error message safe to show without disclosing response data."""


class RelayTemporaryError(RelayError):
    """The worker can retry while keeping the approval queued."""


class UnconfirmedDelivery(RelayError):
    """Railway recorded the attempted delivery without a WG-Gesucht receipt."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_token(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 130):
                raise RelayError("Die Relay-Schlüsseldatei muss dir gehören und Dateirechte 0600 haben.")
            token = handle.read(131).decode("ascii").strip()
    except (OSError, UnicodeError):
        raise RelayError("Die Relay-Schlüsseldatei ist nicht lesbar.") from None
    if not _TOKEN.fullmatch(token):
        raise RelayError("Die Relay-Schlüsseldatei enthält keinen gültigen Schlüssel.")
    return token


class Relay:
    def __init__(self, url: str, token: str):
        try:
            parts = urlsplit(url)
            valid = (
                parts.scheme == "https" and parts.hostname is not None
                and parts.port in {None, 443} and parts.username is None and parts.password is None
                and parts.path in {"", "/"} and not parts.query and not parts.fragment
                and re.fullmatch(r"[a-zA-Z0-9.-]+(?::443)?", parts.netloc)
            )
        except ValueError:
            valid = False
        if not valid:
            raise RelayError("Verwende die HTTPS-Adresse des Railway-Relays ohne Pfad.")
        self.url = url.rstrip("/")
        self.token = token
        self.opener = build_opener(NoRedirects())

    def post(self, endpoint: str, data: dict) -> dict:
        request = Request(
            f"{self.url}/v1/{endpoint}",
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=_REQUEST_TIMEOUT) as response:
                body = response.read(_MAX_RESPONSE + 1)
        except HTTPError as error:
            error.close()
            if error.code in {401, 403}:
                raise RelayError("Railway hat den Relay-Schlüssel abgelehnt.") from None
            if error.code in {408, 429} or 500 <= error.code <= 599:
                raise RelayTemporaryError("Das Railway-Relay ist vorübergehend nicht erreichbar.") from None
            raise RelayError(f"Das Railway-Relay hat die Anfrage abgelehnt (HTTP {error.code}).") from None
        except (OSError, URLError, HTTPException):
            raise RelayTemporaryError("Das Railway-Relay ist vorübergehend nicht erreichbar.") from None
        if len(body) > _MAX_RESPONSE:
            raise RelayError("Die Antwort des Railway-Relays ist zu groß.")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise RelayError("Das Railway-Relay hat eine ungültige Antwort gesendet.") from None
        if not isinstance(result, dict):
            raise RelayError("Das Railway-Relay hat eine ungültige Antwort gesendet.")
        return result


@dataclass(frozen=True)
class Job:
    ref: ListingRef
    revision: str

    @property
    def payload(self) -> dict:
        return {"id": self.ref.id, "revision": self.revision}


def _job(result: dict) -> Job | None:
    if "job" in result and result["job"] is None:
        return None
    raw = result.get("job")
    if isinstance(raw, dict):
        listing_id, url, revision = raw.get("id"), raw.get("url"), raw.get("revision")
        if (isinstance(listing_id, str) and _ID.fullmatch(listing_id) and isinstance(url, str)
                and isinstance(revision, str) and _REVISION.fullmatch(revision)):
            ref = ListingRef.from_url(url)
            if ref and ref.id == listing_id and ref.url == url:
                return Job(ref, revision)
    raise RelayError("Das Railway-Relay hat keine gültige WG-Gesucht-Freigabe geliefert.")


def _listing_fields(listing: Listing) -> dict:
    fields = {name: getattr(listing, name) for name in _LISTING_LIMITS}
    if listing.is_commercial:
        fields["is_commercial"] = True
    for name in ("published_at", "published_day", "available_from", "available_until"):
        value = getattr(listing, name)
        fields[name] = value.isoformat() if value is not None else None
    return fields


def _write_attachment(raw: object, directory: Path) -> Path | None:
    if raw is None:
        return None
    invalid = RelayError("Der freigegebene PDF-Anhang konnte nicht geprüft werden.")
    if not isinstance(raw, dict):
        raise invalid
    name, digest, encoded = raw.get("name"), raw.get("digest"), raw.get("content")
    if (not isinstance(name, str) or not 4 < len(name) <= 180 or not name.casefold().endswith(".pdf")
            or "/" in name or "\\" in name or any(unicodedata.category(char).startswith("C") for char in name)
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_APPLICATION_ATTACHMENT_BYTES + 2) // 3)):
        raise invalid
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise invalid from None
    if (len(content) > MAX_APPLICATION_ATTACHMENT_BYTES or not content.startswith(b"%PDF-")
            or b"%%EOF" not in content[-1024:] or hashlib.sha256(content).hexdigest() != digest):
        raise invalid
    # WG-Gesucht rejects repeated filenames in the account's file library. Only the upload name changes.
    path = directory / f"Bonitaetsnachweis_{secrets.token_hex(8)}.pdf"
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(content)
    except OSError:
        raise invalid from None
    return path


def _finish(relay: Relay, job: Job, outcome: DeliveryOutcome) -> None:
    payload = {**job.payload, "outcome": outcome.name.lower()}
    for attempt in range(3):
        try:
            recorded = relay.post("finish", payload).get("recorded")
            break
        except RelayTemporaryError:
            if attempt == 2:
                raise RelayError("Der Versandstatus konnte nicht an Railway zurückgemeldet werden.") from None
            time.sleep(2)
    if recorded is not True and not (outcome is DeliveryOutcome.SKIPPED and recorded is False):
        raise RelayError("Railway hat den Versandstatus nicht bestätigt.")


def process_next(relay: Relay, sender: BrowserSender, source: WGSource) -> bool:
    """Return whether there was a job, with at most one browser submission."""
    job = _job(relay.post("next", {}))
    if job is None:
        return False
    try:
        listing = source.details(job.ref)
    except WGListingGone:
        _finish(relay, job, DeliveryOutcome.UNAVAILABLE)
        return True
    except WGSourceError:
        _finish(relay, job, DeliveryOutcome.SKIPPED)
        raise RelayTemporaryError("WG-Gesucht konnte die Anzeige gerade nicht bestätigen.") from None
    if listing.id != job.ref.id:
        raise RelayError("WG-Gesucht hat eine andere Anzeige geliefert.")

    details = _listing_fields(listing)
    prepare = {**job.payload, "listing": details}
    if (any(details[name] is not None and len(details[name]) > limit for name, limit in _LISTING_LIMITS.items())
            or len(json.dumps(prepare, ensure_ascii=False).encode("utf-8")) > _MAX_PREPARE_REQUEST):
        _finish(relay, job, DeliveryOutcome.SKIPPED)
        return True
    prepared = relay.post("prepare", prepare)
    if prepared.get("ready") is False:
        return False
    message = prepared.get("message")
    if (prepared.get("ready") is not True or not isinstance(message, str) or not message.strip()
            or len(message) > 6000 or "attachment" not in prepared):
        raise RelayError("Das Railway-Relay hat keinen gültigen Bewerbungstext geliefert.")

    claim_error = False
    claimed = False

    def claim() -> bool:
        nonlocal claim_error, claimed
        if claimed or claim_error:
            return False
        try:
            reply = relay.post("claim", job.payload)
            if type(reply.get("claimed")) is not bool:
                raise RelayError("Die Versandreservierung konnte nicht bestätigt werden.")
            claimed = reply["claimed"]
        except RelayError:
            claim_error = True
        return claimed

    with TemporaryDirectory(prefix="mietradar-pdf-") as workspace:
        attachment = _write_attachment(prepared["attachment"], Path(workspace))
        outcome = sender.send(job.ref.url, message, claim, attachment=attachment)
    if claim_error:
        raise RelayError("Die Versandreservierung konnte nicht bestätigt werden.")
    if claimed and outcome is not DeliveryOutcome.SENT:
        outcome = DeliveryOutcome.UNCONFIRMED
    elif not claimed and outcome in {DeliveryOutcome.SENT, DeliveryOutcome.UNCONFIRMED}:
        outcome = DeliveryOutcome.BLOCKED
    _finish(relay, job, outcome)
    if outcome is DeliveryOutcome.BLOCKED:
        raise RelayError("WG-Gesucht braucht eine Anmeldung oder Bestätigung im lokalen Browser.")
    if outcome is DeliveryOutcome.UNCONFIRMED:
        raise UnconfirmedDelivery("WG-Gesucht hat den Versand nicht bestätigt. Prüfe das Postfach.")
    if outcome is DeliveryOutcome.SENT:
        LOG.info("WG-Gesucht hat eine Bewerbung bestätigt.")
    return True


@contextmanager
def _profile_lock(profile: Path, *, wait: bool = False):
    personal_profile = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if profile.resolve().is_relative_to(personal_profile.resolve()):
        raise RelayError("Verwende für MietRadar einen eigenen privaten Chrome-Profilordner.")
    try:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = profile.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RelayError("Verwende für MietRadar einen eigenen privaten Chrome-Profilordner.")
        descriptor = os.open(profile / ".mietradar.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise RelayError("Der lokale Profilordner ist nicht verfügbar.") from None
    with os.fdopen(descriptor, "wb") as lock:
        try:
            mode = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(lock, mode)
        except OSError:
            raise RelayError("Das MietRadar-Profil wird bereits verwendet.") from None
        yield


def _native_profile_in_use(profile: Path) -> bool:
    """Check the owner of Chromium's profile lock without touching the browser."""
    try:
        target = os.readlink(profile / "SingletonLock")
        pid = int(target.rsplit("-", 1)[-1])
        if pid <= 1:
            return False
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _await_native_profile_release(profile: Path) -> None:
    if _native_profile_in_use(profile):
        LOG.info("Railway ist verbunden. Beende das separate Chrome nach der WG-Gesucht-Anmeldung.")
        while _native_profile_in_use(profile):
            time.sleep(2)


def _check_browser(sender: BrowserSender, *, manual: bool = False, keep_waiting: bool = False) -> None:
    problem = sender.startup_problem()
    if problem:
        raise RelayError(problem)
    try:
        driver = sender._browser()
        driver.get(f"{BASE_URL}/nachrichten.html")
        if manual:
            if (state := _inbox_state(sender, driver)) and state[0] == "ready":
                return
            print("Melde dich im geöffneten Chrome-Fenster bei WG-Gesucht an und öffne das Postfach.", flush=True)

        def inbox_ready(browser):
            state = _inbox_state(sender, browser)
            if state and (state[0] == "ready" or not manual):
                return state[0]
            return None

        wait = WebDriverWait(driver, 300 if manual else 12)
        while True:
            try:
                state = wait.until(inbox_ready)
                break
            except TimeoutException:
                if not keep_waiting:
                    raise
        if state == "ready":
            return
    except (OSError, WebDriverException):
        pass
    raise RelayError("Das WG-Gesucht-Postfach ist im lokalen Profil nicht erreichbar. Starte zuerst `login`.")


def _check_relay(relay: Relay, *, retry: bool) -> None:
    while True:
        try:
            if relay.post("status", {}).get("ok") is not True:
                raise RelayError("Railway hat die Relay-Verbindung nicht bestätigt.")
            return
        except RelayTemporaryError as error:
            if not retry:
                raise
            LOG.warning("%s", error)
            time.sleep(_RETRY_SECONDS)


def _poll_jobs(relay: Relay, sender: BrowserSender, *, visible: bool) -> None:
    source = WGSource(DEFAULT_SEARCH_URLS)
    while True:
        try:
            more = process_next(relay, sender, source)
            time.sleep(2 if more else _POLL_SECONDS)
        except UnconfirmedDelivery as error:
            if not visible:
                raise
            LOG.warning("%s Der bisherige Tab bleibt zur Prüfung offen.", error)
            try:
                sender._browser().switch_to.new_window("tab")
            except (OSError, WebDriverException):
                raise RelayError("Chrome konnte keinen neuen Tab für den weiteren Versand öffnen.") from None
            _check_browser(sender, manual=True, keep_waiting=True)
        except RelayTemporaryError as error:
            LOG.warning("%s", error)
            time.sleep(_RETRY_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("login", "check", "run"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--relay-url")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--visible", action="store_true", help="Chrome beim Versand sichtbar geöffnet halten")
    args = parser.parse_args()
    if args.command != "login" and not args.relay_url:
        parser.error("--relay-url ist für check und run erforderlich")
    if args.visible and args.command != "run":
        parser.error("--visible ist für run vorgesehen")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        relay = None if args.command == "login" else Relay(args.relay_url, read_token(args.token_file.expanduser()))
        profile = args.profile.expanduser()
        with _profile_lock(profile, wait=args.command == "run"):
            sender = BrowserSender(profile, headless=args.command != "login" and not args.visible)
            try:
                if relay:
                    _check_relay(relay, retry=args.command == "run")
                if args.command == "run":
                    _await_native_profile_release(profile)
                _check_browser(sender, manual=args.command == "login" or args.visible, keep_waiting=args.visible)
                if args.command != "run":
                    print("WG-Gesucht-Anmeldung und Postfach sind erreichbar.")
                    if relay:
                        print("Railway-Verbindung erfolgreich geprüft.")
                    return 0
                LOG.info("Mit Railway verbunden. Der lokale WG-Gesucht-Browser ist angemeldet.")
                _poll_jobs(relay, sender, visible=args.visible)
            finally:
                sender.close()
    except KeyboardInterrupt:
        return 0
    except RelayError as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
