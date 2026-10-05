"""Plan locally, then verify real driving legs through selected fuel stations."""
import math
import time
import uuid
from datetime import timedelta
from functools import lru_cache

import geonamescache

from django.conf import settings
from django.utils import timezone

from .errors import APIError
from .geometry import project_stations
from .locations import resolve_location
from .models import Dataset, Station, Trip
from .optimizer import InfeasibleRoute, optimize_fuel
from .routing import Router, fingerprint


@lru_cache(maxsize=2)
def station_catalog(dataset_fingerprint):
    return [
        {'id': row.opis_id, 'name': row.name, 'address': row.address, 'city': row.city,
         'state': row.state, 'price': str(row.price), 'latitude': row.latitude,
         'longitude': row.longitude, 'coordinate_source': row.coordinate_source,
         'coordinate_precision': row.coordinate_precision}
        for row in Station.objects.filter(latitude__isnull=False, longitude__isnull=False,
                                          state__in=geonamescache.GeonamesCache().get_us_states())
    ]


def validate_request(payload):
    if not isinstance(payload, dict):
        raise APIError('invalid_request', 'The request must be a JSON object.')
    unknown = set(payload) - {'start', 'finish', 'initial_fuel_gallons'}
    if unknown:
        raise APIError('invalid_request', 'Unknown request fields.', details={'fields': sorted(unknown)})
    if 'start' not in payload or 'finish' not in payload:
        raise APIError('invalid_request', 'Both start and finish are required.')
    initial = payload.get('initial_fuel_gallons', 50)
    if isinstance(initial, bool) or not isinstance(initial, (int, float)) or not 0 <= initial <= 50 or not math.isfinite(initial):
        raise APIError('invalid_fuel', 'initial_fuel_gallons must be a number between 0 and 50.')
    return resolve_location(payload['start'], 'start'), resolve_location(payload['finish'], 'finish'), float(initial)


def actual_candidates(stops, route):
    mile = 0.0
    result = []
    for stop, leg in zip(stops, route['leg_miles']):
        mile += leg
        item = dict(stop)
        item['projected_mile'] = item.get('projected_mile', item['mile'])
        item['mile'] = min(mile, route['distance_miles'])
        result.append(item)
    return result


def repair_stops(stops, route, candidates, initial):
    """Add an intermediate candidate in each actual leg exceeding its fuel range.

    Only one repair is attempted, followed by one final provider call. If that
    still cannot be driven safely, report an infeasible route rather than return
    an invalid plan. Selection uses base-route station order; verification uses
    real road distances, including access roads and turnarounds.
    """
    result = []
    previous_projected = 0.0
    chosen = {s['id'] for s in stops}
    for index, leg in enumerate(route['leg_miles']):
        limit = initial * 10 if index == 0 else 500
        next_projected = stops[index]['mile'] if index < len(stops) else math.inf
        if leg > limit + 1e-6:
            upper = previous_projected + max(0, limit - 15)
            eligible = [s for s in candidates if s['id'] not in chosen and
                        previous_projected < s['mile'] < min(next_projected, upper)]
            if not eligible:
                raise InfeasibleRoute('No reachable verified station can bridge this driving leg.',
                                      {'gap_miles': leg, 'available_range_miles': limit})
            # Prefer progress; this repair restores feasibility, it is not an
            # exhaustive search over alternative driving routes.
            bridge = max(eligible, key=lambda s: s['mile'])
            result.append(bridge)
            chosen.add(bridge['id'])
        if index < len(stops):
            result.append(stops[index])
            previous_projected = stops[index]['mile']
    return result


