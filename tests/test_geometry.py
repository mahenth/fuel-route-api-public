import math
import unittest

from planner.geometry import haversine_miles, project_stations


def station(lon, lat, identifier=1):
    return {"id": identifier, "name": "Fuel", "price": "3.20", "longitude": lon, "latitude": lat}


class RouteGeometryTests(unittest.TestCase):
    def test_haversine_known_equator_degree(self):
        self.assertAlmostEqual(haversine_miles(0, 0, 1, 0), 69.0934, places=3)

    def test_project_and_scale_to_provider_distance(self):
        result = project_stations([[-100, 0], [-98, 0]], 200, [station(-99, 0)])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["mile"], 100, places=5)
        self.assertEqual(result[0]["lateral_miles"], 0)

    def test_corridor_excludes_distant_stations(self):
        stations = [station(-99, .01, 1), station(-99, .2, 2)]
        result = project_stations([[-100, 0], [-98, 0]], 200, stations, corridor_miles=2)
        self.assertEqual([s["id"] for s in result], [1])
        self.assertAlmostEqual(result[0]["lateral_miles"], .690934, places=5)
        self.assertNotIn("mile", stations[0])

    def test_joins_do_not_duplicate_a_station(self):
        result = project_stations([[-100, 0], [-99, 0], [-98, 0]], 200, [station(-99, 0)])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["mile"], 100, places=5)

    def test_candidates_return_in_driving_order(self):
        result = project_stations([[-100, 0], [-98, 0]], 200, [station(-98.5, 0, 2), station(-99.5, 0, 1)])
        self.assertEqual([s["id"] for s in result], [1, 2])

    def test_route_revisiting_station_returns_separate_opportunities(self):
        result = project_stations([[-100, 0], [-99, 0], [-98, 0], [-98, 1], [-99, 1], [-99, 0], [-100, 0]], 700, [station(-99.5, 0)])
        self.assertEqual(len(result), 2)
        self.assertLess(result[0]["mile"], result[1]["mile"])

    def test_antimeridian_projection(self):
        result = project_stations([[179, 0], [-179, 0]], 200, [station(-179.5, 0)])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["mile"], 150, places=5)

    def test_long_high_latitude_arc_is_indexed_beyond_endpoint_latitude(self):
        start, end = (-130, 60), (-110, 60)
        # For symmetric longitudes, the great-circle midpoint is poleward.
        midpoint = (-120, math.degrees(math.atan(math.tan(math.radians(60)) / math.cos(math.radians(10)))))
        self.assertGreater(midpoint[1], 60.1)
        result = project_stations([start, end], 1000, [station(*midpoint)], corridor_miles=.1)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["lateral_miles"], 0, places=5)

    def test_repeated_vertices_and_zero_distance(self):
        result = project_stations([[-100, 0], [-100, 0], [-98, 0]], 200, [station(-99, 0)])
        self.assertEqual(len(result), 1)
        self.assertEqual(project_stations([[-100, 0], [-100, 0]], 0, []), [])

    def test_invalid_geometry_and_station_inputs(self):
        for coordinates, distance, stations in [([], 200, []), ([[0, 0], [0, 0]], 200, []), ([[0, 0], [1, 0]], -1, []), ([[0, 0], [181, 0]], 200, []), ([[0, 0], [1, 0]], 200, [station(float("nan"), 0)])]:
            with self.subTest(coordinates=coordinates), self.assertRaises(ValueError):
                project_stations(coordinates, distance, stations)


if __name__ == "__main__":
    unittest.main()
