"""Normalize the two platforms' public listing photo URLs."""

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit


MAX_LISTING_PHOTOS = 5
_WG_HOST = "img.wg-gesucht.de"
_KLEIN_HOST = "img.kleinanzeigen.de"
_WG_PATH = re.compile(r"/media/up/(?:[\w-]+/)*[^/\\?#;]+\.(?:jpe?g|png|webp|gif|avif)", re.I)
_KLEIN_PATH = re.compile(
    r"/api/v1/prod-ads/images/[a-f0-9]{2}/[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I,
)
_KLEIN_RULE = re.compile(r"\$_[0-9]{1,3}\.(?:AUTO|JPE?G|PNG|WEBP)", re.I)


def listing_photo_urls(values: Iterable[object], *, platform: str | None = None) -> tuple[str, ...]:
    """Keep ordered, distinct public photo URLs from the selected platform CDN."""
    results = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 2048:
            continue
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            continue
        try:
            parts = urlsplit(f"https:{value}" if value.startswith("//") else value)
        except ValueError:
            continue
        if parts.scheme != "https":
            continue
        host = parts.netloc.lower()
        path = unquote(parts.path)
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            continue
        if host == _WG_HOST and platform in {None, "wg"}:
            if path.startswith("/./"):
                path = path[2:]
            if not _WG_PATH.fullmatch(path):
                continue
            key_path = re.sub(r"\.(?:sized|large|medium|small)(?=\.[^.]+$)", "", path, flags=re.I)
            query = ""
        elif host == _KLEIN_HOST and platform in {None, "ka"}:
            if not _KLEIN_PATH.fullmatch(path):
                continue
            rule = next((rule for field, rule in parse_qsl(parts.query)
                         if field == "rule" and _KLEIN_RULE.fullmatch(rule)), None)
            query = urlencode({"rule": rule}) if rule else ""
            key_path = path.lower()
        else:
            continue
        key = (host, key_path)
        if key not in seen:
            seen.add(key)
            results.append(urlunsplit(("https", host, quote(path, safe="/"), query, "")))
            if len(results) == MAX_LISTING_PHOTOS:
                break
    return tuple(results)
