"""Check Munich public transport connections using MOTIS and OpenStreetMap."""

import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_TRANSIT_API_URL = "https://api.transitous.org/api"
# Station entrances as coordinates keep them independent of feed-specific stop IDs.
HAUPTBAHNHOF = "48.14021,11.55853"
OSTBAHNHOF = "48.12806,11.60365"
STATIONS = (HAUPTBAHNHOF, OSTBAHNHOF)
USER_AGENT = "MietRadar/1.0 (+https://github.com/T-Berger/MietRadar)"
MUNICH_TIME = ZoneInfo("Europe/Berlin")
MAX_DURATION_SECONDS = 30 * 60
SECONDARY_DURATION_SECONDS = 45 * 60
MAX_REQUESTS_PER_DAY = 300
_DAY = 24 * 60 * 60
_CITY_NAMES = {"münchen", "muenchen", "munich"}
_TRANSIT_MODES = ("BUS", "TRAM", "SUBWAY", "SUBURBAN", "REGIONAL_RAIL")
_ADDRESS = re.compile(
    r"(?P<street>[^,]+?)\s+(?P<number>[0-9]{1,4}\s*[a-zA-Z]?"
    r"(?:\s*[-–—/]\s*[0-9]{1,4}\s*[a-zA-Z]?)?),\s*"
    r"(?P<postcode>8[01][0-9]{3})\s+(?P<city>[^,]+)(?:,\s*(?:Deutschland|Germany))?",
    re.IGNORECASE,
)


def _next_weekday_departure(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=MUNICH_TIME)
    local = now.astimezone(MUNICH_TIME)
    departure = local.replace(hour=8, minute=0, second=0, microsecond=0)
    while departure <= local or departure.weekday() >= 5:
        departure += timedelta(days=1)
    return departure.astimezone(timezone.utc)


def _street_key(value: str) -> str:
    street = re.sub(r"str\.$", "strasse", value.strip().casefold())
    return " ".join(street.split())


def _number_key(value: str) -> str:
    return "".join(value.casefold().split()).replace("–", "-").replace("—", "-")


def _matches_address(item, address: re.Match) -> bool:
    if not isinstance(item, dict) or item.get("type") != "ADDRESS" or item.get("country") != "DE":
        return False
    street, number, areas = item.get("street"), item.get("houseNumber"), item.get("areas")
    if not isinstance(street, str) or not isinstance(number, str) or not isinstance(areas, list):
        return False
    return (
        _street_key(street) == _street_key(address["street"])
        and _number_key(number) == _number_key(address["number"])
        and item.get("zip") == address["postcode"]
        and any(
            isinstance(area, dict)
            and area.get("adminLevel") == 6
            and str(area.get("name", "")).casefold() in _CITY_NAMES
            for area in areas
        )
    )


def _exact_coordinates(matches, address: re.Match) -> str | None:
    if not isinstance(matches, list):
        return None
    points = []
    for item in matches:
        if not _matches_address(item, address):
            continue
        lat, lon = item.get("lat"), item.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and 47.99 <= lat <= 48.30 and 11.30 <= lon <= 11.75:
            points.append((lat, lon))
    if not points:
        return None
    lat, lon = points[0]
    # OpenStreetMap can list several entrances for one address. Reject candidates
    # that disagree on the location rather than accepting a fuzzy first result.
    if any(math.hypot((other_lat - lat) * 111_000, (other_lon - lon) * 74_000) > 100
           for other_lat, other_lon in points[1:]):
        return None
    return f"{lat:.7f},{lon:.7f}"


