#!/usr/bin/env python3
"""RouteVelo local proxy/server. Standard-library only; no API key required."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import unicodedata
import hmac
import hashlib
import gzip
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
IGN_GEOCODER = "https://data.geopf.fr/geocodage"
WATER_DATASET = "osm-france-drinking-water"
HUWISE_DATASETS = {
    "water": "osm-france-drinking-water",
    "bakery": "osm-france-shop-craft-office",
}
HUWISE_API_BASES = [
    "https://hub.huwise.com/api/explore/v2.1/catalog/datasets",
    "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets",
]
OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
POI_CACHE_TTL = 900
# V40: cache spatial persistant des POI. Le fichier est cree automatiquement
# sur le serveur et peut etre deplace vers un disque persistant via POI_DB_PATH.
POI_DB_PATH = os.environ.get("POI_DB_PATH", str(ROOT / "routevelo_pois.sqlite3"))
POI_CELL_DEG = 0.02
POI_LOCAL_QUERY_RADIUS_M = 650
POI_COVERAGE_RADIUS_M = {"water": 1200, "bakery": 1200, "cemetery": 850}
POI_LOCAL_TTL = {"water": 30 * 86400, "bakery": 10 * 86400, "cemetery": 30 * 86400}
POI_STALE_RETENTION = 180 * 86400
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


class LocalPoiStore:
    """Petit cache spatial SQLite pour servir les POI sans requete externe.

    Les resultats distants restent la source de verite. Une fois une zone chargee,
    les itineraires suivants qui passent dans la meme zone sont servis depuis le
    disque local en quelques millisecondes. Les zones expirent selon le type de POI
    et sont rafraichies sans bloquer quand une copie locale existe deja.
    """

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=8)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pois (
                    kind TEXT NOT NULL,
                    poi_id TEXT NOT NULL,
                    cell TEXT NOT NULL,
                    lat REAL NOT NULL,
                    lng REAL NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (kind, poi_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pois_kind_cell ON pois(kind, cell);
                CREATE INDEX IF NOT EXISTS idx_pois_updated ON pois(updated_at);
                CREATE TABLE IF NOT EXISTS poi_coverage (
                    kind TEXT NOT NULL,
                    cell TEXT NOT NULL,
                    refreshed_at INTEGER NOT NULL,
                    PRIMARY KEY (kind, cell)
                );
                CREATE INDEX IF NOT EXISTS idx_coverage_refreshed ON poi_coverage(refreshed_at);
                """
            )
            cutoff = int(time.time()) - POI_STALE_RETENTION
            self._db.execute("DELETE FROM pois WHERE updated_at < ?", (cutoff,))
            self._db.execute("DELETE FROM poi_coverage WHERE refreshed_at < ?", (cutoff,))
            self._db.commit()

    @staticmethod
    def _cell_index(lat, lng):
        return math.floor(float(lat) / POI_CELL_DEG), math.floor(float(lng) / POI_CELL_DEG)

    @classmethod
    def _cell_token(cls, lat, lng):
        a, b = cls._cell_index(lat, lng)
        return f"{a}:{b}"

    @staticmethod
    def _sample_route(route, spacing_km=0.75, max_points=700):
        if not route:
            return []
        points = [route[0]]
        for i in range(1, len(route)):
            a, b = route[i - 1], route[i]
            seg_km = haversine_km(a, b)
            pieces = max(1, int(math.ceil(seg_km / spacing_km)))
            for j in range(1, pieces + 1):
                f = j / pieces
                points.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
        if len(points) > max_points:
            step = (len(points) - 1) / (max_points - 1)
            points = [points[round(i * step)] for i in range(max_points)]
        return points

    @classmethod
    def route_cells(cls, route, radius_m=POI_LOCAL_QUERY_RADIUS_M):
        cells = set()
        for lat, lng in cls._sample_route(route):
            lat_pad = radius_m / 110_540.0
            lon_pad = radius_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
            min_i = math.floor((lat - lat_pad) / POI_CELL_DEG)
            max_i = math.floor((lat + lat_pad) / POI_CELL_DEG)
            min_j = math.floor((lng - lon_pad) / POI_CELL_DEG)
            max_j = math.floor((lng + lon_pad) / POI_CELL_DEG)
            for i in range(min_i, max_i + 1):
                for j in range(min_j, max_j + 1):
                    cells.add(f"{i}:{j}")
        return cells

    @staticmethod
    def _chunks(values, size=400):
        values = list(values)
        for i in range(0, len(values), size):
            yield values[i:i + size]

    def coverage_state(self, kind, route):
        cells = self.route_cells(route, POI_LOCAL_QUERY_RADIUS_M)
        if not cells:
            return "missing", cells
        timestamps = {}
        with self._lock:
            for chunk in self._chunks(cells):
                marks = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    f"SELECT cell, refreshed_at FROM poi_coverage WHERE kind=? AND cell IN ({marks})",
                    [kind, *chunk],
                ).fetchall()
                timestamps.update((cell, int(ts)) for cell, ts in rows)
        if len(timestamps) != len(cells):
            return "missing", cells
        threshold = int(time.time()) - int(POI_LOCAL_TTL.get(kind, 7 * 86400))
        if all(ts >= threshold for ts in timestamps.values()):
            return "fresh", cells
        return "stale", cells

    def query_route(self, kind, route):
        cells = self.route_cells(route, POI_LOCAL_QUERY_RADIUS_M)
        if not cells:
            return []
        by_id = {}
        with self._lock:
            for chunk in self._chunks(cells):
                marks = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    f"SELECT poi_id, payload FROM pois WHERE kind=? AND cell IN ({marks})",
                    [kind, *chunk],
                ).fetchall()
                for poi_id, payload in rows:
                    try:
                        item = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(item, dict):
                        by_id[poi_id] = item
        return list(by_id.values())

    @staticmethod
    def _poi_id(kind, item):
        stable = str(item.get("siret") or item.get("osm_id") or item.get("id") or "").strip()
        if stable:
            return stable
        raw = "|".join([
            kind,
            f"{float(item.get('lat', 0)):.6f}",
            f"{float(item.get('lng', 0)):.6f}",
            _clean_label(item.get("name")),
            str(item.get("source") or ""),
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def put_items(self, kind, items, route):
        now = int(time.time())
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                lat, lng = float(item["lat"]), float(item["lng"])
            except Exception:
                continue
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            clean = dict(item)
            clean["lat"] = lat
            clean["lng"] = lng
            rows.append((
                kind,
                self._poi_id(kind, clean),
                self._cell_token(lat, lng),
                lat,
                lng,
                json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                now,
            ))
        coverage_radius = int(POI_COVERAGE_RADIUS_M.get(kind, 850))
        coverage = self.route_cells(route, coverage_radius)
        with self._lock:
            if rows:
                self._db.executemany(
                    """
                    INSERT INTO pois(kind, poi_id, cell, lat, lng, payload, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(kind, poi_id) DO UPDATE SET
                      cell=excluded.cell, lat=excluded.lat, lng=excluded.lng,
                      payload=excluded.payload, updated_at=excluded.updated_at
                    """,
                    rows,
                )
            if coverage:
                self._db.executemany(
                    """
                    INSERT INTO poi_coverage(kind, cell, refreshed_at) VALUES(?,?,?)
                    ON CONFLICT(kind, cell) DO UPDATE SET refreshed_at=excluded.refreshed_at
                    """,
                    [(kind, cell, now) for cell in coverage],
                )
            self._db.commit()
        return len(rows)

    def stats(self):
        with self._lock:
            counts = dict(self._db.execute("SELECT kind, COUNT(*) FROM pois GROUP BY kind").fetchall())
            zones = dict(self._db.execute("SELECT kind, COUNT(*) FROM poi_coverage GROUP BY kind").fetchall())
        return {"points": counts, "cells": zones}


try:
    local_poi_store = LocalPoiStore(POI_DB_PATH)
except Exception as exc:
    print(f"[poi local] impossible d'ouvrir {POI_DB_PATH}: {exc}; cache memoire de secours")
    local_poi_store = LocalPoiStore(":memory:")

poi_refresh_lock = threading.Lock()
poi_refreshing = set()


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
        if len(poi_cache) > 160:
            oldest = min(poi_cache.items(), key=lambda kv: kv[1][0])[0]
            poi_cache.pop(oldest, None)
        poi_cache[key] = (time.monotonic(), data)


def _payload_cache_key(prefix, payload):
    try:
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return None
    return (prefix, hashlib.sha256(normalized.encode("utf-8")).hexdigest())

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



def water_anchors(route, spacing_km=6.0):
    """Echantillonne le trace pour couvrir tout le corridor avec de petites requetes geo."""
    anchors = business_anchors(route, spacing_km=spacing_km)
    if len(anchors) > 18:
        step = (len(anchors) - 1) / 17
        anchors = [anchors[round(i * step)] for i in range(18)]
    return anchors


def _parse_geo_point(value):
    """Accepte les deux formes renvoyees par les API Huwise/ODS."""
    lat = lng = None
    if isinstance(value, dict):
        try:
            lat = float(value.get("lat", value.get("latitude")))
            lng = float(value.get("lon", value.get("lng", value.get("longitude"))))
        except Exception:
            lat = lng = None
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            # Les geopoints ODS historiques sont [latitude, longitude].
            lat, lng = float(value[0]), float(value[1])
        except Exception:
            lat = lng = None
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _huwise_records_near_anchor(dataset, anchor, radius_km, extra_where="", max_pages=2):
    """Interroge l'Explore API v2.1 actuelle de Huwise/OpenDataSoft.

    L'ancienne API records/1.0 utilisee par les versions precedentes pouvait
    renvoyer une liste vide sur Render. Cette version utilise le point de
    terminaison v2.1 documente et garde un second domaine en secours.
    """
    lat, lng = anchor
    geo_where = (
        f"within_distance(meta_geo_point, GEOM'POINT({lng:.6f} {lat:.6f})', "
        f"{float(radius_km):.2f} km)"
    )
    where = f"({extra_where}) and {geo_where}" if extra_where else geo_where
    last_error = None
    for base in HUWISE_API_BASES:
        try:
            out = []
            offset = 0
            for _ in range(max_pages):
                params = {
                    "where": where,
                    "limit": "100",
                    "offset": str(offset),
                    "lang": "fr",
                }
                url = f"{base}/{urllib.parse.quote(dataset)}/records?" + urllib.parse.urlencode(params)
                data, _ = upstream_json(url, headers={"User-Agent": USER_AGENT}, timeout=10)
                results = data.get("results") if isinstance(data, dict) else None
                if not isinstance(results, list):
                    raise RuntimeError("Reponse Huwise invalide.")
                out.extend(x for x in results if isinstance(x, dict))
                try:
                    total = int(data.get("total_count", len(results)))
                except Exception:
                    total = len(results)
                if len(results) < 100 or offset + len(results) >= total:
                    break
                offset += len(results)
            return out
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Huwise/OpenDataSoft indisponible: {last_error}")


def _parse_water_record(record):
    if not isinstance(record, dict):
        return None
    # API v2.1: champs directement dans l'objet. API v1: sous 'fields'.
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    point = _parse_geo_point(fields.get("meta_geo_point"))
    if point is None:
        geometry = record.get("geometry") if isinstance(record.get("geometry"), dict) else {}
        coords = geometry.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                point = (float(coords[1]), float(coords[0]))
            except Exception:
                point = None
    if point is None:
        return None
    lat, lng = point
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
        "source": "Huwise / OpenDataSoft - donnees OSM",
    }


def fetch_fast_drinking_water(route):
    """Points d'eau via l'Explore API Huwise v2.1, sans cle API."""
    found = {}
    errors = []
    anchors = water_anchors(route, spacing_km=6.0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(
                _huwise_records_near_anchor,
                HUWISE_DATASETS["water"], anchor, 3.6, "", 2
            )
            for anchor in anchors
        ]
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
        "source": "Huwise Explore API v2.1 - Points d'eau potable France",
    }


