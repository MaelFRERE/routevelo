#!/usr/bin/env python3
"""RouteVelo local proxy/server. Standard-library only; no API key required."""

from __future__ import annotations

import json
import math
import os
import hmac
import hashlib
import html
import concurrent.futures
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT_ENV = os.environ.get("PORT")
HOST = "0.0.0.0" if PORT_ENV else "127.0.0.1"
PORT = int(PORT_ENV or "8080")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
AUTH_SECRET = os.environ.get("AUTH_SECRET", "").strip() or os.urandom(32).hex()
AUTH_COOKIE = "routevelo_auth"
VALHALLA = "https://valhalla1.openstreetmap.de"
NOMINATIM = "https://nominatim.openstreetmap.org"
BUSINESS_API = "https://recherche-entreprises.api.gouv.fr"
WATER_DATASET = "osm-france-drinking-water"
WATER_DATA_APIS = [
    "https://public.opendatasoft.com/api/records/1.0/search/",
    "https://hub.huwise.com/api/records/1.0/search/",
]
OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
POI_CACHE_TTL = 900
overpass_pick_lock = threading.Lock()
overpass_pick_index = 0
USER_AGENT = "RouteVelo-MVP/1.0 (local road-cycling route planner)"
CLIENT_ID = "routevelo-mvp-local"


class RateGate:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


valhalla_gate = RateGate(1.08)
nominatim_gate = RateGate(1.08)
business_gate = RateGate(0.17)  # API Recherche d'entreprises: 7 appels/s max


def upstream_json(url: str, method: str = "GET", payload=None, headers=None, timeout: int = 45):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype and raw[:1] not in (b"{", b"["):
                raise RuntimeError(f"Réponse non JSON du service distant ({resp.status}).")
            return json.loads(raw.decode("utf-8")), resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = ""
        try:
            parsed = json.loads(raw.decode("utf-8"))
            detail = parsed.get("error") or parsed.get("message") or parsed.get("status_message") or ""
        except Exception:
            detail = raw.decode("utf-8", "replace")[:240]
        if exc.code == 429:
            raise RuntimeError("Le serveur public est momentanément limité. Réessayez dans quelques secondes.") from exc
        raise RuntimeError(detail or f"Service distant: erreur HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Impossible de joindre le service distant. Vérifiez la connexion Internet.") from exc


def post_valhalla(endpoint: str, payload):
    valhalla_gate.wait()
    return upstream_json(
        VALHALLA + endpoint,
        method="POST",
        payload=payload,
        headers={"X-Client-Id": CLIENT_ID, "User-Agent": USER_AGENT},
        timeout=60,
    )[0]


def read_json(handler: BaseHTTPRequestHandler, max_bytes: int = 2_000_000):
    try:
        size = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise ValueError("Taille de requête invalide.")
    if size <= 0 or size > max_bytes:
        raise ValueError("Requête vide ou trop volumineuse.")
    raw = handler.rfile.read(size)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("JSON invalide.") from exc




def valid_route_points(value):
    if not isinstance(value, list) or len(value) < 2 or len(value) > 180:
        return None
    out = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        try:
            lat, lng = float(item[0]), float(item[1])
        except Exception:
            return None
        if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        out.append((lat, lng))
    return out


poi_cache = {}
poi_cache_lock = threading.Lock()

def _cache_get(key):
    now = time.monotonic()
    with poi_cache_lock:
        item = poi_cache.get(key)
        if not item:
            return None
        created, data = item
        if now - created > POI_CACHE_TTL:
            poi_cache.pop(key, None)
            return None
        return data

def _cache_put(key, data):
    with poi_cache_lock:
        if len(poi_cache) > 100:
            oldest = min(poi_cache.items(), key=lambda kv: kv[1][0])[0]
            poi_cache.pop(oldest, None)
        poi_cache[key] = (time.monotonic(), data)

def _overpass_endpoints_ordered():
    global overpass_pick_index
    with overpass_pick_lock:
        start = overpass_pick_index % len(OVERPASS_ENDPOINTS)
        overpass_pick_index += 1
    return OVERPASS_ENDPOINTS[start:] + OVERPASS_ENDPOINTS[:start]


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))


