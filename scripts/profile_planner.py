"""Read-only, offline timing of a previously cached long driving route."""
# Django app imports must follow environment setup and django.setup().
# ruff: noqa: E402
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fuelwise.settings")

import django
django.setup()

from planner.geometry import project_stations
from planner.locations import resolve_location
from planner.models import Dataset, RouteCache
from planner.optimizer import optimize_fuel
from planner.service import station_catalog


def timed(action):
    started = time.perf_counter()
    result = action()
    return result, (time.perf_counter() - started) * 1000


dataset = Dataset.objects.get(key="fuel")
routes, db_ms = timed(lambda: [item.payload for item in RouteCache.objects.all()])
base = max((route for route in routes if len(route["leg_miles"]) == 1), key=lambda route: route["distance_miles"])
_, city_cold_ms = timed(lambda: resolve_location("Chicago, IL", "start"))
_, city_warm_ms = timed(lambda: resolve_location("Los Angeles, CA", "finish"))
_, boundary_ms = timed(lambda: resolve_location({"latitude": 41.8781, "longitude": -87.6298}, "start"))
stations, catalog_ms = timed(lambda: station_catalog(dataset.fingerprint))
geometry_ms = []
for _ in range(3):
    candidates, duration = timed(lambda: project_stations(base["geometry"]["coordinates"], base["distance_miles"], stations))
    geometry_ms.append(duration)
plan, optimizer_ms = timed(lambda: optimize_fuel(base["distance_miles"], candidates))
profiler = cProfile.Profile()
profiler.runcall(project_stations, base["geometry"]["coordinates"], base["distance_miles"], stations)
stream = io.StringIO()
pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(18)
result = {
    "network_calls": 0,
    "route_distance_miles": base["distance_miles"],
    "geometry_points": len(base["geometry"]["coordinates"]),
    "station_count": len(stations),
    "candidate_count": len(candidates),
    "preliminary_stop_count": len(plan["fuel_stops"]),
    "cached_route_db_read_ms": round(db_ms, 2),
    "cold_city_lookup_ms": round(city_cold_ms, 2),
    "warm_city_lookup_ms": round(city_warm_ms, 2),
    "cold_boundary_lookup_ms": round(boundary_ms, 2),
    "catalog_read_ms": round(catalog_ms, 2),
    "geometry_runs_ms": [round(value, 2) for value in geometry_ms],
    "geometry_median_ms": round(statistics.median(geometry_ms), 2),
    "optimizer_ms": round(optimizer_ms, 2),
    "profile": stream.getvalue(),
}
path = BASE / "artifacts" / "local-performance.json"
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