def _parse_huwise_bakery(record):
    if not isinstance(record, dict):
        return None
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    item_type = str(fields.get("type") or "").strip().lower()
    if item_type and item_type != "bakery":
        return None
    point = _parse_geo_point(fields.get("meta_geo_point"))
    if point is None:
        return None
    lat, lng = point
    name = str(fields.get("name") or fields.get("brand") or "Boulangerie").strip() or "Boulangerie"
    siret = str(fields.get("siret") or "").strip()
    osm_id = str(fields.get("meta_osm_id") or "").strip()
    commune = str(fields.get("meta_name_com") or "").strip()
    key = osm_id or siret or f"{lat:.5f},{lng:.5f},{name.lower()}"
    return key, {
        "lat": lat,
        "lng": lng,
        "name": name,
        "address": commune,
        "siret": siret,
        "naf": "",
        "osm_id": osm_id,
        "source": "Huwise / OpenDataSoft - donnees OSM",
    }


def fetch_huwise_bakeries(route):
    """Boulangeries OSM pre-calculees via Huwise, plus stables que Overpass live."""
    found = {}
    errors = []
    anchors = business_anchors(route, spacing_km=5.5)
    if len(anchors) > 20:
        step = (len(anchors) - 1) / 19
        anchors = [anchors[round(i * step)] for i in range(20)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(
                _huwise_records_near_anchor,
                HUWISE_DATASETS["bakery"], anchor, 3.3, 'type = "bakery"', 2
            )
            for anchor in anchors
        ]
        for fut in concurrent.futures.as_completed(futures):
            try:
                records = fut.result()
            except Exception as exc:
                errors.append(exc)
                continue
            for record in records:
                parsed = _parse_huwise_bakery(record)
                if parsed:
                    key, item = parsed
                    found[key] = item
    if not found and errors and len(errors) == len(anchors):
        raise RuntimeError(str(errors[0]))
    return {"bakeries": list(found.values()), "source": "Huwise Explore API v2.1 - commerces OSM"}


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
    """Boulangeries actives via l'Annuaire des Entreprises / SIRENE, sans cle API.

    Les deux codes NAF sont interroges separement: certaines versions de l'API
    n'interpretent pas une valeur separee par virgule comme un OU pour ce filtre.
    """
    allowed_naf = ("10.71C", "47.24Z")
    found = {}
    errors = []
    anchors = business_anchors(route, spacing_km=7.0)

    def query_anchor_naf(anchor, naf):
        lat, lng = anchor
        rows = []
        page = 1
        max_pages = 1
        while page <= max_pages and page <= 2:
            params = {
                "lat": f"{lat:.6f}",
                "long": f"{lng:.6f}",
                "radius": "4.2",
                "activite_principale": naf,
                "etat_administratif": "A",
                "minimal": "true",
                "include": "matching_etablissements",
                "limite_matching_etablissements": "100",
                "page": str(page),
                "per_page": "25",
            }
            business_gate.wait()
            url = BUSINESS_API + "/near_point?" + urllib.parse.urlencode(params)
            data, _ = upstream_json(url, headers={"User-Agent": USER_AGENT}, timeout=12)
            try:
                max_pages = min(2, max(1, int(data.get("total_pages", 1))))
            except Exception:
                max_pages = 1
            rows.extend(data.get("results", []) or [])
            page += 1
        return rows

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for anchor in anchors:
            for naf in allowed_naf:
                tasks.append(pool.submit(query_anchor_naf, anchor, naf))
        for fut in concurrent.futures.as_completed(tasks):
            try:
                companies = fut.result()
            except Exception as exc:
                errors.append(exc)
                continue
            for company in companies:
                if not isinstance(company, dict):
                    continue
                matching = company.get("matching_etablissements") or []
                if not isinstance(matching, list):
                    continue
                for est in matching:
                    if not isinstance(est, dict) or est.get("etat_administratif") not in (None, "A"):
                        continue
                    naf = est.get("activite_principale") or company.get("activite_principale")
                    if naf not in allowed_naf:
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
    if not found and errors and len(errors) == len(tasks):
        raise RuntimeError(str(errors[0]))
    return {"bakeries": list(found.values()), "source": "Annuaire des Entreprises / SIRENE"}



