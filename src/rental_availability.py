"""Filter rental offers by move-in date and minimum usable lease duration."""

import calendar
import re
import sqlite3
from datetime import date, timedelta


LATE_MOVE_IN = "Einzug erst ab dem nächsten Kalenderjahr"
SHORT_RENTAL = "Zwischenmiete unter vier Monaten"

_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}
_DATE = (
    rf"(?:20\d{{2}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\s*\d{{1,2}}\.(?:\s*(?:20\d{{2}}|\d{{2}})(?!\d|\.\d))?"
    rf"|(?:\d{{1,2}}\.\s*)?(?:{'|'.join(_MONTHS)})(?:\s+20\d{{2}})?"
    r"|(?:anfang|mitte|ende)\s+(?:\w+)(?:\s+20\d{2})?"
    r"|20\d{2}|sofort|jetzt|heute|nächst(?:en|em|es)\s+(?:kalender)?jahr)"
)
_START_LABEL = (
    r"(?:frei|verfügbar|beziehbar|mietbar|bezugsfrei)\s+(?:erst\s+)?ab"
    r"|(?:einzug|mietbeginn)\s*(?::\s*|(?:ist\s+)?(?:erst\s+)?ab\s+|(?:ist\s+)?erst\s+)"
)
_END_LABEL = r"(?:frei|verfügbar|mietbar|befristet)\s+bis|(?:mietende|auszug)\s*:\s*"
_RENT = r"(?:zwischenmiet\w*|untermiet\w*|untervermiet\w*|zwischenvermiet\w*|befristet\w*|mietdauer)"
_QUALIFIER = r"(?:maximal|höchstens|nur|ca\.?|etwa|knapp|bis zu)"
_NUMBER = r"(?:\d{1,3}(?:[,.]\d+)?|ein(?:e[nm]?)?|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|halb)"
_DURATION = rf"(?P<number>{_NUMBER})(?:\s*(?:[-–—]|bis|oder)\s*(?P<maximum>{_NUMBER}))?\s*[- ]?\s*(?P<unit>monat|woche|tag)\w*"
_RENT_PERIOD = re.compile(
    rf"\b{_RENT}\s*(?::\s*|(?:für|auf)\s+|\s+)(?:{_QUALIFIER}\s+)?{_DURATION}\b", re.I,
)
_PERIOD_BEFORE_RENT = re.compile(
    rf"\b{_DURATION}\s+(?:{_RENT}|(?:befristet|zur zwischenmiete)\b)", re.I,
)
_RENT_RANGE = re.compile(
    rf"\b(?:{_RENT}|(?:einzug|mietzeitraum))\s*[: ]\s*(?:(?:ab|vom?|dem)\s+)?"
    rf"(?P<start>{_DATE})\s*(?:bis(?:\s+zum)?|[-–—])\s*(?P<end>{_DATE})(?![\w])", re.I,
)
_OPEN_END = re.compile(
    r"\b(?:verlängerung(?: ist)? möglich|mit (?:der )?option auf verlängerung|kann verlängert werden|"
    r"mit übernahmeoption|(?:danach|anschließend) unbefristet)\b", re.I,
)
_OTHER_PURPOSE = re.compile(r"^\s+(?:(?:erste?|mögliche)\s+)?(?:besichtigung|bewerbung|anfrage)\w*\b", re.I)
_OTHER_TITLE_PURPOSE = re.compile(r"\b(?:besichtigung|bewerbung|anfrage)\w*\s+(?:erst\s+)?ab\b", re.I)
_NUMBERS = {
    "ein": 1, "eine": 1, "einen": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "halb": 0.5,
}


