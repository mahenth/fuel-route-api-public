"""A cached OSRM client with one application-wide request gate stored in SQLite."""
import hashlib
import json
import math
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .errors import APIError
from .models import RouteCache, ServiceGate

METERS_PER_MILE = 1609.344
MAX_ROUTE_POINTS = 250_000


def decode_polyline6(encoded):
    """Decode signed, delta-encoded OSRM polyline6 into GeoJSON coordinates.

    Bounds prevent malformed upstream data from creating unbounded integers or
    arbitrarily large point lists. Precision is one millionth of a degree;
    coordinate order changes from encoded latitude/longitude to GeoJSON's
    longitude/latitude convention.
    """
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_ROUTE_POINTS * 12:
        raise ValueError('Invalid or excessively long encoded route geometry')
    coordinates = []
    position = 0
    latitude = longitude = 0
    while position < len(encoded):
        deltas = []
        for _ in range(2):
            value = 0
            for group in range(6):
                if position >= len(encoded):
                    raise ValueError('Truncated encoded route coordinate')
                code = ord(encoded[position]) - 63
                position += 1
                if not 0 <= code <= 63:
                    raise ValueError('Invalid encoded route character')
                value |= (code & 31) << (5 * group)
                if not code & 32:
                    if group and code == 0:
                        raise ValueError('Noncanonical encoded route coordinate')
                    break
            else:
                raise ValueError('Encoded route coordinate exceeds supported bounds')
            deltas.append(~(value >> 1) if value & 1 else value >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        if not -90_000_000 <= latitude <= 90_000_000 or not -180_000_000 <= longitude <= 180_000_000:
            raise ValueError('Decoded route coordinate is outside geographic bounds')
        if len(coordinates) >= MAX_ROUTE_POINTS:
            raise ValueError('Encoded route contains too many coordinates')
        coordinates.append([longitude / 1_000_000, latitude / 1_000_000])
    if len(coordinates) < 2:
        raise ValueError('Encoded route must contain at least two coordinates')
    return coordinates


def normalize_geometry(value):
    if isinstance(value, str):
        return {'type': 'LineString', 'coordinates': decode_polyline6(value)}
    # Some compatible routing deployments return GeoJSON despite the requested
    # encoding. It receives the same geographic/shape checks below.
    if isinstance(value, dict):
        return value
    raise ValueError('Unexpected routing geometry format')


def provider_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Routing measurements must be numeric')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError('Routing measurements must be finite and nonnegative') from None
    if not math.isfinite(number) or number < 0:
        raise ValueError('Routing measurements must be finite and nonnegative')
    return number


def valid_coordinate(point):
    return (
        isinstance(point, (list, tuple)) and len(point) == 2
        and all(not isinstance(n, bool) and isinstance(n, (int, float)) for n in point)
        and -180 <= point[0] <= 180 and -90 <= point[1] <= 90
        and all(math.isfinite(n) for n in point)
    )


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reserve_request_slot():
    # Write first: SQLite serializes this transaction across threads/processes.
    with transaction.atomic():
        ServiceGate.objects.get_or_create(key='osrm')
        ServiceGate.objects.filter(key='osrm').update(key='osrm')
        gate = ServiceGate.objects.get(key='osrm')
        now = time.time()
        scheduled = max(now, gate.available_at)
        if scheduled - now > 5:
            raise APIError('routing_busy', 'Routing is busy. Try again in a few seconds.', 429)
        gate.available_at = scheduled + settings.ROUTING_MIN_INTERVAL
        gate.save(update_fields=['available_at'])
    time.sleep(max(0, scheduled - time.time()))


class Router:
    def __init__(self):
        self.calls = 0
        self.cache_hits = 0
        self.network_ms = 0.0

    def route(self, points):
        if len(points) > 80:
            raise APIError('too_many_stops', 'This route requires too many waypoints for the routing provider.', 422)
        coordinates = ';'.join(f"{p['longitude']:.6f},{p['latitude']:.6f}" for p in points)
        key = fingerprint({'provider': settings.OSRM_BASE_URL, 'coordinates': coordinates, 'version': 3})
        cached = RouteCache.objects.filter(key=key, expires_at__gt=timezone.now()).first()
        if cached:
            self.cache_hits += 1
            return cached.payload
        reserve_request_slot()
        self.calls += 1
        try:
            request_started = time.perf_counter()
            try:
                response = requests.get(
                    f'{settings.OSRM_BASE_URL}/route/v1/driving/{coordinates}',
                    params={'overview': 'full', 'geometries': 'polyline6', 'steps': 'false',
                            'alternatives': 'false', 'generate_hints': 'false',
                            'radiuses': ';'.join('1500' for _ in points)},
                    headers={'User-Agent': settings.ROUTING_USER_AGENT},
                    timeout=(5, settings.ROUTING_TIMEOUT),
                )
            finally:
                # requests.get downloads the response body by default. Include
                # failed requests, but exclude local decoding and DB work.
                self.network_ms += (time.perf_counter() - request_started) * 1000
            if response.status_code == 429:
                raise APIError('provider_rate_limited', 'The routing provider is busy. Please retry later.', 503)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Routing response must be an object')
            if data.get('code') in ('NoRoute', 'NoSegment'):
                raise APIError('no_drivable_route', 'No connected driving route was found between these locations.', 422)
            response.raise_for_status()
            if data.get('code') != 'Ok':
                raise ValueError('Unexpected routing response')
            route = data['routes'][0]
            geometry = normalize_geometry(route['geometry'])
            coords = geometry['coordinates']
            distances = [provider_number(leg['distance']) for leg in route['legs']]
            distance = provider_number(route['distance'])
            duration = provider_number(route['duration'])
            if (geometry['type'] != 'LineString' or
                    not isinstance(coords, list) or not 2 <= len(coords) <= MAX_ROUTE_POINTS or len(distances) != len(points) - 1 or
                    abs(sum(distances) - distance) > max(10, distance * 0.001) or
                    not all(valid_coordinate(point) for point in coords)):
                raise ValueError('Invalid route shape')
            result = {'geometry': geometry, 'distance_miles': distance / METERS_PER_MILE,
                      'duration_seconds': duration, 'leg_miles': [d / METERS_PER_MILE for d in distances],
                      'waypoints': data.get('waypoints', [])}
        except APIError:
            raise
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            raise APIError('routing_unavailable', 'The routing service is temporarily unavailable. Try again later.', 503) from exc
        RouteCache.objects.update_or_create(key=key, defaults={
            'payload': result, 'expires_at': timezone.now() + timedelta(seconds=settings.ROUTING_CACHE_SECONDS),
        })
        return result
