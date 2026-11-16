"""Prepare short, noncommittal German replies for WG-Gesucht."""

from __future__ import annotations

import re

from kleinanzeigen_judge import ReplyJudgment, _redact, judge_reply
from kleinanzeigen_response import draft_response


_ALTERNATIVE = re.compile(
    r"\b(?:andere\w*|weitere\w*|ähnliche\w*|noch ein\w*)\s+(?:(?:freie\w*|passende\w*|ähnliche\w*)\s+)?"
    r"(?:wg[ -]?)?(?:zimmer|wohnung)\b",
    re.I,
)
_OFFER = re.compile(
    r"\b(?:hab\w*|hätt\w*|biete\w*|frei|verfügbar|stattdessen|alternativ|infrage|interesse)\b", re.I,
)
_NO_ALTERNATIVE = re.compile(r"\bkein\w*\s+(?:andere\w*|weitere\w*|ähnliche\w*)\b", re.I)
_UNAVAILABLE = r"(?:vergeben|vermietet|nicht\s+(?:mehr\s+)?frei)"
_TAKEN = re.compile(
    rf"\s*,?\s*(?:(?:das\s+)?(?:ist|wäre)\b[^.!?,]{{0,32}}\b{_UNAVAILABLE}\b"
    rf"|das\b[^.!?,]{{0,32}}\b{_UNAVAILABLE}\s+(?:ist|wäre)\b)",
    re.I,
)
_INTEREST = re.compile(r"\b(?:hast du|habt ihr|haben sie|besteht bei dir)\s+(?:noch|weiterhin)\s+interesse\b", re.I)
_VIEWING = re.compile(r"\b(?:besichtigung\w*|vorbeikommen|kennenlernen)\b", re.I)
_CANCELLED_VIEWING = re.compile(
    r"\bbesichtigung\w*[^.!?]{0,30}\b(?:nicht möglich|fällt aus|findet nicht statt|abgesagt)\b", re.I,
)
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonnabend", "Sonntag")
_SLOT = re.compile(
    rf"\b(?P<day>{'|'.join(_WEEKDAYS)}|heute|morgen|übermorgen|[0-3]?\d\.[01]?\d\.(?:\d{{2,4}})?)"
    r"\s*,?\s+(?P<preposition>um|gegen)\s+(?P<time>[01]?\d|2[0-3])(?::(?P<minutes>[0-5]\d))?\b",
    re.I,
)
_MULTIPLE_OPTIONS = re.compile(r"\boder\b", re.I)


def _offered_alternative(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        room = _ALTERNATIVE.search(sentence)
        if room and _OFFER.search(sentence) and not (_NO_ALTERNATIVE.search(sentence) or _TAKEN.match(sentence[room.end():])):
            return True
    return False


def _viewing_reply(text: str) -> str:
    slots = {_format_slot(match) for match in _SLOT.finditer(text)}
    if len(slots) == 1 and not _MULTIPLE_OPTIONS.search(text):
        when = slots.pop()
        return f"Danke für die Einladung! Ich schau kurz, ob {when} bei mir klappt, und melde mich."
    if slots:
        return "Danke für die Einladung! Ich schau kurz in meinen Kalender und melde mich zur Terminabstimmung."
    return "Danke für die Einladung! Ich würde mich freuen, euch kennenzulernen. Wann würde es passen?"


def _format_slot(match: re.Match[str]) -> str:
    day = match["day"].casefold()
    if day[0].isdigit():
        day = f"der {day}"
    elif day.capitalize() in _WEEKDAYS:
        day = day.capitalize()
    time = match["time"]
    if match["minutes"] is not None:
        time += f":{match['minutes']}"
    return f"{day} {match['preposition'].casefold()} {time} Uhr"


def draft_wg_reply(text: str, judgment: ReplyJudgment, *, provider: str = "none") -> str | None:
    """Draft a reply for review without accepting an offer or confirming availability."""
    redacted = _redact(text)
    if not redacted or judgment.label == "Vorsicht" or judge_reply(text).label == "Vorsicht":
        return None
    if _offered_alternative(redacted):
        return "Danke für die Rückmeldung! Das andere Angebot klingt interessant. Gibt es dazu schon eine Anzeige?"
    if judgment.label == "Absage":
        return "Danke für die Rückmeldung und alles Gute!"
    if _CANCELLED_VIEWING.search(redacted):
        return "Danke fürs Bescheidsagen! Meldet euch gern, falls sich ein neuer Termin ergibt."
    if judgment.label == "Besichtigung" or _VIEWING.search(redacted):
        return _viewing_reply(redacted)
    if _INTEREST.search(redacted):
        return "Danke für die Rückmeldung! Ich hab noch Interesse. Wie geht’s am besten weiter?"
    return draft_response(text, judgment, provider=provider)
