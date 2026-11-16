import unittest
from unittest.mock import patch

from kleinanzeigen_judge import ReplyJudgment, judge_reply
from wg_reply_response import draft_wg_reply


class WGReplyResponseTest(unittest.TestCase):
    def draft(self, text: str) -> str | None:
        return draft_wg_reply(text, judge_reply(text))

    def test_acknowledges_another_room_after_an_initial_decline(self):
        text = "Das Zimmer ist leider schon vergeben. Wir hätten aber im selben Haus noch ein anderes Zimmer frei."
        self.assertEqual(judge_reply(text).label, "Absage")
        self.assertEqual(self.draft(text),
                         "Danke für die Rückmeldung! Das andere Angebot klingt interessant. Gibt es dazu schon eine Anzeige?")

    def test_thanks_for_a_decline_without_inventing_an_alternative(self):
        text = "Das Zimmer ist schon vergeben. Wir haben leider kein anderes Zimmer frei."
        self.assertEqual(self.draft(text), "Danke für die Rückmeldung und alles Gute!")

    def test_notices_a_similar_room_if_it_is_still_available(self):
        text = "Leider ist das Zimmer schon vergeben. Im Nachbarhaus gibt es noch ein ähnliches WG-Zimmer, das frei ist."
        self.assertIn("Das andere Angebot klingt interessant.", self.draft(text))

    def test_does_not_ask_for_a_similar_room_that_is_also_taken(self):
        text = "Das Zimmer ist schon vergeben. Wir hätten noch ein ähnliches WG-Zimmer, das leider auch vermietet ist."
        self.assertEqual(self.draft(text), "Danke für die Rückmeldung und alles Gute!")

    def test_mentions_a_proposed_time_without_confirming_it(self):
        text = "Hallo,\nkannst du am Donnerstag um 18:30 Uhr zur Besichtigung vorbeikommen?"
        self.assertEqual(self.draft(text),
                         "Danke für die Einladung! Ich schau kurz, ob Donnerstag um 18:30 Uhr bei mir klappt, und melde mich.")

    def test_does_not_select_one_of_multiple_offered_times(self):
        for text in ("Für die Besichtigung ginge Dienstag um 19 Uhr oder Mittwoch um 17 Uhr.",
                     "Besichtigung ist Dienstag um 18 Uhr oder 19 Uhr möglich."):
            with self.subTest(text=text):
                self.assertEqual(self.draft(text), "Danke für die Einladung! Ich schau kurz in meinen Kalender "
                                                  "und melde mich zur Terminabstimmung.")

    def test_cancellation_does_not_receive_an_invitation_acknowledgment(self):
        self.assertEqual(self.draft("Die Besichtigung fällt aus."),
                         "Danke fürs Bescheidsagen! Meldet euch gern, falls sich ein neuer Termin ergibt.")

    def test_hazardous_request_is_not_treated_as_a_viewing_invitation(self):
        text = "Du kannst morgen um 18 Uhr zur Besichtigung kommen. Überweise bitte die Kaution vorab."
        as_viewing = ReplyJudgment("Besichtigung", "hoch", "Ein Termin wurde vorgeschlagen.")
        self.assertIsNone(draft_wg_reply(text, as_viewing))

    def test_generic_questions_can_use_the_existing_draft_provider(self):
        text = "Wie lange willst du bleiben?"
        with patch("wg_reply_response.draft_response", return_value="Danke fürs Nachfragen!") as provider:
            self.assertEqual(draft_wg_reply(text, judge_reply(text), provider="mistral"), "Danke fürs Nachfragen!")
            provider.assert_called_once_with(text, judge_reply(text), provider="mistral")


if __name__ == "__main__":
    unittest.main()
