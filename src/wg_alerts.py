"""Read recent saved-search alerts from a dedicated IMAP folder."""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from typing import TypeVar

from bs4 import BeautifulSoup

from wg_source import Listing, ListingRef


_Item = TypeVar("_Item", ListingRef, Listing)


class AlertsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ListingAlert:
    ref: ListingRef
    received_at: datetime


def extract_refs(raw: bytes, sender: str) -> list[ListingRef]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if parseaddr(str(message.get("From", "")))[1].casefold() != sender.casefold():
        return []
    result: dict[str, ListingRef] = {}
    for part in message.walk():
        if part.get_content_disposition() == "attachment" or part.get_content_type() not in {
            "text/plain", "text/html"
        }:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            continue
        if part.get_content_type() == "text/html":
            links = [link["href"] for link in BeautifulSoup(content, "html.parser").select("a[href]")]
        else:
            links = re.findall(r"https://[^\s<>\"']+", content)
        for link in links:
            ref = ListingRef.from_url(link.rstrip(".,);")) if link.startswith("https://") else None
            if ref:
                result.setdefault(ref.id, ref)
    return list(result.values())


def _quote(value: str) -> str:
    if not value.isascii() or any(ord(char) < 32 for char in value):
        raise AlertsError("IMAP folder and sender must use printable ASCII")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _imap_date(date: datetime) -> str:
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{date.day:02d}-{months[date.month - 1]}-{date.year}"


@dataclass(frozen=True)
class AlertsInbox:
    host: str
    user: str
    password: str
    folder: str
    sender: str

    def recent(self, cutoff: datetime, limit: int) -> list[ListingAlert]:
        return [ListingAlert(ref, received_at) for ref, received_at in self.recent_items(cutoff, limit, extract_refs)]

    def recent_items(
        self, cutoff: datetime, limit: int, extract: Callable[[bytes, str], list[_Item]],
    ) -> list[tuple[_Item, datetime]]:
        # IMAP SINCE is day-based and ignores time zones. Filter INTERNALDATE
        # precisely below; include the previous day to handle offset boundaries.
        try:
            with imaplib.IMAP4_SSL(self.host, ssl_context=ssl.create_default_context(), timeout=15) as conn:
                conn.login(self.user, self.password)
                status, _ = conn.select(_quote(self.folder), readonly=True)
                if status != "OK":
                    raise AlertsError("Cannot select the saved-search folder")
                status, data = conn.uid(
                    "SEARCH", None, "SINCE", _imap_date(cutoff - timedelta(days=1)), "FROM", _quote(self.sender)
                )
                if status != "OK":
                    raise AlertsError("Cannot search the saved-search folder")
                ids = sorted((data[0] or b"").split(), key=int, reverse=True)
                result: dict[str, tuple[_Item, datetime]] = {}
                for uid in ids[:max(20, limit * 2)]:
                    status, rows = conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[]<0.262144>)")
                    if status != "OK":
                        raise AlertsError("Cannot read a saved-search alert")
                    metadata = b" ".join(row[0] if isinstance(row, tuple) else row for row in rows if row)
                    stamp = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
                    if not stamp:
                        continue
                    try:
                        received = parsedate_to_datetime(stamp[1].decode("ascii"))
                    except (ValueError, UnicodeError):
                        continue
                    if received.tzinfo is None or received < cutoff:
                        continue
                    for row in rows:
                        if not isinstance(row, tuple):
                            continue
                        for item in extract(row[1], self.sender):
                            prior = result.get(item.id)
                            # Use the first alert in the scan window for repeated listings.
                            if prior is None or received < prior[1]:
                                result[item.id] = (item, received.astimezone(timezone.utc))
                return sorted(result.values(), key=lambda item: item[1], reverse=True)[:limit]
        except (OSError, imaplib.IMAP4.error) as exc:
            raise AlertsError(f"IMAP: {type(exc).__name__}; check server, folder and credentials") from exc
