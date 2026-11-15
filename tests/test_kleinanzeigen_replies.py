import unittest
from email.message import EmailMessage

from kleinanzeigen_replies import extract_replies


SENDER = "noreply@kleinanzeigen.de"
AD_URL = "https://www.kleinanzeigen.de/s-anzeige/wohnung-in-muenchen/1234567890-203-1234"


def email(body, *, subject="Neue Nachricht zu deiner Anfrage", sender=SENDER, identifier="first", html=None):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    if identifier is not None:
        message["Message-ID"] = f"<{identifier}@kleinanzeigen.de>"
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


class KleinanzeigenRepliesTest(unittest.TestCase):
    def test_reply_preview_matches_a_listing_and_excludes_the_platform_footer(self):
        raw = email(f"Ein Nutzer hat dir geschrieben.\n{AD_URL}?tracking=1\n"
                    "Anzeigennummer: 1234567890\nNachricht von: Gastgeber\n"
                    "Hallo! Die Besichtigung ist am Mittwoch um 18 Uhr.\n\n"
                    "Nachricht ansehen\nDein Kleinanzeigen-Team\nImpressum")
        reply, = extract_replies(raw, SENDER)
        self.assertEqual(reply.listing_id, "ka-1234567890")
        self.assertEqual(reply.url, AD_URL)
        self.assertEqual(reply.text, "Hallo! Die Besichtigung ist am Mittwoch um 18 Uhr.")

    def test_conversation_only_notifies_without_guessing_the_preview(self):
        raw = email("In deinem Postfach wartet eine Antwort.", html=(
            '<div><a href="https://www.kleinanzeigen.de/m-nachrichten.html?utm=mail#a1:b2">Nachricht lesen</a></div>'
            '<script>throw new Error(1)</script>'
        ))
        reply, = extract_replies(raw, SENDER)
        self.assertIsNone(reply.listing_id)
        self.assertEqual(reply.url, "https://www.kleinanzeigen.de/m-nachrichten.html#a1:b2")
        self.assertEqual(reply.text, "")

    def test_rejects_unrelated_messages_spoofed_destinations_and_conflicting_listing_ids(self):
        for raw in (
            email(AD_URL, subject="Dein Kleinanzeigen Suchauftrag"),
            email(AD_URL, sender="noreply@kleinanzeigen.de.example.org"),
            email("https://kleinanzeigen.de.example.org/m-nachrichten.html"),
            email(f"Anzeigennummer: 1234\n{AD_URL}"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(extract_replies(raw, SENDER), [])

    def test_keeps_message_identity_stable_but_does_not_merge_distinct_messages(self):
        first, = extract_replies(email(f"{AD_URL}\nNachricht: Hallo", identifier="id-one"), SENDER)
        repeated, = extract_replies(email(f"{AD_URL}\nNachricht: Hallo", identifier="id-one"), SENDER)
        second, = extract_replies(email(f"{AD_URL}\nNachricht: Hallo", identifier="id-two"), SENDER)
        self.assertEqual(first.id, repeated.id)
        self.assertNotEqual(first.id, second.id)

    def test_conflicting_mail_parts_do_not_supply_text_to_the_judge(self):
        raw = email(f"{AD_URL}\nNachricht: Mittwoch passt.", html=(
            f'<a href="{AD_URL}">Anzeige</a><p>Nachricht: Freitag passt.</p>'
        ))
        reply, = extract_replies(raw, SENDER)
        self.assertEqual(reply.text, "")


if __name__ == "__main__":
    unittest.main()
