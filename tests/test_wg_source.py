import io
import json
import unittest
from datetime import datetime, timezone
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

    def test_visit_order_keeps_update_time_separate_from_publication(self):
        def card(listing_id, age, class_name=""):
            return f"""
                <a class="wgg-list-card offer_list_item {class_name}" data-id="{listing_id}"
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
            ),
            self.source.search_urls[1]: card("11", "2 Min.") + card("12", "3 Std."),
        })
        first, second = self.source.candidates()
        self.assertEqual((first.id, second.id), ("11", "12"))

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

    def test_explicit_publication_and_incomplete_address(self):
        ref = ListingRef.from_url(f"{BASE_URL}/123.html")
        self.stub_pages({ref.url: """
            <h1 class="detailed-view-title">Zimmer</h1>
            <meta itemprop="datePosted" content="2026-11-15T10:25:00+01:00">
            <a href="#map_container"><span class="section_panel_detail">Nymphenburger Straße<br>80634 München</span></a>
        """})
        listing = self.source.details(ref)
        self.assertEqual(listing.published_at, datetime(2026, 11, 15, 9, 25, tzinfo=timezone.utc))
        self.assertIsNone(listing.address)

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
