/* Fuelwise renders the backend JSON API. Routing-provider calls run on the server. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const money = (value, digits = 2) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));
  const number = (value, digits = 0) => new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(Number(value));
  let map, routeLayer, markersLayer, currentTrip = null, lastResponse = null, running = false;
  const stopMarkers = [];
  const locationCache = new Map();
  const locationSuggesters = [];

  function closeLocationSuggestions() {
    locationSuggesters.forEach((suggester) => suggester.close());
  }

  function initializeLocationSuggestions(id) {
    const input = $(id), list = $(`${id}-suggestions`), popup = $(`${id}-popup`);
    const message = $(`${id}-suggestion-message`), status = $('location-status');
    let results = [], activeIndex = -1, timer, controller, sequence = 0;
    let wantsSuggestions = false, composing = false;

    function cancelRequest() {
      window.clearTimeout(timer);
      controller?.abort();
      controller = null;
      sequence += 1;
      input.removeAttribute('aria-busy');
    }

    function resetOptions() {
      results = []; activeIndex = -1;
      list.replaceChildren();
      input.removeAttribute('aria-activedescendant');
      input.setAttribute('aria-expanded', 'false');
    }

    function close() {
      wantsSuggestions = false;
      cancelRequest(); resetOptions();
      popup.hidden = true;
      status.textContent = '';
    }

    function showMessage(value) {
      resetOptions();
      list.hidden = true;
      message.textContent = value;
      message.hidden = false;
      popup.hidden = false;
      status.textContent = value;
    }

    function highlight(index) {
      activeIndex = index;
      [...list.children].forEach((option, i) => option.setAttribute('aria-selected', String(i === index)));
      const option = list.children[index];
      if (option) {
        input.setAttribute('aria-activedescendant', option.id);
        // Scroll only the dropdown, without moving the page or the field.
        if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
        else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
      }
    }

    function renderSuggestions(items, initialIndex) {
      resetOptions();
      if (!items.length) { showMessage('No matching cities. You can still enter City, ST.'); return; }
      results = items;
      items.forEach((item, index) => {
        const option = document.createElement('li');
        option.id = `${id}-city-${index}`;
        option.className = 'location-option';
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.dataset.index = String(index);
        option.textContent = item.label;
        list.append(option);
      });
      list.hidden = false; message.hidden = true; popup.hidden = false;
      list.scrollTop = 0;
      input.setAttribute('aria-expanded', 'true');
      status.textContent = `${items.length} ${items.length === 1 ? 'city suggestion' : 'city suggestions'} available. Use the up and down arrow keys, then Enter to choose.`;
      if (initialIndex !== null) highlight(initialIndex < 0 ? items.length - 1 : 0);
    }

    async function search(query, requestSequence, initialIndex) {
      const isCurrent = () => requestSequence === sequence && wantsSuggestions && document.activeElement === input && input.value.trim() === query;
      const cacheKey = query.toLowerCase();
      if (locationCache.has(cacheKey)) {
        if (isCurrent()) renderSuggestions(locationCache.get(cacheKey), initialIndex);
        return;
      }
      const requestController = new AbortController();
      controller = requestController;
      input.setAttribute('aria-busy', 'true');
      showMessage('Finding cities…');
      const timeout = window.setTimeout(() => requestController.abort(), 5000);
      try {
        const response = await fetch(`/api/locations/?q=${encodeURIComponent(query)}`, { signal: requestController.signal, headers: { Accept: 'application/json' } });
        if (!response.ok) throw new Error('City lookup failed');
        const data = await response.json();
        if (!Array.isArray(data.results)) throw new Error('Invalid city suggestions');
        const items = data.results.filter((item) => typeof item?.label === 'string' && item.label.length > 0 && item.label.length <= 200).slice(0, 8);
        locationCache.set(cacheKey, items);
        if (locationCache.size > 100) locationCache.delete(locationCache.keys().next().value);
        if (isCurrent()) renderSuggestions(items, initialIndex);
      } catch (_) {
        if (isCurrent()) showMessage('City suggestions are unavailable. Enter City, ST.');
      } finally {
        window.clearTimeout(timeout);
        if (controller === requestController) controller = null;
        if (requestSequence === sequence) input.removeAttribute('aria-busy');
      }
    }

    function queueSearch(initialIndex = null, delay = 150) {
      close();
      const query = input.value.trim();
      if (!query || composing || document.activeElement !== input) return;
      wantsSuggestions = true;
      const requestSequence = sequence;
      timer = window.setTimeout(() => search(query, requestSequence, initialIndex), delay);
    }

    function select(index) {
      const item = results[index];
      if (!item) return;
      input.value = item.label;
      close();
      status.textContent = `${item.label} selected.`;
    }

    input.addEventListener('input', () => queueSearch());
    input.addEventListener('focus', () => { closeLocationSuggestions(); queueSearch(); });
    input.addEventListener('blur', close);
    input.addEventListener('compositionstart', () => { composing = true; close(); });
    input.addEventListener('compositionend', () => { composing = false; queueSearch(); });
    input.addEventListener('keydown', (event) => {
      if (composing || event.isComposing || event.keyCode === 229) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const direction = event.key === 'ArrowDown' ? 1 : -1;
        if (results.length) highlight(activeIndex < 0 ? (direction > 0 ? 0 : results.length - 1) : (activeIndex + direction + results.length) % results.length);
        else queueSearch(direction > 0 ? 0 : -1, 0);
      } else if (event.key === 'Enter' && results.length) {
        event.preventDefault();
        select(activeIndex < 0 ? 0 : activeIndex);
      } else if (event.key === 'Escape' && wantsSuggestions) {
        event.preventDefault(); close();
      } else if (event.key === 'Tab') close();
    });
    // Keep focus on the combobox so pointer selection does not race its blur handler.
    list.addEventListener('pointerdown', (event) => { if (event.button === 0) event.preventDefault(); });
    list.addEventListener('click', (event) => {
      const option = event.target.closest('[role="option"]');
      if (option && list.contains(option)) select(Number(option.dataset.index));
    });
    locationSuggesters.push({ close });
  }

  function initializeMap() {
    if (!window.L) { $('map-unavailable').hidden = false; return; }
    map = L.map('map', { zoomControl: false, scrollWheelZoom: false, minZoom: 3 }).setView([38.8, -97], 4);
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, referrerPolicy: 'strict-origin-when-cross-origin', attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(map);
    markersLayer = L.layerGroup().addTo(map);
    map.fitBounds([[25, -124.8], [49, -66.9]], { padding: [15, 15], animate: false });
  }

  function setBusy(busy) {
    running = busy;
    $('plan-button').disabled = busy;
    $('run-api').disabled = busy;
    $('swap-locations').disabled = busy;
    $('map-loading').hidden = !busy;
    $('plan-button-label').textContent = busy ? 'Planning your route…' : 'Find my fuel stops';
    $('trip-form').setAttribute('aria-busy', String(busy));
  }

  function popup(title, lines) {
    const content = document.createElement('div');
    const heading = document.createElement('strong'); heading.textContent = title; content.append(heading);
    for (const line of lines) { const p = document.createElement('p'); p.textContent = line; content.append(p); }
    return content;
  }

  function addEndpoint(point, kind, label) {
    if (!map || !point || !Number.isFinite(Number(point.latitude)) || !Number.isFinite(Number(point.longitude))) return;
    L.marker([Number(point.latitude), Number(point.longitude)], {
      title: `${label}: ${point.label || ''}`,
      icon: L.divIcon({ className: `endpoint-marker ${kind}`, html: label[0], iconSize: [26, 26], iconAnchor: [13, 13] })
    }).addTo(markersLayer).bindPopup(popup(label, [point.label || `${point.latitude}, ${point.longitude}`]));
  }

  function fitRoute() {
    if (!map) return;
    map.invalidateSize();
    if (routeLayer) map.fitBounds(routeLayer.getBounds(), { padding: [45, 60], maxZoom: 13, animate: !window.matchMedia('(prefers-reduced-motion: reduce)').matches });
    else map.fitBounds([[25, -124.8], [49, -66.9]], { padding: [15, 15] });
  }

  function renderMap(trip) {
    if (!map) return;
    if (routeLayer) map.removeLayer(routeLayer);
    markersLayer.clearLayers(); stopMarkers.length = 0;
    if (trip.route.geometry) {
      routeLayer = L.geoJSON(trip.route.geometry, { style: { color: '#214d3d', weight: 5, opacity: .94, lineCap: 'round', lineJoin: 'round' } }).addTo(map);
    }
    addEndpoint(trip.start, 'origin', 'Start');
    addEndpoint(trip.finish, 'destination', 'Finish');
    (trip.fuel_stops || []).forEach((stop, index) => {
      const marker = L.marker([Number(stop.latitude), Number(stop.longitude)], {
        title: `Fuel stop ${index + 1}: ${stop.name}`,
        icon: L.divIcon({ className: 'fuel-marker', html: String(index + 1), iconSize: [29, 29], iconAnchor: [14, 14] })
      }).addTo(markersLayer).bindPopup(popup(`${index + 1}. ${stop.name}`, [
        `${money(stop.price_per_gallon, 3)} per gallon · Mile ${number(stop.mile, 1)}`,
        `Buy ${number(stop.gallons, 2)} gallons · ${money(stop.cost_usd)}`,
        `Tank on arrival: ${number(stop.arrival_gallons, 2)} gal`
      ]));
      stopMarkers.push(marker);
    });
    fitRoute();
  }

  function textElement(tag, className, value) {
    const element = document.createElement(tag); element.className = className; element.textContent = value; return element;
  }

  function renderStops(stops) {
    $('stops-list').replaceChildren();
    stops.forEach((stop, index) => {
      const item = document.createElement('li');
      const button = document.createElement('button'); button.type = 'button'; button.className = 'stop-button';
      button.setAttribute('aria-label', `Show stop ${index + 1}: ${stop.name}, ${money(stop.price_per_gallon, 3)} per gallon, on map`);
      button.append(textElement('span', 'stop-number', index + 1));
      const content = document.createElement('div');
      const top = textElement('div', 'stop-topline', '');
      top.append(textElement('span', 'stop-name', stop.name), textElement('span', 'stop-price', `${money(stop.price_per_gallon, 3)}/gal`));
      content.append(top, textElement('span', 'stop-location', `Mile ${number(stop.mile, 1)} along your route`));
      const purchase = textElement('div', 'stop-purchase', '');
      purchase.append(textElement('span', '', `Buy ${number(stop.gallons, 2)} gal`), textElement('strong', '', money(stop.cost_usd)));
      content.append(purchase); button.append(content); item.append(button); $('stops-list').append(item);
      button.addEventListener('click', () => {
        if (!map || !stopMarkers[index]) return;
        $('map').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
        map.setView(stopMarkers[index].getLatLng(), 12); stopMarkers[index].openPopup();
      });
    });
    $('no-stops').hidden = stops.length !== 0;
    $('stop-count').textContent = `${stops.length} ${stops.length === 1 ? 'STOP' : 'STOPS'}`;
  }

  function renderTrip(trip) {
    closeLocationSuggestions();
    currentTrip = trip;
    const stops = trip.fuel_stops || [];
    const summary = trip.summary;
    if (summary.initial_gallons !== undefined) {
      const initial = String(summary.initial_gallons);
      if (![...$('initial-fuel').options].some(option => option.value === initial)) {
        const option = document.createElement('option'); option.value = initial; option.textContent = `${number(initial, 2)} gallons`; $('initial-fuel').append(option);
      }
      $('initial-fuel').value = initial;
    }
    $('empty-stops').hidden = true; $('stops-section').hidden = false; $('trip-details').hidden = false;
    $('map-idle-note').hidden = true; $('map-title').textContent = 'YOUR ROUTE, MAPPED OUT';
    $('total-cost').replaceChildren(document.createTextNode(money(summary.total_cost_usd)), textElement('span', '', ' USD'));
    $('total-distance').replaceChildren(document.createTextNode(number(trip.route.distance_miles)), textElement('span', '', ' mi'));
    $('total-stops').textContent = String(stops.length);
    $('gallons-caption').textContent = `${number(summary.gallons_purchased, 1)} gallons to purchase`;
    const minutes = Math.round(Number(trip.route.duration_seconds) / 60);
    $('drive-time').textContent = `${number(Math.floor(minutes / 60))} hr ${minutes % 60} min driving`;
    $('trip-label').textContent = `${trip.start?.label || 'Starting point'} → ${trip.finish?.label || 'Destination'}`;
    $('cost-caption').textContent = 'Excludes fuel already in your tank';
    const metrics = trip.metrics || {};
    const status = [metrics.cache_hit ? 'Saved route · cache hit' : 'Route planned'];
    if (metrics.processing_ms !== undefined) status.push(`${number(metrics.processing_ms)} ms`);
    if (metrics.routing_calls !== undefined) status.push(`${metrics.routing_calls} routing ${metrics.routing_calls === 1 ? 'call' : 'calls'}`);
    $('trip-status').replaceChildren(textElement('span', 'small-dot', ''), document.createTextNode(status.join(' · ')));
    const safeMapPath = /^\/maps\/[\w-]+\/?$/.test(trip.map_url || '') ? trip.map_url : (trip.id ? `/maps/${encodeURIComponent(trip.id)}/` : null);
    $('share-trip').hidden = !safeMapPath;
    if (safeMapPath) $('share-trip').href = safeMapPath;
    $('assumptions-list').replaceChildren(); $('warnings-list').replaceChildren();
    (trip.assumptions || []).forEach((value) => $('assumptions-list').append(textElement('li', '', value)));
    (trip.warnings || []).forEach((value) => $('warnings-list').append(textElement('li', '', value)));
    renderStops(stops); renderMap(trip);
  }

  function showResponse(response, body) {
    lastResponse = body;
    $('api-response').textContent = JSON.stringify(body, null, 2);
    $('response-status').textContent = `${response.status} ${response.statusText || (response.ok ? 'OK' : 'ERROR')}`;
    $('response-status').classList.toggle('error', !response.ok);
    $('download-response').disabled = false;
  }

  async function requestTrip(body, tripId) {
    if (running) return;
    closeLocationSuggestions();
    $('error-message').hidden = true; setBusy(true);
    try {
      const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 120000);
      let response;
      try {
        response = await fetch(tripId ? `/api/routes/${encodeURIComponent(tripId)}/` : '/api/routes/', {
          method: tripId ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
          ...(tripId ? {} : { body: JSON.stringify(body) }), signal: controller.signal
        });
      } finally { window.clearTimeout(timeout); }
      let data;
      try { data = await response.json(); } catch (_) { throw new Error('The server returned an unreadable response. Please try again.'); }
      showResponse(response, data);
      if (!response.ok) throw new Error(data.error?.message || 'This route could not be planned. Please check your locations and try again.');
      renderTrip(data);
      if (tripId) {
        if (data.start?.label) $('start').value = data.start.label;
        if (data.finish?.label) $('finish').value = data.finish.label;
      }
    } catch (error) {
      $('error-message').textContent = error.name === 'AbortError' ? 'The route request timed out. Please try again.' : (error.message || 'We could not reach the server. Please try again.');
      $('error-message').hidden = false;
      $('trip-status').textContent = currentTrip ? 'Showing previous successful trip · latest request failed' : 'Route could not be planned';
    } finally { setBusy(false); }
  }

  $('trip-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const body = { start: $('start').value.trim(), finish: $('finish').value.trim(), initial_fuel_gallons: Number($('initial-fuel').value) };
    $('api-request').value = JSON.stringify(body, null, 2); requestTrip(body);
  });
  $('swap-locations').addEventListener('click', () => { closeLocationSuggestions(); const previous = $('start').value; $('start').value = $('finish').value; $('finish').value = previous; });
  $('fit-route').addEventListener('click', fitRoute);
  $('run-api').addEventListener('click', () => {
    try { const body = JSON.parse($('api-request').value); requestTrip(body); }
    catch (_) { $('response-status').textContent = 'INVALID JSON'; $('response-status').classList.add('error'); $('api-response').textContent = 'The request body is not valid JSON. Check commas, quotes, and brackets.'; }
  });
  $('download-response').addEventListener('click', () => {
    if (!lastResponse) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(lastResponse, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `fuelwise-${currentTrip?.id || 'response'}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  document.querySelector('a[href="#api-console"]').addEventListener('click', () => { $('api-console').open = true; });
  ['start', 'finish'].forEach(initializeLocationSuggestions);
  initializeMap();
  const tripId = document.body.dataset.tripId;
  if (tripId) requestTrip(null, tripId);
})();
