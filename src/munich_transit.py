"""Check Munich public transport connections using MOTIS and OpenStreetMap."""

import json
import math
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_TRANSIT_API_URL = "https://api.transitous.org/api"
# Station entrances as coordinates keep them independent of feed-specific stop IDs.
STATIONS = ("48.14021,11.55853", "48.12806,11.60365")
USER_AGENT = "MietRadar/1.0 (+https://github.com/T-Berger/MietRadar)"
MUNICH_TIME = ZoneInfo("Europe/Berlin")
MAX_DURATION_SECONDS = 30 * 60
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


def _route_within_limit(itinerary, departure: datetime) -> bool | None:
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
        return False
    if any(leg.get("cancelled") for leg in legs):
        return False
    try:
        start = datetime.fromisoformat(itinerary["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(itinerary["endTime"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        return None
    return start >= departure and (end - departure).total_seconds() <= MAX_DURATION_SECONDS


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
        self._routes: dict[tuple[str, datetime], tuple[float, bool]] = {}

    def qualifies(self, origin: str, now: datetime) -> bool | None:
        """Return whether either station is within 30 minutes, or None if unknown."""
        address = _ADDRESS.fullmatch(" ".join((origin or "").split()))
        if not address or address["city"].casefold() not in _CITY_NAMES:
            return None

        canonical = f"{address['street']} {address['number'].strip()}, {address['postcode']} München"
        departure = _next_weekday_departure(now)
        clock = time.monotonic()
        key = (canonical, departure)
        if (cached_route := self._routes.get(key)) is not None and cached_route[0] > clock:
            return cached_route[1]

        if (cached_point := self._geocodes.get(canonical)) is not None and cached_point[0] > clock:
            coordinates = cached_point[1]
        else:
            matches = self._get_json("v1/geocode", {"text": canonical, "type": "ADDRESS", "language": "de"})
            coordinates = _exact_coordinates(matches, address)
            if isinstance(matches, list):
                self._cache(self._geocodes, canonical, clock + _DAY, coordinates)
        if not coordinates:
            return None

        outcomes = []
        for station in STATIONS:
            outcome = self._station_qualifies(coordinates, station, departure)
            if outcome is True:
                self._cache(self._routes, key, clock + 600, True)
                return True
            outcomes.append(outcome)
        if None in outcomes:
            return None
        self._cache(self._routes, key, clock + 600, False)
        return False

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

    def _station_qualifies(self, origin: str, station: str, departure: datetime) -> bool | None:
        payload = self._get_json("v6/plan", {
            "fromPlace": origin,
            "toPlace": station,
            "time": departure.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "transitModes": ",".join(_TRANSIT_MODES),
            "directModes": "",
            "maxTravelTime": "30",
            "searchWindow": str(MAX_DURATION_SECONDS),
            "useRoutedTransfers": "true",
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("itineraries"), list):
            return None
        unknown = False
        for itinerary in payload["itineraries"]:
            outcome = _route_within_limit(itinerary, departure)
            if outcome is True:
                return True
            unknown = unknown or outcome is None
        return None if unknown else False
