"""Check the configured Mistral application judge using fictional drafts."""

import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from munich_delivery import save_application_draft
from munich_radar import _database
from outgoing_judge import _UNAVAILABLE


def main() -> int:
    if os.getenv("MUNICH_OUTGOING_JUDGE_PROVIDER", "").strip().lower() != "mistral":
        print("Mistral-Prüfung: MUNICH_OUTGOING_JUDGE_PROVIDER muss mistral sein.")
        return 1
    if not os.getenv("MISTRAL_API_KEY", "").strip():
        print("Mistral-Prüfung: MISTRAL_API_KEY fehlt.")
        return 1

    listing = "Ein WG-Zimmer in München mit Balkon. Besichtigungen nach Absprache."
    cases = (
        ("Bewerbung", "Hallo, ich interessiere mich für euer WG-Zimmer. "
         "Kann ich es mir bei einer Besichtigung anschauen? Viele Grüße, Alex", True),
        ("Themenfremde Nachricht", "Guten Tag, wir bieten Ihnen ein Abonnement für unsere Fußballzeitschrift an.", False),
    )
    now = datetime.now(timezone.utc)
    success = True
    with TemporaryDirectory() as directory, closing(_database(Path(directory) / "draft-test.sqlite3")) as db:
        for number, (label, message, expected) in enumerate(cases):
            with db:
                save_application_draft(
                    db, f"smoke-{number}", "https://example.test/wohnungsanzeige", now.timestamp(),
                    "Test", message, context=listing, provider="mistral", now=now,
                )
            approved, reason = db.execute(
                """SELECT c.approved, c.reason FROM outgoing_checks c JOIN drafts d ON d.id = c.id
                   WHERE c.id = ? AND c.kind = 'application' AND d.message = ?""", (f"smoke-{number}", message),
            ).fetchone()
            print(f"Mistral-Prüfung · {label} · {'freigegeben' if approved else 'zurückgehalten'} · {reason}")
            success &= bool(approved) == expected and reason != _UNAVAILABLE
        success &= not db.execute("SELECT 1 FROM delivery").fetchone()
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
