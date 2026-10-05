"""Idempotently import price rows and separately sourced site coordinates."""
import csv
import hashlib
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import Dataset, Station
from planner.service import station_catalog


class Command(BaseCommand):
    help = 'Import supplied price CSV and prepared station coordinates atomically.'

    def add_arguments(self, parser):
        parser.add_argument('--prices', default=str(settings.BASE_DIR / 'data' / 'fuel-prices.csv'))
        parser.add_argument('--coordinates', default=str(settings.BASE_DIR / 'data' / 'station_coordinates.csv'))

    @transaction.atomic
    def handle(self, *args, **options):
        prices_path, coords_path = Path(options['prices']), Path(options['coordinates'])
        if not prices_path.exists() or not coords_path.exists():
            raise CommandError('Both the price CSV and prepared station_coordinates.csv are required.')
        coords = {}
        with coords_path.open(encoding='utf-8-sig', newline='') as file:
            for row in csv.DictReader(file):
                try:
                    ident = int(row['id'])
                    lat, lon = float(row['latitude']), float(row['longitude'])
                    if (not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or
                            not -180 <= lon <= 180 or row['precision'] not in ('site', 'exact') or not row['source']):
                        raise ValueError('Coordinates must be verified site locations with a source.')
                    if ident in coords:
                        raise ValueError(f'Duplicate coordinate ID {ident}')
                    coords[ident] = row
                except (ValueError, KeyError) as exc:
                    raise CommandError(f'Invalid coordinate row: {exc}') from exc
        unique = {}
        invalid = duplicates = conflicts = rows = 0
        with prices_path.open(encoding='utf-8-sig', newline='') as file:
            reader = csv.DictReader(file)
            required = {'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City', 'State', 'Retail Price'}
            if not required.issubset(reader.fieldnames or []):
                raise CommandError('The fuel CSV is missing required headers.')
            for row in reader:
                rows += 1
                try:
                    ident = int(row['OPIS Truckstop ID'])
                    price = Decimal(row['Retail Price'])
                    if ident < 1 or not price.is_finite() or not 0 < price < 100 or len(row['State'].strip()) != 2:
                        raise ValueError('Invalid ID, price or state')
                except (ValueError, TypeError, InvalidOperation):
                    invalid += 1
                    continue
                if ident in unique:
                    duplicates += 1
                    if unique[ident].price != price:
                        conflicts += 1
                    if unique[ident].price <= price:
                        continue
                coordinate = coords.get(ident)
                unique[ident] = Station(opis_id=ident, name=row['Truckstop Name'].strip(),
                    address=row['Address'].strip(), city=row['City'].strip(), state=row['State'].strip().upper(),
                    price=price, latitude=float(coordinate['latitude']) if coordinate else None,
                    longitude=float(coordinate['longitude']) if coordinate else None,
                    coordinate_source=coordinate['source'] if coordinate else '',
                    coordinate_precision=coordinate['precision'] if coordinate else '')
        if not unique:
            raise CommandError('No valid stations were found; previous data was preserved.')
        Station.objects.all().delete()
        Station.objects.bulk_create(unique.values(), batch_size=250)
        fingerprint = hashlib.sha256(prices_path.read_bytes() + b'\0' + coords_path.read_bytes()).hexdigest()
        summary = {'source_rows': rows, 'unique_stations': len(unique),
                   'located_stations': sum(s.latitude is not None for s in unique.values()),
                   'unlocated_stations': sum(s.latitude is None for s in unique.values()),
                   'duplicate_rows': duplicates, 'conflicting_price_rows': conflicts, 'invalid_rows': invalid,
                   'duplicate_price_policy': 'lowest quoted price per OPIS ID', 'fingerprint': fingerprint}
        Dataset.objects.update_or_create(key='fuel', defaults={'fingerprint': fingerprint, 'summary': summary})
        station_catalog.cache_clear()
        self.stdout.write(self.style.SUCCESS(str(summary)))
