"""Read public WG-Gesucht search and listing pages."""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


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
_BERLIN = ZoneInfo("Europe/Berlin")
_LAYOUT_LIMIT = 4 * 1024 * 1024
_PARTNER_NAMES = re.compile(r"housinganywhere|spacest|medici|spotahome|uniplaces", re.I)


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


def _publication(value):
    if not isinstance(value, str):
        return None
    value = _clean(value)
    if not re.search(r"\d{1,2}:\d{2}", value):
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


def _details_publication(soup, listing_id):
    for element in soup.select('[itemprop="datePosted"], meta[property="article:published_time"]'):
        published = _publication(
            element.get("datetime") or element.get("content") or _text(element)
        )
        if published:
            return published
    for item in _structured_listings(soup):
        url = item.get("url")
        ref = ListingRef.from_url(url) if isinstance(url, str) else None
        if ref and ref.id == listing_id:
            published = _publication(item.get("datePosted"))
            if published:
                return published
    for label in soup.select("th, td"):
        if _text(label).casefold().rstrip(":") in {"veröffentlicht am", "inseriert am", "erstellt am"}:
            published = _publication(_text(label.find_next_sibling(["td", "th"])))
            if published:
                return published
    return None


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


def _parse_candidates(soup):
    results = []
    for card in soup.select("a.wgg-list-card.offer_list_item[data-id]"):
        if (
            "premium_sticky_asset" in card.get("class", [])
            or _PARTNER_NAMES.search(card.get("data-campaign_partner", ""))
        ):
            continue
        ref = ListingRef.from_url(card.get("href", ""))
        if ref is None or ref.id != card["data-id"]:
            continue
        online = _text(card.select_one(".card-offer-online-status .card-subtitle"))
        results.append((ref, _online_age(online)))
    return results


class WGSource:
    def __init__(self, search_urls: Sequence[str], max_candidates: int, min_request_interval: float = 1.0):
        if max_candidates < 1:
            raise ValueError("max_candidates muss mindestens 1 sein.")
        self.search_urls = tuple(dict.fromkeys(_search_url(url) for url in search_urls))
        if not self.search_urls:
            raise ValueError("Mindestens eine WG-Gesucht-Such-URL ist erforderlich.")
        self.max_candidates = max_candidates
        self.min_request_interval = min_request_interval
        self._last_request = 0.0
        self._opener = build_opener(_PublicRedirect())

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
        for url in self.search_urls:
            soup = self._get_html(url)
            page = _parse_candidates(soup)
            if not page and not soup.select_one("a.wgg-list-card.offer_list_item[data-id]"):
                headings = " ".join(_text(heading).casefold() for heading in soup.select("h1, h2, h3"))
                if not re.search(r"keine (?:passenden )?(?:angebote|anzeigen)|\b0 (?:wg-zimmer|angebote|wohnungen)", headings):
                    raise WGSourceError("WG-Gesucht hat keine erkennbaren Suchergebnisse geliefert.")
            candidates.extend(page)

        # Compare candidates across categories before applying the global request budget.
        # The public update time sets visit order only. Details determine publication.
        candidates.sort(key=lambda item: item[1])
        unique = {}
        for ref, _ in candidates:
            unique.setdefault(ref.id, ref)
            if len(unique) == self.max_candidates:
                break
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
        return Listing(
            id=ref.id,
            url=listing_url,
            title=title,
            description=description,
            published_at=_details_publication(soup, ref.id),
            address=address,
            wanted=wanted,
            resident_ages=resident_ages,
            wg_type=wg_type,
            district=district,
            is_wg=bool(residents or wanted or urlsplit(listing_url).path.startswith("/wg-zimmer-")),
        )