def _clean_label(value):
    text = str(value or "").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _distance_m(a, b):
    return haversine_km((float(a["lat"]), float(a["lng"])), (float(b["lat"]), float(b["lng"]))) * 1000.0


def _dedupe_businesses(items, max_distance_m=65):
    """Fusionne les doublons SIRENE/OSM sans fusionner deux commerces distincts proches."""
    out = []
    by_id = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        stable_id = str(item.get("siret") or item.get("osm_id") or item.get("id") or "").strip()
        if stable_id and stable_id in by_id:
            existing = by_id[stable_id]
            for k in ("address", "siret", "naf", "osm_id"):
                if not existing.get(k) and item.get(k):
                    existing[k] = item[k]
            continue
        name = _clean_label(item.get("name"))
        duplicate = None
        for existing in out:
            try:
                if _distance_m(item, existing) > max_distance_m:
                    continue
            except Exception:
                continue
            other = _clean_label(existing.get("name"))
            generic = {"", "boulangerie", "boulangerie patisserie", "bakery"}
            try:
                distance = _distance_m(item, existing)
            except Exception:
                continue
            if name in generic or other in generic:
                same_name = distance <= 20
            elif name == other:
                same_name = distance <= max_distance_m
            else:
                same_name = bool(name and other and (name in other or other in name) and distance <= 45)
            if same_name:
                duplicate = existing
                break
        if duplicate is not None:
            for k in ("address", "siret", "naf", "osm_id"):
                if not duplicate.get(k) and item.get(k):
                    duplicate[k] = item[k]
            sources = [x for x in str(duplicate.get("source") or "").split(" + ") if x]
            src = str(item.get("source") or "").strip()
            if src and src not in sources:
                duplicate["source"] = " + ".join(sources + [src])
            continue
        out.append(dict(item))
        if stable_id:
            by_id[stable_id] = out[-1]
    return out


