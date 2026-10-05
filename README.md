# Fuelwise

A Django API and map interface for planning fuel purchases on US road trips. Enter a starting city and destination, receive a driving route, fuel stops with itemized purchases, and the total amount to spend along the way.

The vehicle gets **10 miles per US gallon**, carries **50 gallons**, and has a **500-mile maximum range**. A new route normally needs **one routing call without fuel stops or two with stops**; a bounded repair may make a third. Repeated plans use a persistent cache and need **zero routing calls**.

## Run locally

Requires Python 3.12 or newer. The project pins **Django 6.1.1**, the latest stable release when this exercise was implemented on October 5, 2026. Internet access is needed to install packages, request new road routes, and load browser map tiles/assets. City lookup and station matching run locally.

```sh
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

Then install, prepare the database, import the bundled data, and start the application:

```sh
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py import_fuel
python serve.py
```

Open [the trip planner](http://127.0.0.1:8000/). The server defaults to `127.0.0.1:8000`; `HOST` and `PORT` environment variables override those values. `serve.py` runs Waitress and serves the local static assets without needing Django debug mode.

The supplied price CSV and prepared coordinate sidecar are in `data/`. To import a different copy of the assessment CSV:

```sh
python manage.py import_fuel --prices /path/to/fuel-prices-for-be-assessment.csv --coordinates data/station_coordinates.csv
```

The import is atomic and repeatable. It validates rows, de-duplicates OPIS IDs using the lowest quoted price, and records a dataset fingerprint. A changed dataset causes new planning cache keys. Coordinates are matched by OPIS ID; price and coordinate files must describe the same station IDs.

## API

| Method | Endpoint | Result |
| --- | --- | --- |
| `POST` | `/api/routes/` | Create or retrieve a cached fuel plan |
| `GET` | `/api/routes/<uuid>/` | Retrieve a saved plan |
| `GET` | `/maps/<uuid>/` | Open the saved interactive map |
| `GET` | `/api/health/` | Service and imported dataset status |
| `GET` | `/api/schema/` | OpenAPI schema |

The route API accepts JSON, without an account or API key. Unknown fields and invalid values produce a structured error response.

```http
POST /api/routes/
Content-Type: application/json