def _calendar_date(raw: str, anchor: date, *, end: bool = False) -> date | None:
    value = raw.casefold().strip()
    if value in {"sofort", "jetzt", "heute"}:
        return anchor
    if value.startswith("nächst"):
        return date(anchor.year + 1, 1, 1)
    if re.fullmatch(r"20\d{2}", value):
        return date(int(value), 12, 31) if end else date(int(value), 1, 1)
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    numeric = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{2}|20\d{2}))?", value)
    if numeric:
        day, month, year = numeric.groups()
        month = int(month)
    else:
        named = re.fullmatch(r"(?:(\d{1,2})\.\s*|(?:anfang|mitte|ende)\s+)?([a-zä]+)(?:\s+(20\d{2}))?", value)
        if not named or named[2] not in _MONTHS:
            return None
        day, name, year = named.groups()
        month = _MONTHS[name]
    try:
        year_number = (int(year) + (2000 if len(year) == 2 else 0)) if year else anchor.year
        if not year and not end and anchor.month - month >= 6:
            year_number += 1
        day_number = int(day) if day else calendar.monthrange(year_number, month)[1] if end else 1
        result = date(year_number, month, day_number)
        if end and not year and result < anchor:
            result = date(year_number + 1, month, min(day_number, calendar.monthrange(year_number + 1, month)[1]))
        return result
    except ValueError:
        return None


def _label_date(text: str, prefix: str, anchor: date, *, end: bool = False, suffix: str = "") -> date | None:
    pattern = rf"(?:{prefix})\s*:?\s*(?:(?:dem|zum)\s+)?(?P<date>{_DATE})(?![\w]){suffix}"
    for match in re.finditer(pattern, text, re.I):
        if not _OTHER_PURPOSE.match(text[match.end():]):
            if value := _calendar_date(match["date"], anchor, end=end):
                return value
    return None


def _plus_four_months(start: date) -> date:
    year, month = divmod(start.year * 12 + start.month - 1 + 4, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _short_stated_period(text: str, start: date) -> bool:
    for pattern in (_RENT_PERIOD, _PERIOD_BEFORE_RENT):
        for match in pattern.finditer(text):
            if re.search(r"\b(?:ab|mind\.?|mindestens|minimum|min\.?)\s*$", text[:match.start()], re.I):
                continue
            raw = (match["maximum"] or match["number"]).casefold()
            try:
                length = _NUMBERS[raw] if raw in _NUMBERS else float(raw.replace(",", "."))
            except ValueError:
                continue
            if match["unit"].casefold() == "monat":
                return length < 4
            return length * (7 if match["unit"].casefold() == "woche" else 1) < (_plus_four_months(start) - start).days
    return False


def availability_rejection(
    title: str, description: str, today: date, *,
    available_from: date | None = None, available_until: date | None = None,
) -> str | None:
    """Reject only a clear move-in or a stated maximum rental period."""
    text = f"{title}\n{description}"
    range_match = _RENT_RANGE.search(text)
    start = available_from or _label_date(text, rf"\b(?:{_START_LABEL})", today)
    if start is None and range_match:
        start = _calendar_date(range_match["start"], today)
    if start is None and not _OTHER_TITLE_PURPOSE.search(title):
        start = _label_date(title, r"\bab\b", today)
    if start is None:
        start = _label_date(
            text, r"\b(?:(?:wird|werden)\s+(?:erst\s+)?(?:ab\s+)?|ab\b|erst\b)", today,
            suffix=r"\s+(?:frei|verfügbar|beziehbar)\b",
        )
    if start and start.year > today.year:
        return LATE_MOVE_IN

    if any(not re.search(r"\bkein\w*\s+$", text[:match.start()], re.I) for match in _OPEN_END.finditer(text)):
        return None
    if _short_stated_period(text, start or today):
        return SHORT_RENTAL
    lease_start = max(start, today) if start else today
    end = available_until or _label_date(text, rf"\b(?:{_END_LABEL})", start or today, end=True)
    if end is None and range_match:
        end = _calendar_date(range_match["end"], start or today, end=True)
    if end and lease_start <= end < _plus_four_months(lease_start) - timedelta(days=1):
        return SHORT_RENTAL
    return None


def install_listing_availability(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS listing_availability (id TEXT PRIMARY KEY, reason TEXT)")


def record_listing_availability(db: sqlite3.Connection, listing_id: str, reason: str | None) -> None:
    db.execute("""
        INSERT INTO listing_availability (id, reason) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET reason = excluded.reason
    """, (listing_id, reason))
