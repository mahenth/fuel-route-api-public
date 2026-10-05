import copy
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from planner.errors import APIError
from planner.models import RouteCache
from planner.routing import METERS_PER_MILE, Router, decode_polyline6


POINTS = [
    {"latitude": 35.0, "longitude": -105.0},
    {"latitude": 35.0, "longitude": -96.0},
]


def encode_test_geometry(coordinates):
    """Independent fixture encoder for the standard signed-varint format."""
    previous = [0, 0]
    output = []
    for lon, lat in coordinates:
        for index, coordinate in enumerate((lat, lon)):
            current = round(coordinate * 1_000_000)
            delta = current - previous[index]
            previous[index] = current
            unsigned = delta * 2 if delta >= 0 else -delta * 2 - 1
            while unsigned >= 32:
                unsigned, remainder = divmod(unsigned, 32)
                output.append(chr(63 + remainder + 32))
            output.append(chr(63 + unsigned))
    return ''.join(output)


class PolylineDecoderTests(SimpleTestCase):
    def test_standard_documented_example_at_six_decimal_scale(self):
        # Canonical encoded-polyline example uses five decimal places. The same
        # signed integers at precision six are exactly one tenth its coordinates.
        encoded = '_p~iF~ps|U_ulLnnqC_mqNvxq`@'
        self.assertEqual(decode_polyline6(encoded), [
            [-12.02, 3.85], [-12.095, 4.07], [-12.6453, 4.3252],
        ])

    def test_millionth_degree_precision_and_negative_deltas_round_trip(self):
        coordinates = [[-122.123456, 37.123456], [-122.123455, 37.123457], [-122.123457, 37.123455]]
        self.assertEqual(decode_polyline6(encode_test_geometry(coordinates)), coordinates)

    def test_zero_deltas_and_antimeridian_round_trip(self):
        coordinates = [[180, 0], [-180, 0], [-180, 0], [0, 90], [0, -90]]
        self.assertEqual(decode_polyline6(encode_test_geometry(coordinates)), coordinates)

    def test_rejects_malformed_truncated_and_overlong_varints(self):
        for encoded in (None, '', '?', '_', '???', '????_', '!!!!!!', '______?', '_???', 'é???'):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                decode_polyline6(encoded)

    def test_rejects_out_of_bounds_coordinates(self):
        for coordinates in ([[-181, 0], [0, 0]], [[0, 91], [0, 0]]):
            with self.subTest(coordinates=coordinates), self.assertRaises(ValueError):
                decode_polyline6(encode_test_geometry(coordinates))

    @patch('planner.routing.MAX_ROUTE_POINTS', 3)
    def test_limits_decoded_points_and_encoded_size(self):
        with self.assertRaises(ValueError):
            decode_polyline6('????????')
        with self.assertRaises(ValueError):
            decode_polyline6('?' * 37)


def provider_payload():
    return {
        "code": "Ok",
        "routes": [{
            "distance": 600 * METERS_PER_MILE,
            "duration": 36000,
            "legs": [{"distance": 600 * METERS_PER_MILE}],
            "geometry": {"type": "LineString", "coordinates": [[-105, 35], [-99, 35], [-96, 35]]},
        }],
        "waypoints": [{"location": [-105, 35]}, {"location": [-96, 35]}],
    }


def response(payload=None, status=200):
    mocked = Mock(status_code=status)
    mocked.json.return_value = provider_payload() if payload is None else payload
    if status >= 400:
        mocked.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    return mocked