def _dedupe_water_items(items, max_distance_m=30):
    """Déduplique les points d'eau; une source confirmée prime sur une source potentielle."""
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        duplicate = None
        for existing in out:
            try:
                if _distance_m(item, existing) <= max_distance_m:
                    duplicate = existing
                    break
            except Exception:
                continue
        if duplicate is None:
            out.append(dict(item))
            continue
        old_confirmed = duplicate.get("potable") is not False
        new_confirmed = item.get("potable") is not False
        if new_confirmed and not old_confirmed:
            keep = dict(item)
            if not keep.get("description") and duplicate.get("description"):
                keep["description"] = duplicate["description"]
            out[out.index(duplicate)] = keep
        else:
            src = str(item.get("source") or "").strip()
            if src and src not in str(duplicate.get("source") or ""):
                duplicate["source"] = " + ".join(x for x in [duplicate.get("source", ""), src] if x)
    return out


def fetch_ign_route_pois(route, categories, radius_m=1800, spacing_km=3.0, limit=50):
    """POI BD TOPO via le geocodeur IGN, public et sans cle.

    Chaque categorie est interrogee separement sur des cercles chevauchants.
    Cela evite qu'une API/instance interprete mal un filtre multi-categories et
    assure une couverture continue du trajet. Le navigateur garde ensuite
    uniquement les points situes a 400 m du trace exact.
    """
    anchors = business_anchors(route, spacing_km=spacing_km)
    if not anchors:
        return []
    if isinstance(categories, (list, tuple, set)):
        category_values = [str(x).strip() for x in categories if str(x).strip()]
    else:
        category_values = [str(categories).strip()]
    found = {}
    errors = []

    def one(anchor, category):
        lat, lng = anchor
        searchgeom = json.dumps({
            "type": "Circle",
            "coordinates": [round(lng, 7), round(lat, 7)],
            "radius": int(radius_m),
        }, separators=(",", ":"), ensure_ascii=False)
        params = {
            "index": "poi",
            "searchgeom": searchgeom,
            "lon": f"{lng:.7f}",
            "lat": f"{lat:.7f}",
            "limit": str(max(1, min(50, int(limit)))),
            "category": category,
        }
        url = IGN_GEOCODER + "/reverse?" + urllib.parse.urlencode(params)
        data, _ = upstream_json(url, headers={"User-Agent": USER_AGENT}, timeout=8)
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            raise RuntimeError("Reponse IGN POI invalide.")
        return features

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, anchor, category) for anchor in anchors for category in category_values]
        for fut in concurrent.futures.as_completed(futures):
            try:
                features = fut.result()
            except Exception as exc:
                errors.append(exc)
                continue
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                geometry = feature.get("geometry") or {}
                coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
                if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                    continue
                try:
                    lng, lat = float(coords[0]), float(coords[1])
                except Exception:
                    continue
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    continue
                props = feature.get("properties") or {}
                if not isinstance(props, dict):
                    props = {}
                cats = props.get("category") or []
                if isinstance(cats, str):
                    cats = [cats]
                name = props.get("toponym")
                if not name:
                    names = props.get("name") or []
                    if isinstance(names, list) and names:
                        name = names[0]
                    elif isinstance(names, str):
                        name = names
                extra = props.get("extrafields") or {}
                cleabs = str(extra.get("cleabs") or "") if isinstance(extra, dict) else ""
                key = cleabs or f"ign-{lat:.5f}-{lng:.5f}-{_clean_label(name)}"
                found[key] = {
                    "id": key,
                    "lat": lat,
                    "lng": lng,
                    "name": str(name or "Point d'interet").strip(),
                    "categories": [str(x) for x in cats],
                    "city": ", ".join(str(x) for x in (props.get("city") or []) if x) if isinstance(props.get("city"), list) else str(props.get("city") or ""),
                    "source": "IGN BD TOPO / Geoplateforme",
                }
    if not found and errors and len(errors) == len(futures):
        raise RuntimeError(str(errors[0]))
    return list(found.values())


