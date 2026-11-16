import unittest

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

    def test_extending_the_private_intro_replaces_the_entire_cached_intro(self):
        old_intro = f"{INTRO} Wenn ich Zeit habe, zeichne ich oft."
        new_intro = f"{old_intro} Nebenbei mache ich Musik."
        draft = f"Hi zusammen,\n\n{old_intro}\n\n{DETAIL}\n\n{QUESTION}\n\nViele Grüße\nAlex"

        refreshed = revise_first_message_draft(draft, new_intro)

        self.assertEqual(refreshed, draft.replace(old_intro, new_intro, 1))
        self.assertEqual(revise_first_message_draft(refreshed, new_intro), refreshed)


if __name__ == "__main__":
    unittest.main()
