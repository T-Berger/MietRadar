import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError

from src.munich_transit import ROUTES_URL, STATIONS, TransitRouter


ORIGIN = "Beispielstraße 1, 81667 München"
FRIDAY_BEFORE_CLOCK_CHANGE = datetime(2026, 3, 27, 15, tzinfo=timezone.utc)


def response(payload):
    return io.StringIO(json.dumps(payload))


class TransitRouterTest(unittest.TestCase):
    @patch("src.munich_transit.urlopen")
    def test_checks_both_stations_and_accepts_exact_limit(self, urlopen):
        urlopen.side_effect = [
            response({"routes": [{"duration": "1800.000000001s"}]}),
            response({"routes": [{"duration": "2100s"}, {"duration": "1800s"}]}),
        ]
        self.assertIs(TransitRouter("key").qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([request.full_url for request in requests], [ROUTES_URL, ROUTES_URL])
        bodies = [json.loads(request.data) for request in requests]
        self.assertEqual([body["destination"]["address"] for body in bodies], list(STATIONS))
        self.assertEqual(bodies[0]["origin"], {"address": ORIGIN})
        self.assertEqual(bodies[0]["travelMode"], "TRANSIT")
        self.assertEqual(bodies[0]["departureTime"], "2026-03-30T06:00:00Z")

    @patch("src.munich_transit.urlopen")
    def test_rejects_over_limit_and_no_connection(self, urlopen):
        urlopen.side_effect = [response({"routes": [{"duration": "1801s"}]}), response({})]
        self.assertIs(TransitRouter("key").qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), False)

    @patch("src.munich_transit.urlopen")
    def test_failed_station_stays_unknown_without_other_match(self, urlopen):
        urlopen.side_effect = [URLError("offline"), response({"routes": [{"duration": "2100s"}]})]
        self.assertIsNone(TransitRouter("key").qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))

    @patch("src.munich_transit.urlopen")
    def test_partial_address_match_does_not_qualify(self, urlopen):
        urlopen.side_effect = [
            response({
                "routes": [{"duration": "900s"}],
                "geocodingResults": {"origin": {"partialMatch": True}},
            }),
            response({}),
        ]
        self.assertIsNone(TransitRouter("key").qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))

    @patch("src.munich_transit.urlopen")
    def test_missing_key_or_address_makes_no_request(self, urlopen):
        self.assertIsNone(TransitRouter(None).qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))
        self.assertIsNone(TransitRouter("key").qualifies(" ", FRIDAY_BEFORE_CLOCK_CHANGE))
        urlopen.assert_not_called()
