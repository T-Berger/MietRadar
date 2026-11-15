"""Conservative filters for explicit WG eligibility and resident ages."""

import re


_STUDENT = r"student(?:en|innen|in)?|studierende[nrms]?|immatrikuliert\w*"
_FEMALE = r"frau(?:en)?|weiblich\w*|mitbewohnerin(?:nen)?|mädchen"
_OTHER_GENDER = re.compile(
    r"\b(?:m\s*/\s*w|w\s*/\s*m|w\s*/\s*d|w\s*/\s*x|flinta\w*|divers\w*|"
    r"egal|männlich\w*|mann|männer|mitbewohnern?)\b"
)
_OTHER_STATUS = re.compile(r"\b(?:egal|berufstätig\w*|angestellt\w*|auszubildende\w*|azubis?)\b")
_ONLY = r"(?:nur|ausschließlich|ausschliesslich|exklusiv)"
_EXCLUSIONS = (
    ("nur Studierende", _STUDENT),
    ("nur Frauen", _FEMALE),
)


def _exclusive(text: str, group: str) -> bool:
    alternative = _OTHER_GENDER if group == _FEMALE else _OTHER_STATUS
    for clause in re.split(r"[.!?;\n]+", text.casefold()):
        if re.search(r"\bnicht\s+(?:nur|ausschließlich)\b", clause):
            continue
        match = re.search(
            rf"\b{_ONLY}\s+(?:(?:an|für|eine?|noch|an\s+berufstätige|berufstätige)\s+)*(?:{group})\b"
            rf"|\b(?:{group})\s+{_ONLY}\b|\b(?:{group})\s+only\b",
            clause,
        )
        if not match:
            continue
        before, after = clause[:match.start()], clause[match.end():]
        if re.search(r"\b(?:wir\s+sind|hier\s+wohnen|bei\s+uns\s+wohnen)\s*$", before):
            continue
        if re.match(r"\s*(?:und/oder\b|oder\b|und\b|[/&*:\u00b7]|,)", after) and alternative.search(after):
            continue
        return True
    return False


def _ages(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"(?<!\d)(?:1[6-9]|[2-9][0-9]|10[0-9])(?!\d)", text)]


def rejection_reason(
    title: str,
    description: str,
    *,
    wanted: str = "",
    wg_type: str = "",
    resident_ages: str = "",
    age: int = 28,
    age_gap: int = 7,
) -> str | None:
    """Return a reason only when a listing states a conflicting restriction."""
    text = f"{title}\n{description}".casefold()
    target = wanted.casefold().strip()
    category = wg_type.casefold().strip()
    for reason, group in _EXCLUSIONS:
        if _exclusive(f"{text}\n{target}", group):
            return reason
    if re.search(rf"(?:^|·\s*)(?:geschlecht\s*:\s*)?(?:eine\s+)?(?:{_FEMALE})(?=\s+(?:zwischen|ab|bis|gesucht)\b|\s*(?:·|$))", target) or any(
        re.search(r"\b(?:suchen\s+(?:eine\s+)?mitbewohnerin|mitbewohnerin\s+gesucht)\b(?![*:/])", clause)
        and not _OTHER_GENDER.search(clause) and not re.search(r"\b(?:bevorzugt|vorzugsweise|am liebsten)\b", clause)
        for clause in re.split(r"[.!?;\n]+", text)
    ):
        return "nur Frauen"
    if re.fullmatch(rf"(?:{_STUDENT})(?:\s+gesucht)?", target) or re.search(
        r"(?:^|[,;|]\s*)studentenwohnheim(?:$|[,;|])", category
    ) or re.search(r"\b(?:studienbescheinigung|immatrikulationsbescheinigung)\s+(?:ist\s+)?(?:pflicht|erforderlich|voraussetzung)\b", text):
        return "nur Studierende"

    found = _ages(resident_ages)
    ages_pattern = r"\d{2,3}(?:(?:\s*(?:,|/|–|—|-|\bund\b|\bbis\b)\s*)\d{2,3})*"
    for match in re.finditer(
        rf"\b(?:wir\s+sind(?:\s+beide)?|die\s+mitbewohner(?:innen)?\s+sind|"
        rf"die\s+bewohner(?:innen)?\s+sind)\s+({ages_pattern})\s*jahre\s+alt\b"
        rf"|\bwir\s*\(({ages_pattern})\)",
        text,
    ):
        found.extend(_ages(match.group(1) or match.group(2)))
    if any(abs(resident - age) > age_gap for resident in found):
        return "Alter der Mitbewohner außerhalb der Spanne"
    return None
