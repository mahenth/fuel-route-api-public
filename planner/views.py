import json
import logging
from pathlib import Path
from threading import Lock

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from .errors import APIError
from .locations import suggest_cities
from .models import Dataset, Trip
from .service import plan_trip

logger = logging.getLogger(__name__)
planning_lock = Lock()


def error_response(error):
    response = JsonResponse({'error': {'code': error.code, 'message': error.message, 'details': error.details}}, status=error.status)
    if error.status in (429, 503):
        response['Retry-After'] = '5'
    return response


@ensure_csrf_cookie
def index(request, trip_id=None):
    return render(request, 'planner/index.html', {'trip_id': str(trip_id) if trip_id else ''})


@csrf_exempt
def create_route(request):
    # Deliberately stateless public API; no cookies, accounts or user privileges.
    if request.method != 'POST':
        response = error_response(APIError('method_not_allowed', 'Use POST with a JSON body.', 405))
        response['Allow'] = 'POST'
        return response
    if request.content_type != 'application/json':
        return error_response(APIError('unsupported_media_type', 'Content-Type must be application/json.', 415))
    try:
        if len(request.body) > 16_384:
            raise RequestDataTooBig()
        payload = json.loads(request.body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not planning_lock.acquire(timeout=5):
            raise APIError('routing_busy', 'Routing is busy. Try again in a few seconds.', 429)
        try:
            result = plan_trip(payload)
        finally:
            planning_lock.release()
        result['map_url'] = request.build_absolute_uri(result['map_url'])
        response = JsonResponse(result)
        response['X-Routing-Calls'] = str(result['metrics']['routing_calls'])
        response['X-Cache'] = 'HIT' if result['metrics']['cache_hit'] else 'MISS'
        response['Cache-Control'] = 'no-store'
        return response
    except APIError as exc:
        return error_response(exc)
    except RequestDataTooBig:
        return error_response(APIError('body_too_large', 'Request body must be 16 KB or smaller.', 413))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.debug('Invalid JSON request: %s', exc)
        return error_response(APIError('invalid_json', 'Request body must be valid JSON with finite numbers.'))


def route_detail(request, trip_id):
    if request.method != 'GET':
        return error_response(APIError('method_not_allowed', 'Use GET.', 405))
    trip = Trip.objects.filter(id=trip_id).first()
    if trip is None:
        return error_response(APIError('not_found', 'This saved route does not exist.', 404))
    result = dict(trip.payload)
    result['map_url'] = request.build_absolute_uri(result['map_url'])
    return JsonResponse(result)


def health(request):
    dataset = Dataset.objects.filter(key='fuel').first()
    return JsonResponse({'status': 'ok' if dataset else 'setup_required',
                         'django_version': __import__('django').get_version(),
                         'dataset': dataset.summary if dataset else None}, status=200 if dataset else 503)


def locations(request):
    if request.method != 'GET':
        response = error_response(APIError('method_not_allowed', 'Use GET for city suggestions.', 405))
        response['Allow'] = 'GET'
        return response
    try:
        response = JsonResponse({'results': suggest_cities(request.GET.get('q', ''))})
    except APIError as exc:
        return error_response(exc)
    response['Cache-Control'] = 'public, max-age=86400'
    response['X-Routing-Calls'] = '0'
    return response


def schema(request):
    return JsonResponse(json.loads((Path(settings.BASE_DIR) / 'docs' / 'openapi.json').read_text(encoding='utf-8')))
