# Data provenance and coverage

`fuel-prices.csv` is an unchanged copy of the assignment attachment
`fuel-prices-for-be-assessment.csv`. It supplies all fuel prices used by the app.
The source contains duplicate OPIS IDs, inconsistent aliases, and Canadian
province codes as well as US states. It has no station coordinates or price
observation dates. Prices are an assignment snapshot, not live quotes.

`station_coordinates.csv` supplements the prices with physical station marker
coordinates from the following official public sources, retrieved on October 5,
2026:

| Provider | Official source | Matched OPIS IDs |
| --- | --- | ---: |
| Pilot / Flying J | [Public locator](https://locations.pilotflyingj.com/search), fetched with `Accept: application/json`, `per=50`, and paginated `offset` | 604 |
| Love's | [Official store feed](https://www.loves.com/api/fetch_stores) used by its public locator | 548 |
| TA | [Official location workbook](https://www.ta-petro.com/api/locationsdata/download/), linked by the [location page](https://www.ta-petro.com/location/) | 179 |
| Petro | Same official TA/Petro workbook | 46 |
| **Total** | | **1,377** |

The input contains **6,738 unique OPIS IDs**, of which **6,626 are in US states**
and **112 use Canadian province codes**. The coordinate snapshot covers 1,377 US
IDs across 45 states. The remaining 5,249 US IDs are retained as price records
with unknown coordinates and cannot safely be included as route candidates.
Results are optimal only among the verified candidate locations considered by
the planner, not among every business in the attachment or every station in the
USA. `coordinate_coverage.json` provides the generated counts and state list.

## Matching and precision

- Numbered stores require an exact chain, published store number, and state
  match. The OPIS ID is a separate identifier and is never treated as a chain's
  store number. All aliases of an OPIS ID are checked; conflicting matches are
  rejected. Matching current official store IDs can legitimately yield a
  neighboring mailing-city name different from the assignment city.
- Unnumbered stores require a unique chain/city/state match and either a shared
  highway-exit number or compatible published site name. If both sources provide
  highway exits, conflicting exits are rejected. This prevents the historical
  TA Knoxville East record from matching the current TA Knoxville West site.
- Ambiguous/missing matches are excluded. No random positions, city centroids,
  or interpolated highway locations are assigned to stations.
- `precision=site` means a provider's physical site marker. It does not claim a
  surveyed pump position or a verified driveway entry point. The Pilot feed's
  `cityCoordinate` is explicitly never used.
- Every matched row includes a source URL, the matching method, and provider
  name/city/state for audit. Two distinct OPIS IDs for TA Mt. Vernon, Missouri,
  identify the same published physical site and are retained as source records.

These joins cannot verify historical relocations or that a business still honors
an old assignment price. The separate source fields preserve that distinction.

## Rebuilding station coordinates

From the repository root:

```console
python scripts/prepare_station_coordinates.py
python scripts/prepare_station_coordinates.py --refresh
python manage.py import_fuel
```

The preparation script uses only Python's standard library. It reads the original
price CSV and downloads the public provider feeds once during preparation. The
first run needs roughly 20 source requests: one Love's download, one TA workbook,
and the paginated Pilot locator (18 pages in this snapshot). Responses are cached
under ignored `data/source-cache/`; repeat preparation uses the cache unless
`--refresh` is supplied. API requests never call these feeds and never geocode
the thousands of source station rows individually. Committed coordinates make
the normal project setup independent of those source websites.

## USA boundary data

`us_states.geojson` contains generalized state/DC polygons from the US Census
Bureau's [ACS 2024 TIGERweb State/County layer](https://tigerweb.geo.census.gov/arcgis/rest/services/Generalized_ACS2024/State_County/MapServer/7).
The query uses `where=1=1`, fields `NAME,STUSAB,STATE`, GeoJSON/WGS84 output,
`geometryPrecision=4`, and `maxAllowableOffset=0.001`. Features with numeric
`STATE >= 60` are excluded, retaining all 50 states and the District of Columbia.

Rebuild with `python scripts/prepare_boundaries.py`. Generalized boundaries
support validation; points close to a coastline or international border can be
affected by simplification. These polygons are not cadastral or legal boundaries.

## Attribution and rights

Station location facts are attributed to the providers above. No reuse license
was supplied with the assignment CSV, and this project does not assert an open
data license for the provider feeds. Any repository software license applies to
the project code, not automatically to these datasets. Confirm the applicable
data terms before broader redistribution or production reuse. Census geography
is attributed separately to the US Census Bureau.