def fetch_ign_cemeteries(route):
    items = fetch_ign_route_pois(route, ["cimetière"], radius_m=1850, spacing_km=3.0, limit=50)
    elements = []
    for item in items:
        elements.append({
            "type": "node",
            "id": item.get("id"),
            "lat": item["lat"],
            "lon": item["lng"],
            "tags": {
                "landuse": "cemetery",
                "name": item.get("name") or "Cimetière",
                "source": item.get("source") or "IGN BD TOPO / Géoplateforme",
            },
        })
    return {"elements": elements, "source": "IGN BD TOPO / Géoplateforme"}


def fetch_ign_water_candidates(route):
    # Ces objets ne garantissent pas la potabilité. Ils sont affichés comme eau potentielle.
    items = fetch_ign_route_pois(
        route,
        ["fontaine", "point d'eau", "source captée", "lavoir"],
        radius_m=1850,
        spacing_km=3.0,
        limit=50,
    )
    out = []
    for item in items:
        cats = [str(x).casefold() for x in item.get("categories") or []]
        detail = next((x for x in ("fontaine", "point d'eau", "source captée", "lavoir") if x in cats), "point d'eau")
        out.append({
            "lat": item["lat"],
            "lng": item["lng"],
            "name": item.get("name") or detail.capitalize(),
            "operator": "",
            "description": f"{detail.capitalize()} : potabilité à vérifier sur place.",
            "source": item.get("source") or "IGN BD TOPO / Géoplateforme",
            "potable": False,
            "water_kind": detail,
        })
    return out

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
    """Cimetieres OSM via petites bounding boxes et plusieurs instances publiques."""
    boxes = cemetery_search_boxes(route, padding_m=max(450, min(900, int(radius))), max_boxes=8)
    if not boxes:
        return {"elements": []}

    # nwr remplace 3 requetes node/way/relation et reduit fortement la requete.
    clauses = []
    for s, w, n, e in boxes:
        bbox = f"({s:.6f},{w:.6f},{n:.6f},{e:.6f})"
        clauses.extend([
            f'nwr["landuse"="cemetery"]{bbox};',
            f'nwr["amenity"="grave_yard"]{bbox};',
        ])

    query = "[out:json][timeout:8];(" + "".join(clauses) + ");out center tags qt;"
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
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                raise RuntimeError("Reponse Overpass invalide.")
            return data
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Recherche des cimetieres indisponible: {last_error}")


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
                f'nwr["amenity"="water_point"]{bbox};',
                f'nwr["drinking_water"~"^(yes|treated)$"]{bbox};',
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
    """Fusionne eau confirmée OSM/Huwise et points d'eau potentiels IGN."""
    errors = []
    waters = []

    # Les deux services stables sont interrogés en parallèle.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(fetch_fast_drinking_water, route): "huwise",
            pool.submit(fetch_ign_water_candidates, route): "ign",
        }
        for fut, label in list(futures.items()):
            try:
                data = fut.result()
                if label == "huwise":
                    for item in data.get("waters", []) or []:
                        entry = dict(item)
                        entry["potable"] = True
                        waters.append(entry)
                else:
                    waters.extend(data or [])
            except Exception as exc:
                errors.append(exc)

    # Si Huwise ne fournit aucun point confirme, Overpass tente de completer les
    # points potables OSM, meme si l'IGN a deja trouve des fontaines potentielles.
    if not any(item.get("potable") is not False for item in waters):
        try:
            for el in fetch_osm_route_pois(route, radius, "water"):
                pt = _osm_element_point(el)
                if not pt:
                    continue
                lat, lng = pt
                tags = el.get("tags") or {}
                if tags.get("access") == "private" or tags.get("fee") == "yes" or tags.get("drinking_water") == "no":
                    continue
                confirmed = tags.get("amenity") == "drinking_water" or tags.get("drinking_water") in {"yes", "treated"}
                potential = tags.get("amenity") == "water_point"
                if not (confirmed or potential):
                    continue
                waters.append({
                    "lat": lat,
                    "lng": lng,
                    "name": tags.get("name") or ("Eau potable" if confirmed else "Point d'eau"),
                    "operator": tags.get("operator") or "",
                    "description": tags.get("description") or ("" if confirmed else "Potabilité à vérifier sur place."),
                    "osm_id": str(el.get("id") or ""),
                    "source": "OpenStreetMap / Overpass",
                    "potable": bool(confirmed),
                    "water_kind": "water_point" if potential and not confirmed else "drinking_water",
                })
        except Exception as exc:
            errors.append(exc)

    waters = _dedupe_water_items(waters)
    if waters:
        return {"waters": waters, "source": "Huwise + IGN BD TOPO + OpenStreetMap"}
    if errors:
        raise RuntimeError(" ; ".join(str(e) for e in errors[:3]))
    return {"waters": [], "source": "Huwise + IGN BD TOPO + OpenStreetMap"}