def plan_trip(payload):
    started = time.perf_counter()
    start, finish, initial = validate_request(payload)
    dataset = Dataset.objects.filter(key='fuel').first()
    if dataset is None:
        raise APIError('data_not_ready', 'Fuel data has not been imported. Run python manage.py import_fuel.', 503)
    trip_key = fingerprint({'start': [start['latitude'], start['longitude']],
                            'finish': [finish['latitude'], finish['longitude']], 'fuel': initial,
                            'dataset': dataset.fingerprint, 'provider': settings.OSRM_BASE_URL,
                            'corridor': settings.STATION_CORRIDOR_MILES, 'planner_version': 3})
    now = timezone.now()
    existing = Trip.objects.filter(key=trip_key, expires_at__gt=now).first()
    if existing:
        result = dict(existing.payload)
        result['metrics'] = {'routing_calls': 0, 'route_cache_hits': 0, 'provider_ms': 0, 'cache_hit': True,
                             'processing_ms': round((time.perf_counter() - started) * 1000, 2)}
        return result
    router = Router()
    identical = (start['latitude'], start['longitude']) == (finish['latitude'], finish['longitude'])
    if identical:
        coords = [[start['longitude'], start['latitude']]] * 2
        base = {'distance_miles': 0.0, 'duration_seconds': 0.0,
                'geometry': {'type': 'LineString', 'coordinates': coords}, 'leg_miles': [0]}
    else:
        base = router.route([start, finish])
    route = base
    candidates = []
    waypoints = []
    try:
        if base['distance_miles'] <= initial * 10:
            plan = optimize_fuel(base['distance_miles'], [], initial_gallons=initial)
        else:
            catalog = station_catalog(dataset.fingerprint)
            if not catalog:
                raise APIError('station_coordinates_missing', 'No verified station coordinates are loaded.', 503)
            candidates = project_stations(base['geometry']['coordinates'], base['distance_miles'], catalog,
                                          corridor_miles=settings.STATION_CORRIDOR_MILES)
            preliminary = optimize_fuel(base['distance_miles'], candidates, initial_gallons=initial)
            selected = preliminary['fuel_stops']
            route = router.route([start, *selected, finish])
            verified = actual_candidates(selected, route)
            try:
                plan = optimize_fuel(route['distance_miles'], verified, initial_gallons=initial)
            except InfeasibleRoute:
                selected = repair_stops(selected, route, candidates, initial)
                route = router.route([start, *selected, finish])
                verified = actual_candidates(selected, route)
                plan = optimize_fuel(route['distance_miles'], verified, initial_gallons=initial)
            waypoints = verified
    except InfeasibleRoute as exc:
        raise APIError('insufficient_station_coverage',
                       'A safe fuel plan could not be found with the verified stations in this corridor. '
                       'The vehicle must never travel beyond its available fuel range.', 422,
                       {**exc.details, 'dataset': dataset.summary, 'routing_calls': router.calls}) from exc

    for number, stop in enumerate(plan['fuel_stops'], 1):
        stop['stop_number'] = number
    warnings = [
        'Prices are from the supplied CSV snapshot; they are not live pump prices.',
        'Only stations with verified site coordinates are eligible. Unlocated CSV stations are excluded.',
        'Cost is minimized over the selected driving itinerary; alternative highways and all possible station detours are not exhaustively searched.',
    ]
    if waypoints and len(waypoints) > len(plan['fuel_stops']):
        warnings.append('The verified route includes pass-through station waypoints where no purchase is needed.')
    summary = {key: value for key, value in plan.items() if key != 'fuel_stops'}
    summary['stop_count'] = len(plan['fuel_stops'])
    summary['base_route_miles'] = round(base['distance_miles'], 3)
    summary['additional_driving_miles'] = round(route['distance_miles'] - base['distance_miles'], 3)
    trip_id = existing.id if existing else uuid.uuid4()
    # Reuse the UUID of an expired entry so its persisted map keeps working.
    expired = Trip.objects.filter(key=trip_key).first()
    if expired:
        trip_id = expired.id
    result = {
        'id': str(trip_id), 'map_url': f'/maps/{trip_id}/', 'start': start, 'finish': finish,
        'route': route, 'fuel_stops': plan['fuel_stops'], 'visited_stations': waypoints,
        'summary': summary, 'dataset': dataset.summary, 'candidate_station_count': len(candidates),
        'assumptions': [f'Start with {initial:g} gallons already in the tank; its historical cost is excluded.',
                        '10 miles per US gallon; 50-gallon capacity; 500-mile maximum range; no reserve.',
                        'USD per US gallon. Fractional purchases allowed; each purchase is rounded to cents.',
                        'City inputs resolve offline to city centers. Supply coordinates for precise endpoints.',
                        'Driving distances include the selected fuel-station detours. Standard car routing; no truck clearance constraints.'],
        'warnings': warnings,
        'metrics': {'routing_calls': router.calls, 'route_cache_hits': router.cache_hits,
                    'provider_ms': round(getattr(router, 'network_ms', 0), 2), 'cache_hit': False,
                    'processing_ms': round((time.perf_counter() - started) * 1000, 2)},
        'attribution': {'routing': 'OSRM', 'map_data': 'OpenStreetMap contributors (ODbL)',
                        'cities': 'GeoNames (CC BY 4.0)', 'boundaries': 'US Census Bureau'},
    }
    Trip.objects.update_or_create(key=trip_key, defaults={'id': trip_id, 'payload': result,
                                   'expires_at': now + timedelta(seconds=settings.ROUTING_CACHE_SECONDS)})
    return result
