"""Convenient cross-platform local server with static files; production uses a proxy."""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fuelwise.settings')

from django.contrib.staticfiles.handlers import StaticFilesHandler
from fuelwise.wsgi import application
from waitress import serve

if __name__ == '__main__':
    # Pay for parsing the offline gazetteer and country polygons once at startup.
    from planner.locations import city_index, us_boundary
    city_index()
    us_boundary()
    serve(StaticFilesHandler(application), host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8000')), threads=4)