{
  "start": "Chicago, IL",
  "finish": "Los Angeles, CA",
  "initial_fuel_gallons": 50
}
```

Use `City, ST` for bundled US cities, or latitude/longitude objects for precise endpoints. Arbitrary street addresses are not geocoded. Either endpoint can use either representation:

```json
{
  "start": {"latitude": 40.7128, "longitude": -74.0060},
  "finish": {"latitude": 39.9526, "longitude": -75.1652},
  "initial_fuel_gallons": 50
}
```

`initial_fuel_gallons` is optional, defaults to `50`, and accepts a finite number from `0` through `50`. An empty tank is feasible only when an eligible station is reachable at the starting point. Coordinate inputs are checked against US state boundaries. Supported endpoints are in the 50 states or Washington, DC; a routable road connection and sufficient station coverage must still exist. A US-to-US route may transit Canada; the routing provider is not instructed to avoid foreign roads. Canadian price records are not eligible fuel stops.

The response includes:

- `route`: distance in miles, duration in seconds, GeoJSON `LineString`, and verified driving leg distances.
- `fuel_stops`: station coordinates and prices, cumulative route mile, gallons to buy, itemized dollar cost, and tank levels before and after purchase.
- `summary`: purchased and consumed gallons, remaining fuel, total purchase cost, stop count, and added driving distance from station detours.
- `map_url`: a saved interactive map, including the route and numbered stops.
- `metrics`: routing call count, cache status, and request processing time.
- `dataset`, `assumptions`, and `warnings`: coverage, model choices, and limitations.

Monetary amounts and per-gallon prices are **decimal strings** to preserve exact base-10 values; measured distances and gallons are JSON numbers. For example, `"total_cost_usd": "123.45"`.

Errors use `{"error":{"code":"...","message":"...","details":{...}}}`. Invalid inputs return `400`; insufficient reachable station coverage returns `422`; provider and setup errors use appropriate `5xx` responses. An impossible plan returns an explanation rather than a route that exceeds available fuel.

The homepage includes an editable **Under the hood** API explorer. Send JSON requests, inspect the exact response, and download results directly in the browser. The [OpenAPI schema](docs/openapi.json) describes the endpoints and request formats for other API clients.

## What “fuel cost” and “optimal” mean

Starting fuel is already owned. Its historical purchase price is not in the supplied data, so the trip cost is:

```text
total_cost_usd = sum(round(gallons_bought_at_stop × price_per_gallon, 2))
gallons_consumed = verified_route_miles / 10
arrival_gallons = initial_gallons + gallons_purchased − gallons_consumed
```

A 100-mile trip consumes 10 gallons but costs $0 in **new purchases** when it starts with a full tank. This is explicitly different from valuing all fuel consumed. Fractional purchases are allowed, there is no reserve requirement, and fuel availability at an eligible listed station is assumed.

For a fixed, ordered set of stations and distances, the optimizer finds the minimum-cost purchase plan: buy just enough to reach the first cheaper reachable station; if there is none, carry enough for the destination or fill to tank capacity. A monotone stack finds the next cheaper station in linear time after sorting. Fuel calculations use `Decimal`; only individual money charges and serialized measurements are rounded.

The **overall route and station selection are a heuristic**, not a global search over all roads and all detours:

1. Request a base driving route from OSRM.
2. Project preloaded, verified station sites into a two-mile corridor along that route and compute a preliminary fuel plan locally.
3. If stops are needed, request one road route through all selected stations. Re-optimize purchases using the actual driving leg distances, including access roads and detours.
4. If a verified leg exceeds available range, try one bounded repair and one final route request. Reject the plan if it still cannot satisfy the tank/range limits.

The returned map geometry and distance describe that verified itinerary. It can include pass-through station waypoints where a recalculation finds that no fuel purchase is needed. The exact cost guarantee applies to the chosen ordered itinerary, not to every possible route or every CSV station.

## Fuel data and coordinate coverage

The assessment CSV contains prices, names, and address text but **no latitude or longitude**. Assigning every station to a city center would create misleading stops and distance guarantees. Instead, preprocessing matches CSV station IDs to official chain location data and stores site coordinates with provenance in `data/station_coordinates.csv`.

The prepared dataset currently has **1,377 located stations across 45 states**, matched against 6,626 US station IDs and 112 Canadian IDs in the source. The 5,249 unlocated US stations are retained in the imported price dataset but excluded from route candidates until verified coordinates are supplied. This means some otherwise driveable routes cannot receive a fuel plan; the API exposes that limitation and returns `422` for an unbridgeable gap. It does not substitute fabricated locations or prices.

See [coordinate coverage](data/coordinate_coverage.json), [the data documentation](data/README.md), and [the coordinate preparation script](scripts/prepare_station_coordinates.py) for provenance and matching details. Prices are the supplied snapshot, not current pump prices. The project does not grant a redistribution license for the user-supplied CSV or third-party station data.

## Performance and free routing

Routing uses [OSRM](https://project-osrm.org/) with its public demonstration service as the configurable default. Map display uses [Leaflet](https://leafletjs.com/) and [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles. Route API calls are separate from browser tile requests. No per-station geocoding or directions request runs inside a planning request.

- City names resolve from bundled GeoNames data; precise coordinates are checked locally.
- Verified station coordinates are loaded ahead of time and kept in a process-local catalog keyed by dataset fingerprint.
- Route responses and complete plans are cached in SQLite for **24 hours**. Complete plan keys include endpoints, starting fuel, provider, planner version, corridor, and dataset fingerprint.
- Saved trip IDs remain readable; expiration controls re-planning, not deletion of the saved map.
- Provider calls have a timeout, a shared database rate gate with at least **1.1 seconds between calls**, and no automatic retry loop.
- A process lock prevents duplicate planning work within the single application process. Concurrent requests that cannot obtain it promptly receive a retryable busy response.

The public OSRM demo is free to use for this assessment but has no production availability guarantee. Respect its [usage policy](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy). For sustained traffic, run OSRM yourself or configure a suitable provider. Use an appropriate production database and distributed concurrency control before scaling to multiple application workers. The default profile is standard car routing, not a truck height, weight, or hazardous-load routing service.

The OSRM request uses full-resolution **polyline6** geometry, decoded locally to GeoJSON. The measured cross-country geometry was 80% smaller on the wire than its GeoJSON equivalent, preserving the same coordinates. `metrics.provider_ms` separates provider exchange time from overall processing time. Responses also include `X-Cache` and `X-Routing-Calls` headers.

Live acceptance measurements on October 5, 2026 (one run, not an availability guarantee):

| Request | HTTP wall time | Server processing | Routing calls |
| --- | --- | --- | --- |
| Chicago to Los Angeles, uncached | 11.80 s | 11.70 s, including 10.32 s at the provider | 2 |
| Identical cached request | 96 ms | 27 ms | 0 |
| New York City to Philadelphia, uncached | 1.98 s | 1.96 s | 1 |
| Canadian endpoint rejected | 27 ms | — | 0 |

The cross-country result was 2,024.12 miles with six stops and **$504.83** in new purchases from an initial full tank. These are the supplied snapshot prices, not a live quote. Local projection of 1,377 stations onto the roughly 23,000-point route took about 273 ms; the purchase optimizer took under 1 ms. Free-provider latency dominates uncached requests and varies. `serve.py` warms the city and boundary indexes before accepting traffic.

## Configuration

[`.env.example`](.env.example) documents the supported values. Environment variables must be set in your shell or process manager; the application does not automatically load a `.env` file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_DEBUG` | `0` | Django debug mode; use `1` only for local debugging |
| `DJANGO_SECRET_KEY` | Local assessment value | Override for a deployed service |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` | Accepted hostnames |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | OSRM-compatible routing service |
| `ROUTING_TIMEOUT` | `20` | Provider request timeout in seconds |
| `ROUTING_MIN_INTERVAL` | `1.1` | Minimum provider call interval in seconds |
| `ROUTING_USER_AGENT` | `FuelwiseAssessment/1.0` | Provider request identification |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Local server binding |

## Verify

```sh
python -m pip install -r requirements-dev.txt
python manage.py check
python manage.py test tests
```

Tests use mocked provider responses and deterministic station data, so they do not consume the public routing service. The 62 tests cover the optimizer against an independent exhaustive dynamic-programming oracle (243 pricing scenarios), fuel balance and capacity, route projection, city/coordinate validation, provider failures, verified detours, cache behavior, compact route decoding, data matching, and API error handling.

With the server running, `python scripts/smoke_api.py` checks a real cross-country trip, its cached repeat, a short trip, and rejection of a Canadian endpoint. It validates tank capacity, reachability, fuel balance and the sum of all purchase charges. This optional live check uses the routing service; results are written into ignored `artifacts/` files.

## Code map

| File | Responsibility |
| --- | --- |
| `planner/views.py` | HTTP endpoints and structured errors |
| `planner/service.py` | Cached planning flow and driving-leg verification |
| `planner/optimizer.py` | Fixed-route minimum-cost fuel purchases |
| `planner/geometry.py` | Station projection and route corridor filtering |
| `planner/routing.py` | OSRM client, caching, timeout, and rate limiting |
| `planner/locations.py` | Offline city lookup and US boundary validation |
| `planner/management/commands/import_fuel.py` | Price and coordinate import |
| `planner/templates/planner/index.html` | Route planner and built-in API explorer |
| `planner/static/planner/` | Responsive map interface |