def _route_seconds(itinerary, departure: datetime) -> float | None:
    if not isinstance(itinerary, dict):
        return None
    legs = itinerary.get("legs")
    if not isinstance(legs, list) or not legs or any(not isinstance(leg, dict) for leg in legs):
        return None
    modes = [leg.get("mode") for leg in legs]
    if any(not isinstance(mode, str) for mode in modes):
        return None
    if any(mode != "WALK" and mode not in _TRANSIT_MODES for mode in modes) or not any(
        mode in _TRANSIT_MODES for mode in modes
    ):
        return math.inf
    if any(leg.get("cancelled") for leg in legs):
        return math.inf
    try:
        start = datetime.fromisoformat(itinerary["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(itinerary["endTime"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        return None
    return (end - departure).total_seconds() if start >= departure else math.inf


@dataclass(frozen=True)
class _StationRoutes:
    fastest_seconds: float = math.inf
    incomplete: bool = False

    def within(self, limit: int) -> bool | None:
        if self.fastest_seconds <= limit:
            return True
        return None if self.incomplete else False


class TransitRouter:
    def __init__(self, base_url: str | None = None):
        url = ((base_url or "").strip() or DEFAULT_TRANSIT_API_URL).rstrip("/")
        parts = urlsplit(url)
        private_host = parts.hostname in ("localhost", "127.0.0.1", "::1") or (
            (parts.hostname or "").endswith(".railway.internal")
        )
        if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment
            or (parts.scheme != "https" and not (parts.scheme == "http" and private_host))):
            raise ValueError("MUNICH_TRANSIT_API_URL must be an HTTPS MOTIS URL or a private Railway/local HTTP URL")
        self.base_url = url if parts.path.endswith("/api") else f"{url}/api"
        self._requests: deque[float] = deque()
        self._retry_after = 0.0
        self._geocodes: dict[str, tuple[float, str | None]] = {}
        self._routes: dict[tuple[str, str, datetime, int], tuple[float, _StationRoutes]] = {}

    def qualifies(self, origin: str, now: datetime) -> bool | None:
        """Return whether either station is within 30 minutes, or None if unknown."""
        coordinates = self._coordinates(origin)
        if not coordinates:
            return None
        return self._qualifies(coordinates, _next_weekday_departure(now))

    def _qualifies(self, coordinates: str, departure: datetime) -> bool | None:
        incomplete = False
        for station in STATIONS:
            routes = self._routes_between(coordinates, station, departure, MAX_DURATION_SECONDS)
            result = routes.within(MAX_DURATION_SECONDS)
            if result is True:
                return True
            incomplete |= result is None
        return None if incomplete else False

    def classify(self, origin: str, now: datetime) -> tuple[Literal["a", "b", "c", "outside"] | None, int | None]:
        """Classify the commute. B and C measure from the station to the listing."""
        coordinates = self._coordinates(origin)
        if not coordinates:
            return None, None
        departure = _next_weekday_departure(now)
        primary = self._qualifies(coordinates, departure)
        if primary is True:
            return "a", None
        if primary is None:
            return None, None
        for station, tier in ((OSTBAHNHOF, "b"), (HAUPTBAHNHOF, "c")):
            routes = self._routes_between(station, coordinates, departure, SECONDARY_DURATION_SECONDS)
            result = routes.within(SECONDARY_DURATION_SECONDS)
            if result is None:
                return None, None
            if result:
                return tier, math.ceil(routes.fastest_seconds / 60)
        return "outside", None

    def _coordinates(self, origin: str) -> str | None:
        address = _ADDRESS.fullmatch(" ".join((origin or "").split()))
        if not address or address["city"].casefold() not in _CITY_NAMES:
            return None

        canonical = f"{address['street']} {address['number'].strip()}, {address['postcode']} München"
        clock = time.monotonic()
        if (cached_point := self._geocodes.get(canonical)) is not None and cached_point[0] > clock:
            return cached_point[1]
        matches = self._get_json("v1/geocode", {"text": canonical, "type": "ADDRESS", "language": "de"})
        coordinates = _exact_coordinates(matches, address)
        if isinstance(matches, list):
            self._cache(self._geocodes, canonical, clock + _DAY, coordinates)
        return coordinates

    @staticmethod
    def _cache(cache, key, expires_at, value) -> None:
        if key not in cache and len(cache) >= 1024:
            cache.pop(next(iter(cache)))
        cache[key] = (expires_at, value)

    def _get_json(self, endpoint: str, params: dict[str, str]):
        now = time.monotonic()
        while self._requests and self._requests[0] <= now - _DAY:
            self._requests.popleft()
        if now < self._retry_after or len(self._requests) >= MAX_REQUESTS_PER_DAY:
            return None

        request = Request(
            f"{self.base_url}/{endpoint}?{urlencode(params)}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._requests.append(now)
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read(4 * 1024 * 1024))
        except HTTPError as error:
            with error:
                if error.code == 429:
                    retry = error.headers.get("Retry-After", "") if error.headers else ""
                    self._retry_after = now + (max(60, int(retry)) if retry.isdecimal() else 900)
            return None
        except (OSError, ValueError, HTTPException):
            return None

    def _routes_between(self, origin: str, destination: str, departure: datetime, limit: int) -> _StationRoutes:
        clock = time.monotonic()
        key = (origin, destination, departure, limit)
        if (cached := self._routes.get(key)) is not None and cached[0] > clock:
            return cached[1]

        payload = self._get_json("v6/plan", {
            "fromPlace": origin,
            "toPlace": destination,
            "time": departure.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "transitModes": ",".join(_TRANSIT_MODES),
            "directModes": "",
            "maxTravelTime": str(limit // 60),
            "searchWindow": str(limit),
            "useRoutedTransfers": "true",
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("itineraries"), list):
            return _StationRoutes(incomplete=True)
        fastest = math.inf
        incomplete = False
        for itinerary in payload["itineraries"]:
            seconds = _route_seconds(itinerary, departure)
            if seconds is None:
                incomplete = True
            else:
                fastest = min(fastest, seconds)
        result = _StationRoutes(fastest, incomplete)
        if not incomplete or fastest <= limit:
            self._cache(self._routes, key, clock + 600, result)
        return result