def business_anchors(route, spacing_km=8.0):
    """Points le long du trace pour interroger l'annuaire sans trou entre deux requetes."""
    if not route:
        return []
    anchors = [route[0]]
    carried = 0.0
    for i in range(1, len(route)):
        a = route[i - 1]
        b = route[i]
        seg = haversine_km(a, b)
        if seg <= 0:
            continue
        remaining = seg
        start = a
        while carried + remaining >= spacing_km:
            need = spacing_km - carried
            frac = need / remaining if remaining else 1.0
            lat = start[0] + (b[0] - start[0]) * frac
            lon = start[1] + (b[1] - start[1]) * frac
            pt = (lat, lon)
            anchors.append(pt)
            start = pt
            remaining = haversine_km(start, b)
            carried = 0.0
        carried += remaining
    if haversine_km(anchors[-1], route[-1]) > 1.0:
        anchors.append(route[-1])
    # Evite une explosion de requetes sur un tres long trace.
    if len(anchors) > 24:
        step = (len(anchors) - 1) / 23
        sampled = [anchors[round(i * step)] for i in range(24)]
        anchors = sampled
    return anchors



def water_anchors(route, spacing_km=12.0):
    """Echantillonne le trace pour interroger la base nationale d'eau rapidement."""
    anchors = business_anchors(route, spacing_km=spacing_km)
    if len(anchors) > 16:
        step = (len(anchors) - 1) / 15
        anchors = [anchors[round(i * step)] for i in range(16)]
    return anchors


def _parse_water_record(record):
    if not isinstance(record, dict):
        return None
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    geometry = record.get("geometry") if isinstance(record.get("geometry"), dict) else {}
    lat = lng = None

    coords = geometry.get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except Exception:
            lat = lng = None

    if lat is None or lng is None:
        point = fields.get("meta_geo_point")
        if isinstance(point, dict):
            try:
                lat = float(point.get("lat", point.get("latitude")))
                lng = float(point.get("lon", point.get("lng", point.get("longitude"))))
            except Exception:
                lat = lng = None
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            try:
                lat, lng = float(point[0]), float(point[1])
            except Exception:
                lat = lng = None

    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None

    fee = str(fields.get("fee") or "").strip().lower()
    if fee in {"yes", "true", "paid", "pay"}:
        return None

    name = str(fields.get("name") or "Eau potable").strip() or "Eau potable"
    operator = str(fields.get("operator") or "").strip()
    description = str(fields.get("description") or "").strip()
    osm_id = str(fields.get("meta_osm_id") or record.get("recordid") or "").strip()
    osm_url = str(fields.get("meta_osm_url") or "").strip()
    commune = str(fields.get("meta_name_com") or "").strip()
    key = osm_id or f"{lat:.5f},{lng:.5f},{name.lower()}"
    return key, {
        "lat": lat,
        "lng": lng,
        "name": name,
        "operator": operator,
        "description": description,
        "osm_id": osm_id,
        "osm_url": osm_url,
        "commune": commune,
        "source": "Huwise / OpenDataSoft - données OSM",
    }


def _fetch_water_anchor(anchor):
    lat, lng = anchor
    radius_m = 6700
    last_error = None
    for endpoint in WATER_DATA_APIS:
        try:
            all_records = []
            start = 0
            for _ in range(2):
                params = {
                    "dataset": WATER_DATASET,
                    "rows": "1000",
                    "start": str(start),
                    "geofilter.distance": f"{lat:.6f},{lng:.6f},{radius_m}",
                    "fields": "name,operator,fee,description,meta_geo_point,meta_osm_id,meta_osm_url,meta_name_com",
                }
                url = endpoint + "?" + urllib.parse.urlencode(params)
                data, _ = upstream_json(url, headers={"User-Agent": USER_AGENT}, timeout=8)
                records = data.get("records") if isinstance(data, dict) else None
                if records is None and isinstance(data, dict):
                    records = data.get("results")
                if not isinstance(records, list):
                    raise RuntimeError("Réponse eau potable invalide.")
                all_records.extend(records)
                total = data.get("nhits", data.get("total_count", len(records))) if isinstance(data, dict) else len(records)
                try:
                    total = int(total)
                except Exception:
                    total = len(records)
                if start + len(records) >= total or len(records) < 1000:
                    break
                start += len(records)
            return all_records
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Base nationale d'eau potable indisponible: {last_error}")


