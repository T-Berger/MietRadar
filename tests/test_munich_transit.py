import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from src.munich_transit import DEFAULT_TRANSIT_API_URL, STATIONS, TransitLeg, TransitRoute, TransitRouter


ORIGIN = "Beispielstraße 1, 81667 München"
FRIDAY_BEFORE_CLOCK_CHANGE = datetime(2026, 3, 27, 15, tzinfo=timezone.utc)
GEOCODE = {
    "type": "ADDRESS", "street": "Beispielstraße", "houseNumber": "1", "zip": "81667",
    "lat": 48.128, "lon": 11.603, "country": "DE",
    "areas": [{"name": "München", "adminLevel": 6}],
}


def response(payload):
    return io.StringIO(json.dumps(payload))


def route(start, end, mode="SUBURBAN"):
    return {"startTime": f"2026-03-30T{start}Z", "endTime": f"2026-03-30T{end}Z",
            "legs": [{"mode": "WALK"}, {"mode": mode}]}


class TransitRouterTest(unittest.TestCase):
    @patch("src.munich_transit.urlopen")
    def test_checks_both_stations_and_counts_waiting_with_dst(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]),
            response({"itineraries": [route("06:12:00", "06:30:01")]}),
            response({"itineraries": [route("06:10:00", "06:30:00")]}),
        ]
        self.assertIs(TransitRouter().qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertTrue(all(req.full_url.startswith(DEFAULT_TRANSIT_API_URL + "/") for req in requests))
        self.assertIn("MietRadar", requests[0].get_header("User-agent"))
        addresses = parse_qs(urlsplit(requests[0].full_url).query)
        self.assertEqual(addresses["text"], [ORIGIN])
        plans = [parse_qs(urlsplit(req.full_url).query, keep_blank_values=True) for req in requests[1:]]
        self.assertEqual(tuple(plan["toPlace"][0] for plan in plans), STATIONS)
        self.assertEqual(plans[0]["time"], ["2026-03-30T06:00:00Z"])
        self.assertEqual(plans[0]["directModes"], [""])

    @patch("src.munich_transit.urlopen")
    def test_rejects_walking_only_and_no_transit(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]),
            response({"itineraries": [route("06:00:00", "06:03:00", mode="WALK")]}),
            response({"itineraries": []}),
        ]
        self.assertIs(TransitRouter().qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), False)

    @patch("src.munich_transit.urlopen")
    def test_failed_station_stays_unknown_without_other_match(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]), URLError("offline"), response({"itineraries": []}),
        ]
        self.assertIsNone(TransitRouter().qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))

    @patch("src.munich_transit.urlopen")
    def test_partial_address_and_wrong_municipality_do_not_qualify(self, urlopen):
        urlopen.return_value = response([
            dict(GEOCODE, houseNumber="10"),
            dict(GEOCODE, zip="81669"),
            dict(GEOCODE, areas=[{"name": "Landkreis München", "adminLevel": 6}]),
        ])
        self.assertIsNone(TransitRouter().qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))
        urlopen.assert_called_once()

    @patch("src.munich_transit.urlopen")
    def test_incomplete_address_does_not_make_a_request(self, urlopen):
        self.assertIsNone(TransitRouter().qualifies("81667 München", FRIDAY_BEFORE_CLOCK_CHANGE))
        self.assertIsNone(TransitRouter().qualifies("Beispielstraße 1, 81667 Berlin", FRIDAY_BEFORE_CLOCK_CHANGE))
        urlopen.assert_not_called()

    @patch("src.munich_transit.urlopen")
    def test_custom_instance_and_repeated_address_share_cached_route(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE, dict(GEOCODE, lat=48.1281)]),
            response({"itineraries": [route("06:00:00", "06:20:00")]}),
        ]
        router = TransitRouter("http://motis.railway.internal:8080")
        self.assertIs(router.qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        self.assertIs(router.qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        self.assertEqual(urlopen.call_count, 2)
        self.assertTrue(urlopen.call_args.args[0].full_url.startswith("http://motis.railway.internal:8080/api/v6/plan?"))

    @patch("src.munich_transit.MAX_REQUESTS_PER_DAY", 2)
    @patch("src.munich_transit.urlopen")
    def test_daily_request_budget_leaves_unchecked_station_unknown(self, urlopen):
        urlopen.side_effect = [response([GEOCODE]), response({"itineraries": []})]
        self.assertIsNone(TransitRouter().qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE))
        self.assertEqual(urlopen.call_count, 2)

    @patch("src.munich_transit.urlopen")
    def test_primary_check_and_tiers_use_the_correct_route_direction(self, urlopen):
        coordinates = f"{GEOCODE['lat']:.7f},{GEOCODE['lon']:.7f}"
        hbf, ost = STATIONS
        primary_plans = [(coordinates, hbf, "30", "1800"), (coordinates, ost, "30", "1800")]
        inbound_plans = [(hbf, coordinates, "45", "2700"), (ost, coordinates, "45", "2700")]
        cases = (
            (("06:20:00", "06:21:00"), ("a*", 20), inbound_plans),
            (("06:20:00", "06:16:01"), ("a*", 17), inbound_plans),
            (("06:40:00", "06:17:00"), ("a*", 17), inbound_plans),
            (("06:20:01", "06:22:00", "06:42:00", "06:30:00"), ("a", None), inbound_plans + primary_plans),
            (("06:23:00", "06:45:00", "06:33:00", "06:38:00"), ("b", 45), inbound_plans + primary_plans),
            (("06:25:01", "06:45:01", "06:33:00", "06:38:00"), ("c", 26), inbound_plans + primary_plans),
            (("06:45:00", "06:47:00", "06:45:00", "06:46:00"), ("c", 45), inbound_plans + primary_plans),
            (("06:45:01", "06:47:00", "06:45:01", "06:46:00"), ("outside", None), inbound_plans + primary_plans),
        )
        for arrivals, expected, expected_plans in cases:
            with self.subTest(arrivals=arrivals):
                urlopen.reset_mock()
                urlopen.side_effect = [response([GEOCODE])] + [
                    response({"itineraries": [route("06:10:00", arrival)]}) for arrival in arrivals]
                self.assertEqual(TransitRouter().classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), expected)
                plans = [parse_qs(urlsplit(call.args[0].full_url).query) for call in urlopen.call_args_list[1:]]
                self.assertEqual([
                    (plan["fromPlace"][0], plan["toPlace"][0], plan["maxTravelTime"][0], plan["searchWindow"][0])
                    for plan in plans
                ], expected_plans)

    @patch("src.munich_transit.urlopen")
    def test_a_verified_primary_route_can_be_reused_for_the_strict_check(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]), response({"itineraries": []}), response({"itineraries": []}),
            response({"itineraries": [route("06:10:00", "06:30:00")]}),
        ]
        router = TransitRouter()
        self.assertEqual(router.classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), ("a", None))
        self.assertIs(router.qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        self.assertEqual(router.coordinates(ORIGIN), (48.128, 11.603))
        self.assertEqual(urlopen.call_count, 4)

    @patch("src.munich_transit.urlopen")
    def test_cached_connections_preserve_winning_itinerary_and_waiting_time(self, urlopen):
        slower = route("06:00:00", "06:28:00")
        fastest = dict(route("06:04:00", "06:18:01"), legs=[
            {"mode": "WALK", "duration": 150, "from": {"name": "START"}, "to": {"name": "Hauptbahnhof"}},
            {"mode": "SUBWAY", "routeShortName": "U5", "duration": 419, "from": {"name": "Hauptbahnhof"},
             "to": {"name": "Innsbrucker Ring"}, "headsign": "Neuperlach Süd"},
            {"mode": "SUBWAY", "displayName": "U8", "duration": 179,
             "from": {"name": "Innsbrucker Ring"}, "to": {"name": "Karl-Preis-Platz"},
             "interlineWithPreviousLeg": True},
            {"mode": "WALK", "duration": 91, "from": {"name": "Karl-Preis-Platz"}, "to": {"name": "END"}},
        ])
        cancelled = dict(route("06:00:00", "06:12:00"), legs=[{"mode": "TRAM", "cancelled": True}])
        urlopen.side_effect = [
            response([GEOCODE]), response({"itineraries": [slower, fastest, cancelled]}), response({"itineraries": []}),
        ]
        router = TransitRouter()
        coordinates = (GEOCODE["lat"], GEOCODE["lon"])
        self.assertEqual(router.classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), ("a*", 19))
        connections = router.connections(coordinates, FRIDAY_BEFORE_CLOCK_CHANGE, cached_only=True)
        planned_for = datetime(2026, 3, 30, 6, tzinfo=timezone.utc).timestamp()
        expected = TransitRoute("Hauptbahnhof", "from_station", 19, (
            TransitLeg("WALK", None, None, "Hauptbahnhof", 3),
            TransitLeg("SUBWAY", "U5", "Hauptbahnhof", "Innsbrucker Ring", 7, "Neuperlach Süd"),
            TransitLeg("SUBWAY", "U8", "Innsbrucker Ring", "Karl-Preis-Platz", 3, through_service=True),
            TransitLeg("WALK", None, "Karl-Preis-Platz", None, 2),
        ), planned_for)
        self.assertEqual(connections, (expected,))
        self.assertEqual(urlopen.call_count, 3)

    @patch("src.munich_transit.MAX_REQUESTS_PER_DAY", 4)
    @patch("src.munich_transit.urlopen")
    def test_route_details_reuse_the_short_plan_and_reserve_requests_for_search(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]),
            response({"itineraries": [route("06:00:00", "06:25:00")]}),
            response({"itineraries": [route("06:00:00", "06:34:00")]}),
            response({"itineraries": [route("06:00:00", "06:17:00")]}),
        ]
        router = TransitRouter()
        coordinates = (GEOCODE["lat"], GEOCODE["lon"])
        self.assertIs(router.qualifies(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), True)
        connections = router.connections(coordinates, FRIDAY_BEFORE_CLOCK_CHANGE)
        self.assertEqual([(item.station, item.direction, item.minutes) for item in connections], [
            ("Hauptbahnhof", "to_station", 25), ("Ostbahnhof", "to_station", 34),
        ])
        supplement = parse_qs(urlsplit(urlopen.call_args.args[0].full_url).query)
        self.assertEqual(supplement["toPlace"], [STATIONS[1]])
        self.assertEqual(supplement["maxTravelTime"], ["45"])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(router.classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), ("a*", 17))
        self.assertEqual(urlopen.call_count, 4)

    @patch("src.munich_transit.urlopen")
    def test_one_confirmed_20_minute_route_is_enough(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]), URLError("offline"), response({"itineraries": [route("06:10:00", "06:20:00")]}),
        ]
        self.assertEqual(TransitRouter().classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), ("a*", 20))
        self.assertEqual(urlopen.call_count, 3)

    @patch("src.munich_transit.urlopen")
    def test_unknown_route_keeps_the_tier_unknown(self, urlopen):
        urlopen.side_effect = [
            response([GEOCODE]), response({"itineraries": []}), URLError("offline"),
            response({"itineraries": []}), response({"itineraries": []}),
        ]
        self.assertEqual(TransitRouter().classify(ORIGIN, FRIDAY_BEFORE_CLOCK_CHANGE), (None, None))
