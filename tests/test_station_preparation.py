from collections import defaultdict
from unittest import TestCase

from scripts.prepare_station_coordinates import compatible_site, identify_chain, match_station


class StationPreparationTests(TestCase):
    def test_unique_city_does_not_merge_different_highway_exits(self):
        original = {'Truckstop Name': 'TA KNOXVILLE EAST TRAVEL CENTER',
                    'Address': 'I-40 & I-75, EXIT 374'}
        published = {'name': 'TA Knoxville West', 'directions': 'I-40, Exit 369'}
        self.assertFalse(compatible_site(original, published))

    def test_changed_marketing_name_can_match_same_exit(self):
        original = {'Truckstop Name': 'TA IOWA 80 TRAVEL PLAZA',
                    'Address': 'I-80, EXIT 284'}
        published = {'name': 'TA Walcott', 'directions': 'I-80 & County Y-40, Exit 284'}
        self.assertTrue(compatible_site(original, published))

    def test_different_number_must_not_fall_back_to_city(self):
        row = {'Truckstop Name': 'PILOT #123', 'City': 'Phoenix', 'State': 'AZ'}
        candidate = {'number': '456', 'name': 'Pilot #456'}
        cities = defaultdict(list, {('pilot', 'PHOENIX', 'AZ'): [candidate]})
        self.assertEqual(match_station(row, defaultdict(list), cities), (None, None))

    def test_independent_petroleum_company_is_not_petro_chain(self):
        self.assertIsNone(identify_chain('PETRO MART'))
        self.assertEqual(identify_chain('PETRO STOPPING CENTER #306'), 'petro')

    def test_number_requires_matching_state(self):
        row = {'Truckstop Name': 'LOVES TRAVEL STOP #5', 'City': 'Kingfisher', 'State': 'TX'}
        numbers = defaultdict(list, {('loves', '5', 'OK'): [{'number': '5'}]})
        self.assertEqual(match_station(row, numbers, defaultdict(list)), (None, None))