def merge_bakery_sources(route, radius):
    """Fusionne systématiquement Huwise OSM + SIRENE au lieu de s'arrêter à la première source."""
    errors = []
    bakeries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(fetch_huwise_bakeries, route),
            pool.submit(fetch_official_bakeries, route),
        ]
        for fut in futures:
            try:
                data = fut.result()
                bakeries.extend(data.get("bakeries", []) or [])
            except Exception as exc:
                errors.append(exc)

    # Overpass uniquement en secours si les deux sources principales sont vides.
    if not bakeries:
        try:
            for el in fetch_osm_route_pois(route, radius, "bakery"):
                pt = _osm_element_point(el)
                if not pt:
                    continue
                lat, lng = pt
                tags = el.get("tags") or {}
                if tags.get("shop") != "bakery":
                    continue
                address_parts = [
                    tags.get("addr:housenumber", ""), tags.get("addr:street", ""),
                    tags.get("addr:postcode", ""), tags.get("addr:city", "")
                ]
                bakeries.append({
                    "lat": lat,
                    "lng": lng,
                    "name": tags.get("name") or tags.get("brand") or "Boulangerie",
                    "address": " ".join(x for x in address_parts if x).strip(),
                    "siret": "",
                    "naf": "",
                    "osm_id": str(el.get("id") or ""),
                    "source": "OpenStreetMap / Overpass",
                })
        except Exception as exc:
            errors.append(exc)

    bakeries = _dedupe_businesses(bakeries)
    if bakeries:
        return {"bakeries": bakeries, "source": "Huwise OSM + Annuaire des Entreprises / SIRENE"}
    if errors:
        raise RuntimeError(" ; ".join(str(e) for e in errors[:3]))
    return {"bakeries": [], "source": "Huwise + SIRENE + OpenStreetMap"}


