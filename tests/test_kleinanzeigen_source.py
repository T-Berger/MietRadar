import unittest
from email.message import EmailMessage

from kleinanzeigen_source import extract_listings, normalize_munich_address


SENDER = "alerts@kleinanzeigen.de"
ROOM = "https://www.kleinanzeigen.de/s-anzeige/wg-zimmer-am-isartor/12345-199-6440"
FLAT = "https://www.kleinanzeigen.de/s-anzeige/wohnung-in-haidhausen/12346-203-9634"


class KleinanzeigenSourceTest(unittest.TestCase):
    def test_uses_only_the_listing_card_address_and_never_treats_district_as_street(self):
        email = EmailMessage()
        email["From"] = f"Kleinanzeigen <{SENDER}>"
        email.set_content(f"{ROOM}\n{FLAT}")
        email.add_alternative(f"""<table>
          <tr><td><table>
            <tr><td><a href="{ROOM}?utm_source=newsletter">WG-Zimmer am Isartor</a></td></tr>
            <tr><td>80331 München - Altstadt</td></tr>
            <tr><td>620 € · 17,5 m²</td></tr>
            <tr><td>Nur für Studierende.</td></tr>
          </table></td></tr>
          <tr><td><table>
            <tr><td><a href="{FLAT}">Wohnung in Haidhausen</a></td></tr>
            <tr><td>Musterstraße 1a<br>81667 München</td></tr>
            <tr><td>Warmmiete:<br>1.200,50 €</td></tr>
            <tr><td>Wohnfläche: 63 m²</td></tr>
            <tr><td>Kaution:<br>3.000 €</td></tr>
          </table></td></tr>
        </table>""", subtype="html")
        room, flat = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual((room.id, room.url, room.title, room.district),
                         ("ka-12345", ROOM, "WG-Zimmer am Isartor", "80331 München - Altstadt"))
        self.assertTrue(room.is_wg)
        self.assertIn("Nur für Studierende.", room.description)
        self.assertIsNone(room.address)
        self.assertIsNone(room.published_at)
        self.assertEqual((room.rent_eur, room.rent_kind, room.area_sqm), (620, "unspecified", 17.5))
        self.assertEqual(flat.address, "Musterstraße 1a, 81667 München")
        self.assertNotIn("Musterstraße", flat.description)
        self.assertFalse(flat.is_wg)
        self.assertEqual((flat.rent_eur, flat.rent_kind, flat.area_sqm), (1200.50, "warm", 63))

    def test_plain_fallback_accepts_only_direct_rental_links_from_the_exact_sender(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"""Zwei Zimmer am Ostbahnhof
Adresse: Am Harras 2, 81373 München
Kaltmiete: 1.150 € monatlich
Wohnfläche: 53,7 qm
{FLAT}.

https://www.kleinanzeigen.de/s-anzeige/fahrrad/12348-217-6450
https://www.kleinanzeigen.de.evil.example/s-anzeige/wohnung/12349-203-6440
https://www.kleinanzeigen.de/ck/?url={ROOM}
""")
        email.add_alternative('<a href="https://tracking.example/path">Zur Anzeige</a>', subtype="html")
        [listing] = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual((listing.id, listing.url, listing.address),
                         ("ka-12346", FLAT, "Am Harras 2, 81373 München"))
        self.assertEqual((listing.rent_eur, listing.rent_kind, listing.area_sqm), (1150, "cold", 53.7))
        self.assertEqual(extract_listings(email.as_bytes(), "other@kleinanzeigen.de"), [])

    def test_rejects_deposit_area_of_entire_wg_and_price_per_night(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"""Kaution:
1.500 €
Wohnfläche: 75 m²
30 € pro Nacht
{ROOM}
""")
        [listing] = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual((listing.rent_eur, listing.rent_kind, listing.area_sqm), (None, None, None))

    def test_manual_location_must_be_a_complete_munich_address(self):
        self.assertEqual(normalize_munich_address("Maximilianstr. 4 A, 80539 München"),
                         "Maximilianstr. 4 A, 80539 München")
        for invalid in ("Haidhausen", "81667 München", "Musterstraße, 81667 München",
                        "Musterstraße 1, 86150 Augsburg", "Musterstraße 1, 85521 München"):
            self.assertIsNone(normalize_munich_address(invalid), invalid)
