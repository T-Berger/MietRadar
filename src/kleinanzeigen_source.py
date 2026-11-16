"""Read rental listings from received Kleinanzeigen saved-search email bodies."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from listing_photos import listing_photo_urls
from wg_source import Listing, RentKind, parse_area_sqm, parse_rent_eur, rent_kind_for_label

_HOSTS = {"www.kleinanzeigen.de", "kleinanzeigen.de"}
_LISTING_PATH = re.compile(r"/s-anzeige/[\w-]+/(?P<id>[0-9]+)-(?P<category>[0-9]+)-[0-9]+")
_RENTAL_CATEGORIES = {"199", "203"}
_URL = re.compile(r"https://[^\s<>\"']+")
_MUNICH_POSTCODE = r"(?:80[3-9][0-9]{2}|81[0-9]{3})"
_MUNICH = r"M(?:ü|ue)nchen"
_HOUSE_NUMBER = r"[1-9][0-9]{0,3}(?:\s?[a-z])?(?:\s*[-–/]\s*[1-9][0-9]{0,3}(?:\s?[a-z])?)?"
_ADDRESS = re.compile(
    rf"(?P<street>[^\W\d_][\w .’'()/\-–]{{1,109}}?)\s+(?P<number>{_HOUSE_NUMBER})"
    rf"(?:\s*,\s*|\s+)(?P<postcode>{_MUNICH_POSTCODE})\s+{_MUNICH}(?:\s*,\s*Deutschland)?",
    re.IGNORECASE,
)
_STREET_LABEL = re.compile(r"^(?:adresse|anschrift|standort)\s*:\s*", re.IGNORECASE)
_STREET_END = re.compile(r"(?:straße|strasse|str\.|weg|allee|platz|ring|gasse|ufer|steig|damm|promenade|chaussee)$", re.I)
_AREA = re.compile(rf"(?:{_MUNICH_POSTCODE}\s+)?{_MUNICH}(?:\s*[-–]\s*[\w -]{{2,45}})?", re.I)
_GENERIC_LINK = {"anzeige ansehen", "zur anzeige", "angebot ansehen", "jetzt ansehen", "mehr erfahren", "kleinanzeigen"}
_FACT_FIELD = re.compile(
    r"^(?P<label>bruttowarmmiete|warmmiete|gesamtmiete|nettokaltmiete|kaltmiete|miete|"
    r"preis(?:\s+pro\s+(?:m[²2]|qm|quadratmeter))?|zimmergröße|zimmerfläche|wohnungsgröße|wohnfläche|größe|"
    r"(?:miet)?kaution|ablöse(?:vereinbarung)?|nebenkosten|betriebskosten|heizkosten|sonstige kosten)"
    r"\b\s*:?\s*(?P<value>.*)$", re.I,
)


def _clean(value: str) -> str:
    return " ".join(value.split())


def normalize_munich_address(text: str) -> str | None:
    """Require a street, house number, Munich postcode, and municipality."""
    match = _ADDRESS.fullmatch(_clean(text))
    if not match:
        return None
    return f"{match['street']} {match['number']}, {match['postcode']} München"


@dataclass(frozen=True)
class _Reference:
    id: str
    url: str
    is_wg: bool


def _reference(url: str, *, rentals_only: bool = True) -> _Reference | None:
    try:
        parts = urlsplit(url.rstrip(".,);"))
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() not in _HOSTS:
        return None
    path = unquote(parts.path).rstrip("/")
    match = _LISTING_PATH.fullmatch(path)
    if not match or (rentals_only and match["category"] not in _RENTAL_CATEGORIES):
        return None
    return _Reference(f"ka-{match['id']}", "https://www.kleinanzeigen.de" + quote(path), match['category'] == "199")


def _line_address(lines: list[str]) -> tuple[str | None, range]:
    for index, line in enumerate(lines):
        candidate = _STREET_LABEL.sub("", line)
        labelled = candidate != line
        for end in range(index + 1, min(len(lines), index + 2) + 1):
            text = " ".join([candidate, *lines[index + 1:end]])
            match = _ADDRESS.fullmatch(_clean(text))
            if match and (labelled or _STREET_END.search(match["street"])):
                return normalize_munich_address(text), range(index, end)
    return None, range(0)


def _html_card(link: Tag, ref: _Reference) -> tuple[list[str], tuple[str, ...]]:
    # Ascend inside one listing card only. A wrapper shared with another
    # listing cannot provide an address or preferences for this one.
    lines: list[str] = []
    photo_nodes = list(link.select("img"))
    for node in link.parents:
        if not isinstance(node, Tag) or node.name in {"body", "html", "[document]"}:
            break
        links = [_reference(anchor["href"], rentals_only=False) for anchor in node.select("a[href]")]
        if any(linked.id != ref.id for linked in links if linked is not None):
            break
        candidates = [_clean(text) for text in node.get_text("\n", strip=True).splitlines()]
        candidates = [text for text in candidates if text]
        if sum(map(len, candidates)) > 1500 or len(candidates) > 24:
            break
        lines = candidates
        # Email newsletters also use table cells as cards. An unlinked photo can
        # belong to that cell when every link there points to this same listing.
        image_is_scoped = node.name in {"article", "li", "td", "tr", "table"} and all(links)
        for image in node.select("img"):
            anchor = image.find_parent("a", href=True)
            linked = _reference(anchor["href"], rentals_only=False) if anchor else None
            if (linked and linked.id == ref.id) or (not anchor and image_is_scoped):
                photo_nodes.append(image)
        if node.name in {"article", "li"}:
            break
    photos = listing_photo_urls((value for image in photo_nodes
                                 if not any(image.get(key) == "1" for key in ("width", "height"))
                                 for value in (image.get("src"), image.get("data-src"))), platform="ka")
    return lines, photos


def _alert_facts(lines: list[str], *, is_wg: bool) -> tuple[float | None, RentKind | None, float | None]:
    rents: dict[RentKind, set[float]] = {}
    labelled_areas: set[float] = set()
    bare_areas: set[float] = set()
    area_labels = {"größe", "zimmergröße", "zimmerfläche"} if is_wg else {"größe", "wohnfläche", "wohnungsgröße"}
    pending_label = None

    for line in lines:
        for part in re.split(r"\s*[·•|]\s*", line):
            if not part:
                continue
            field = _FACT_FIELD.fullmatch(part)
            if field:
                label = field["label"].lower()
                value = field["value"]
                pending_label = None if value else label
                if not value:
                    continue
            else:
                label, value = pending_label, part
                pending_label = None

            kind = rent_kind_for_label(label) if label else "unspecified"
            if kind and (amount := parse_rent_eur(value)) is not None:
                rents.setdefault(kind, set()).add(amount)
            if (label is None or label in area_labels) and (area := parse_area_sqm(value)) is not None:
                (bare_areas if label is None else labelled_areas).add(area)

    areas = labelled_areas or bare_areas
    area = next(iter(areas)) if len(areas) == 1 else None
    for kind in ("warm", "total", "cold", "unspecified"):
        amounts = rents.get(kind)
        if amounts:
            return (next(iter(amounts)), kind, area) if len(amounts) == 1 else (None, None, area)
    return None, None, area


def _listing(ref: _Reference, title: str, lines: list[str], photo_urls: tuple[str, ...] = ()) -> Listing:
    address, address_lines = _line_address(lines)
    rent_eur, rent_kind, area_sqm = _alert_facts(lines, is_wg=ref.is_wg)
    area = None
    text = []
    for index, line in enumerate(lines):
        area_line = _STREET_LABEL.sub("", line)
        if _AREA.fullmatch(area_line):
            area = area or area_line
        elif index not in address_lines and line.casefold() not in _GENERIC_LINK and not _URL.search(line):
            text.append(line)
    return Listing(
        id=ref.id, url=ref.url, title=title, description="\n".join(dict.fromkeys(text)),
        published_at=None, address=address, district=area, is_wg=ref.is_wg,
        rent_eur=rent_eur, rent_kind=rent_kind, area_sqm=area_sqm, photo_urls=photo_urls,
    )


def _with_fallback_facts(primary: Listing, fallback: Listing) -> Listing:
    use_fallback_rent = primary.rent_eur is None or (
        primary.rent_eur == fallback.rent_eur and primary.rent_kind == "unspecified"
    )
    return replace(
        primary, title=primary.title or fallback.title,
        rent_eur=fallback.rent_eur if use_fallback_rent else primary.rent_eur,
        rent_kind=fallback.rent_kind if use_fallback_rent else primary.rent_kind,
        area_sqm=primary.area_sqm if primary.area_sqm is not None else fallback.area_sqm,
        photo_urls=listing_photo_urls((*primary.photo_urls, *fallback.photo_urls), platform="ka"),
    )


def _body_text(message, subtype: str) -> str:
    part = message.get_body(preferencelist=(subtype,))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        return ""
    return content if isinstance(content, str) else ""


def extract_listings(raw: bytes, sender: str) -> list[Listing]:
    """Extract direct WG/flat links and facts present in the received message."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    actual_sender = parseaddr(str(message.get("From", "")))[1]
    if not actual_sender or actual_sender.casefold() != sender.casefold():
        return []

    results: dict[str, Listing] = {}
    html = BeautifulSoup(_body_text(message, "html"), "html.parser")
    for node in html.select("script, style, template"):
        node.decompose()
    for link in html.select("a[href]"):
        ref = _reference(link["href"])
        if not ref:
            continue
        title = _clean(link.get_text(" "))
        if title.casefold() in _GENERIC_LINK:
            title = ""
        lines, photo_urls = _html_card(link, ref)
        listing = _listing(ref, title, lines, photo_urls)
        existing = results.get(ref.id)
        if not existing:
            results[ref.id] = listing
        elif (bool(listing.address), bool(listing.title)) > (bool(existing.address), bool(existing.title)):
            results[ref.id] = _with_fallback_facts(listing, existing)
        else:
            results[ref.id] = _with_fallback_facts(existing, listing)

    # Plain text often accompanies HTML and can contain a direct link when the
    # HTML version only contains a tracking redirect. No redirects are opened.
    for block in re.split(r"\n\s*\n", _body_text(message, "plain")):
        refs = {ref.id: ref for url in _URL.findall(block) if (ref := _reference(url)) is not None}
        if len(refs) != 1:
            # A digest that uses one unbroken block can still provide links,
            # but its lines cannot be safely attributed to a particular flat.
            for ref in refs.values():
                results.setdefault(ref.id, _listing(ref, "", []))
            continue
        ref = next(iter(refs.values()))
        lines = [_clean(line) for line in block.splitlines() if line.strip()]
        listing = _listing(ref, "", lines[:24])
        existing = results.get(ref.id)
        if not existing:
            results[ref.id] = listing
        elif not existing.address and listing.address:
            results[ref.id] = _with_fallback_facts(listing, existing)
        else:
            results[ref.id] = _with_fallback_facts(existing, listing)
    return list(results.values())
