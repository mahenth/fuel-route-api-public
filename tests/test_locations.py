from unittest.mock import patch

from django.test import SimpleTestCase

from planner import locations


class LocationSuggestionAPITests(SimpleTestCase):
    def setUp(self):
        self.http = patch('requests.get', side_effect=AssertionError('Autocomplete must work offline')).start()
        self.router = patch('planner.service.Router.route', side_effect=AssertionError('Autocomplete must not route')).start()
        self.addCleanup(patch.stopall)

    def suggestions(self, query):
        response = self.client.get('/api/locations/', {'q': query})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(response.json()), {'results'})
        return response.json()['results']

    def test_city_prefix_returns_canonical_city_and_state(self):
        results = self.suggestions('Chi')
        self.assertEqual(results[0], {'label': 'Chicago, IL', 'city': 'Chicago', 'state': 'IL'})
        self.assertLessEqual(len(results), 8)
        self.assertTrue(all(result['city'].casefold().startswith('chi') for result in results))
        self.http.assert_not_called()
        self.router.assert_not_called()

    def test_one_letter_query_is_bounded_and_every_result_can_be_selected(self):
        results = self.suggestions('s')
        self.assertEqual(len(results), 8)
        self.assertEqual(len({result['label'] for result in results}), len(results))
        for result in results:
            with self.subTest(label=result['label']):
                self.assertEqual(set(result), {'label', 'city', 'state'})
                self.assertEqual(result['label'], f"{result['city']}, {result['state']}")
                resolved = locations.resolve_location(result['label'], 'start')
                self.assertEqual(resolved['label'], result['label'])
        self.http.assert_not_called()
        self.router.assert_not_called()

    def test_state_prefixes_and_accents_are_case_insensitive(self):
        cases = [
            ('SÁN JOSÉ, cA', 'San Jose, CA', 'CA'),
            ('San Jose, calif', 'San Jose, CA', 'CA'),
            ('Chicago, ill', 'Chicago, IL', 'IL'),
            ('Chicago, il', 'Chicago, IL', 'IL'),
        ]
        for query, expected, state in cases:
            with self.subTest(query=query):
                results = self.suggestions(query)
                self.assertEqual(results[0]['label'], expected)
                self.assertTrue(all(result['state'] == state for result in results))

    def test_new_york_alias_returns_a_selectable_canonical_label(self):
        results = self.suggestions('New York, NY')
        self.assertTrue(results)
        self.assertIn(results[0]['label'], {'New York, NY', 'New York City, NY'})
        self.assertEqual(locations.resolve_location(results[0]['label'], 'finish')['label'], results[0]['label'])

    def test_empty_queries_and_non_us_locations_have_no_suggestions(self):
        response = self.client.get('/api/locations/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'results': []})
        for query in ['', '  ', 'Toronto, ON', 'San Juan, PR', 'San Juan, Puerto Rico', 'NoSuchCityForFuelwise']:
            with self.subTest(query=query):
                self.assertEqual(self.suggestions(query), [])

    def test_query_length_is_limited_before_lookup(self):
        self.assertEqual(self.suggestions('x' * 200), [])
        response = self.client.get('/api/locations/', {'q': 'x' * 201})
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.json())
        self.http.assert_not_called()
        self.router.assert_not_called()

    def test_result_limit_cannot_be_increased_by_a_query_parameter(self):
        response = self.client.get('/api/locations/', {'q': 's', 'limit': 10000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 8)

    def test_post_is_rejected_with_allow_header(self):
        response = self.client.post('/api/locations/', {'q': 'Chicago'})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response['Allow'], 'GET')


class LocationSuggestionRankingTests(SimpleTestCase):
    def setUp(self):
        rows = [
            ('Spring', 'TX', 60000, []),
            ('Springs', 'UT', 50000, ['Spring']),
            ('Springfield', 'IL', 110000, ['Capital City', 'Spring Field']),
            ('Springfield', 'MA', 150000, []),
            ('Springvale', 'CA', 200000, []),
            ('Spring Valley', 'NY', 200000, []),
            ('Springfield', 'IL', 1000, ['Spring Hamlet']),
        ]
        cities = {}
        for identity, (name, state, population, aliases) in enumerate(rows, start=1):
            city = {
                'geonameid': identity, 'name': name, 'admin1code': state,
                'countrycode': 'US', 'population': population,
                'latitude': 40.0, 'longitude': -100.0, 'alternatenames': aliases,
            }
            for alias in [name, *aliases]:
                key = (locations.normalize(alias), state)
                if key not in cities or cities[key]['population'] < population:
                    cities[key] = city
        state_names = {name: state for name, state in [
            ('texas', 'TX'), ('utah', 'UT'), ('illinois', 'IL'),
            ('massachusetts', 'MA'), ('california', 'CA'), ('new york', 'NY'),
        ]}
        self.city_index = patch.object(locations, 'city_index', return_value=(cities, state_names))
        self.city_index.start()
        locations.city_suggestion_index.cache_clear()
        self.addCleanup(self.city_index.stop)
        self.addCleanup(locations.city_suggestion_index.cache_clear)

    def test_exact_city_or_alias_precedes_more_populous_prefix_matches(self):
        results = locations.suggest_cities('SPRÍNG')
        self.assertEqual([result['label'] for result in results], [
            'Spring, TX', 'Springs, UT', 'Spring Valley, NY',
            'Springvale, CA', 'Springfield, MA', 'Springfield, IL',
        ])

    def test_aliases_and_duplicate_records_do_not_duplicate_canonical_choices(self):
        results = locations.suggest_cities('Spring', limit=8)
        self.assertEqual(len({result['label'] for result in results}), len(results))
        for result in results:
            with self.subTest(label=result['label']):
                self.assertEqual(locations.resolve_location(result['label'], 'start')['label'], result['label'])

    def test_alias_match_returns_canonical_name_that_the_route_api_accepts(self):
        results = locations.suggest_cities('capital city')
        self.assertEqual(results, [{'label': 'Springfield, IL', 'city': 'Springfield', 'state': 'IL'}])
        self.assertEqual(locations.resolve_location(results[0]['label'], 'start')['label'], 'Springfield, IL')

    def test_unrelated_alias_prefixes_do_not_crowd_city_name_suggestions(self):
        self.assertEqual(locations.suggest_cities('capit'), [])

    def test_state_qualifier_filters_before_applying_limit(self):
        results = locations.suggest_cities('spring, ill', limit=1)
        self.assertEqual(results, [{'label': 'Springfield, IL', 'city': 'Springfield', 'state': 'IL'}])
