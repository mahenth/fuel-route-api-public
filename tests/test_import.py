import csv
import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from planner.models import Dataset, Station
from planner.service import station_catalog


PRICE_HEADERS = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Retail Price"]
COORD_HEADERS = ["id", "latitude", "longitude", "source", "precision"]


def price_row(identifier=1, price="3.50", state="OK"):
    return dict(zip(PRICE_HEADERS, [identifier, f"Fuel {identifier}", "1 Main St", "Testville", state, price]))


def coordinate_row(identifier=1, **overrides):
    return {"id": identifier, "latitude": 35, "longitude": -99, "source": "verified site survey", "precision": "site", **overrides}


class FuelImportTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="fuel-import-test-")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(station_catalog.cache_clear)
        self.prices = Path(self.directory.name) / "prices.csv"
        self.coords = Path(self.directory.name) / "coordinates.csv"

    @staticmethod
    def write_csv(path, headers, rows):
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def write_inputs(self, prices, coordinates=None):
        self.write_csv(self.prices, PRICE_HEADERS, prices)
        self.write_csv(self.coords, COORD_HEADERS, [coordinate_row()] if coordinates is None else coordinates)

    def run_import(self):
        call_command("import_fuel", prices=str(self.prices), coordinates=str(self.coords), stdout=io.StringIO())

    def test_duplicate_price_policy_and_idempotence(self):
        self.write_inputs([
            price_row(1, "4.00"), price_row(1, "3.00"), price_row(2, "5.00"), price_row(1, "3.50"),
        ])
        self.run_import()
        first = Dataset.objects.get(key="fuel")
        self.assertEqual(Station.objects.count(), 2)
        self.assertEqual(Station.objects.get(pk=1).price, Decimal("3.00"))
        self.assertEqual(first.summary["source_rows"], 4)
        self.assertEqual(first.summary["duplicate_rows"], 2)
        self.assertEqual(first.summary["conflicting_price_rows"], 2)
        self.assertEqual(first.summary["located_stations"], 1)
        self.assertEqual(first.summary["unlocated_stations"], 1)
        self.assertIsNone(Station.objects.get(pk=2).latitude)
        self.run_import()
        second = Dataset.objects.get(key="fuel")
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(second.summary, first.summary)
        self.assertEqual(Station.objects.count(), 2)

    def test_invalid_price_rows_are_counted_and_excluded(self):
        self.write_inputs([
            price_row(1, "3.00"), price_row(2, "NaN"), price_row(3, "-1"),
            price_row(4, "0"), price_row(5, "101"), price_row("oops", "3"),
            price_row(7, "4", state="O"),
        ])
        self.run_import()
        self.assertEqual(list(Station.objects.values_list("opis_id", flat=True)), [1])
        self.assertEqual(Dataset.objects.get(key="fuel").summary["invalid_rows"], 6)

    def test_truncated_price_row_is_counted_instead_of_crashing_import(self):
        self.write_inputs([price_row()])
        with self.prices.open("a", encoding="utf-8", newline="") as file:
            file.write("2,Truncated row\n")
        self.run_import()
        self.assertEqual(list(Station.objects.values_list("opis_id", flat=True)), [1])
        self.assertEqual(Dataset.objects.get(key="fuel").summary["invalid_rows"], 1)

    def test_malformed_coordinate_import_preserves_previous_dataset(self):
        self.write_inputs([price_row(1, "3.00")])
        self.run_import()
        previous_fingerprint = Dataset.objects.get(key="fuel").fingerprint
        invalid = [
            coordinate_row(latitude="NaN"), coordinate_row(longitude=181),
            coordinate_row(precision="city_center"), coordinate_row(source=""),
        ]
        for malformed in invalid:
            self.write_inputs([price_row(2, "4.00")], [malformed])
            with self.subTest(coordinate=malformed), self.assertRaises(CommandError):
                self.run_import()
            self.assertEqual(list(Station.objects.values_list("opis_id", flat=True)), [1])
            self.assertEqual(Dataset.objects.get(key="fuel").fingerprint, previous_fingerprint)

    def test_duplicate_coordinate_ids_are_rejected(self):
        self.write_inputs([price_row()], [coordinate_row(), coordinate_row()])
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(Station.objects.exists())

    def test_missing_price_headers_raise_command_error(self):
        self.write_inputs([price_row()])
        self.write_csv(self.prices, ["OPIS Truckstop ID", "Retail Price"], [{"OPIS Truckstop ID": 1, "Retail Price": "3.00"}])
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(Station.objects.exists())

    def test_empty_valid_dataset_preserves_existing_rows(self):
        self.write_inputs([price_row()])
        self.run_import()
        self.write_inputs([price_row(2, "NaN")])
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertTrue(Station.objects.filter(pk=1).exists())

    def test_coordinate_change_updates_fingerprint_and_clears_catalog_cache(self):
        self.write_inputs([price_row()])
        self.run_import()
        before = Dataset.objects.get(key="fuel").fingerprint
        self.assertEqual(station_catalog(before)[0]["latitude"], 35)
        self.write_inputs([price_row()], [coordinate_row(latitude=36)])
        self.run_import()
        after = Dataset.objects.get(key="fuel").fingerprint
        self.assertNotEqual(after, before)
        self.assertEqual(station_catalog.cache_info().currsize, 0)
        self.assertEqual(station_catalog(after)[0]["latitude"], 36)