def merge_cemetery_sources(route, radius):
    """IGN BD TOPO en source principale; Overpass seulement en secours."""
    errors = []
    ign_ok = False
    try:
        ign = fetch_ign_cemeteries(route)
        ign_ok = True
        if ign.get("elements"):
            return ign
    except Exception as exc:
        errors.append(exc)
    try:
        osm = fetch_fast_cemeteries(route, radius)
        if osm.get("elements"):
            return osm
    except Exception as exc:
        errors.append(exc)
    # Si IGN a repondu correctement mais sans resultat, une panne du serveur de
    # secours ne doit pas transformer un resultat vide en « indisponible ».
    if ign_ok:
        return {"elements": [], "source": "IGN BD TOPO / Geoplateforme"}
    if errors:
        raise RuntimeError(" ; ".join(str(e) for e in errors[:2]))
    return {"elements": [], "source": "IGN BD TOPO + OpenStreetMap"}



def _normalize_cemetery_items(data):
    items = []
    for el in (data or {}).get("elements", []) or []:
        if not isinstance(el, dict):
            continue
        point = _osm_element_point(el)
        if not point:
            continue
        lat, lng = point
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        source = tags.get("source") or (data or {}).get("source") or ""
        items.append({
            "id": str(el.get("id") or ""),
            "lat": lat,
            "lng": lng,
            "name": tags.get("name") or "Cimetière",
            "tags": dict(tags),
            "source": source,
        })
    return items


def _extract_poi_items(kind, data):
    if kind == "water":
        return [dict(x) for x in (data or {}).get("waters", []) or [] if isinstance(x, dict)]
    if kind == "bakery":
        return [dict(x) for x in (data or {}).get("bakeries", []) or [] if isinstance(x, dict)]
    if kind == "cemetery":
        return _normalize_cemetery_items(data)
    return []


def _poi_response(kind, items, cache_state="local"):
    if kind == "water":
        return {
            "waters": items,
            "source": "Base locale RouteVelo",
            "cache": cache_state,
        }
    if kind == "bakery":
        return {
            "bakeries": items,
            "source": "Base locale RouteVelo",
            "cache": cache_state,
        }
    if kind == "cemetery":
        elements = []
        for item in items:
            tags = dict(item.get("tags") or {})
            tags.setdefault("landuse", "cemetery")
            tags.setdefault("name", item.get("name") or "Cimetière")
            if item.get("source"):
                tags.setdefault("source", item.get("source"))
            elements.append({
                "type": "node",
                "id": item.get("id") or "",
                "lat": item.get("lat"),
                "lon": item.get("lng"),
                "tags": tags,
            })
        return {
            "elements": elements,
            "source": "Base locale RouteVelo",
            "cache": cache_state,
        }
    return {}


def _fetch_remote_pois(kind, route, radius):
    if kind == "water":
        return merge_water_sources(route, radius)
    if kind == "bakery":
        return merge_bakery_sources(route, radius)
    if kind == "cemetery":
        return merge_cemetery_sources(route, radius)
    raise RuntimeError("Type de POI local inconnu.")


def _refresh_poi_store(kind, route, radius):
    data = _fetch_remote_pois(kind, route, radius)
    items = _extract_poi_items(kind, data)
    local_poi_store.put_items(kind, items, route)
    # On relit par cellules afin de renvoyer aussi les points deja connus dans
    # les zones chevauchantes, pas uniquement ceux du dernier fournisseur.
    return local_poi_store.query_route(kind, route)


def _refresh_key(kind, route):
    cells = local_poi_store.route_cells(route, POI_LOCAL_QUERY_RADIUS_M)
    raw = kind + "|" + "|".join(sorted(cells))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _schedule_poi_refresh(kind, route, radius):
    key = _refresh_key(kind, route)
    with poi_refresh_lock:
        if key in poi_refreshing:
            return
        poi_refreshing.add(key)

    route_copy = [tuple(p) for p in route]

    def worker():
        try:
            _refresh_poi_store(kind, route_copy, radius)
            print(f"[poi local] {kind}: zone rafraichie en arriere-plan")
        except Exception as exc:
            print(f"[poi local] rafraichissement {kind} impossible: {exc}")
        finally:
            with poi_refresh_lock:
                poi_refreshing.discard(key)

    thread = threading.Thread(target=worker, name=f"poi-refresh-{kind}", daemon=True)
    thread.start()