def fetch_fast_drinking_water(route):
    """Points d'eau via la base nationale Huwise/OpenDataSoft, plus rapide qu'Overpass live."""
    anchors = water_anchors(route)
    found = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_fetch_water_anchor, anchor) for anchor in anchors]
        for fut in concurrent.futures.as_completed(futures):
            try:
                records = fut.result()
            except Exception as exc:
                errors.append(exc)
                continue
            for record in records:
                parsed = _parse_water_record(record)
                if parsed:
                    key, item = parsed
                    found[key] = item
    if not found and errors and len(errors) == len(anchors):
        raise RuntimeError(str(errors[0]))
    return {
        "waters": list(found.values()),
        "source": "Huwise / OpenDataSoft - Points d'eau potable France (données OSM)",
    }


def _pick_business_name(company, establishment):
    brands = establishment.get("liste_enseignes") or []
    if isinstance(brands, list):
        for item in brands:
            if isinstance(item, str) and item.strip():
                return item.strip()
    for key in ("nom_commercial",):
        value = establishment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("nom_complet", "nom_raison_sociale"):
        value = company.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Boulangerie"


def fetch_official_bakeries(route):
    """Boulangeries actives via l'Annuaire des Entreprises / SIRENE, sans cle API."""
    allowed_naf = {"10.71C", "47.24Z"}
    found = {}
    for lat, lng in business_anchors(route, spacing_km=8.0):
        page = 1
        max_pages = 1
        while page <= max_pages and page <= 2:
            params = {
                "lat": f"{lat:.6f}",
                "long": f"{lng:.6f}",
                "radius": "5",
                "activite_principale": "10.71C,47.24Z",
                "etat_administratif": "A",
                "minimal": "true",
                "include": "matching_etablissements",
                "limite_matching_etablissements": "100",
                "page": str(page),
                "per_page": "25",
            }
            business_gate.wait()
            url = BUSINESS_API + "/near_point?" + urllib.parse.urlencode(params)
            data, _ = upstream_json(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            try:
                max_pages = min(2, max(1, int(data.get("total_pages", 1))))
            except Exception:
                max_pages = 1
            for company in data.get("results", []) or []:
                if not isinstance(company, dict):
                    continue
                establishments = []
                matching = company.get("matching_etablissements") or []
                if isinstance(matching, list):
                    establishments.extend(x for x in matching if isinstance(x, dict))
                siege = company.get("siege")
                if isinstance(siege, dict):
                    establishments.append(siege)
                for est in establishments:
                    if est.get("etat_administratif") not in (None, "A"):
                        continue
                    naf = est.get("activite_principale") or company.get("activite_principale")
                    if naf and naf not in allowed_naf:
                        continue
                    try:
                        blat = float(est.get("latitude"))
                        blng = float(est.get("longitude"))
                    except Exception:
                        coords = est.get("coordonnees")
                        try:
                            blat, blng = map(float, str(coords).split(",", 1))
                        except Exception:
                            continue
                    if not (-90 <= blat <= 90 and -180 <= blng <= 180):
                        continue
                    siret = str(est.get("siret") or "").strip()
                    name = _pick_business_name(company, est)
                    address = str(est.get("adresse") or est.get("geo_adresse") or "").strip()
                    key = siret or f"{blat:.5f},{blng:.5f},{name.lower()}"
                    found[key] = {
                        "lat": blat,
                        "lng": blng,
                        "name": name,
                        "address": address,
                        "siret": siret,
                        "naf": naf or "",
                        "source": "Annuaire des Entreprises / SIRENE",
                    }
            page += 1
    return {"bakeries": list(found.values()), "source": "Annuaire des Entreprises / SIRENE"}


def cemetery_search_boxes(route, padding_m=700, max_boxes=8):
    """Construit quelques petites bounding boxes le long du trace.

    Overpass traite beaucoup plus vite plusieurs petites boites qu'un `around`
    contenant des dizaines/centaines de coordonnees du parcours.
    """
    if not route:
        return []

    total_km = 0.0
    for i in range(1, len(route)):
        total_km += haversine_km(route[i - 1], route[i])

    # Environ 8 a 14 km par boite, sans depasser max_boxes.
    target_chunk_km = max(8.0, min(14.0, total_km / max_boxes if total_km else 8.0))
    if total_km / target_chunk_km > max_boxes:
        target_chunk_km = max(8.0, total_km / max_boxes + 0.25)

    chunks = []
    current = [route[0]]
    carried = 0.0
    for i in range(1, len(route)):
        a, b = route[i - 1], route[i]
        current.append(b)
        carried += haversine_km(a, b)
        if carried >= target_chunk_km and len(current) >= 2:
            chunks.append(current)
            current = [b]
            carried = 0.0
    if len(current) >= 2 or not chunks:
        chunks.append(current)

    # Si le trace tres sinueux produit encore trop de boites, regroupe les
    # tranches voisines. Cela borne la taille de la requete Overpass.
    while len(chunks) > max_boxes:
        merged = []
        i = 0
        while i < len(chunks):
            if i + 1 < len(chunks):
                merged.append(chunks[i] + chunks[i + 1][1:])
                i += 2
            else:
                merged.append(chunks[i])
                i += 1
        chunks = merged

    boxes = []
    for pts in chunks:
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        mid_lat = sum(lats) / len(lats)
        lat_pad = padding_m / 110_540.0
        lon_scale = max(0.2, math.cos(math.radians(mid_lat)))
        lng_pad = padding_m / (111_320.0 * lon_scale)
        boxes.append((
            max(-90.0, min(lats) - lat_pad),
            max(-180.0, min(lngs) - lng_pad),
            min(90.0, max(lats) + lat_pad),
            min(180.0, max(lngs) + lng_pad),
        ))
    return boxes


def fetch_fast_cemeteries(route, radius):
    """Cimetieres OSM via petites bounding boxes, avec repli d'instance."""
    boxes = cemetery_search_boxes(route, padding_m=max(450, min(900, int(radius))))
    if not boxes:
        return {"elements": []}

    clauses = []
    for s, w, n, e in boxes:
        bbox = f"({s:.6f},{w:.6f},{n:.6f},{e:.6f})"
        clauses.extend([
            f'node["landuse"="cemetery"]{bbox};',
            f'way["landuse"="cemetery"]{bbox};',
            f'relation["landuse"="cemetery"]{bbox};',
            f'node["amenity"="grave_yard"]{bbox};',
            f'way["amenity"="grave_yard"]{bbox};',
            f'relation["amenity"="grave_yard"]{bbox};',
        ])

    query = "[out:json][timeout:6];(" + "".join(clauses) + ");out center tags qt;"
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error = None
    for endpoint in _overpass_endpoints_ordered():
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("Reponse Overpass invalide.")
            return data
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Recherche rapide des cimetieres indisponible: {last_error}")



def fetch_osm_route_pois(route, radius, kind):
    """POI OSM rapides via petites bounding boxes le long du trace.

    Utilise en secours/complement les donnees OSM sans envoyer un enorme
    `around` contenant tout le parcours. Le filtrage exact a 400 m reste
    effectue cote navigateur.
    """
    boxes = cemetery_search_boxes(route, padding_m=max(450, min(700, int(radius) + 80)), max_boxes=8)
    if not boxes:
        return []

    clauses = []
    for south, west, north, east in boxes:
        bbox = f"({south:.6f},{west:.6f},{north:.6f},{east:.6f})"
        if kind == "water":
            clauses.extend([
                f'nwr["amenity"="drinking_water"]{bbox};',
                f'nwr["drinking_water"="yes"]{bbox};',
            ])
        elif kind == "bakery":
            clauses.extend([
                f'nwr["shop"="bakery"]{bbox};',
            ])
        else:
            return []

    query = "[out:json][timeout:7];(" + "".join(clauses) + ");out center tags qt;"
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_error = None
    for endpoint in _overpass_endpoints_ordered():
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=9) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elements = data.get("elements") if isinstance(data, dict) else None
            if not isinstance(elements, list):
                raise RuntimeError("Reponse Overpass invalide.")
            return elements
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"POI OpenStreetMap indisponibles: {last_error}")


