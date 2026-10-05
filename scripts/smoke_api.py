"""Run a small real-provider acceptance check against an already running server.

This is intentionally separate from the offline test suite. It makes at most
four routing-provider calls (long trip, possible repair, short trip).
"""
import argparse
import json
import time
from decimal import Decimal
from pathlib import Path

import requests


def verify_trip(body):
    fuel = body['summary']['initial_gallons']
    previous = 0
    cost = Decimal('0')
    for stop in body['fuel_stops']:
        fuel -= (stop['mile'] - previous) / 10
        assert fuel >= -0.00001, 'Stop is beyond available fuel range'
        assert abs(fuel - stop['arrival_gallons']) < 0.00001
        fuel += stop['gallons']
        assert fuel <= 50.00001, 'Tank capacity exceeded'
        cost += Decimal(stop['cost_usd'])
        previous = stop['mile']
    fuel -= (body['route']['distance_miles'] - previous) / 10
    assert fuel >= -0.00001, 'Destination is beyond available fuel range'
    assert abs(fuel - body['summary']['arrival_gallons']) < 0.00001
    assert cost == Decimal(body['summary']['total_cost_usd'])
    assert body['metrics']['routing_calls'] <= 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    long_trip = {'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'}
    cases = [('cross_country', long_trip), ('cross_country_cached', long_trip),
             ('short_trip', {'start': 'New York City, NY', 'finish': 'Philadelphia, PA'}),
             ('reject_canada', {'start': {'latitude': 43.6532, 'longitude': -79.3832},
                                'finish': 'Chicago, IL'})]
    report = []
    output = Path(__file__).resolve().parents[1] / 'artifacts'
    output.mkdir(exist_ok=True)
    for name, payload in cases:
        started = time.perf_counter()
        response = requests.post(args.base_url.rstrip('/') + '/api/routes/', json=payload, timeout=100)
        body = response.json()
        seconds = round(time.perf_counter() - started, 3)
        if name == 'reject_canada':
            assert response.status_code == 400, body
        else:
            assert response.status_code == 200, body
            verify_trip(body)
            if name.endswith('cached'):
                assert body['metrics']['routing_calls'] == 0
                assert body['metrics']['cache_hit']
            if name == 'short_trip':
                assert not body['fuel_stops']
        item = {'case': name, 'status': response.status_code, 'wall_seconds': seconds,
                'metrics': body.get('metrics'), 'summary': body.get('summary'),
                'map_url': body.get('map_url')}
        report.append(item)
        print(json.dumps(item), flush=True)
        (output / f'{name}.json').write_text(json.dumps(body, indent=2), encoding='utf-8')
    (output / 'live-checks.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
