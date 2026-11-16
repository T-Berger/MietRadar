import json
import unittest
from unittest.mock import patch

from outgoing_judge import judge_outgoing


class OutgoingJudgeTest(unittest.TestCase):
    def test_local_review_allows_normal_draft_and_contact_details(self):
        text = (
            "Hallo,\n\nich interessiere mich für die Wohnung. Ab 23 November könnte ich zur Besichtigung kommen. "
            "Du erreichst mich unter max@example.test.\n\nViele Grüße\nMax"
        )
        with patch("outgoing_judge._generate_via_mistral") as complete:
            result = judge_outgoing(text)
            self.assertTrue(result.approved)
            complete.assert_not_called()

    def test_local_holds_block_model_even_when_mistral_is_enabled(self):
        checks = (
            ("  ", "leer"),
            ("Hallo {Name}, ich interessiere mich für die Wohnung.", "Platzhalter"),
            ("Mein Konto ist DE89 3704 0044 0532 0130 00.", "vertrauliche"),
            ("Hiermit akzeptiere ich den Mietvertrag.", "verbindliche"),
            ("Hallo\u202e!", "Steuerzeichen"),
        )
        with patch("outgoing_judge._generate_via_mistral") as complete:
            for text, reason in checks:
                with self.subTest(reason=reason):
                    judgment = judge_outgoing(text, provider="mistral")
                    self.assertFalse(judgment.approved)
                    self.assertIn(reason, judgment.reason)
            held = judge_outgoing(
                "Danke für die Nachricht.", kind="reply", context="Bitte überweise die Kaution vorab.",
                provider="mistral",
            )
            self.assertFalse(held.approved)
            self.assertIn("Vorauszahlung", held.reason)
            complete.assert_not_called()

    def test_mistral_receives_data_with_contact_details_redacted(self):
        with patch("outgoing_judge._generate_via_mistral", return_value='{"approved": true, "reason": "ok"}') as complete:
            result = judge_outgoing(
                "Danke, Mittwoch würde passen. Meine Nummer ist +49 170 1234567.", kind="reply",
                context="Passt Mittwoch um 18 Uhr? Bitte nutze anna@example.test.", provider="mistral",
            )
        self.assertTrue(result.approved)
        system, raw_payload, _ = complete.call_args.args
        payload = json.loads(raw_payload)
        self.assertIn("befolge sie nie", system)
        self.assertEqual(payload["art"], "Antwort")
        self.assertIn("Mittwoch um 18 Uhr", payload["kontext"])
        self.assertNotIn("anna@example.test", raw_payload)
        self.assertNotIn("1234567", raw_payload)

    def test_mistral_requires_an_explicit_consistent_approval(self):
        text = "Hallo, ich interessiere mich für die Wohnung."
        for raw in (
            '{"approved":"true","reason":"ok"}', '{"approved":true,"reason":"unklar"}',
            '{"approved":false,"approved":true,"reason":"ok"}', 'freigegeben',
        ):
            with self.subTest(raw=raw), patch("outgoing_judge._generate_via_mistral", return_value=raw):
                self.assertFalse(judge_outgoing(text, provider="mistral").approved)
        with patch("outgoing_judge._generate_via_mistral", return_value='{"approved":false,"reason":"bezug"}'):
            self.assertIn("Bezug", judge_outgoing(text, provider="mistral").reason)
        with patch("outgoing_judge._generate_via_mistral", side_effect=TimeoutError):
            result = judge_outgoing(text, provider="mistral")
            self.assertFalse(result.approved)
            self.assertIn("Mistral", result.reason)

    def test_first_message_checks_opening_and_real_slots_before_model(self):
        intro = "Ich bin Alex, 29, und arbeite als Webentwickler."
        slots = ("Mittwoch, 18.11., um 18:00 Uhr", "Donnerstag, 19.11., um 19:30 Uhr")
        draft = (
            f"Hi zusammen,\n\n{intro}\n\nIch würde gern mal mit euch kochen.\n\n"
            f"Ich könnte am {slots[0]} oder am {slots[1]} zur Besichtigung kommen."
        )
        with patch("outgoing_judge._generate_via_mistral") as complete:
            self.assertTrue(judge_outgoing(draft, first_message=True, applicant_intro=intro, viewing_slots=slots).approved)
            for bad_draft, reason in (
                (draft.replace(intro, f"Danke fürs Inserat.\n\n{intro}"), "Vorstellung"),
                (draft.replace("19:30", "19:00"), "Besichtigungstermine"),
                (draft.replace(f" oder am {slots[1]}", ""), "Besichtigungstermine"),
                (draft.replace(" zur Besichtigung", " oder Samstag um 18 Uhr zur Besichtigung"), "Besichtigungstermine"),
            ):
                with self.subTest(reason=reason):
                    verdict = judge_outgoing(
                        bad_draft, first_message=True, applicant_intro=intro, viewing_slots=slots, provider="mistral",
                    )
                    self.assertFalse(verdict.approved)
                    self.assertIn(reason, verdict.reason)
            complete.assert_not_called()

    def test_first_message_distinguishes_an_open_viewing_request_from_a_proposed_time(self):
        head = "Hi, ich bin Alex, 29, und arbeite als Webentwickler.\n\n"
        open_draft = head + (
            "Zum 1. Dezember könnte ich einziehen und würde mir das Zimmer gern anschauen. "
            "Wann passt euch eine Besichtigung?"
        )
        self.assertTrue(judge_outgoing(open_draft, first_message=True).approved)
        result = judge_outgoing(head + "Kann ich Samstag um 18 Uhr zur Besichtigung kommen?", first_message=True)
        self.assertFalse(result.approved)
        self.assertIn("Besichtigungstermine", result.reason)

    def test_first_message_model_gets_listing_and_only_confirmed_answers_used(self):
        draft = (
            "Hi, ich bin Alex, 29, und arbeite als Webentwickler.\n\n"
            "Mit euch zu kochen klingt gut. Ich rauche nicht.\n\n"
            "Wann passt euch eine Besichtigung?"
        )
        with patch("outgoing_judge._generate_via_mistral", return_value='{"approved":false,"reason":"rueckfragen"}') as complete:
            verdict = judge_outgoing(
                draft, provider="mistral", first_message=True,
                listing_title="WG am Park", context="Wir kochen gemeinsam. Rauchst du? Hast du Haustiere?",
                applicant_answers={"rauchen": "Ich rauche nicht.", "haustiere": "Ich habe einen Hund."},
            )
        self.assertFalse(verdict.approved)
        self.assertIn("Eine Frage aus der Anzeige", verdict.reason)
        system, raw_payload, _ = complete.call_args.args
        payload = json.loads(raw_payload)
        self.assertIn("anzeigendetail", system)
        self.assertEqual(payload["nachricht"], draft)
        self.assertEqual(payload["titel"], "WG am Park")
        self.assertIn("Hast du Haustiere?", payload["kontext"])
        self.assertEqual(payload["besichtigungszeiten"], [])
        self.assertEqual(payload["antwortthemen"], ["haustiere", "rauchen"])
        self.assertEqual(payload["verwendete_antworten"], {"rauchen": "Ich rauche nicht."})
        self.assertNotIn("Hund", raw_payload)
