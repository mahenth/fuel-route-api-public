#!/usr/bin/env python3
"""Build station coordinates from official locator feeds, once before serving.

Run from any directory: python scripts/prepare_station_coordinates.py
Only Python's standard library is required. Public feed responses are cached in
data/source-cache; rerun with --refresh to update. No fuel prices are downloaded.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
CACHE = DATA / 'source-cache'
LOVES_URL = 'https://www.loves.com/api/fetch_stores'
PILOT_URL = 'https://locations.pilotflyingj.com/search'
TA_URL = 'https://www.ta-petro.com/api/locationsdata/download/'
STATE_NAMES = dict(zip(
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'.split('|'),
    'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split()
))


def normalize(value: str) -> str:
    text = unicodedata.normalize('NFKD', value or '').upper()
    text = re.sub(r'\bSAINT\b', 'ST', text)
    text = re.sub(r'\bMOUNT\b', 'MT', text)
    return re.sub('[^A-Z0-9]', '', text)


def fetch(url: str, name: str, refresh: bool = False) -> bytes:
    path = CACHE / name
    if path.exists() and not refresh:
        return path.read_bytes()
    req = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'FuelRouteAssessment/1.0 (offline station coordinate preparation)',
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as response:
                raw = response.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            print(f'Downloaded {name}: {len(raw):,} bytes', flush=True)
            return raw
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError('Unreachable')


def workbook_rows(raw: bytes):
    """Read the TA public XLSX without making Excel a setup dependency."""
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [
                ''.join(item.itertext())
                for item in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', ns)
            ]
        tree = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        header = None
        for row in tree.findall('s:sheetData/s:row', ns):
            values = {}
            for cell in row.findall('s:c', ns):
                column = re.sub('[0-9]', '', cell.attrib['r'])
                kind = cell.attrib.get('t')
                value_node = cell.find('s:v', ns)
                value = value_node.text if value_node is not None else ''
                if kind == 's':
                    value = strings[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(cell.find('s:is', ns).itertext())
                values[column] = value
            if header is None:
                header = values
            else:
                yield {title: values.get(col, '') for col, title in header.items()}


def provider_locations(refresh: bool):
    loves = json.loads(fetch(LOVES_URL, 'loves.json', refresh))
    for loc in loves['stores']:
        if not loc.get('isLoveStore'):
            continue
        yield {
            'chain': 'loves', 'number': str(loc['number']),
            'name': f"Love's #{loc['number']}", 'city': loc['city'], 'state': loc['state'],
            'latitude': loc['latitude'], 'longitude': loc['longitude'], 'source': LOVES_URL,
        }

    first = json.loads(fetch(PILOT_URL + '?per=50&offset=0', 'pilot-0.json', refresh))
    count = first['response']['count']
    seen = set()
    for offset in range(0, count, 50):
        page = first if offset == 0 else json.loads(fetch(
            PILOT_URL + f'?per=50&offset={offset}', f'pilot-{offset}.json', refresh))
        for entity in page['response']['entities']:
            loc = entity['profile']
            address = loc['address']
            number = str(loc['meta']['id'])
            if address['countryCode'] != 'US' or number in seen:
                continue
            seen.add(number)
            # Physical site coordinate. Never substitute the cityCoordinate field.
            point = loc.get('yextDisplayCoordinate') or loc.get('displayCoordinate')
            if not point:
                continue
            yield {
                'chain': 'pilot', 'number': number, 'name': loc.get('c_pagesName', loc['name']),
                'city': address['city'], 'state': address['region'],
                'latitude': point['lat'], 'longitude': point['long'],
                'source': loc.get('landingPageUrl', PILOT_URL),
            }
        # Be polite to the public setup source; this never happens on an API request.
        if offset:
            time.sleep(0.15)

    for loc in workbook_rows(fetch(TA_URL, 'ta.xlsx', refresh)):
        if not loc.get('Latitude') or not loc.get('Longitude'):
            continue
        yield {
            'chain': 'petro' if loc['Brand'].lower().startswith('petro') else 'ta',
            'number': str(int(loc['Site ID'])), 'name': loc['Location'],
            'city': loc['City'], 'state': STATE_NAMES.get(loc['State'], loc['State']),
            'latitude': float(loc['Latitude']), 'longitude': float(loc['Longitude']),
            'source': TA_URL, 'directions': loc['Directions'],
        }


def identify_chain(name: str) -> str | None:
    name = name.upper().strip()
    if re.match(r"LOVE'?S\b", name):
        return 'loves'
    if re.match(r'(PILOT|FLYING\s*J)\b', name):
        return 'pilot'
    if re.match(r'(TA\b|TRAVELCENTERS? OF AMERICA)', name):
        return 'ta'
    if re.match(r'PETRO\b', name) and ('STOPPING' in name or '#' in name):
        return 'petro'
    return None


def match_station(row, by_number, by_city):
    chain = identify_chain(row['Truckstop Name'])
    if not chain:
        return None, None
    state = row['State'].strip().upper()
    numbers = re.findall(r'#\s*0*(\d+)', row['Truckstop Name'])
    if numbers:
        candidates = by_number[(chain, str(int(numbers[-1])), state)]
        if len(candidates) == 1:
            return candidates[0], 'chain_number_state'
        # Do not override a mismatched published store number with a city guess.
        return None, None
    candidates = by_city[(chain, normalize(row['City']), state)]
    if len(candidates) == 1:
        candidate = candidates[0]
        # A chain can close an East site while retaining a West site in the city.
        # Unique city alone is therefore insufficient evidence of the same site.
        if compatible_site(row, candidate):
            return candidate, 'unique_chain_city_state_with_exit_or_name'
    return None, None


def compatible_site(row, candidate):
    def exit_numbers(value):
        return set(re.findall(r'\bEXIT\s*(\d+)', value.upper()))

    original_exits = exit_numbers(row['Address'])
    published_exits = exit_numbers(candidate.get('directions', ''))
    if original_exits and published_exits:
        return bool(original_exits & published_exits)

    def site_name(value):
        value = re.sub(r'\b(TRAVEL\s*CENTERS?|TRAVEL PLAZA|STOPPING CENTER|EXPRESS|TA|PETRO|OF)\b', '', value.upper())
        return normalize(value)

    left, right = site_name(row['Truckstop Name']), site_name(candidate['name'])
    return bool(left and right and (left == right or left in right or right in left))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true', help='Download new provider snapshots')
    args = parser.parse_args()
    locations = list(provider_locations(args.refresh))
    by_number, by_city = defaultdict(list), defaultdict(list)
    for loc in locations:
        if not (18 < float(loc['latitude']) < 72 and -180 <= float(loc['longitude']) < -60):
            continue
        by_number[(loc['chain'], loc['number'], loc['state'])].append(loc)
        by_city[(loc['chain'], normalize(loc['city']), loc['state'])].append(loc)

    # Preserve all aliases per OPIS ID so a valid store-number alias can be used.
    rows = defaultdict(list)
    with (DATA / 'fuel-prices.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            rows[row['OPIS Truckstop ID']].append(row)
    output, unresolved = [], []
    for station_id, aliases in sorted(rows.items(), key=lambda item: int(item[0])):
        matches = []
        for row in aliases:
            loc, method = match_station(row, by_number, by_city)
            if loc:
                matches.append((loc, method))
        unique = {(m[0]['chain'], m[0]['number']) for m in matches}
        if len(unique) == 1:
            loc, method = matches[0]
            output.append({
                'id': station_id, 'latitude': loc['latitude'], 'longitude': loc['longitude'],
                'source': loc['source'], 'precision': 'site', 'match_method': method,
                'chain': loc['chain'], 'source_name': loc['name'],
                'source_city': loc['city'], 'source_state': loc['state'],
            })
        else:
            unresolved.append(aliases[0])

    fields = ['id', 'latitude', 'longitude', 'source', 'precision', 'match_method',
              'chain', 'source_name', 'source_city', 'source_state']
    with (DATA / 'station_coordinates.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output)
    report = {
        'prepared_at_utc': datetime.now(timezone.utc).isoformat(),
        'input_unique_stations': len(rows), 'matched_stations': len(output),
        'unmatched_stations': len(unresolved),
        'input_unique_us_stations': sum(aliases[0]['State'] in STATE_NAMES.values() for aliases in rows.values()),
        'input_unique_non_us_stations': sum(aliases[0]['State'] not in STATE_NAMES.values() for aliases in rows.values()),
        'matched_by_chain': dict(Counter(row['chain'] for row in output)),
        'matched_by_method': dict(Counter(row['match_method'] for row in output)),
        'matched_states': sorted({row['source_state'] for row in output}),
        'sources': [LOVES_URL, PILOT_URL, TA_URL],
        'precision': 'Official physical-site coordinates, not city centroids.',
        'limitation': 'Only matched official-chain sites are routing candidates; unmatched source rows have no verified coordinates.',
    }
    (DATA / 'coordinate_coverage.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
