import json
import unittest
from unittest.mock import patch

from llm_personalizer import personalise_message, revise_first_message_draft


INTRO = "Ich bin Alex, 31. Ich koche gern und wandere viel."
DETAIL = "Das Zimmer mit Balkon interessiert mich."
MOVE_IN = "Ich könnte zum 1. Dezember einziehen."
QUESTION = "Wann könnte ich vorbeikommen und euch kennenlernen?"


class WGDraftStructureTest(unittest.TestCase):
    def test_template_assembles_detail_and_move_in_before_one_viewing_question(self):
        template = (
            f"Hi zusammen,\n\n{INTRO}\n\nWann passt euch eine Besichtigung?\n\n{{LLM_TEXT}}\n\n"
            "Ich könnte zum 1. Dezember einziehen und würde mir das Zimmer gerne anschauen. "
            "Ist es noch frei? Wann wäre eine Besichtigung möglich?\nViele Grüße\nAlex"
        )
        draft = personalise_message(template, "", provider="none", first_message=True,
                                    listing_title="Zimmer mit Balkon", listing_type="wg")
        self.assertEqual(draft, f"Hi zusammen,\n\n{INTRO}\n\n{DETAIL}\n\n{MOVE_IN}\n\n{QUESTION}\n\nViele Grüße\nAlex")

    def test_cached_draft_moves_the_ask_below_the_detail_and_drops_generic_transit_copy(self):
        old_intro = ("Ich bin Alex, 31. In meiner Freizeit lese ich. Beruflich arbeite ich im Theater. "
                     "Neben der Arbeit helfe ich im Sportverein.")
        template = (
            f"Hi zusammen,\n\n{old_intro}\n\nWann passt euch eine Besichtigung?\n\n"
            "Ich liebe es, wenn die Tram direkt vor der Tür fährt – so kommt man schnell überall hin.\n\n"
            "Ist es noch frei? Wann wäre eine Besichtigung möglich?\n\nViele Grüße\nAlex"
        )
        draft = revise_first_message_draft(template, INTRO, listing_title="Zimmer mit Balkon")
        self.assertEqual(draft, f"Hi zusammen,\n\n{INTRO}\n\n{DETAIL}\n\n{QUESTION}\n\nViele Grüße\nAlex")
        self.assertEqual(revise_first_message_draft(draft, INTRO, listing_title="Zimmer mit Balkon"), draft)

    def test_revising_the_private_intro_preserves_one_move_in_paragraph(self):
        old_intro = "Ich bin Alex, 31. Ich koche gern. Wenn ich Zeit habe, geh ich zeichnen. Ich arbeite in einer Bücherei."
        move_in = "Ich kann zum 1. Juni einziehen."
        revised = "Ich bin Alex, 31. Nach Feierabend koche ich gern."
        draft = f"Hi zusammen,\n\n{old_intro}\n\n{move_in}\n\n{DETAIL}\n\n{QUESTION}\n\nViele Grüße\nAlex"
        expected = draft.replace(old_intro, revised, 1)
        self.assertEqual(revise_first_message_draft(draft, f"{revised}\n\n{move_in}"), expected)

    def test_extending_the_private_intro_replaces_the_entire_cached_intro(self):
        old_intro = f"{INTRO} Wenn ich Zeit habe, zeichne ich oft."
        new_intro = f"{old_intro} Nebenbei mache ich Musik."
        draft = f"Hi zusammen,\n\n{old_intro}\n\n{DETAIL}\n\n{QUESTION}\n\nViele Grüße\nAlex"

        refreshed = revise_first_message_draft(draft, new_intro)

        self.assertEqual(refreshed, draft.replace(old_intro, new_intro, 1))
        self.assertEqual(revise_first_message_draft(refreshed, new_intro), refreshed)

    def test_matches_a_shared_activity_without_repeating_the_private_hobby(self):
        intro = "Ich bin Alex. Nebenbei mache ich Musik. Abends koche ich gern."
        template = f"Hi zusammen,\n\n{intro}\n\n{{LLM_TEXT}}\n\nViele Grüße\nAlex"
        message = personalise_message(
            template, "Wir kochen ab und zu zusammen.", provider="none", first_message=True,
            listing_type="wg", listing_title="Zimmer mit Balkon", applicant_intro=intro,
        )
        self.assertIn(intro, message)
        self.assertEqual(message.count("koche ich gern"), 1)
        self.assertIn("Wenn ihr zusammen kocht, wär ich gern dabei.", message)
        self.assertNotIn("Balkon", message)
        self.assertEqual(revise_first_message_draft(message, intro, listing_title="Zimmer mit Balkon"), message)

    def test_does_not_claim_an_affinity_for_an_activity_the_profile_does_not_share(self):
        intro = "Ich bin Alex. Ich koche nicht gern."
        template = f"Hi zusammen,\n\n{intro}\n\n{{LLM_TEXT}}\n\nViele Grüße\nAlex"
        message = personalise_message(
            template, "Wir kochen ab und zu zusammen.", provider="none", first_message=True,
            listing_type="wg",
        )
        self.assertIn("zusammen kocht", message)
        self.assertNotIn("wär ich gern dabei.", message)

    def test_offline_repair_removes_advertiser_copy_and_keeps_the_first_person_reply(self):
        reaction = "Die Idee, mal zusammen zu kochen, gefällt mir."
        previous = (
            f"Hi zusammen,\n\n{INTRO}\n\n{MOVE_IN} "
            "Ihr könnt zusammen kochen und im Innenhof sitzen.\n\n"
            "Das Zimmer ist möbliert und verfügt über ein Regal und einen Tisch.\n\n"
            "Die effiziente Nutzung des Raums beeindruckt mich.\n\n"
            f"{reaction}\n\n{QUESTION}\n\nViele Grüße\nAlex"
        )
        result = revise_first_message_draft(previous, INTRO, listing_title="Zimmer mit Balkon")
        self.assertEqual(result, f"Hi zusammen,\n\n{INTRO}\n\n{MOVE_IN}\n\n{reaction}\n\n{QUESTION}\n\nViele Grüße\nAlex")

    def test_wg_model_can_omit_room_flattery_even_if_the_ad_has_an_amenity(self):
        template = f"Hi zusammen,\n\n{INTRO}\n\n{{LLM_TEXT}}\n\nViele Grüße\nAlex"
        evidence = "Die Raumaufteilung ist kompakt."
        forced_praise = "Ich finde, dass die Raumaufteilung jeden einzelnen Quadratzentimeter gut nutzt."
        for room in (None, {"text": forced_praise, "evidence": evidence}):
            with self.subTest(room=room), patch("llm_personalizer._generate_text", return_value=json.dumps({
                "household": None, "room": room, "questions": {},
            })):
                message = personalise_message(
                    template, evidence, provider="mistral", first_message=True,
                    listing_type="wg", listing_title="Zimmer mit Balkon",
                )
                self.assertEqual(message, f"Hi zusammen,\n\n{INTRO}\n\n{QUESTION}\n\nViele Grüße\nAlex")

    def test_commercial_inquiry_keeps_supplied_profile_and_move_in_without_wg_hobbies(self):
        intro = "Ich bin Alex Neumann, 31, und arbeite als Architekt. In meiner Freizeit koche ich gern."
        draft = personalise_message(
            "Hi {name},\n\n{LLM_TEXT}\n\nIch kann ab 1. Dezember einziehen und würde euch gern kennenlernen.\n\n"
            "Viele Grüße\nAlex Neumann",
            "Wir kochen jeden Freitag gemeinsam. Einzug ab 1. Februar. Bitte sagen Sie, ob Sie Haustiere haben.",
            poster_name="Hausverwaltung Nord", provider="none", listing_type="wg", commercial=True,
            applicant_intro=intro, applicant_answers={"haustiere": "Ich habe keine Haustiere."},
        )
        self.assertEqual(draft, (
            "Guten Tag,\n\nmein Name ist Alex Neumann. Ich arbeite als Architekt.\n\n"
            "Ich interessiere mich für Ihr Zimmer.\n\nIch kann ab 1. Dezember einziehen. Ich habe keine Haustiere."
            "\n\nKönnten Sie mir einen Besichtigungstermin vorschlagen?\n\nMit freundlichen Grüßen\nAlex Neumann"
        ))

    def test_commercial_inquiry_does_not_treat_a_profession_as_a_name(self):
        draft = personalise_message("Hallo!", "", provider="none", listing_type="wohnung", commercial=True,
                                    applicant_intro="Ich bin Architekt.")
        self.assertIn("ich interessiere mich für Ihre Wohnung.", draft)
        self.assertNotIn("Name ist Architekt", draft)


if __name__ == "__main__":
    unittest.main()
