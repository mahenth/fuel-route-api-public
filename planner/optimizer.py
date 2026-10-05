"""Minimum-cost fuel purchases on an already chosen, ordered route.

The model allows fractional gallons, constant mileage, and fuel available at
each candidate's projected route position. It does not optimize the driving
route or include unmeasured station-access detours. Initial fuel is a sunk cost.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class InfeasibleRoute(ValueError):
    """The vehicle cannot reach the next available fuel location."""

    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def _decimal(value, label, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label} must be a finite number.") from None
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    if positive and result <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    if nonnegative and result < 0:
        raise ValueError(f"{label} must be zero or greater.")
    return result


def _quantity(value):
    return float(round(value, 6))


def _money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def optimize_fuel(
    total_miles, stations, initial_gallons=50, mpg=10, tank_gallons=50
):
    """Return an exact greedy purchase plan for this fixed route model.

    At every station, carry just enough to reach the first cheaper station
    within range. If there is none, fill up only as far as the destination or
    tank capacity requires. A monotone stack finds the next cheaper station in
    O(n); sorting candidates costs O(n log n). Passing a station requires no
    purchase and is not returned as a stop.

    Monetary values are JSON-safe decimal strings; measured quantities are
    floats. Each purchase is rounded to cents, then charges are summed. Gallon
    quantities are rounded only when serializing, never during optimization.
    """
    total = _decimal(total_miles, "total_miles", nonnegative=True)
    efficiency = _decimal(mpg, "mpg", positive=True)
    capacity = _decimal(tank_gallons, "tank_gallons", positive=True)
    initial = _decimal(initial_gallons, "initial_gallons", nonnegative=True)
    if initial > capacity:
        raise ValueError("initial_gallons cannot exceed tank_gallons.")
    if not isinstance(stations, (list, tuple)):
        raise ValueError("stations must be a list of station objects.")

    candidates = []
    for index, station in enumerate(stations):
        if not isinstance(station, dict):
            raise ValueError(f"stations[{index}] must be an object.")
        position = _decimal(station.get("mile"), f"stations[{index}].mile", nonnegative=True)
        price = _decimal(station.get("price"), f"stations[{index}].price", nonnegative=True)
        if position > total:
            raise ValueError(f"stations[{index}].mile cannot exceed total_miles.")
        # Buying at the finish can never improve a trip's cost.
        if position < total:
            candidates.append((position, price, station))
    candidates.sort(key=lambda item: (item[0], item[1]))

    # Stations at exactly the same modeled location are interchangeable for
    # range purposes, so retain the least expensive one.
    unique = []
    for candidate in candidates:
        if not unique or candidate[0] != unique[-1][0]:
            unique.append(candidate)
    candidates = unique
    count = len(candidates)
    next_cheaper = [None] * count
    stack = []
    for index in range(count - 1, -1, -1):
        while stack and candidates[stack[-1]][1] >= candidates[index][1]:
            stack.pop()
        next_cheaper[index] = stack[-1] if stack else None
        stack.append(index)

    fuel = initial
    previous_mile = Decimal(0)
    previous_stop_mile = Decimal(0)
    purchased = Decimal(0)
    total_charge = Decimal(0)
    stops = []
    maximum_range = capacity * efficiency
    # Decimal division can introduce a final digit of harmless roundoff.
    tolerance = Decimal("1e-18")

    for index in range(count + 1):
        is_destination = index == count
        position = total if is_destination else candidates[index][0]
        leg_miles = position - previous_mile
        required = leg_miles / efficiency
        if required - fuel > tolerance:
            available_range = fuel * efficiency
            details = {
                "from_mile": _quantity(previous_mile),
                "to_mile": _quantity(position),
                "gap_miles": _quantity(leg_miles),
                "available_range_miles": _quantity(available_range),
                "maximum_range_miles": _quantity(maximum_range),
                "shortfall_miles": _quantity(leg_miles - available_range),
            }
            raise InfeasibleRoute(
                f"Cannot cover the {float(leg_miles):.2f}-mile gap from route "
                f"mile {float(previous_mile):.2f} to {float(position):.2f}; "
                f"available fuel covers {float(available_range):.2f} miles. "
                "No usable fuel station is available in this gap.",
                details,
            )
        fuel = max(Decimal(0), fuel - required)
        previous_mile = position
        if is_destination:
            break

        _, price, station = candidates[index]
        target = min(capacity, (total - position) / efficiency)
        cheaper_index = next_cheaper[index]
        if cheaper_index is not None:
            cheaper_distance = candidates[cheaper_index][0] - position
            if cheaper_distance <= maximum_range:
                target = cheaper_distance / efficiency
        gallons = max(Decimal(0), target - fuel)
        if gallons <= tolerance:
            continue

        charge = _money(gallons * price)
        stop = dict(station)
        stop.update(
            price=format(price, "f"),
            price_per_gallon=format(price, "f"),
            mile=_quantity(position),
            gallons=_quantity(gallons),
            cost_usd=format(charge, ".2f"),
            arrival_gallons=_quantity(fuel),
            departure_gallons=_quantity(fuel + gallons),
            distance_since_previous_stop_miles=_quantity(position - previous_stop_mile),
        )
        stops.append(stop)
        fuel += gallons
        purchased += gallons
        total_charge += charge
        previous_stop_mile = position

    return {
        "fuel_stops": stops,
        "total_cost_usd": format(total_charge, ".2f"),
        "gallons_purchased": _quantity(purchased),
        "gallons_consumed": _quantity(total / efficiency),
        "initial_gallons": _quantity(initial),
        "arrival_gallons": _quantity(fuel),
        "total_distance_miles": _quantity(total),
        "mpg": _quantity(efficiency),
        "tank_capacity_gallons": _quantity(capacity),
        "maximum_range_miles": _quantity(maximum_range),
        "cost_assumption": (
            "Total cost includes fuel bought during this trip only. Initial "
            "tank fuel is already owned and its historical cost is excluded."
        ),
    }
