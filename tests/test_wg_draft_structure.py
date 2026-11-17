import json
import unittest
from unittest.mock import patch

from llm_personalizer import application_render_signature, personalise_message, revise_first_message_draft


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

    def test_commercial_inquiry_keeps_supplied_profile_and_requested_facts(self):
        intro = "Ich bin Alex Neumann, 31, und arbeite als Architekt. In meiner Freizeit koche ich gern."
        draft = personalise_message(
            "Hi {name},\n\n{LLM_TEXT}\n\nIch kann ab 1. Dezember einziehen und würde euch gern kennenlernen.\n\n"
            "Viele Grüße\nAlex Neumann",
            "Wir kochen jeden Freitag gemeinsam. Einzug ab 1. Februar. Bitte sagen Sie, ob Sie Haustiere haben.",
            poster_name="Hausverwaltung Nord", provider="none", listing_type="wg", commercial=True,
            applicant_intro=intro, applicant_answers={"haustiere": "Ich habe keine Haustiere."},
        )
        self.assertTrue(draft.startswith("Guten Tag,\n\n"))
        self.assertIn("Alex Neumann, 31, und arbeite als Architekt.", draft)
        self.assertIn("In meiner Freizeit koche ich gern.", draft)
        self.assertIn("Ich kann ab 1. Dezember einziehen.", draft)
        self.assertIn("Ich habe keine Haustiere.", draft)
        self.assertIn("Wann könnte ich Ihr Zimmer besichtigen?", draft)
        self.assertTrue(draft.endswith("Mit freundlichen Grüßen\nAlex Neumann"))
        self.assertNotIn("euch", draft)

    def test_commercial_inquiry_does_not_treat_a_profession_as_a_name(self):
        draft = personalise_message("Hallo!", "", provider="none", listing_type="wohnung", commercial=True,
                                    applicant_intro="Ich bin Architekt.")
        self.assertIn("könnte ich Ihre Wohnung besichtigen?", draft)
        self.assertNotIn("Name ist Architekt", draft)
        self.assertNotIn("\nArchitekt", draft)

    def test_same_room_gets_a_different_inquiry_for_each_contact_role(self):
        intro = "Ich bin Alex Neumann, 31. Ich arbeite als Architekt. In meiner Freizeit koche ich gern."
        template = f"Hi {{name}},\n\n{{LLM_TEXT}}\n\n{MOVE_IN}\n\nViele Grüße\nAlex Neumann"
        description = "Wir kochen freitags zusammen."
        options = {
            "provider": "none",
            "listing_type": "wg",
            "applicant_intro": intro,
            "applicant_answers": {"einzug": MOVE_IN},
        }
        manager = personalise_message(template, description, recipient_role="manager", **options)
        roommate = personalise_message(template, description, poster_name="Sam", recipient_role="roommate",
                                       commercial=True, **options)
        unknown = personalise_message(template, description, recipient_role="unknown", **options)
        self.assertIn("Alex Neumann, 31.", manager)
        self.assertIn("Ich arbeite als Architekt.", manager)
        self.assertIn("Wann könnte ich Ihr Zimmer besichtigen?", manager)
        self.assertTrue(roommate.startswith(f"Hi Sam,\n\n{intro}"))
        self.assertIn("Wenn ihr zusammen kocht, wär ich gern dabei.", roommate)
        self.assertIn(MOVE_IN, roommate)
        self.assertIn(QUESTION, roommate)
        self.assertTrue(unknown.startswith("Hallo,\n\n"))
        self.assertIn("Alex Neumann, 31.", unknown)
        self.assertIn("Ich arbeite als Architekt.", unknown)
        self.assertIn("In meiner Freizeit koche ich gern.", unknown)
        self.assertIn(MOVE_IN, unknown)
        self.assertIn("Wann könnte ich mir das Zimmer anschauen?", unknown)
        self.assertTrue(unknown.endswith("Viele Grüße\nAlex Neumann"))
        self.assertIn(MOVE_IN, manager)
        self.assertNotIn("euch", manager)

    def test_all_contact_roles_include_the_complete_profile_and_one_viewing_question(self):
        intro = ("Ich bin Alex Neumann, 31, und arbeite als Architekt. Nach Feierabend koche ich gern, "
                 "spiele Brettspiele und gehe wandern. Neben der Arbeit helfe ich ehrenamtlich im Sportverein. "
                 "Ich rauche nicht und habe keine Allergien.")
        phone = "+49 30 23125000"
        template = f"Hallo {{name}},\n\n{{APPLICANT_INTRO}}\n\n{{LLM_TEXT}}\n\nViele Grüße\nAlex Neumann\nTelefon: {phone}"
        for role, listing_type in (("roommate", "wg"), ("unknown", "zimmer"), ("manager", "wohnung")):
            with self.subTest(role=role):
                draft = personalise_message(
                    template, "Bitte sag kurz, ob du rauchst.", provider="none", listing_type=listing_type,
                    first_message=True, recipient_role=role, applicant_intro=intro,
                    applicant_answers={"einzug": MOVE_IN, "rauchen": "Ich rauche nicht."},
                )
                for fact in ("Alex Neumann, 31", "arbeite als Architekt", "koche ich gern", "Brettspiele",
                             "gehe wandern", "ehrenamtlich im Sportverein", "rauche nicht", "keine Allergien", MOVE_IN,
                             phone):
                    self.assertEqual(draft.count(fact), 1, draft)
                self.assertEqual(draft.count("?"), 1, draft)
                self.assertNotIn("{APPLICANT_INTRO}", draft)

    def test_neutral_inquiry_without_applicant_info_mentions_the_offer(self):
        draft = personalise_message("Hallo!", "", provider="none", listing_type="wohnung",
                                    recipient_role="unknown")
        self.assertIn("wann könnte ich mir die Wohnung anschauen?", draft)
        self.assertEqual(draft.count("?"), 1)
        self.assertNotIn("ich bin", draft.lower())

    def test_render_signature_changes_with_the_contact_role(self):
        roles = ("manager", "roommate", "unknown", None)
        signatures = {
            application_render_signature("Hallo", "mistral", first_message=True, recipient_role=role)
            for role in roles
        }
        self.assertEqual(len(signatures), len(roles))


if __name__ == "__main__":
    unittest.main()