def _osm_element_point(el):
    try:
        lat = float(el.get("lat", (el.get("center") or {}).get("lat")))
        lng = float(el.get("lon", (el.get("center") or {}).get("lon")))
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def merge_water_sources(route, radius):
    """Fusionne base nationale et OSM ; une panne d'une source ne donne plus 0."""
    found = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {
            pool.submit(fetch_fast_drinking_water, route): "national",
            pool.submit(fetch_osm_route_pois, route, radius, "water"): "osm",
        }
        for fut in concurrent.futures.as_completed(jobs):
            source = jobs[fut]
            try:
                result = fut.result()
            except Exception as exc:
                errors.append((source, exc))
                continue
            if source == "national":
                for item in result.get("waters", []) or []:
                    try:
                        lat, lng = float(item.get("lat")), float(item.get("lng"))
                    except Exception:
                        continue
                    key = item.get("osm_id") or f"{lat:.5f},{lng:.5f}"
                    found[str(key)] = item
            else:
                for el in result:
                    pt = _osm_element_point(el)
                    if not pt:
                        continue
                    lat, lng = pt
                    tags = el.get("tags") or {}
                    if tags.get("access") == "private" or tags.get("fee") == "yes":
                        continue
                    if not (tags.get("amenity") == "drinking_water" or tags.get("drinking_water") == "yes"):
                        continue
                    key = f"osm-{el.get('type','')}-{el.get('id','')}"
                    found[key] = {
                        "lat": lat, "lng": lng,
                        "name": tags.get("name") or "Eau potable",
                        "operator": tags.get("operator") or "",
                        "description": tags.get("description") or "",
                        "osm_id": str(el.get("id") or ""),
                        "source": "OpenStreetMap / Overpass",
                    }
    if not found and len(errors) == 2:
        raise RuntimeError("Sources d'eau potable indisponibles.")
    return {"waters": list(found.values()), "source": "Base nationale + OpenStreetMap"}