def get_route_pois_local_first(kind, route, radius):
    """Local d'abord, API distante uniquement si la zone manque ou doit etre amorcee."""
    state, _ = local_poi_store.coverage_state(kind, route)
    local_items = local_poi_store.query_route(kind, route)

    if state == "fresh":
        return _poi_response(kind, local_items, "local")

    # Zone deja connue mais trop ancienne: on ne bloque pas l'utilisateur.
    # La copie locale est servie tout de suite puis actualisee en arriere-plan.
    if state == "stale" and local_items:
        _schedule_poi_refresh(kind, route, radius)
        return _poi_response(kind, local_items, "local-stale-refreshing")

    try:
        refreshed = _refresh_poi_store(kind, route, radius)
        return _poi_response(kind, refreshed, "refreshed")
    except Exception:
        # En cas de panne externe, une copie locale meme partielle vaut mieux
        # qu'un echec complet. Elle reste filtree a 400 m cote navigateur.
        if local_items:
            return _poi_response(kind, local_items, "local-fallback")
        raise


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
    server_version = "RouteVelo/4.0"

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
:root{{--bg:#faf7ff;--text:#261d31;--muted:#746a80;--line:#ebe4f2;--accent:#7c3aed;--soft:#f4efff}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;padding:20px;background:var(--bg);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text)}}
.card{{width:min(100%,390px);background:#fff;border:1px solid var(--line);border-radius:22px;padding:26px;box-shadow:0 16px 44px rgba(76,29,149,.12)}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:24px}}.icon{{width:48px;height:48px;display:grid;place-items:center;background:var(--soft);border-radius:15px;font-size:26px}}
h1{{font-size:22px;margin:0}}p{{margin:5px 0 0;color:var(--muted);font-size:13px}}label{{display:block;font-size:13px;font-weight:700;margin-bottom:7px}}
input{{width:100%;min-height:50px;border:1px solid var(--line);border-radius:13px;padding:11px 13px;font:inherit;font-size:16px;outline:none}}input:focus{{border-color:#a78bfa;box-shadow:0 0 0 3px rgba(124,58,237,.12)}}
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

    def _send_bytes(self, raw, content_type, status=200, cache_control="no-store", etag=None):
        use_gzip = len(raw) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        body = gzip.compress(raw, compresslevel=5) if use_gzip else raw
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Vary", "Accept-Encoding")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(raw, "application/json; charset=utf-8", status=status, cache_control="no-store")

    def send_error_json(self, message, status=500):
        self.send_json({"error": str(message)}, status=status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json({"status": "ok", "version": "40", "poi_cache": local_poi_store.stats()})
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
            if parsed.path in {"/api/route", "/api/trace", "/api/height"}:
                payload = read_json(self)
                endpoint = {
                    "/api/route": "/route",
                    "/api/trace": "/trace_attributes",
                    "/api/height": "/height",
                }[parsed.path]
                key = _payload_cache_key("valhalla-v25:" + endpoint, payload)
                cached = _cache_get(key) if key else None
                if cached is not None:
                    return self.send_json(cached)
                data = post_valhalla(endpoint, payload)
                if key:
                    _cache_put(key, data)
                return self.send_json(data)
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
            cache_key = ("geocode-v25", unicodedata.normalize("NFKC", q).strip().casefold())
            cached = _cache_get(cache_key)
            if cached is not None:
                return self.send_json(cached)
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
            result = {
                "lat": float(hit["lat"]),
                "lng": float(hit["lon"]),
                "label": hit.get("display_name") or q,
            }
            _cache_put(cache_key, result)
            return self.send_json(result)
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

        if kind in {"water", "bakery", "cemetery"}:
            if not route:
                labels = {
                    "water": "l'eau potable",
                    "bakery": "les boulangeries",
                    "cemetery": "les cimetieres",
                }
                return self.send_error_json(f"Le trace est requis pour rechercher {labels[kind]}.", 400)
            try:
                data = get_route_pois_local_first(kind, route, radius)
                return self.send_json(data)
            except RuntimeError as exc:
                print(f"[poi {kind}] {exc}")
                return self.send_error_json(f"POI {kind} indisponibles: {exc}", 502)

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
        stat = candidate.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        raw = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        content_type = ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"} else "")
        self._send_bytes(raw, content_type, cache_control="no-cache", etag=etag)


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
