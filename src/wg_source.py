"""Read public WG-Gesucht search and listing pages."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from typing import Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from listing_photos import listing_photo_urls


BASE_URL = "https://www.wg-gesucht.de"
DEFAULT_SEARCH_URLS = (
    f"{BASE_URL}/wg-zimmer-in-Muenchen.90.0.1.0.html",
    f"{BASE_URL}/1-zimmer-wohnungen-in-Muenchen.90.1.1.0.html",
    f"{BASE_URL}/wohnungen-in-Muenchen.90.2.1.0.html",
)
_OFFER_CATEGORIES = r"(?:wg-zimmer|1-zimmer-wohnungen|wohnungen|haeuser)"
_DETAIL_PATTERN = re.compile(
    rf"/(?:{_OFFER_CATEGORIES}-in-[\w-]+\.)?(?P<id>[0-9]+)\.html",
    re.IGNORECASE,
)
_SEARCH_PATTERN = re.compile(
    rf"/(?:angebote\.html|{_OFFER_CATEGORIES}(?:-und-{_OFFER_CATEGORIES})*-in-[\w-]+"
    r"\.[0-9]+\.[0-9]+(?:\+[0-9]+)*\.1\.[0-9]+\.html)",
    re.IGNORECASE,
)
_COMPANY_PROFILE = re.compile(r"/profile/company/[^/]+\.[0-9]+\.html", re.I)
_MANAGING_ROLE = (
    r"(?:hausverwaltung|immobilienverwaltung|verwaltung|(?:haus)?verwalter(?:in|:in)?|"
    r"(?:immobilien)?makler(?:in|:in)?|vermieter(?:in|:in)?|eigent[üu]mer(?:in|:in)?)"
)
_RESIDENT_ROLE = r"(?:mitbewohner|bewohner)(?:(?:\*|:)?in(?:nen)?)?"
_ROLE_VALUE = rf"(?:d(?:er|ie)\s+)?(?P<role>{_MANAGING_ROLE}|{_RESIDENT_ROLE})[.!]?"
_CONTACT_PANEL_ROLE = re.compile(_ROLE_VALUE, re.I)
_CONTACT_ROLE = re.compile(
    rf"(?:ansprechpartner(?:in|:in)?|kontakt(?:person)?|rolle)\s*:\s*{_ROLE_VALUE}",
    re.I,
)
_EXPLICIT_CONTACT_ROLE = re.compile(
    rf"(?:kontakt(?:aufnahme)?\s+(?:direkt\s+)?über|ansprechpartner(?:in|:in)?\s+ist)\s+"
    rf"(?:d(?:ie|en|er)\s+)?(?P<role>{_MANAGING_ROLE}|{_RESIDENT_ROLE})[.!]?",
    re.I,
)
_MANAGER_VOICE = re.compile(
    rf"(?:ich bin|wir sind)\s+(?:(?:der|die|ein(?:e)?)\s+)?{_MANAGING_ROLE}\b"
    r"|(?:ich verwalte|wir verwalten)\s+(?:d(?:iese[sn]?|er|ie|as)\s+)?(?:wohnung|haus|immobilie|wg|objekt)\b"
    r"|(?:ich vermiete|wir vermieten)\b[^.!?\n]{0,70}\bim auftrag (?:des|der)\s+"
    r"(?:eigent[üu]mer|hausverwaltung|vermieter)\w*\b",
    re.I,
)
_HOUSEMATE_VOICE = re.compile(
    r"(?:ich wohne|wir wohnen)\s+[^.!?\n]{0,75}\b(?:hier|in (?:der|dieser|unserer)\s+(?:wg|wohnung))\b"
    rf"|wir sind\s+(?:eure|deine|die)\s+(?:zukünftigen|neuen)\s+{_RESIDENT_ROLE}\b"
    r"|wir suchen\s+[^.!?\n]{0,65}\bmitbewohner\w*\s+für\s+unsere\s+wg\b"
    r"|ich bin\s+[^.!?\n]{1,45}\bund wohne\s+[^.!?\n]{0,65}\b(?:hier|in (?:der|dieser|unserer)\s+(?:wg|wohnung))\b",
    re.I,
)
_NO_LONGER_RESIDENT = re.compile(r"\b(?:nicht|kein\w*|bald|demnächst|erst ab)\b", re.I)
_BERLIN = ZoneInfo("Europe/Berlin")
_LAYOUT_LIMIT = 4 * 1024 * 1024
_NUMBER = r"(?:[0-9]{1,3}(?:[. ][0-9]{3})+(?:,[0-9]{1,2})?|[0-9]{1,7}(?:[,.][0-9]{1,2})?)"
_RENT_VALUE = re.compile(rf"(?P<number>{_NUMBER})\s*(?:€|EUR|Euro)(?:\s*(?:/\s*Monat|monatlich))?(?:\s*VB)?", re.I)
_AREA_VALUE = re.compile(rf"(?P<number>{_NUMBER})\s*(?:m(?:²|\^?2)|qm)", re.I)
_RENT_KINDS = {
    "warmmiete": "warm", "gesamtmiete": "total", "bruttowarmmiete": "warm",
    "kaltmiete": "cold", "nettokaltmiete": "cold", "miete": "unspecified", "preis": "unspecified",
}
RentKind = Literal["warm", "total", "cold", "unspecified"]
RecipientRole = Literal["manager", "roommate", "unknown"]


class WGSourceError(Exception):
    """The public site failed to return usable data."""


class WGAccessBlocked(WGSourceError):
    """The site returned an access challenge or rate limit."""


class WGListingGone(WGSourceError):
    """The listing is no longer available."""


def _public_parts(url):
    try:
        parts = urlsplit(urljoin(BASE_URL, url))
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or parts.netloc.lower() not in {"www.wg-gesucht.de", "wg-gesucht.de"}
    ):
        return None
    return parts


@dataclass(frozen=True)
class ListingRef:
    id: str
    url: str

    @classmethod
    def from_url(cls, url: str) -> "ListingRef | None":
        if not url.startswith(("/", "https://", "http://")):
            return None
        parts = _public_parts(url)
        path = unquote(parts.path) if parts else ""
        if not (match := _DETAIL_PATTERN.fullmatch(path)):
            return None
        return cls(match["id"], BASE_URL + quote(path))


@dataclass(frozen=True)
class Listing:
    id: str
    url: str
    title: str
    description: str
    published_at: datetime | None
    address: str | None
    wanted: str | None = None
    resident_ages: str | None = None
    wg_type: str | None = None
    district: str | None = None
    is_wg: bool = False
    published_day: date | None = None
    rent_eur: float | None = None
    rent_kind: RentKind | None = None
    area_sqm: float | None = None
    photo_urls: tuple[str, ...] = ()
    available_from: date | None = None
    available_until: date | None = None
    is_commercial: bool = False
    recipient_role: RecipientRole = "unknown"


def _search_url(url):
    parts = _public_parts(url)
    path = unquote(parts.path) if parts else ""
    if not _SEARCH_PATTERN.fullmatch(path):
        raise ValueError("WG_SEARCH_URLS muss auf öffentliche WG-Gesucht-Suchergebnisse zeigen.")
    return urlunsplit(("https", urlsplit(BASE_URL).netloc, quote(path, safe="/+"), parts.query, ""))


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        previous = ListingRef.from_url(request.full_url)
        if previous:
            target = ListingRef.from_url(new_url)
            if target and target.id == previous.id:
                return super().redirect_request(
                    request, response, code, message, headers, target.url
                )
        else:
            try:
                safe_url = _search_url(new_url)
            except ValueError:
                pass
            else:
                return super().redirect_request(
                    request, response, code, message, headers, safe_url
                )
        raise WGAccessBlocked("WG-Gesucht leitet von der öffentlichen Seite weg.")


def _clean(text):
    return " ".join(text.split())


def _text(node):
    return _clean(node.get_text(" ", strip=True)) if node else ""


def _positive_number(value: float, ceiling: int) -> float | None:
    return value if 0 < value <= ceiling else None


def _displayed_quantity(pattern: re.Pattern[str], text: str, ceiling: int) -> float | None:
    match = pattern.fullmatch(_clean(text))
    if not match:
        return None
    number = match["number"]
    if "," in number:
        number = number.replace(".", "").replace(" ", "").replace(",", ".")
    elif re.fullmatch(r"[0-9]{1,3}(?:[. ][0-9]{3})+", number):
        number = number.replace(".", "").replace(" ", "")
    return _positive_number(float(number), ceiling)


def parse_rent_eur(text: str) -> float | None:
    return _displayed_quantity(_RENT_VALUE, text, 1_000_000)


def parse_area_sqm(text: str) -> float | None:
    return _displayed_quantity(_AREA_VALUE, text, 10_000)


def rent_kind_for_label(label: str) -> RentKind | None:
    return _RENT_KINDS.get(_clean(label).casefold().rstrip(" :"))


def _publication(value) -> datetime | date | None:
    if not isinstance(value, str):
        return None
    value = _clean(value)
    if not re.search(r"\d{1,2}:\d{2}", value):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return date.fromisoformat(value)
            if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
                return datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            pass
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        result = None
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y, %H:%M", "%d.%m.%Y um %H:%M"):
            try:
                result = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        if result is None:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=_BERLIN)
    return result.astimezone(timezone.utc)


def _structured_listings(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        if not script.string:
            continue
        try:
            document = json.loads(script.string.strip().removesuffix(";"))
        except (TypeError, ValueError):
            continue
        for entry in document if isinstance(document, list) else [document]:
            if isinstance(entry, dict) and entry.get("@type") == "RealEstateListing":
                yield entry


def _structured_ad(soup, listing_id):
    for item in _structured_listings(soup):
        url = item.get("url")
        ref = ListingRef.from_url(url) if isinstance(url, str) else None
        if ref and ref.id == listing_id:
            yield item


def _detail_photos(soup, listing_id: str) -> tuple[str, ...]:
    urls = []
    for image in soup.select("#WG-Pictures #gallery_slides .sp-slide img.sp-image"):
        variants = listing_photo_urls((image.get(key) for key in ("data-medium", "data-src", "data-large", "src")),
                                      platform="wg")
        if variants:
            urls.append(variants[0])
    if urls:
        return listing_photo_urls(urls, platform="wg")

    # Machine-readable photos belong to this specific listing or its page preview.
    for ad in _structured_ad(soup, listing_id):
        images = ad.get("image")
        for image in images if isinstance(images, list) else [images]:
            if isinstance(image, dict):
                urls.extend((image.get("contentUrl"), image.get("url")))
            else:
                urls.append(image)
    urls.extend(meta.get("content") for meta in soup.select('meta[property="og:image"]'))
    return listing_photo_urls(urls, platform="wg")


def _machine_quantity(value, ceiling: int) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _positive_number(value, ceiling)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        return _positive_number(float(value), ceiling)
    return None


def _section_value(label) -> str:
    next_column = label.parent.find_next_sibling()
    return _text(next_column.select_one(".section_panel_value")) if next_column else ""


def _detail_facts(soup, listing_id: str, *, is_wg: bool) -> tuple[float | None, RentKind | None, float | None]:
    rent: dict[RentKind, float] = {}
    area = None
    area_labels = {"größe", "zimmergröße", "zimmerfläche"} if is_wg else {"größe", "wohnfläche", "wohnungsgröße"}
    for label in soup.select(".key_fact_detail, .section_panel_detail"):
        name = _text(label).lower().rstrip(" :")
        kind = rent_kind_for_label(name)
        if not kind and name not in area_labels:
            continue
        if "key_fact_detail" in label.get("class", []):
            value = _text(label.parent.select_one(".key_fact_value"))
        else:
            value = _section_value(label)
        if kind and (amount := parse_rent_eur(value)) is not None:
            rent.setdefault(kind, amount)
        if name in area_labels and area is None:
            area = parse_area_sqm(value)

    if not rent or area is None:
        for ad in _structured_ad(soup, listing_id):
            offers = ad.get("offers")
            if not rent and isinstance(offers, dict) and offers.get("priceCurrency") == "EUR":
                amount = _machine_quantity(offers.get("price"), 1_000_000)
                if amount is not None:
                    rent["unspecified"] = amount
            entity = ad.get("mainEntity")
            # A WG listing may advertise a room in a much larger apartment.
            if area is None and isinstance(entity, dict) and (not is_wg or entity.get("@type") == "Room"):
                size = entity.get("floorSize")
                if isinstance(size, dict) and size.get("unitCode") == "MTK":
                    area = _machine_quantity(size.get("value"), 10_000)

    for kind in ("warm", "total", "cold", "unspecified"):
        if kind in rent:
            return rent[kind], kind, area
    return None, None, area


def _detail_availability(soup) -> tuple[date | None, date | None]:
    heading = next((
        item for item in soup.select("h2.section_panel_title")
        if _text(item).casefold().rstrip(":") == "verfügbarkeit"
    ), None)
    if heading is None:
        return None, None
    dates = {}
    for label in heading.parent.select(".section_panel_detail"):
        name = _text(label).casefold().rstrip(" :")
        if name not in {"frei ab", "frei bis"}:
            continue
        try:
            dates.setdefault(name, datetime.strptime(_section_value(label), "%d.%m.%Y").date())
        except ValueError:
            continue
    return dates.get("frei ab"), dates.get("frei bis")


def _online_age(text):
    """The public update time is useful only as a tie-breaker for visiting ads."""
    match = re.fullmatch(
        r"(?:vor\s+)?(\d{1,6})\s*(Min(?:ute[n]?)?|Std|Stunden?|Tag(?:e[n]?)?)\.?",
        text,
        re.I,
    )
    if match is None:
        return timedelta.max
    count, unit = match.groups()
    multiplier = {"m": 1, "s": 60, "t": 1440}[unit.lower()[0]]
    return timedelta(minutes=int(count) * multiplier)


def _publication_fields(soup, listing_id):
    for element in soup.select('[itemprop="datePosted"], meta[property="article:published_time"]'):
        yield element.get("datetime") or element.get("content") or _text(element)
    for item in _structured_ad(soup, listing_id):
        yield item.get("datePosted")
    for label in soup.select("th, td"):
        if _text(label).casefold().rstrip(":") in {"veröffentlicht am", "inseriert am", "erstellt am"}:
            yield _text(label.find_next_sibling(["td", "th"]))


def _details_publication(soup, listing_id) -> tuple[datetime | None, date | None]:
    published_day = None
    for field in _publication_fields(soup, listing_id):
        published = _publication(field)
        if isinstance(published, datetime):
            return published, None
        if published is not None and published_day is None:
            published_day = published
    return None, published_day


def _detail_address(soup):
    block = soup.select_one('a[href="#map_container"] .section_panel_detail')
    if block is None:
        return None, None
    fields = [_clean(text) for text in block.get_text("\n", strip=True).splitlines() if text.strip()]
    if not fields:
        return None, None
    city = re.fullmatch(r"(\d{5})\s+(?:München|Munich)(?:\s+(.*))?", fields[-1], re.I)
    district = city[2] if city else None
    street = " ".join(fields[:-1])
    # Routing needs a published house number. A street or district alone is imprecise.
    if city is None or not re.fullmatch(r".+\s+\d{1,4}\s*[a-z]?(?:\s*[-–/]\s*\d{1,4}\s*[a-z]?)?", street, re.I):
        return None, district
    return f"{street}, {city[1]} München", district


def _detail_group(soup, label):
    for heading in soup.select("h3.headline"):
        if _text(heading).rstrip(":") == label:
            return [_text(field) for field in heading.parent.select("li .section_panel_detail")]
    return []


def _commercial_provider(soup) -> bool:
    # Company profiles in the contact panel identify the poster. A company
    # mentioned in the listing body or a sponsor elsewhere does not.
    for link in soup.select(".rhs_contact_information a[href], .contact_box_sticky a[href], .bottom_contact_box a[href]"):
        parts = _public_parts(link["href"])
        if parts and _COMPANY_PROFILE.fullmatch(unquote(parts.path)):
            return True
    return False


def listing_recipient_role(
    description: str, *, commercial: bool = False, contact_roles: Sequence[str] = (),
) -> RecipientRole:
    """Identify the addressee using contact labels or a clear first-person statement."""
    statements = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n", description) if part.strip()]
    marked: set[RecipientRole] = set()
    for lines, patterns in ((contact_roles, (_CONTACT_PANEL_ROLE, _CONTACT_ROLE, _EXPLICIT_CONTACT_ROLE)),
                            (statements, (_CONTACT_ROLE, _EXPLICIT_CONTACT_ROLE))):
        for line in lines:
            normalized = _clean(line)
            marker = next((match for pattern in patterns if (match := pattern.fullmatch(normalized))), None)
            if not marker:
                continue
            marked.add("manager" if re.fullmatch(_MANAGING_ROLE, marker["role"], re.I) else "roommate")
    if marked:
        return next(iter(marked)) if len(marked) == 1 else "unknown"

    manager = any(_MANAGER_VOICE.match(part) for part in statements)
    roommate = any((match := _HOUSEMATE_VOICE.match(part)) and not _NO_LONGER_RESIDENT.search(match[0])
                   for part in statements)
    if manager and roommate:
        return "unknown"
    if roommate:
        return "roommate"
    return "manager" if manager or commercial else "unknown"


def _parse_candidates(soup):
    results = []
    for card in soup.select("a.wgg-list-card.offer_list_item[data-id]"):
        if (
            "premium_sticky_asset" in card.get("class", [])
            or card.get("data-campaign_partner", "").strip()
        ):
            continue
        ref = ListingRef.from_url(card.get("href", ""))
        if ref is None or ref.id != card["data-id"]:
            continue
        online = _text(card.select_one(".card-offer-online-status .card-subtitle"))
        results.append((ref, _online_age(online)))
    return results


def _next_page(soup, current_url):
    current = urlsplit(current_url)
    current_page = re.fullmatch(r"(.*\.)(\d+)\.html", current.path, re.I)
    for link in soup.select('a[rel~="next"][href], .text-center > a.black_text_link[href]'):
        if "next" not in link.get("rel", []) and _text(link) not in {">", "›", "»"}:
            continue
        try:
            target = _search_url(urljoin(current_url, link["href"]))
        except ValueError:
            continue
        if not current_page:
            return target
        prefix, page_number = current_page.groups()
        if urlsplit(target).path.lower() == f"{prefix}{int(page_number) + 1}.html".lower():
            return target
    return None


class WGSource:
    def __init__(self, search_urls: Sequence[str], pages_per_search: int = 2, min_request_interval: float = 1.0):
        if not 1 <= pages_per_search <= 3:
            raise ValueError("pages_per_search muss zwischen 1 und 3 liegen.")
        self.search_urls = tuple(dict.fromkeys(_search_url(url) for url in search_urls))
        if not self.search_urls:
            raise ValueError("Mindestens eine WG-Gesucht-Such-URL ist erforderlich.")
        self.pages_per_search = pages_per_search
        self.min_request_interval = min_request_interval
        self._last_request = 0.0
        self._opener = build_opener(_PublicRedirect())

    @property
    def inventory_scope(self) -> str:
        coverage = json.dumps([sorted(self.search_urls), self.pages_per_search])
        return hashlib.sha256(coverage.encode()).hexdigest()

    def _get_html(self, url, *, detail=False):
        wait = self._last_request + self.min_request_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        request = Request(url, headers={
            "User-Agent": "MietRadar/1.0 (personal rental search)",
            "Accept": "text/html",
            "Accept-Language": "de-DE, de;q=0.9",
        })
        try:
            with self._opener.open(request, timeout=25) as response:
                document = response.read(_LAYOUT_LIMIT + 1)
        except HTTPError as error:
            error.close()
            if error.code in {401, 403, 429}:
                raise WGAccessBlocked(f"WG-Gesucht hat die Anfrage abgelehnt (HTTP {error.code}).") from error
            if detail and error.code in {404, 410}:
                raise WGListingGone("WG-Gesucht-Anzeige ist nicht mehr verfügbar.") from error
            raise WGSourceError(f"WG-Gesucht antwortet mit HTTP {error.code}.") from error
        except (URLError, OSError, HTTPException) as error:
            raise WGSourceError("WG-Gesucht ist gerade nicht erreichbar.") from error
        if len(document) > _LAYOUT_LIMIT:
            raise WGSourceError("WG-Gesucht hat eine zu große Antwort geliefert.")
        soup = BeautifulSoup(document, "html.parser", from_encoding="utf-8")
        title = _text(soup.title).casefold()
        headings = " ".join(_text(heading).casefold() for heading in soup.select("title, h1, h2"))
        if title == "überprüfung" or any(marker in headings for marker in (
            "bestätigen sie, dass sie ein mensch sind", "warum erscheint diese seite",
            "zugriff verweigert", "access denied", "just a moment",
        )):
            raise WGAccessBlocked("WG-Gesucht fordert eine menschliche Überprüfung.")
        return soup

    def candidates(self) -> list[ListingRef]:
        candidates = []
        for starting_url in self.search_urls:
            url = starting_url
            visited = set()
            for _ in range(self.pages_per_search):
                if url is None or url in visited:
                    break
                visited.add(url)
                soup = self._get_html(url)
                page = _parse_candidates(soup)
                if not page and not soup.select_one("a.wgg-list-card.offer_list_item[data-id]"):
                    headings = " ".join(_text(heading).casefold() for heading in soup.select("h1, h2, h3"))
                    if not re.search(r"keine (?:passenden )?(?:angebote|anzeigen)|\b0 (?:wg-zimmer|angebote|wohnungen)", headings):
                        raise WGSourceError("WG-Gesucht hat keine erkennbaren Suchergebnisse geliefert.")
                candidates.extend(page)
                url = _next_page(soup, url)

        # Detail requests have a separate budget. The visible update time only sets visit order.
        candidates.sort(key=lambda item: item[1])
        unique = {}
        for ref, _ in candidates:
            unique.setdefault(ref.id, ref)
        return list(unique.values())

    def details(self, ref: ListingRef) -> Listing:
        safe = ListingRef.from_url(ref.url)
        if safe is None or safe.id != ref.id:
            raise ValueError("Ungültige WG-Gesucht-Anzeige.")
        soup = self._get_html(safe.url, detail=True)
        canonical = soup.select_one('link[rel="canonical"][href]')
        canonical_ref = ListingRef.from_url(canonical["href"]) if canonical else None
        if canonical_ref and canonical_ref.id != ref.id:
            raise WGListingGone("WG-Gesucht zeigt eine andere Anzeige an.")
        listing_url = canonical_ref.url if canonical_ref else safe.url
        title = _text(soup.select_one("h1.detailed-view-title"))
        if not title:
            if re.search(
                r"(?:anzeige|angebot).{0,50}(?:nicht mehr verfügbar|nicht verfügbar|deaktiviert|gelöscht)",
                _text(soup).casefold(),
            ):
                raise WGListingGone("WG-Gesucht-Anzeige ist nicht mehr verfügbar.")
            raise WGSourceError("WG-Gesucht hat keine erkennbare Anzeigenseite geliefert.")
        description = "\n".join(
            text for index in range(4)
            if (text := _text(soup.select_one(f"#freitext_{index}")))
        )
        address, district = _detail_address(soup)
        residents = _detail_group(soup, "Die WG")
        wanted = " · ".join(_detail_group(soup, "Gesucht wird")) or None
        resident_ages = next(
            (field.partition(":")[2].strip() for field in residents if field.startswith("Bewohneralter:")),
            None,
        )
        wg_type = next(
            (
                field for field in residents
                if re.search(r"[-\w]+-WG\b|\bgemischte WG\b|\bWohnheim\b", field, re.I)
            ),
            None,
        )
        published_at, published_day = _details_publication(soup, ref.id)
        available_from, available_until = _detail_availability(soup)
        is_wg = bool(residents or wanted or urlsplit(listing_url).path.startswith("/wg-zimmer-"))
        rent_eur, rent_kind, area_sqm = _detail_facts(soup, ref.id, is_wg=is_wg)
        commercial = _commercial_provider(soup)
        contact_roles = [
            text for panel in soup.select(".rhs_contact_information, .contact_box_sticky, .bottom_contact_box")
            for text in panel.stripped_strings
        ]
        return Listing(
            id=ref.id,
            url=listing_url,
            title=title,
            description=description,
            published_at=published_at,
            address=address,
            wanted=wanted,
            resident_ages=resident_ages,
            wg_type=wg_type,
            district=district,
            is_wg=is_wg,
            published_day=published_day,
            rent_eur=rent_eur,
            rent_kind=rent_kind,
            area_sqm=area_sqm,
            photo_urls=_detail_photos(soup, ref.id),
            available_from=available_from,
            available_until=available_until,
            is_commercial=commercial,
            recipient_role=listing_recipient_role(description, commercial=commercial, contact_roles=contact_roles),
        )