def merge_bakery_sources(route, radius):
    """Fusionne SIRENE et OSM pour eviter les faux 0 boulangerie."""
    found = {}
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {
            pool.submit(fetch_official_bakeries, route): "sirene",
            pool.submit(fetch_osm_route_pois, route, radius, "bakery"): "osm",
        }
        for fut in concurrent.futures.as_completed(jobs):
            source = jobs[fut]
            try:
                result = fut.result()
            except Exception as exc:
                errors.append((source, exc))
                continue
            if source == "sirene":
                for item in result.get("bakeries", []) or []:
                    try:
                        lat, lng = float(item.get("lat")), float(item.get("lng"))
                    except Exception:
                        continue
                    key = item.get("siret") or f"sirene-{lat:.5f},{lng:.5f}"
                    found[str(key)] = item
            else:
                for el in result:
                    pt = _osm_element_point(el)
                    if not pt:
                        continue
                    lat, lng = pt
                    tags = el.get("tags") or {}
                    if tags.get("shop") != "bakery":
                        continue
                    key = f"osm-{el.get('type','')}-{el.get('id','')}"
                    address_parts = [
                        tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
                        tags.get("addr:postcode", ""), tags.get("addr:city", "")
                    ]
                    found[key] = {
                        "lat": lat, "lng": lng,
                        "name": tags.get("name") or tags.get("brand") or "Boulangerie",
                        "address": " ".join(x for x in address_parts if x).strip(),
                        "siret": "", "naf": "",
                        "source": "OpenStreetMap / Overpass",
                    }
    if not found and len(errors) == 2:
        raise RuntimeError("Sources de boulangeries indisponibles.")
    return {"bakeries": list(found.values()), "source": "SIRENE + OpenStreetMap"}

