import unittest
from datetime import date
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

    def test_move_in_labels_are_card_local_and_keep_month_only_values_as_text(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"{ROOM}\n{FLAT}")
        email.add_alternative(f"""<body>
          <article><a href="{ROOM}">WG-Zimmer</a><div>16.11.2026</div>
            <span>Verfügbar ab</span><span>Januar 2027</span></article>
          <article><a href="{FLAT}">Wohnung</a><div>16.11.2026</div>
            <span>Verfügbar ab:</span><span>1.12.2026</span><div>Frei bis 31.03.2027</div></article>
        </body>""", subtype="html")
        room, flat = extract_listings(email.as_bytes(), SENDER)
        self.assertIn("Verfügbar ab Januar 2027", room.description)
        self.assertEqual((room.available_from, room.available_until), (None, None))
        self.assertEqual((flat.available_from, flat.available_until), (date(2026, 12, 1), date(2027, 3, 31)))
        self.assertIsNone(flat.published_at)

    def test_merges_explicit_move_in_dates_from_the_plain_alternate(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"16.11.2026\nMietbeginn: 2026-12-15\n{ROOM}")
        email.add_alternative(f"""<article><a href="{ROOM}">Zimmer mit Balkon</a>
          <div>Verfügbar bis: 15.04.2027</div></article>""", subtype="html")
        [listing] = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual((listing.available_from, listing.available_until), (date(2026, 12, 15), date(2027, 4, 15)))
        self.assertIsNone(listing.published_at)

    def test_commercial_status_is_scoped_to_the_ad_with_an_explicit_provider_label(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"{ROOM}\n\nAnbietertyp: Gewerblich\n{FLAT}")
        email.add_alternative(f'''<body>
          <article><a href="{ROOM}">Zimmer in einer Business-WG</a></article>
          <article><a href="{FLAT}">Wohnung</a><div>Wohnfläche: 53 m²</div></article>
        </body>''', subtype="html")
        room, flat = extract_listings(email.as_bytes(), SENDER)
        self.assertFalse(room.is_commercial)
        self.assertTrue(flat.is_commercial)

        email.clear_content()
        email.set_content(f"{ROOM}\n{FLAT}")
        # A shared wrapper or another listing cannot supply a provider type.
        email.add_alternative(f'''<section><p>Gewerblicher Anbieter</p>
          <article><a href="{ROOM}">Zimmer</a><span>Anbietertyp</span><span>Gewerblich</span></article>
          <article><a href="{FLAT}">Wohnung</a></article>
        </section>''', subtype="html")
        room, flat = extract_listings(email.as_bytes(), SENDER)
        self.assertTrue(room.is_commercial)
        self.assertFalse(flat.is_commercial)

    def test_recipient_role_is_ad_local_and_kept_from_either_email_format(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"Ich wohne selbst in der WG.\n{ROOM}\n\n{FLAT}")
        email.add_alternative(f'''<section><p>Kontakt: Hausverwaltung</p>
          <article><a href="{ROOM}">WG-Zimmer</a><div>Der Vermieter kennt das Haus.</div></article>
          <article><a href="{FLAT}">Wohnung</a><div>Ich bin die Vermieterin.</div></article>
        </section>''', subtype="html")
        room, flat = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual(room.recipient_role, "roommate")
        self.assertEqual(flat.recipient_role, "manager")
        self.assertFalse(flat.is_commercial)

    def test_keeps_a_move_in_month_found_only_in_the_plain_alternate(self):
        email = EmailMessage()
        email["From"] = SENDER
        email.set_content(f"Verfügbar ab Januar 2027\n{ROOM}")
        email.add_alternative(f'<article><a href="{ROOM}">Zimmer mit Balkon</a></article>', subtype="html")
        [listing] = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual(listing.title, "Zimmer mit Balkon")
        self.assertIn("Verfügbar ab Januar 2027", listing.description)

    def test_manual_location_must_be_a_complete_munich_address(self):
        self.assertEqual(normalize_munich_address("Maximilianstr. 4 A, 80539 München"),
                         "Maximilianstr. 4 A, 80539 München")
        for invalid in ("Haidhausen", "81667 München", "Musterstraße, 81667 München",
                        "Musterstraße 1, 86150 Augsburg", "Musterstraße 1, 85521 München"):
            self.assertIsNone(normalize_munich_address(invalid), invalid)

    def test_photos_stay_with_their_own_email_card(self):
        email = EmailMessage()
        email["From"] = SENDER
        cdn = "https://img.kleinanzeigen.de/api/v1/prod-ads/images/"
        room = f"{cdn}aa/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        flat = f"{cdn}bb/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        unrelated = f"{cdn}cc/cccccccc-cccc-cccc-cccc-cccccccccccc"
        email.set_content(f"{ROOM}\n{FLAT}")
        email.add_alternative(f"""<body><img src="{unrelated}?rule=$_59.AUTO">
          <article>
            <a href="{ROOM}"><img src="{room}?rule=$_59.AUTO&amp;tracking=private#preview"></a>
            <a href="{ROOM}">WG-Zimmer am Isartor</a>
            <img src="{unrelated}" width="1">
            <img src="https://img.kleinanzeigen.de.evil.test/api/v1/prod-ads/images/aa/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa">
          </article>
          <table><tr><td><a href="{FLAT}">Wohnung in Haidhausen</a>
            <img data-src="{flat}?rule=$_2.JPG">
          </td></tr></table></body>""", subtype="html")
        room_ad, flat_ad = extract_listings(email.as_bytes(), SENDER)
        self.assertEqual(room_ad.photo_urls, (f"{room}?rule=%24_59.AUTO",))
        self.assertEqual(flat_ad.photo_urls, (f"{flat}?rule=%24_2.JPG",))
