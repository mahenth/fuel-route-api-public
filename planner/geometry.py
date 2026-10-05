"""Match fuel stations to a route without additional routing-service calls.

Stations within the corridor are projected onto the route. Their road access
and detour distances are unknown: lateral_miles is a straight-line distance,
not a drivable detour. The optimizer consumes these projected mile positions.
"""

import math

from shapely.geometry import LineString, box
from shapely.strtree import STRtree


EARTH_RADIUS_MILES = 3958.7613


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label} must be a finite number.") from None
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number.")
    return result


def _coordinate(value, label):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError(f"{label} must be a [longitude, latitude] pair.")
    lon = _finite(value[0], f"{label} longitude")
    lat = _finite(value[1], f"{label} latitude")
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise ValueError(f"{label} contains out-of-range coordinates.")
    return lon, lat


def _angle(lon1, lat1, lon2, lat2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlon = math.radians(lon2 - lon1)
    hav = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(min(1, max(0, hav))))


def haversine_miles(lon1, lat1, lon2, lat2):
    """Great-circle distance in miles between two longitude/latitude points."""
    return EARTH_RADIUS_MILES * _angle(lon1, lat1, lon2, lat2)


def _bearing(lon1, lat1, lon2, lat2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta = math.radians(lon2 - lon1)
    return math.atan2(
        math.sin(delta) * math.cos(phi2),
        math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta),
    )


def _project(lon, lat, start, end, length):
    """Return along-segment and lateral miles on a great-circle segment."""
    delta13 = _angle(*start, lon, lat)
    bearing_delta = _bearing(*start, lon, lat) - _bearing(*start, *end)
    along_angle = math.atan2(
        math.sin(delta13) * math.cos(bearing_delta), math.cos(delta13)
    )
    along = along_angle * EARTH_RADIUS_MILES
    if along <= 0:
        return 0.0, delta13 * EARTH_RADIUS_MILES
    if along >= length:
        return length, haversine_miles(*end, lon, lat)
    cross = math.asin(max(-1, min(1, math.sin(delta13) * math.sin(bearing_delta))))
    return along, abs(cross) * EARTH_RADIUS_MILES


def _subsegments(start, end, length):
    """Densify long great-circle arcs for reliable spatial bounding boxes."""
    count = max(1, math.ceil(length / 10))
    if count == 1:
        yield start, end, length
        return
    angular = length / EARTH_RADIUS_MILES
    sine = math.sin(angular)
    if abs(sine) < 1e-12:
        raise ValueError("Route geometry contains an ambiguous antipodal segment.")
    lon1, lat1, lon2, lat2 = map(math.radians, (*start, *end))
    first = (math.cos(lat1) * math.cos(lon1), math.cos(lat1) * math.sin(lon1), math.sin(lat1))
    last = (math.cos(lat2) * math.cos(lon2), math.cos(lat2) * math.sin(lon2), math.sin(lat2))
    previous = start
    for i in range(1, count + 1):
        if i == count:
            point = end
        else:
            fraction = i / count
            a = math.sin((1 - fraction) * angular) / sine
            b = math.sin(fraction * angular) / sine
            x, y, z = (a * first[j] + b * last[j] for j in range(3))
            point = (math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y))))
        yield previous, point, length / count
        previous = point


def project_stations(route_coordinates, distance_miles, stations, corridor_miles=2):
    """Return route-ordered station candidates using a spatial index.

    ``route_coordinates`` uses GeoJSON [longitude, latitude] order. Input
    stations are dictionaries with longitude and latitude. Provider-reported
    route miles scale the cumulative great-circle polyline length, so the
    finish always agrees with the route's advertised length. Use the provider's
    full route geometry: simplified geometry can miss roadside stations.

    The same physical station can be returned twice if the route passes it on
    separate, non-adjacent stretches. No input dictionaries are mutated.
    """
    distance = _finite(distance_miles, "distance_miles")
    corridor = _finite(corridor_miles, "corridor_miles")
    if distance < 0:
        raise ValueError("distance_miles must be zero or greater.")
    if corridor <= 0:
        raise ValueError("corridor_miles must be greater than zero.")
    if not isinstance(route_coordinates, (list, tuple)) or len(route_coordinates) < 2:
        raise ValueError("route_coordinates must contain at least two coordinate pairs.")
    coordinates = [_coordinate(point, f"route_coordinates[{i}]") for i, point in enumerate(route_coordinates)]
    if not isinstance(stations, (list, tuple)):
        raise ValueError("stations must be a list of station objects.")

    segments = []
    segment_data = []
    cumulative = 0.0
    # Index short subsegments so their rectangular bounds follow curved arcs.
    # Unwrap antimeridian segments; mirrored queries handle Aleutian stations.
    for start, end in zip(coordinates, coordinates[1:]):
        length = haversine_miles(*start, *end)
        if length <= 1e-10:
            continue
        for part_start, part_end, part_length in _subsegments(start, end, length):
            end_lon = part_start[0] + ((part_end[0] - part_start[0] + 180) % 360 - 180)
            segments.append(LineString([part_start, (end_lon, part_end[1])]))
            segment_data.append((part_start, part_end, part_length, cumulative))
            cumulative += part_length
    if cumulative == 0:
        if distance == 0:
            return []
        raise ValueError("A nonzero route distance requires nonzero route geometry.")
    if distance == 0:
        return []
    tree = STRtree(segments)
    scaling = distance / cumulative
    matches = []
    # Extra padding admits slight arc bowing between indexed segment endpoints;
    # exact spherical distance below still enforces the requested corridor.
    lat_margin = math.degrees((corridor + 0.1) / EARTH_RADIUS_MILES)
    for station_index, station in enumerate(stations):
        if not isinstance(station, dict):
            raise ValueError(f"stations[{station_index}] must be an object.")
        lon, lat = _coordinate(
            [station.get("longitude"), station.get("latitude")],
            f"stations[{station_index}]",
        )
        lon_margin = min(180, lat_margin / max(0.001, math.cos(math.radians(abs(lat) + lat_margin))))
        indices = set()
        for shift in (0, -360, 360):
            indices.update(int(i) for i in tree.query(box(
                lon + shift - lon_margin, lat - lat_margin,
                lon + shift + lon_margin, lat + lat_margin,
            )))
        projections = []
        for segment_index in sorted(indices):
            start, end, length, offset = segment_data[segment_index]
            along, lateral = _project(lon, lat, start, end, length)
            if lateral <= corridor + 1e-9:
                projections.append((segment_index, lateral, (offset + along) * scaling))
        groups = []
        for projection in projections:
            if not groups or projection[0] > groups[-1][-1][0] + 1:
                groups.append([])
            groups[-1].append(projection)
        for group in groups:
            _, lateral, mile = min(group, key=lambda item: (item[1], item[2]))
            candidate = dict(station)
            candidate.update(
                latitude=lat,
                longitude=lon,
                mile=min(distance, max(0.0, mile)),
                lateral_miles=round(lateral, 6),
            )
            matches.append(candidate)
    return sorted(matches, key=lambda station: station["mile"])
