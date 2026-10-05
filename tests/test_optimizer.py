import itertools
import unittest
from decimal import Decimal

from planner.optimizer import InfeasibleRoute, optimize_fuel


def station(mile, price, identifier=None):
    return {"id": identifier if identifier is not None else mile, "name": f"Station {mile}", "mile": mile, "price": str(price)}


class FuelOptimizerTests(unittest.TestCase):
    def test_short_trip_needs_no_purchase(self):
        plan = optimize_fuel(250, [])
        self.assertEqual(plan["fuel_stops"], [])
        self.assertEqual(plan["total_cost_usd"], "0.00")
        self.assertEqual(plan["gallons_consumed"], 25)
        self.assertEqual(plan["arrival_gallons"], 25)

    def test_exact_range_is_reachable(self):
        self.assertEqual(optimize_fuel(500, [])["arrival_gallons"], 0)
        self.assertEqual(optimize_fuel(1000, [station(500, 3)])["total_cost_usd"], "150.00")

    def test_expensive_station_only_buys_enough_for_cheaper_one(self):
        plan = optimize_fuel(1100, [station(450, 5), station(600, 2), station(950, 4)])
        self.assertEqual([stop["mile"] for stop in plan["fuel_stops"]], [450, 600])
        self.assertEqual([stop["gallons"] for stop in plan["fuel_stops"]], [10, 50])
        self.assertEqual(plan["total_cost_usd"], "150.00")
        self.assertEqual(plan["gallons_purchased"], 60)
        self.assertEqual(plan["arrival_gallons"], 0)

    def test_fills_at_cheap_station_before_more_expensive_stops(self):
        plan = optimize_fuel(1250, [station(400, 2), station(800, 5), station(1100, 6)])
        self.assertEqual([stop["gallons"] for stop in plan["fuel_stops"]], [40, 35])
        self.assertEqual(plan["total_cost_usd"], "255.00")

    def test_coincident_candidates_use_cheapest_and_preserve_input(self):
        candidates = [station(500, 9, "high"), station(500, 2, "low")]
        original = [dict(item) for item in candidates]
        result = optimize_fuel(600, candidates)
        self.assertEqual(result["fuel_stops"][0]["id"], "low")
        self.assertEqual(candidates, original)

    def test_partial_initial_tank_and_origin_station(self):
        result = optimize_fuel(100, [station(0, "3.125")], initial_gallons=0)
        self.assertEqual(result["gallons_purchased"], 10)
        self.assertEqual(result["total_cost_usd"], "31.25")

    def test_no_fuel_needed_at_destination(self):
        self.assertEqual(optimize_fuel(500, [station(500, 0)])["fuel_stops"], [])
        self.assertEqual(optimize_fuel(0, [station(0, 3)])["fuel_stops"], [])

    def test_gap_reports_actionable_distance(self):
        with self.assertRaises(InfeasibleRoute) as context:
            optimize_fuel(1200, [station(300, 2), station(900, 3)])
        self.assertEqual(context.exception.details["gap_miles"], 600)
        self.assertEqual(context.exception.details["shortfall_miles"], 100)

    def test_initial_tank_can_make_first_station_unreachable(self):
        with self.assertRaises(InfeasibleRoute):
            optimize_fuel(400, [station(250, 2)], initial_gallons=20)

    def test_rejects_invalid_numeric_inputs(self):
        cases = [
            {"total_miles": -1}, {"total_miles": float("nan")},
            {"mpg": 0}, {"tank_gallons": 0}, {"initial_gallons": 51},
            {"initial_gallons": True}, {"initial_gallons": "Infinity"},
            {"stations": [station(200, -1)]}, {"stations": [station(1001, 2)]},
        ]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                optimize_fuel(**({"total_miles": 1000, "stations": []} | override))

    def test_cents_are_rounded_per_purchase(self):
        result = optimize_fuel(3, [station(0, "1.005"), station(2, "2.005")], initial_gallons=0, mpg=1, tank_gallons=2)
        self.assertEqual(result["total_cost_usd"], "4.02")
        self.assertEqual(sum(Decimal(s["cost_usd"]) for s in result["fuel_stops"]), Decimal("4.02"))

    def test_greedy_matches_exhaustive_integer_fuel_dynamic_program(self):
        # Integer positions and consumption have an integer optimal solution.
        # Compare 243 price combinations against an independent exhaustive DP.
        capacity = 3
        positions = [0, 1, 2, 4, 5]
        destination = 7
        for prices in itertools.product((1, 2, 4), repeat=len(positions)):
            states = {1: 0}
            previous = 0
            for position, price in zip(positions, prices):
                arrival = {}
                for fuel, cost in states.items():
                    remaining = fuel - (position - previous)
                    if remaining >= 0:
                        arrival[remaining] = min(cost, arrival.get(remaining, float("inf")))
                states = {}
                for fuel, cost in arrival.items():
                    for buy in range(capacity - fuel + 1):
                        new_fuel = fuel + buy
                        states[new_fuel] = min(states.get(new_fuel, float("inf")), cost + buy * price)
                previous = position
            expected = min(cost for fuel, cost in states.items() if fuel >= destination - previous)
            result = optimize_fuel(destination, [station(p, c) for p, c in zip(positions, prices)], initial_gallons=1, mpg=1, tank_gallons=capacity)
            self.assertEqual(Decimal(result["total_cost_usd"]), Decimal(expected), prices)


if __name__ == "__main__":
    unittest.main()
