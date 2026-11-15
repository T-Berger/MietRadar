"""Check a Munich listing's public transport connection to either main station."""

import json
import re
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
STATIONS = ("München Hauptbahnhof, Deutschland", "München Ostbahnhof, Deutschland")
MUNICH_TIME = ZoneInfo("Europe/Berlin")
MAX_DURATION_SECONDS = 30 * 60
_DURATION = re.compile(r"(\d+(?:\.\d{1,9})?)s")


def _next_weekday_departure(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=MUNICH_TIME)
    local = now.astimezone(MUNICH_TIME)
    departure = local.replace(hour=8, minute=0, second=0, microsecond=0)
    while departure <= local or departure.weekday() >= 5:
        departure += timedelta(days=1)
    return departure.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TransitRouter:
    def __init__(self, api_key: str | None):
        self.api_key = (api_key or "").strip()

    def qualifies(self, origin: str, now: datetime) -> bool | None:
        """Return whether either station is within 30 minutes, or None if unknown."""
        if not self.api_key or not origin or not origin.strip():
            return None

        departure = _next_weekday_departure(now)
        outcomes = []
        for station in STATIONS:
            outcome = self._station_qualifies(origin, station, departure)
            if outcome is True:
                return True
            outcomes.append(outcome)
        return None if None in outcomes else False

    def _station_qualifies(self, origin: str, station: str, departure: str) -> bool | None:
        body = {
            "origin": {"address": origin},
            "destination": {"address": station},
            "travelMode": "TRANSIT",
            "departureTime": departure,
            "computeAlternativeRoutes": True,
            "regionCode": "de",
        }
        request = Request(
            ROUTES_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": (
                    "routes.duration,fallbackInfo,geocodingResults.origin.partialMatch,"
                    "geocodingResults.origin.geocoderStatus.code"
                ),
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except (OSError, ValueError, HTTPException):
            return None

        if not isinstance(payload, dict) or "error" in payload or payload.get("fallbackInfo"):
            return None
        try:
            origin_result = payload.get("geocodingResults", {}).get("origin", {})
            geocoder_code = origin_result.get("geocoderStatus", {}).get("code")
        except AttributeError:
            return None
        if origin_result.get("partialMatch") or geocoder_code:
            return None

        routes = payload.get("routes", [])
        if not isinstance(routes, list):
            return None
        unknown_duration = False
        for route in routes:
            duration = route.get("duration") if isinstance(route, dict) else None
            match = _DURATION.fullmatch(duration) if isinstance(duration, str) else None
            if not match:
                unknown_duration = True
            elif float(match[1]) <= MAX_DURATION_SECONDS:
                return True
        return None if unknown_duration else False
