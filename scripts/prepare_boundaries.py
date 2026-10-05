#!/usr/bin/env python3
"""Refresh generalized US state polygons from the official Census TIGERweb API."""
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOURCE = 'https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2024/State_County/MapServer/7/query'
OUTPUT = Path(__file__).resolve().parents[1] / 'data' / 'us_states.geojson'


def main():
    query = urlencode({
        'where': '1=1', 'outFields': 'NAME,STUSAB,STATE', 'f': 'geojson',
        'outSR': '4326', 'returnGeometry': 'true',
        'geometryPrecision': '4', 'maxAllowableOffset': '0.001',
    })
    request = Request(SOURCE + '?' + query, headers={'User-Agent': 'FuelRouteAssessment/1.0'})
    with urlopen(request, timeout=60) as response:
        result = json.load(response)
    if result.get('type') != 'FeatureCollection':
        raise ValueError('Census response is not a GeoJSON FeatureCollection')
    result['features'] = [feature for feature in result['features']
                          if int(feature['properties']['STATE']) < 60]
    if len(result['features']) != 51:
        raise ValueError('Expected 50 states and District of Columbia; refusing to replace snapshot')
    OUTPUT.write_text(json.dumps(result, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Wrote {len(result["features"])} state/DC polygons to {OUTPUT}')


if __name__ == '__main__':
    main()
