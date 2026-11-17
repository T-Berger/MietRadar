import io
import json
import unittest
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request

from src.wg_source import (
    BASE_URL,
    DEFAULT_SEARCH_URLS,
    ListingRef,
    WGAccessBlocked,
    WGListingGone,
    WGSource,
    WGSourceError,
    _PublicRedirect,
)


class PublicPageTests(unittest.TestCase):
    def setUp(self):
        self.source = WGSource(DEFAULT_SEARCH_URLS[:2], 2, min_request_interval=0)

    def stub_pages(self, responses):
        class FakeOpener:
            def open(self, request, timeout):
                response = responses[request.full_url]
                if isinstance(response, int):
                    raise HTTPError(request.full_url, response, "", {}, None)
                return io.BytesIO(response.encode())

        self.source._opener = FakeOpener()

    def test_canonical_public_links(self):
        ref = ListingRef.from_url(
            "https://wg-gesucht.de/wg-zimmer-in-Muenchen.123.html?src=foo#message"
        )
        self.assertEqual(ref, ListingRef("123", f"{BASE_URL}/wg-zimmer-in-Muenchen.123.html"))
        for url in (
            DEFAULT_SEARCH_URLS[0],
            "https://example.com/123.html",
            "https://www.wg-gesucht.de@example.com/123.html",
            "https://www.wg-gesucht.de/api/123.html",
            "https://www.wg-gesucht.de/nachricht-senden/123.html",
        ):
            self.assertIsNone(ListingRef.from_url(url), url)

    def test_extracts_only_public_offer_gallery_photos(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wohnungen-in-Muenchen.123.html")
        cdn = "https://img.wg-gesucht.de/media/up/2026/45"
        self.stub_pages({ref.url: f"""
            <meta property="og:image" content="{cdn}/outside.large.JPEG">
            <h1 class="detailed-view-title">Wohnung in München</h1>
            <div id="WG-Pictures"><div id="gallery_slides">
                <div class="sp-slide"><img class="sp-image" src="img/d.gif"
                    data-src="{cdn}/room.large.JPEG" data-medium="https://img.wg-gesucht.de/./media/up/2026/45/room.sized.JPEG?track=secret#display"></div>
                <div class="sp-slide"><img class="sp-image" src="{cdn}/room.large.JPEG"></div>
                <div class="sp-slide"><img class="sp-image" src="https://img.wg-gesucht.de.evil.example/media/up/evil.jpg"></div>
                <div class="sp-slide"><img class="sp-image" src="https://img.wg-gesucht.de:443/media/up/blocked.jpg"></div>
                <div class="sp-slide"><img class="sp-image" src="https://img.wg-gesucht.de/media/up/%2e%2e/profile.jpg"></div>
                <div class="sp-slide"><img class="sp-image" src="{cdn}/kitchen.large.JPEG"></div>
            </div></div>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.photo_urls, (f"{cdn}/room.sized.JPEG", f"{cdn}/kitchen.large.JPEG"))

    def test_photo_metadata_is_scoped_to_the_current_ad(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wohnungen-in-Muenchen.123.html")
        cdn = "https://img.wg-gesucht.de/media/up/2026/45"
        metadata = json.dumps([
            {"@type": "RealEstateListing", "url": f"{BASE_URL}/456.html", "image": f"{cdn}/unrelated.jpg"},
            {"@type": "RealEstateListing", "url": ref.url, "image": [{"contentUrl": f"{cdn}/matched.jpg"}]},
        ])
        self.stub_pages({ref.url: f"""
            <h1 class="detailed-view-title">Wohnung in München</h1>
            <script type="application/ld+json">{metadata}</script>
        """})
        self.assertEqual(self.source.details(ref).photo_urls, (f"{cdn}/matched.jpg",))

    def test_distinguishes_the_company_contact_from_business_wg_and_unrelated_links(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wg-zimmer-in-Muenchen.123.html")
        ad = """
            <h1 class="detailed-view-title">WG-Zimmer in München</h1>
            <div id="freitext_0">Business-WG für Berufstätige.
                <a href="/profile/company/anderes-unternehmen.789.html">Firma aus der Nachbarschaft</a>
            </div>
            <div class="rhs_contact_information"><a href="https://example.org/profile/company/fremd.999.html">Verifiziert</a></div>
        """
        self.stub_pages({ref.url: ad + '<div class="contact_box_sticky">Private:r Nutzer:in</div>'})
        listing = self.source.details(ref)
        self.assertFalse(listing.is_commercial)
        self.assertEqual(listing.recipient_role, "unknown")

        self.stub_pages({ref.url: ad + '''
            <div class="contact_box_sticky">
                <a href="/profile/company/vermietung.456.html">Zum Profil</a>
            </div>
        '''})
        listing = self.source.details(ref)
        self.assertTrue(listing.is_commercial)
        self.assertEqual(listing.recipient_role, "manager")

    def test_recipient_role_comes_from_the_contact_or_narrator(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wg-zimmer-in-Muenchen.123.html")
        for contact, description, expected in (
            ("Private:r Nutzer:in", "Ich verwalte diese Wohnung.", "manager"),
            ("Private:r Nutzer:in", "Ich bin Lea und wohne selbst in der WG. Unser Vermieter ist nett.", "roommate"),
            ("Kontakt: Hausverwaltung", "Wir wohnen hier schon seit zwei Jahren.", "manager"),
            ("Private:r Nutzer:in", "Das Zimmer ist frei. Der Vermieter wohnt nebenan.", "unknown"),
            ("Private:r Nutzer:in", "Ich wohne nicht mehr in der WG.", "unknown"),
        ):
            with self.subTest(description=description, contact=contact):
                self.stub_pages({ref.url: f"""
                    <h1 class="detailed-view-title">WG-Zimmer in München</h1>
                    <div class="contact_box_sticky">{contact}</div>
                    <div id="freitext_0">{description}</div>
                """})
                listing = self.source.details(ref)
                self.assertFalse(listing.is_commercial)
                self.assertEqual(listing.recipient_role, expected)

    def test_company_contact_requires_a_published_persons_name(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wohnungen-in-Muenchen.123.html")
        for label, expected in (
            ("<span>Ansprech-partner:</span><span>Frau Dr. Nele Vogel</span>", "Frau Dr. Nele Vogel"),
            ("<p>Kontakt: Robin von Stein.</p>", "Robin von Stein"),
            ("<p>Kontaktperson: Frau</p>", None),
            ("<p>Kontaktperson: Herr</p>", None),
            ("<p>Kontakt: Hausverwaltung Nord</p>", None),
            ("", None),
        ):
            with self.subTest(label=label):
                self.stub_pages({ref.url: f"""
                    <h1 class="detailed-view-title">Wohnung in München</h1>
                    <div id="freitext_0">Mieterin ist Frau Reger.</div>
                    <div class="rhs_contact_information">
                      <p class="text-bold">Münchner Wohnen</p>
                      <a href="/profile/company/muenchner-wohnen.456.html">Zum Profil</a>
                      {label}
                    </div>
                """})
                listing = self.source.details(ref)
                self.assertEqual(listing.recipient_role, "manager")
                self.assertEqual(listing.contact_name, expected)

    def test_visit_order_keeps_update_time_separate_from_publication(self):
        def card(listing_id, age, class_name="", partner=""):
            return f"""
                <a class="wgg-list-card offer_list_item {class_name}" data-id="{listing_id}"
                    data-campaign_partner="{partner}"
                    href="/wg-zimmer-in-Muenchen.{listing_id}.html">
                  <span class="card-offer-online-status">
                    <span class="sr-only">Online:</span><span class="card-subtitle">{age}</span>
                  </span>
                </a>
            """

        def timestamp(listing_id, published):
            return {
                "@type": "RealEstateListing",
                "url": f"{BASE_URL}/{listing_id}.html",
                "datePosted": published,
            }

        metadata = json.dumps([
            timestamp("10", "2026-11-15T10:25:00+01:00"),
            timestamp("11", "2026-11-15"),
        ])
        self.stub_pages({
            self.source.search_urls[0]: (
                f'<script type="application/ld+json">{metadata}</script>'
                + card("10", "3 Std.") + card("12", "20 Min.") + card("13", "1 Min.", "premium_sticky_asset")
                + '<div class="text-center"><a class="black_text_link" href="/wg-zimmer-in-Muenchen.90.0.1.1.html">&gt;</a></div>'
            ),
            f"{BASE_URL}/wg-zimmer-in-Muenchen.90.0.1.1.html": (
                card("14", "6 Std.") + card("15", "1 Min.", partner="Wunderflats")
                + '<div class="text-center"><a class="black_text_link" href="/wg-zimmer-in-Muenchen.90.0.1.2.html">&gt;</a></div>'
            ),
            self.source.search_urls[1]: card("11", "2 Min.") + card("12", "3 Std."),
        })
        self.assertEqual([item.id for item in self.source.candidates()], ["11", "12", "10", "14"])

    def test_reads_public_listing_fields_and_ignores_lock_placeholder(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <link rel="canonical" href="/wg-zimmer-in-Muenchen-Sendling.123.html">
            <h1 class="detailed-view-title">Zimmer am Park</h1>
            <div id="freitext_0">Sonniges Zimmer.</div>
            <div id="freitext_2">Wir kochen gern zusammen.</div>
            <table><tr><td>Online seit:</td><td><span style="filter: blur(4px)">Dummy x</span></td></tr></table>
            <a href="#map_container"><span class="section_panel_detail">Straße 9<br>81373 München Sendling</span></a>
            <div><h3 class="headline">Die WG:</h3><ul>
                <li><span class="section_panel_detail">Bewohneralter: 23 bis 31 Jahre</span></li>
                <li><span class="section_panel_detail">Berufstätigen-WG</span></li>
            </ul></div>
            <div><h3 class="headline">Gesucht wird:</h3><ul>
                <li><span class="section_panel_detail">Frau</span></li>
            </ul></div>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.url, f"{BASE_URL}/wg-zimmer-in-Muenchen-Sendling.123.html")
        self.assertEqual(listing.address, "Straße 9, 81373 München")
        self.assertEqual(listing.district, "Sendling")
        self.assertEqual(listing.wanted, "Frau")
        self.assertEqual(listing.resident_ages, "23 bis 31 Jahre")
        self.assertEqual(listing.wg_type, "Berufstätigen-WG")
        self.assertEqual(listing.description, "Sonniges Zimmer.\nWir kochen gern zusammen.")
        self.assertTrue(listing.is_wg)
        self.assertIsNone(listing.published_at)
        self.assertIsNone(listing.published_day)

    def test_publication_day_does_not_supply_a_time(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <table>
                <tr><td>Online:</td><td>16.11.2026</td></tr>
                <tr><td>Veröffentlicht am:</td><td>14.11.2026</td></tr>
            </table>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.published_day, date(2026, 11, 14))
        self.assertIsNone(listing.published_at)
        self.assertIsNone(listing.available_from)
        self.assertIsNone(listing.available_until)

    def test_availability_reads_only_dates_from_the_availability_panel(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <meta itemprop="datePosted" content="2026-11-15">
            <div>
                <h2 class="section_panel_title mb10 m0">Verfügbarkeit</h2>
                <div class="row">
                    <div class="col-xs-6"><span class="section_panel_detail">frei ab:</span></div>
                    <div class="col-xs-6"><span class="section_panel_value">01.01.2027</span></div>
                </div>
                <div class="row">
                    <div class="col-xs-6"><span class="section_panel_detail">frei bis:</span></div>
                    <div class="col-xs-6"><span class="section_panel_value">28.02.2027</span></div>
                </div>
                <div class="row">
                    <div class="col-xs-6"><span class="section_panel_detail">Online:</span></div>
                    <div class="col-xs-6"><b>45 Minuten</b></div>
                </div>
            </div>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.available_from, date(2027, 1, 1))
        self.assertEqual(listing.available_until, date(2027, 2, 28))
        self.assertEqual(listing.published_day, date(2026, 11, 15))

    def test_availability_without_an_exact_calendar_date_remains_unknown(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <div>
                <h2 class="section_panel_title">Verfügbarkeit</h2>
                <div><div><span class="section_panel_detail">frei ab:</span></div>
                <div><span class="section_panel_value">nach Absprache</span></div></div>
                <div><div><span class="section_panel_detail">frei bis:</span></div>
                <div><span class="section_panel_value">31.02.2027</span></div></div>
            </div>
        """})
        listing = self.source.details(ref)
        self.assertIsNone(listing.available_from)
        self.assertIsNone(listing.available_until)

    def test_explicit_publication_and_incomplete_address(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <meta itemprop="datePosted" content="2026-11-15">
            <meta itemprop="datePosted" content="2026-11-15T10:25:00+01:00">
            <a href="#map_container"><span class="section_panel_detail">Nymphenburger Straße<br>80634 München</span></a>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.published_at, datetime(2026, 11, 15, 9, 25, tzinfo=timezone.utc))
        self.assertIsNone(listing.address)

    def test_reads_headline_rent_and_room_area_without_using_deposits_or_apartment_size(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wg-zimmer-in-Muenchen.123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <div><span class="key_fact_detail">Zimmergröße</span><b class="key_fact_value">17,5m²</b></div>
            <div><span class="key_fact_detail">Wohnfläche</span><b class="key_fact_value">75 m²</b></div>
            <div><span class="key_fact_detail">Gesamtmiete</span><b class="key_fact_value">1.234,50 €</b></div>
            <div><div><span class="section_panel_detail">Miete:</span></div>
            <div><span class="section_panel_value">950 €</span></div></div>
            <div><div><span class="section_panel_detail">Kaution:</span></div>
            <div><span class="section_panel_value">2.500 €</span></div></div>
        """})
        listing = self.source.details(ref)
        self.assertEqual((listing.rent_eur, listing.rent_kind, listing.area_sqm), (1234.50, "total", 17.5))

    def test_exact_ad_metadata_supplies_untyped_rent_and_square_meters(self):
        ref = ListingRef.from_url(f"{BASE_URL}/wohnungen-in-Muenchen.123.html")
        metadata = json.dumps([
            {"@type": "RealEstateListing", "url": f"{BASE_URL}/121.html",
             "offers": {"price": 1, "priceCurrency": "EUR"}},
            {"@type": "RealEstateListing", "url": ref.url,
             "offers": {"price": "1500.50", "priceCurrency": "EUR"},
             "mainEntity": {"floorSize": {"value": 54, "unitCode": "MTK"}}},
        ])
        self.stub_pages({ref.url: f"""
            <h1 class="detailed-view-title">Wohnung</h1>
            <script type="application/ld+json">{metadata}</script>
        """})
        listing = self.source.details(ref)
        self.assertEqual((listing.rent_eur, listing.rent_kind, listing.area_sqm), (1500.50, "unspecified", 54))

    def test_missing_or_blocked_response_is_explicit(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        error_cases = (
            (404, WGListingGone),
            (429, WGAccessBlocked),
            ("<title>Überprüfung</title>", WGAccessBlocked),
            ("<h1>Bitte bestätigen Sie, dass Sie ein Mensch sind</h1>", WGAccessBlocked),
            ("<h1>Unexpected format</h1>", WGSourceError),
        )
        for response, exception in error_cases:
            with self.subTest(response=response):
                self.stub_pages({ref.url: response})
                with self.assertRaises(exception):
                    self.source.details(ref)

    def test_redirect_to_non_public_endpoint_is_declined(self):
        redirect = _PublicRedirect()
        with self.assertRaises(WGAccessBlocked):
            redirect.redirect_request(
                Request(f"{BASE_URL}/123.html"), None, 302, "", {}, f"{BASE_URL}/api/123.html"
            )


if __name__ == "__main__":
    unittest.main()
