"""Parse notification emails from a dedicated Kleinanzeigen replies folder."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from kleinanzeigen_source import _body_text, _reference

_INBOX_URL = "https://www.kleinanzeigen.de/m-nachrichten.html"
_SUBJECT = re.compile(
    r"^(?:neue nachricht\b|nachricht von\b|du hast eine (?:neue )?nachricht\b|"
    r"antwort (?:auf|zu)\b|.{1,120} hat dir eine (?:neue )?nachricht geschrieben\b|"
    r"ein interessent hat eine anfrage zu ihrer anzeige gesendet\b)",
    re.IGNORECASE,
)
_START = re.compile(
    r"^(?:(?:(?:deine )?(?:neue )?nachricht(?: lautet)?|antwort)\s*:\s*(?P<text>.*)|"
    r"nachricht von\s*:\s*.{1,120})$",
    re.IGNORECASE,
)
_END = re.compile(
    r"^(?:[\s>*-]{3,}|(?:jetzt )?antworten[.!]?|"
    r"(?:frage|nachricht|konversation) (?:anzeigen|ansehen|lesen|öffnen)[.!]?|"
    r"(?:öffne|lies) (?:die |deine )?(?:nachricht|konversation)\b.*|"
    r"(?:beantworte(?:n sie)?|um .* zu antworten)\b.*|"
    r"(?:diese (?:e-mail|nachricht)|zum schutz unserer nutzer|falls sie uns diese e-mail)\b.*|"
    r"(?:dein|ihr) (?:ebay )?kleinanzeigen[ -]team[.!]?|"
    r"impressum|datenschutz(?:erklärung)?)$",
    re.IGNORECASE,
)
_AD_LABEL = re.compile(r"^(?:anzeigen(?:nummer|[ -]id)|inseratsnummer)\s*:\s*([0-9]+)\s*$", re.I | re.M)
_WEB_URL = re.compile(r"https://[^\s<>\"']+")
_FRAGMENT = re.compile(r"[A-Za-z0-9:._-]{1,150}")
_NONPRINTING = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True)
class KleinanzeigenReply:
    id: str
    listing_id: str | None
    url: str
    text: str = field(repr=False)


def _inbox_link(url: str) -> str | None:
    try:
        parts = urlsplit(url.rstrip(".,);"))
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.netloc.lower() not in {"kleinanzeigen.de", "www.kleinanzeigen.de"}
            or parts.path != "/m-nachrichten.html"):
        return None
    return f"{_INBOX_URL}#{parts.fragment}" if _FRAGMENT.fullmatch(parts.fragment) else _INBOX_URL


def _lines(content: str) -> list[str]:
    return [_NONPRINTING.sub("", line).strip() for line in content.splitlines()]


def _split_preview(lines: list[str]) -> tuple[list[str], str]:
    for index, line in enumerate(lines):
        match = _START.fullmatch(line)
        if not match:
            continue
        preview = [match["text"]] if match["text"] else []
        for content in lines[index + 1:]:
            if content.startswith(">") or _END.fullmatch(content) or _WEB_URL.fullmatch(content):
                break
            preview.append(content)
        text = "\n".join(preview).strip()
        if _END.fullmatch(text) or _WEB_URL.fullmatch(text):
            text = ""
        return lines[:index], text[:2500]
    return lines, ""


def extract_replies(raw: bytes, sender: str) -> list[KleinanzeigenReply]:
    """Read a recognized reply notification without opening links or attachments."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    actual_sender = parseaddr(str(message.get("From", "")))[1].casefold()
    subject = " ".join(str(message.get("Subject", "")).split())
    if not actual_sender or actual_sender != sender.strip().casefold() or not _SUBJECT.match(subject):
        return []

    plain = _body_text(message, "plain")
    page = BeautifulSoup(_body_text(message, "html"), "html.parser")
    for node in page.select("script, style, template"):
        node.decompose()
    plain_lines, plain_preview = _split_preview(_lines(plain))
    html_lines, html_preview = _split_preview(_lines(page.get_text("\n", strip=True)))
    links = _WEB_URL.findall(plain) + [anchor["href"] for anchor in page.select("a[href]")]
    listings = {ref.id: ref.url for url in links if (ref := _reference(url)) is not None}
    labels = {f"ka-{number}" for number in _AD_LABEL.findall("\n".join(plain_lines + html_lines))}
    listing_ids = set(listings) | labels
    inbox_links = {url for link in links if (url := _inbox_link(link)) is not None}
    if len(listing_ids) > 1 or (not listing_ids and not inbox_links):
        return []

    listing_id = next(iter(listing_ids), None)
    conversations = inbox_links - {_INBOX_URL}
    if len(conversations) > 1:
        return []
    url = next(iter(conversations), listings.get(listing_id, _INBOX_URL))
    text = plain_preview or html_preview
    if plain_preview and html_preview and " ".join(plain_preview.split()) != " ".join(html_preview.split()):
        # Two different previews do not reliably identify the counterparty's text.
        text = ""
    message_id = str(message.get("Message-ID", "")).strip()
    identity = message_id or "\n".join((listing_id or "", url, subject, str(message.get("Date", "")), text))
    digest = hashlib.sha256(f"{actual_sender}\n{identity}".encode("utf-8")).hexdigest()
    return [KleinanzeigenReply(f"ka-reply-{digest}", listing_id, url, text)]