def valid_bbox(value):
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        s, w, n, e = map(float, value)
    except Exception:
        return None
    if not all(math.isfinite(v) for v in (s, w, n, e)):
        return None
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        return None
    if n - s > 5 or e - w > 7:
        return None
    return s, w, n, e


class Handler(BaseHTTPRequestHandler):
    server_version = "RouteVelo/1.1"

    def _auth_token(self):
        return hmac.new(AUTH_SECRET.encode("utf-8"), b"routevelo-auth-v1", hashlib.sha256).hexdigest()

    def _is_authenticated(self):
        if not APP_PASSWORD:
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == AUTH_COOKIE:
                return hmac.compare_digest(value, self._auth_token())
        return False

    def _login_page(self, error=""):
        message = f'<div class="error">{html.escape(error)}</div>' if error else ''
        page = f'''<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Connexion - RouteVelo</title><style>
:root{{--bg:#f4f7f7;--text:#162421;--muted:#63736f;--line:#dce5e2;--accent:#126b58;--soft:#eaf4f1}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;padding:20px;background:var(--bg);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text)}}
.card{{width:min(100%,390px);background:#fff;border:1px solid var(--line);border-radius:22px;padding:26px;box-shadow:0 16px 44px rgba(24,56,49,.12)}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:24px}}.icon{{width:48px;height:48px;display:grid;place-items:center;background:var(--soft);border-radius:15px;font-size:26px}}
h1{{font-size:22px;margin:0}}p{{margin:5px 0 0;color:var(--muted);font-size:13px}}label{{display:block;font-size:13px;font-weight:700;margin-bottom:7px}}
input{{width:100%;min-height:50px;border:1px solid var(--line);border-radius:13px;padding:11px 13px;font:inherit;font-size:16px;outline:none}}input:focus{{border-color:#7db5a8;box-shadow:0 0 0 3px rgba(18,107,88,.1)}}
button{{width:100%;min-height:50px;margin-top:12px;border:0;border-radius:13px;background:var(--accent);color:#fff;font:inherit;font-weight:800;cursor:pointer}}
.error{{margin:0 0 14px;padding:10px 12px;border-radius:11px;background:#fff0f0;color:#9e2e2e;font-size:13px}}
</style></head><body><main class="card"><div class="brand"><div class="icon">🚴</div><div><h1>RouteVelo</h1><p>Accès privé</p></div></div>{message}
<form method="post" action="/login"><label for="password">Mot de passe</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit">Ouvrir RouteVelo</button></form></main></body></html>'''
        raw = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _handle_login(self):
        try:
            size = min(int(self.headers.get("Content-Length", "0")), 4096)
        except ValueError:
            size = 0
        raw = self.rfile.read(size).decode("utf-8", "replace") if size else ""
        password = (urllib.parse.parse_qs(raw).get("password") or [""])[0]
        if APP_PASSWORD and hmac.compare_digest(password, APP_PASSWORD):
            token = self._auth_token()
            secure = bool(os.environ.get("RENDER")) or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            cookie = f"{AUTH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
            if secure:
                cookie += "; Secure"
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self._login_page("Mot de passe incorrect.")

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_error_json(self, message, status=500):
        self.send_json({"error": str(message)}, status=status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json({"status": "ok"})
        if parsed.path == "/login":
            if self._is_authenticated():
                return self._redirect("/")
            return self._login_page()
        if parsed.path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
            return
        if not self._is_authenticated():
            return self._redirect("/login")
        if parsed.path == "/api/geocode":
            return self.handle_geocode(parsed)
        if parsed.path.startswith("/api/"):
            return self.send_error_json("Endpoint introuvable.", 404)
        return self.serve_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            return self._handle_login()
        if not self._is_authenticated():
            return self.send_error_json("Authentification requise.", 401)
        try:
            if parsed.path == "/api/route":
                payload = read_json(self)
                return self.send_json(post_valhalla("/route", payload))
            if parsed.path == "/api/trace":
                payload = read_json(self)
                return self.send_json(post_valhalla("/trace_attributes", payload))
            if parsed.path == "/api/height":
                payload = read_json(self)
                return self.send_json(post_valhalla("/height", payload))
            if parsed.path == "/api/pois":
                return self.handle_pois()
            return self.send_error_json("Endpoint introuvable.", 404)
        except ValueError as exc:
            return self.send_error_json(exc, 400)
        except RuntimeError as exc:
            return self.send_error_json(exc, 502)
        except Exception as exc:
            return self.send_error_json(f"Erreur interne: {exc}", 500)

    def handle_geocode(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        q = (params.get("q") or [""])[0].strip()
        if not q:
            return self.send_error_json("Recherche vide.", 400)
        if len(q) > 220:
            return self.send_error_json("Recherche trop longue.", 400)
        try:
            nominatim_gate.wait()
            query = urllib.parse.urlencode({
                "q": q,
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 1,
                "accept-language": "fr",
            })
            data, _ = upstream_json(
                NOMINATIM + "/search?" + query,
                headers={"User-Agent": USER_AGENT, "Referer": f"http://{HOST}:{self.server.server_port}/"},
                timeout=30,
            )
            if not data:
                return self.send_error_json("Adresse introuvable.", 404)
            hit = data[0]
            return self.send_json({
                "lat": float(hit["lat"]),
                "lng": float(hit["lon"]),
                "label": hit.get("display_name") or q,
            })
        except RuntimeError as exc:
            return self.send_error_json(exc, 502)
        except Exception:
            return self.send_error_json("La recherche d’adresse a échoué.", 502)

    def handle_pois(self):
        payload = read_json(self, 300_000)
        if not isinstance(payload, dict):
            return self.send_error_json("Requête POI invalide.", 400)

        route = valid_route_points(payload.get("route"))
        bbox = valid_bbox(payload.get("bbox"))
        kind = str(payload.get("kind") or "all").strip().lower()
        if kind not in {"water", "core", "cemetery", "bakery", "all"}:
            return self.send_error_json("Type de POI invalide.", 400)

        radius = payload.get("radius", 500)
        try:
            radius = int(radius)
        except Exception:
            radius = 500
        radius = max(250, min(900, radius))

        if kind == "water":
            if not route:
                return self.send_error_json("Le trace est requis pour rechercher l'eau potable.", 400)
            cache_key = (
                "hybrid-water-v1",
                tuple((round(lat, 4), round(lng, 4)) for lat, lng in route),
            )
            cached = _cache_get(cache_key)
            if cached is not None:
                return self.send_json(cached)
            try:
                data = merge_water_sources(route, radius)
                _cache_put(cache_key, data)
                return self.send_json(data)
            except RuntimeError as exc:
                print(f"[water hybrid] {exc}")

        if kind == "bakery":
            if not route:
                return self.send_error_json("Le trace est requis pour rechercher les boulangeries.", 400)
            cache_key = (
                "hybrid-bakery-v1",
                tuple((round(lat, 4), round(lng, 4)) for lat, lng in route),
            )
            cached = _cache_get(cache_key)
            if cached is not None:
                return self.send_json(cached)
            try:
                data = merge_bakery_sources(route, radius)
            except RuntimeError as exc:
                return self.send_error_json(f"Boulangeries indisponibles: {exc}", 502)
            _cache_put(cache_key, data)
            return self.send_json(data)

        if kind == "cemetery":
            if not route:
                return self.send_error_json("Le trace est requis pour rechercher les cimetieres.", 400)
            cache_key = (
                "fast-cemetery-v2", radius,
                tuple((round(lat, 4), round(lng, 4)) for lat, lng in route),
            )
            cached = _cache_get(cache_key)
            if cached is not None:
                return self.send_json(cached)
            try:
                data = fetch_fast_cemeteries(route, radius)
            except RuntimeError as exc:
                return self.send_error_json(str(exc), 502)
            _cache_put(cache_key, data)
            return self.send_json(data)

        if route:
            line = ",".join(f"{lat:.5f},{lng:.5f}" for lat, lng in route)
            area_filter = f"(around:{radius},{line})"
            cache_key = (
                "route", kind, radius,
                tuple((round(lat, 4), round(lng, 4)) for lat, lng in route),
            )
        elif bbox:
            s, w, n, e = bbox
            area_filter = f"({s:.6f},{w:.6f},{n:.6f},{e:.6f})"
            cache_key = ("bbox", kind, round(s, 4), round(w, 4), round(n, 4), round(e, 4))
        else:
            return self.send_error_json("Tracé ou zone de recherche invalide.", 400)

        cached = _cache_get(cache_key)
        if cached is not None:
            return self.send_json(cached)

        clauses = []
        if kind in {"water", "core", "all"}:
            # Eau potable uniquement via OpenStreetMap. Les boulangeries viennent
            # de l'Annuaire des Entreprises / SIRENE pour une meilleure couverture en France.
            clauses.extend([
                f'node["amenity"="drinking_water"]{area_filter};',
                f'node["drinking_water"="yes"]{area_filter};',
            ])
        if kind in {"cemetery", "all"}:
            clauses.extend([
                f'node["landuse"="cemetery"]{area_filter};',
                f'way["landuse"="cemetery"]{area_filter};',
                f'node["amenity"="grave_yard"]{area_filter};',
                f'way["amenity"="grave_yard"]{area_filter};',
            ])

        query_timeout = 7 if kind in {"water", "core"} else 9
        query = f"[out:json][timeout:{query_timeout}];(" + "".join(clauses) + ");out center tags qt;"
        body = urllib.parse.urlencode({"data": query}).encode("utf-8")

        last_error = None
        # Alterne le serveur prioritaire pour répartir la charge. En cas d'échec,
        # bascule rapidement sur le second au lieu d'attendre de longues minutes.
        request_timeout = 10 if kind in {"water", "core"} else 12
        for endpoint in _overpass_endpoints_ordered():
            req = urllib.request.Request(
                endpoint,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    raise RuntimeError("Réponse Overpass invalide.")
                _cache_put(cache_key, data)
                return self.send_json(data)
            except Exception as exc:
                last_error = exc
                continue

        return self.send_error_json(
            "Les points d’intérêt OpenStreetMap sont momentanément indisponibles. Réessayez dans quelques secondes.",
            502,
        )

    def serve_static(self, url_path):
        rel = "index.html" if url_path in ("", "/") else urllib.parse.unquote(url_path.lstrip("/"))
        candidate = (ROOT / rel).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            return self.send_error_json("Chemin interdit.", 403)
        if not candidate.is_file():
            return self.send_error_json("Fichier introuvable.", 404)
        raw = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass


def _make_server():
    if PORT_ENV:
        return ThreadingHTTPServer((HOST, PORT), Handler)
    last_error = None
    for port in range(PORT, PORT + 20):
        try:
            return ThreadingHTTPServer((HOST, port), Handler)
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Aucun port disponible entre {PORT} et {PORT + 19}: {last_error}")


def main():
    try:
        server = _make_server()
    except RuntimeError as exc:
        print(f"Impossible de démarrer RouteVelo: {exc}")
        input("Appuyez sur Entrée pour fermer...")
        return

    actual_port = server.server_port
    url = f"http://{HOST}:{actual_port}"
    print(f"RouteVelo démarré sur {url}")
    if actual_port != PORT:
        print(f"Le port {PORT} était occupé; utilisation automatique du port {actual_port}.")
    print("Protection par mot de passe activée." if APP_PASSWORD else "Protection par mot de passe désactivée (APP_PASSWORD non défini).")

    opener = None
    if not PORT_ENV:
        print("Le navigateur va s'ouvrir automatiquement.")
        opener = threading.Timer(0.7, _open_browser, args=(url,))
        opener.daemon = True
        opener.start()
    print("Appuyez sur Ctrl+C pour arrêter le serveur.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt de RouteVelo.")
    finally:
        if opener:
            opener.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
