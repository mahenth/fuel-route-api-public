"""Offline city lookup and polygon checks: no geocoder API calls on requests."""
import json
import math
import unicodedata
from bisect import bisect_left, bisect_right
from functools import lru_cache

import geonamescache
from django.conf import settings
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from .errors import APIError


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value) if not unicodedata.combining(c)).strip().casefold()


@lru_cache(maxsize=1)
def city_index():
    gc = geonamescache.GeonamesCache(min_city_population=500)
    states = gc.get_us_states()
    state_names = {normalize(s['name']): code for code, s in states.items()}
    result = {}
    for city in gc.get_cities().values():
        if city['countrycode'] != 'US' or city['admin1code'] not in states:
            continue
        names = [city['name'], *city.get('alternatenames', [])]
        for name in names:
            if not isinstance(name, str):
                continue
            key = (normalize(name), city['admin1code'])
            if key not in result or city['population'] > result[key]['population']:
                result[key] = city
    return result, state_names


@lru_cache(maxsize=1)
def city_suggestion_index():
    """Index the same aliases accepted by routing, built once per process."""
    cities, state_names = city_index()
    entries = sorted(
        ((alias, state, city) for (alias, state), city in cities.items()),
        key=lambda entry: (entry[0], entry[1]),
    )
    return [entry[0] for entry in entries], entries, state_names


def suggest_cities(query, limit=8):
    """Return US city prefixes, ranked by exact match and population.

    Search is entirely local. Prefix bounds avoid scanning the full gazetteer
    on every keystroke, and aliases collapse to selectable canonical labels.
    An optional comma-separated state prefix narrows ambiguous city names.
    """
    if not isinstance(query, str) or len(query) > 200:
        raise APIError('invalid_query', 'City search must be at most 200 characters.')
    parts = [normalize(part) for part in query.split(',')]
    if not parts[0] or len(parts) > 2 or limit <= 0:
        return []
    prefix = parts[0]
    names, entries, state_names = city_suggestion_index()
    states = None
    if len(parts) == 2 and parts[1]:
        state_prefix = parts[1]
        states = {code for name, code in state_names.items()
                  if name.startswith(state_prefix) or code.casefold().startswith(state_prefix)}
        if not states:
            return []
    left = bisect_left(names, prefix)
    right = bisect_right(names, prefix + '\U0010ffff')
    cities, _ = city_index()
    matches = {}
    for alias, state, city in entries[left:right]:
        if states is not None and state not in states:
            continue
        # Same-name places within one state must resolve identically when the
        # chosen label is subsequently submitted to the routing endpoint.
        canonical = cities.get((normalize(city['name']), state))
        if canonical is None:
            continue
        canonical_name = normalize(canonical['name'])
        # Foreign-language transliterations often start with unrelated letters
        # (e.g. a Houston alias starts with "Chi"). Match visible city prefixes;
        # aliases are useful only when entered exactly, such as "NYC".
        if not canonical_name.startswith(prefix) and (alias != prefix or len(prefix) < 2):
            continue
        label = f"{canonical['name']}, {state}"
        rank = (alias != prefix, -canonical['population'], label.casefold())
        result = {'label': label, 'city': canonical['name'], 'state': state}
        if label not in matches or rank < matches[label][0]:
            matches[label] = (rank, result)
    ordered = sorted(matches.values(), key=lambda match: match[0])
    return [result for _, result in ordered[:min(limit, 8)]]


@lru_cache(maxsize=1)
def us_boundary():
    path = settings.BASE_DIR / 'data' / 'us_states.geojson'
    if not path.exists():
        raise APIError('data_not_ready', 'US boundary data is missing. Run the setup instructions.', 503)
    data = json.loads(path.read_text(encoding='utf-8'))
    return unary_union([shape(feature['geometry']) for feature in data['features']])


def within_usa(latitude, longitude):
    return us_boundary().covers(Point(longitude, latitude))


def resolve_location(value, field):
    if isinstance(value, str):
        if len(value) > 200:
            raise APIError('invalid_location', f'{field} must be at most 200 characters.')
        parts = [part.strip() for part in value.split(',')]
        if len(parts) == 3 and normalize(parts[-1]) in ('usa', 'us', 'united states'):
            parts.pop()
        if len(parts) != 2 or not all(parts):
            raise APIError('invalid_location', f'Use "City, ST" or a latitude/longitude object for {field}.')
        cities, state_names = city_index()
        state = state_names.get(normalize(parts[1]), parts[1].upper())
        city = cities.get((normalize(parts[0]), state))
        if city is None:
            raise APIError('location_not_found', f'{field} was not found in the US city gazetteer. Supply precise latitude/longitude coordinates for small towns or addresses.')
        return {'label': f"{city['name']}, {state}", 'latitude': float(city['latitude']),
                'longitude': float(city['longitude']), 'precision': 'city_center'}
    if not isinstance(value, dict) or set(value) != {'latitude', 'longitude'}:
        raise APIError('invalid_location', f'{field} must be "City, ST" or an object containing latitude and longitude.')
    lat, lon = value['latitude'], value['longitude']
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not -180 <= n <= 180 or not math.isfinite(n) for n in (lat, lon)):
        raise APIError('invalid_location', f'{field} coordinates must be finite numbers.')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or not within_usa(lat, lon):
        raise APIError('outside_usa', f'{field} must be within the United States (50 states and Washington, DC).')
    return {'label': f'{lat:.5f}, {lon:.5f}', 'latitude': lat, 'longitude': lon, 'precision': 'coordinate'}
