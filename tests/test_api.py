import json
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from planner.errors import APIError
from planner.models import Dataset, Station


START = {"latitude": 35.0, "longitude": -105.0}
FINISH = {"latitude": 35.0, "longitude": -96.0}


def route_result(distance=600, legs=None):
    return {
        "geometry": {"type": "LineString", "coordinates": [[-105, 35], [-99, 35], [-96, 35]]},
        "distance_miles": distance,
        "duration_seconds": 36000,
        "leg_miles": [distance] if legs is None else legs,
        "waypoints": [],
    }


class RouteAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Dataset.objects.create(key="fuel", fingerprint="test-dataset-v1", summary={"total_stations": 1, "geocoded_stations": 1})
        Station.objects.create(
            opis_id=101, name="Test truck stop", address="100 Main St", city="Testville", state="OK",
            price=Decimal("3.00"), latitude=35.0, longitude=-99.0,
            coordinate_source="test survey", coordinate_precision="address",
        )

    def post(self, payload=None, **kwargs):
        if payload is None:
            payload = {"start": START, "finish": FINISH}
        return self.client.post("/api/routes/", data=json.dumps(payload), content_type="application/json", **kwargs)

    @staticmethod
    def verified_route(router, points):
        router.calls += 1
        return route_result() if len(points) == 2 else route_result(620, [410, 210])

    @patch("planner.service.Router.route", autospec=True, side_effect=verified_route)
    def test_route_uses_actual_driving_legs_for_fuel_and_returns_saved_map(self, route):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(len(result["fuel_stops"]), 1)
        self.assertAlmostEqual(result["fuel_stops"][0]["gallons"], 12)
        self.assertEqual(Decimal(result["summary"]["total_cost_usd"]), Decimal("36"))
        self.assertEqual(result["summary"]["total_distance_miles"], 620)
        self.assertLessEqual(route.call_count, 3)
        self.assertGreaterEqual(route.call_count, 2)
        detail = self.client.get(f'/api/routes/{result["id"]}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], result["id"])
        self.assertIn("map_url", result)

    @patch("planner.service.Router.route", autospec=True, side_effect=verified_route)
    def test_repeated_request_uses_saved_result_without_routing_calls(self, route):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        call_count = route.call_count
        second = self.post()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(route.call_count, call_count)
        self.assertEqual(first.json()["id"], second.json()["id"])

    @patch("planner.service.Router.route", autospec=True, side_effect=verified_route)
    def test_dataset_change_invalidates_computed_plan_cache(self, route):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        call_count = route.call_count
        Dataset.objects.filter(key="fuel").update(fingerprint="test-dataset-v2")
        Station.objects.filter(opis_id=101).update(price=Decimal("4.00"))
        second = self.post()
        self.assertEqual(second.status_code, 200, second.content)
        self.assertGreater(route.call_count, call_count)
        self.assertEqual(Decimal(second.json()["summary"]["total_cost_usd"]), Decimal("48"))

    @patch("planner.service.Router.route", autospec=True)
    def test_detour_that_exceeds_range_is_rejected(self, route):
        def detour(router, points):
            router.calls += 1
            return route_result() if len(points) == 2 else route_result(611, [501, 110])
        route.side_effect = detour
        response = self.post()
        self.assertEqual(response.status_code, 422, response.content)
        self.assertLessEqual(route.call_count, 3)

    @patch("planner.service.Router.route", autospec=True)
    def test_short_trip_needs_only_one_call_and_no_fuel_purchase(self, route):
        def short_route(router, points):
            router.calls += 1
            return route_result(200)
        route.side_effect = short_route
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["fuel_stops"], [])
        self.assertEqual(Decimal(response.json()["summary"]["total_cost_usd"]), 0)
        self.assertEqual(route.call_count, 1)

    @patch("planner.service.Router.route", autospec=True, side_effect=APIError("routing_unavailable", "Unavailable", 503))
    def test_provider_failure_is_clear_503(self, route):
        self.assertEqual(self.post().status_code, 503)

    @patch("planner.service.Router.route", autospec=True)
    def test_invalid_requests_never_call_router(self, route):
        invalid = [
            {}, [], {"start": START},
            {"start": START, "finish": FINISH, "unexpected": True},
            {"start": "Toronto, ON", "finish": FINISH},
            {"start": {"latitude": 43.6532, "longitude": -79.3832}, "finish": FINISH},
            {"start": {"latitude": float("nan"), "longitude": -105}, "finish": FINISH},
            {"start": {"latitude": True, "longitude": -105}, "finish": FINISH},
            {"start": {"latitude": 10**400, "longitude": -105}, "finish": FINISH},
            {"start": {"latitude": 35, "longitude": -105, "country": "US"}, "finish": FINISH},
            *[{"start": START, "finish": FINISH, "initial_fuel_gallons": value} for value in [-1, 51, True, "50", None, float("inf"), 10**400]],
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400, response.content)
        route.assert_not_called()

    @patch("planner.service.Router.route", autospec=True)
    def test_bad_json_and_wrong_content_type_are_rejected(self, route):
        response = self.client.post("/api/routes/", data="{not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/routes/", data="start=Denver", content_type="text/plain")
        self.assertEqual(response.status_code, 415)
        route.assert_not_called()

    def test_saved_trip_not_found(self):
        self.assertEqual(self.client.get(f"/api/routes/{uuid.uuid4()}/").status_code, 404)

    def test_health_and_schema_are_available(self):
        self.assertEqual(self.client.get("/api/health/").status_code, 200)
        schema = self.client.get("/api/schema/")
        self.assertEqual(schema.status_code, 200)
        self.assertIn("/api/routes/", schema.json()["paths"])
