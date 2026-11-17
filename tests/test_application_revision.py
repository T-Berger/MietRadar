import json
import unittest
from unittest.mock import patch

from application_revision import ApplicationRevisionError, regenerate_application


BODY = "Mein Name ist Max Beispiel. Ab dem 15. Dezember kann ich einziehen.\n\nWann passt Ihnen eine Besichtigung?"
GREETING = "Guten Tag Frau Muster,"
FOOTER = "Mit freundlichen Grüßen\nMax Beispiel\nTelefon: +49 170 1234567"
DRAFT = f"{GREETING}\n\n{BODY}\n\n{FOOTER}"
REVISED = "Mein Name ist Max Beispiel, und ich könnte am 15. Dezember einziehen.\n\nWann würde Ihnen eine Besichtigung passen?"


class ApplicationRevisionTest(unittest.TestCase):
    @patch("application_revision._generate_via_mistral", return_value=json.dumps({"text": REVISED}))
    def test_preserves_named_greeting_and_contact_footer_outside_provider(self, complete):
        proposed = regenerate_application(
            DRAFT, "Formuliere den Einzug natürlicher.", listing_title="Wohnung am Park", recipient_role="manager",
        )

        self.assertEqual(proposed, f"{GREETING}\n\n{REVISED}\n\n{FOOTER}")
        payload = json.loads(complete.call_args.args[1])
        self.assertEqual(payload, {
            "entwurf": BODY,
            "fokus": "Formuliere den Einzug natürlicher.",
            "anzeigentitel": "Wohnung am Park",
            "kontakt": "Hausverwaltung",
        })

    @patch("application_revision._generate_via_mistral", return_value=json.dumps({"text": REVISED}))
    def test_preserves_legacy_formal_greeting_and_bare_contact_line(self, complete):
        footer = "+49 170 1234567"
        for greeting in ("Sehr geehrte Frau Dr. Muster,", "Guten Tag, Frau Muster,"):
            with self.subTest(greeting=greeting):
                self.assertEqual(regenerate_application(f"{greeting}\n\n{BODY}\n\n{footer}", "  "),
                                 f"{greeting}\n\n{REVISED}\n\n{footer}")
                payload = json.loads(complete.call_args.args[1])
                self.assertTrue(payload["fokus"])
                self.assertEqual(payload["entwurf"], BODY)

    @patch("application_revision._generate_via_mistral")
    def test_rejects_sensitive_body_and_focus_before_provider_call(self, complete):
        for draft, focus in (
            (DRAFT.replace(BODY, "Meine Ausweisnummer lautet TEST123."), "Kürzer bitte."),
            (DRAFT.replace(BODY, "Rufen Sie mich unter +49 170 1234567 an."), "Kürzer bitte."),
            (DRAFT, "Das Passwort lautet Muster123."),
            (DRAFT, "x" * 401),
        ):
            with self.subTest(focus=focus), self.assertRaises(ApplicationRevisionError):
                regenerate_application(draft, focus)
        complete.assert_not_called()

    @patch("application_revision._generate_via_mistral")
    def test_rejects_bad_provider_results(self, complete):
        for generated in (
            "kein JSON",
            '{"text":"Doppelt", "text":"Klingt gut"}',
            json.dumps({"text": REVISED.replace("15.", "16.")}),
            json.dumps({"text": REVISED.replace("Ihnen", "dir")}),
            json.dumps({"text": f"{GREETING}\n\n{REVISED}"}),
            json.dumps({"text": f"{REVISED}\n\nWelche Unterlagen brauchen Sie?"}),
            RuntimeError("test API response with potentially private data"),
        ):
            with self.subTest(generated=generated), self.assertRaisesRegex(ApplicationRevisionError, "Mistral"):
                complete.side_effect = [generated]
                regenerate_application(DRAFT, "Kürzer bitte.", recipient_role="manager")


if __name__ == "__main__":
    unittest.main()