@override_settings(ROUTING_MIN_INTERVAL=0)
class RoutingClientTests(TestCase):
    @patch("planner.routing.requests.get")
    def test_full_geometry_single_call_and_persistent_cache(self, get):
        get.return_value = response()
        first = Router()
        result = first.route(POINTS)
        self.assertAlmostEqual(result["distance_miles"], 600)
        self.assertEqual(result["leg_miles"], [600])
        self.assertEqual(first.calls, 1)
        self.assertEqual(get.call_args.kwargs["params"]["overview"], "full")
        self.assertEqual(get.call_args.kwargs["params"]["geometries"], "polyline6")
        second = Router()
        self.assertEqual(second.route(POINTS), result)
        self.assertEqual(second.calls, 0)
        self.assertEqual(second.cache_hits, 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(RouteCache.objects.count(), 1)
        self.assertEqual(second.network_ms, 0)

    @patch("planner.routing.time.perf_counter", side_effect=[10.0, 10.125])
    @patch("planner.routing.requests.get")
    def test_compact_provider_geometry_is_decoded_to_geojson_and_timed(self, get, clock):
        data = provider_payload()
        expected = data['routes'][0]['geometry']
        data['routes'][0]['geometry'] = encode_test_geometry(expected['coordinates'])
        get.return_value = response(data)
        router = Router()
        result = router.route(POINTS)
        self.assertEqual(result['geometry'], expected)
        self.assertEqual(router.network_ms, 125)

    @patch("planner.routing.time.perf_counter", side_effect=[10.0, 10.075])
    @patch("planner.routing.requests.get", side_effect=requests.Timeout("upstream timeout"))
    def test_failed_provider_request_is_included_in_network_timing(self, get, clock):
        router = Router()
        with self.assertRaises(APIError):
            router.route(POINTS)
        self.assertAlmostEqual(router.network_ms, 75)

    @patch("planner.routing.requests.get")
    def test_malformed_encoded_geometry_becomes_service_unavailable(self, get):
        data = provider_payload()
        data['routes'][0]['geometry'] = '????_'
        get.return_value = response(data)
        with self.assertRaises(APIError) as error:
            Router().route(POINTS)
        self.assertEqual(error.exception.status, 503)

    @patch("planner.routing.requests.get")
    def test_all_waypoints_are_sent_in_one_route_request(self, get):
        data = provider_payload()
        data["routes"][0]["legs"] = [{"distance": 400 * METERS_PER_MILE}, {"distance": 200 * METERS_PER_MILE}]
        get.return_value = response(data)
        middle = {"latitude": 35.0, "longitude": -99.0}
        result = Router().route([POINTS[0], middle, POINTS[1]])
        self.assertEqual(result["leg_miles"], [400, 200])
        self.assertIn("-105.000000,35.000000;-99.000000,35.000000;-96.000000,35.000000", get.call_args.args[0])
        self.assertEqual(get.call_count, 1)

    @patch("planner.routing.requests.get", side_effect=requests.Timeout("upstream timeout"))
    def test_provider_timeout_becomes_503_and_is_not_cached(self, get):
        with self.assertRaises(APIError) as error:
            Router().route(POINTS)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(error.exception.code, "routing_unavailable")
        self.assertFalse(RouteCache.objects.exists())

    @patch("planner.routing.requests.get")
    def test_rate_limit_becomes_service_unavailable(self, get):
        get.return_value = response({}, status=429)
        with self.assertRaises(APIError) as error:
            Router().route(POINTS)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(error.exception.code, "provider_rate_limited")

    @patch("planner.routing.requests.get")
    def test_no_route_is_distinguished_from_provider_outage(self, get):
        for code in ("NoRoute", "NoSegment"):
            get.return_value = response({"code": code}, status=400)
            with self.subTest(code=code), self.assertRaises(APIError) as error:
                Router().route(POINTS)
            self.assertEqual(error.exception.status, 422)

    @patch("planner.routing.requests.get")
    def test_malformed_response_does_not_escape_as_internal_server_error(self, get):
        valid = provider_payload()
        malformed = [[], {}, {"code": "Ok", "routes": []}]
        bad_legs = copy.deepcopy(valid)
        bad_legs["routes"][0]["legs"] = []
        malformed.append(bad_legs)
        inconsistent = copy.deepcopy(valid)
        inconsistent["routes"][0]["legs"][0]["distance"] = 1
        malformed.append(inconsistent)
        nonfinite = copy.deepcopy(valid)
        nonfinite["routes"][0]["distance"] = float("nan")
        malformed.append(nonfinite)
        bad_coordinates = copy.deepcopy(valid)
        bad_coordinates["routes"][0]["geometry"]["coordinates"] = [[-105, 35], [None, 35]]
        malformed.append(bad_coordinates)
        out_of_range = copy.deepcopy(valid)
        out_of_range["routes"][0]["geometry"]["coordinates"][1] = [-200, 35]
        malformed.append(out_of_range)
        wrong_geometry_type = copy.deepcopy(valid)
        wrong_geometry_type["routes"][0]["geometry"]["type"] = "Polygon"
        malformed.append(wrong_geometry_type)
        boolean_distance = copy.deepcopy(valid)
        boolean_distance["routes"][0]["distance"] = True
        malformed.append(boolean_distance)
        for data in malformed:
            get.return_value = response(data)
            with self.subTest(payload=data), self.assertRaises(APIError) as error:
                Router().route(POINTS)
            self.assertEqual(error.exception.status, 503)
        self.assertFalse(RouteCache.objects.exists())

    @patch("planner.routing.requests.get")
    def test_invalid_json_is_service_unavailable(self, get):
        get.return_value = response()
        get.return_value.json.side_effect = ValueError("not JSON")
        with self.assertRaises(APIError) as error:
            Router().route(POINTS)
        self.assertEqual(error.exception.status, 503)

    @patch("planner.routing.requests.get")
    def test_waypoint_limit_is_checked_without_network_call(self, get):
        with self.assertRaises(APIError) as error:
            Router().route([POINTS[0]] * 81)
        self.assertEqual(error.exception.status, 422)
        get.assert_not_called()
