"""Private settings for WG-Gesucht application messages."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from llm_personalizer import APPLICATION_QUESTION_TOPICS


_BERLIN = ZoneInfo("Europe/Berlin")
_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_SLOT_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", flags=re.ASCII)


def viewing_availability(value: str) -> tuple[datetime, ...]:
    """Parse one Munich local date and time per line and store the instants in UTC."""
    slots = set()
    for line_number, line in enumerate(value.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            if not _SLOT_FORMAT.fullmatch(line):
                raise ValueError
            local = datetime.fromisoformat(line)
            candidates = {local.replace(tzinfo=_BERLIN, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
            valid = {slot for slot in candidates if slot.astimezone(_BERLIN).replace(tzinfo=None) == local}
        except (ValueError, OverflowError):
            valid = set()
        if len(valid) != 1:
            raise ValueError(
                f"MUNICH_VIEWING_SLOTS enthält in Zeile {line_number} keinen eindeutigen "
                "Münchner Termin im Format JJJJ-MM-TTTHH:MM."
            )
        slots.update(valid)
    return tuple(sorted(slots))


def viewing_slot_labels(availability: tuple[datetime, ...], now: datetime) -> tuple[str, ...]:
    """Show at most two dates in German, using Munich time."""
    slots = sorted(slot for slot in availability if slot > now)[:2]
    local_slots = (slot.astimezone(_BERLIN) for slot in slots)
    return tuple(f"{_WEEKDAYS[slot.weekday()]}, {slot:%d.%m., um %H:%M Uhr}" for slot in local_slots)


def applicant_answers(value: str) -> dict[str, str]:
    """Read optional answers to common questions from private configuration."""
    if not value.strip():
        return {}
    try:
        answers = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError("MUNICH_APPLICANT_ANSWERS muss ein gültiges JSON-Objekt sein.") from None
    if not isinstance(answers, dict) or any(
        topic not in APPLICATION_QUESTION_TOPICS or not isinstance(answer, str) or not answer.strip()
        for topic, answer in answers.items()
    ):
        raise ValueError("MUNICH_APPLICANT_ANSWERS braucht bekannte Themen mit einer Antwort als Text.")
    return {topic: answer.strip() for topic, answer in answers.items()}
