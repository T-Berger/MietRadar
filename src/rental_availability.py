"""Filter rental offers by move-in date and minimum usable lease duration."""

import calendar
import re
import sqlite3
from datetime import date, timedelta


LATE_MOVE_IN = "Einzug erst ab dem nächsten Kalenderjahr"
SHORT_RENTAL = "Zwischenmiete unter vier Monaten"
AVAILABILITY_POLICY_VERSION = 2

_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "okt": 10, "nov": 11, "dez": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE = (
    rf"(?:20\d{{2}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\s*\d{{1,2}}\.(?:\s*(?:20\d{{2}}|\d{{2}})(?!\d|\.\d))?"
    rf"|\d{{1,2}}[./]20\d{{2}}"
    rf"|(?:(?:anfang|mitte|ende)\s+|\d{{1,2}}\.\s*)?(?:{_MONTH_NAMES})\.?(?:\s+20\d{{2}})?"
    r"|20\d{2}|sofort|jetzt|heute|nächst(?:en|em|es)\s+(?:kalender)?jahr)"
)
_START_LABEL = (
    r"(?:frei|verfügbar|beziehbar|mietbar|bezugsfrei)\s*:?\s+(?:erst\s+)?ab\b"
    r"|(?:einzug|mietbeginn|bezugsfrei)\s*(?::\s*|\s+)(?:ist\s+)?"
    r"(?:möglich\s+)?(?:(?:erst|frühestens)\s+)?(?:ab\s+)?"
)
_END_LABEL = r"(?:frei|verfügbar|mietbar|befristet)\s+bis|(?:mietende|auszug)\s*:\s*"
_RENT = r"(?:zwischenmiet\w*|untermiet\w*|untervermiet\w*|zwischenvermiet\w*|vermietung|befristet\w*|mietdauer)"
_QUALIFIER = r"(?:maximal|max\.?|höchstens|nur|ca\.?|etwa|knapp|bis zu)"
_NUMBER = r"(?:\d{1,3}(?:[,.]\d+)?|ein(?:e[nm]?)?|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|halb)"
_DURATION = rf"(?P<number>{_NUMBER})(?:\s*(?:[-–—]|bis|oder)\s*(?P<maximum>{_NUMBER}))?\s*[- ]?\s*(?P<unit>monat|woche|tag)\w*"
_RENT_PERIOD = re.compile(
    rf"\b{_RENT}\s*(?::\s*|\s+)(?:(?:ist|beträgt)\s+)?"
    rf"(?:{_QUALIFIER}\s+)?(?:(?:für|auf|von)\s+)?(?:{_QUALIFIER}\s+)?{_DURATION}\b", re.I,
)
_PERIOD_BEFORE_RENT = re.compile(
    rf"\b{_DURATION}\s+(?:{_RENT}|(?:befristet|zur zwischenmiete)\b)", re.I,
)
_DATE_RANGE = rf"(?P<start>{_DATE})\s*(?:bis(?:\s+(?:zum|einschließlich))?|[-–—])\s*(?P<end>{_DATE})(?!\w)"
_RENT_RANGE = re.compile(
    rf"\b(?:(?:{_RENT}|einzug|mietzeitraum)(?:\s*[:(]\s*|\s+)(?:(?:ab|vo[nm]|dem)\s+)?"
    r"|(?:frei|verfügbar|beziehbar)\s+(?:vo[nm]|ab)\s+"
    r"|(?:vermiete|vermieten)\s+[^.!?;\n]{0,55}?\b(?:vo[nm]|ab)\s+)"
    rf"{_DATE_RANGE}", re.I,
)
_TITLE_RANGE = re.compile(rf"\b{_DATE_RANGE}", re.I)
_OPEN_END = re.compile(
    r"\b(?:verlängerung(?: ist)? möglich|mit (?:der )?option auf verlängerung|kann verlängert werden|"
    r"mit übernahmeoption|(?:danach|anschließend) unbefristet)\b", re.I,
)
_OTHER_PURPOSE = re.compile(r"^\s+(?:(?:erste?|mögliche)\s+)?(?:besichtigung|bewerbung|anfrage)\w*\b", re.I)
_PREFERRED_DATE = re.compile(r"^\s+(?:wird\s+)?(?:bevorzugt|wünschenswert|gewünscht|ideal)\b", re.I)
_OTHER_TITLE_PURPOSE = re.compile(r"\b(?:besichtigung|bewerbung|anfrage)\w*\s+(?:erst\s+)?ab\b", re.I)
_OTHER_RENT_RANGE_PURPOSE = re.compile(r"\b(?:besichtigung|bewerbung|anfrage)\w*\b", re.I)
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
    month_year = re.fullmatch(r"(\d{1,2})[./](20\d{2})", value)
    numeric = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{2}|20\d{2}))?", value)
    if numeric:
        day, month, year = numeric.groups()
        month = int(month)
    elif month_year:
        day, month, year = None, int(month_year[1]), month_year[2]
    else:
        named = re.fullmatch(r"(?:(\d{1,2})\.\s*|(?:anfang|mitte|ende)\s+)?([a-zä]+)\.?(?:\s+(20\d{2}))?", value)
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
        following = text[match.end():]
        if _OTHER_PURPOSE.match(following) or (not end and _PREFERRED_DATE.match(following)):
            continue
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
    if range_match is None and not _OTHER_RENT_RANGE_PURPOSE.search(title):
        range_match = _TITLE_RANGE.search(title)
    stated_start = _label_date(text, rf"\b(?:{_START_LABEL})", today)
    if stated_start is None:
        stated_start = _label_date(
            text, r"\b(?:(?:wird|werden)\s+(?:erst\s+)?(?:ab\s+)?|ab\b|erst\b)", today,
            suffix=r"\s+(?:(?:wird|werden|kann|können|könnt|kannst)\s+(?:(?:ihr|du|man)\s+)?)?"
                   r"(?:frei|verfügbar|beziehbar|bezogen|einziehen)\b",
        )
    if stated_start is None and range_match:
        stated_start = _calendar_date(range_match["start"], today)
    # The form and the advert can describe different minimum dates. A definitively
    # later date in the advert still prevents an earlier move-in.
    start = max((day for day in (available_from, stated_start) if day is not None), default=None)
    if start is None and not _OTHER_TITLE_PURPOSE.search(title):
        start = _label_date(title, r"\bab\b", today)
    if start and start.year > today.year:
        return LATE_MOVE_IN

    if any(not re.search(r"\bkein\w*\s+$", text[:match.start()], re.I) for match in _OPEN_END.finditer(text)):
        return None
    if _short_stated_period(text, start or today):
        return SHORT_RENTAL
    lease_start = max(start, today) if start else today
    stated_end = _label_date(text, rf"\b(?:{_END_LABEL})", start or today, end=True)
    if stated_end is None:
        stated_end = _label_date(
            text, r"\bbis\b", start or today, end=True,
            suffix=r"\s+(?:befristet|vermietet|untervermietet)\b",
        )
    if stated_end is None and range_match:
        stated_end = _calendar_date(range_match["end"], start or today, end=True)
    end = min((day for day in (available_until, stated_end) if day is not None), default=None)
    if end and lease_start <= end < _plus_four_months(lease_start) - timedelta(days=1):
        return SHORT_RENTAL
    return None


def install_listing_availability(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS listing_availability (
            id TEXT PRIMARY KEY, reason TEXT, policy_version INTEGER NOT NULL DEFAULT 0
        )
    """)
    if not _has_policy_version(db):
        try:
            db.execute("ALTER TABLE listing_availability ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            # Another process may have finished the migration while this one was waiting.
            if not _has_policy_version(db):
                raise


def _has_policy_version(db: sqlite3.Connection) -> bool:
    return any(column[1] == "policy_version" for column in db.execute("PRAGMA table_info(listing_availability)"))


def record_listing_availability(db: sqlite3.Connection, listing_id: str, reason: str | None) -> None:
    db.execute("""
        INSERT INTO listing_availability (id, reason, policy_version) VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET reason = excluded.reason, policy_version = excluded.policy_version
    """, (listing_id, reason, AVAILABILITY_POLICY_VERSION))
