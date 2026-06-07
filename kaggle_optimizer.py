# %% [markdown]
# # Mauritius Trip Optimizer — Skyscanner API (v3)
#
# Multi-phase search with adaptive refinement, parallel workers, API budget cap,
# Monte Carlo daily monitoring, hub-graph routing, and all-time-low alerts.
#
# **No API key required.** Run locally or on Kaggle with Internet enabled.

# %%
"""
Skyscanner Mauritius Trip Optimizer v3
Adaptive search, parallel execution, Monte Carlo monitoring, hub-graph routing.
"""

import argparse
import csv
import json
import os
import random
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from itertools import product
from typing import Dict, List, Optional, Set, Tuple

import requests

# ============================================================
# ENVIRONMENT
# ============================================================

IS_KAGGLE = os.path.exists("/kaggle/working")
OUTPUT_DIR = "results"
os.makedirs(OUTPUT_DIR, exist_ok=True)
PRICE_HISTORY_PATH = os.path.join(OUTPUT_DIR, "price_history.json")
ALERTS_PATH = os.path.join(OUTPUT_DIR, "alerts.json")

print(f"Environment: {'Kaggle' if IS_KAGGLE else 'Local'}")
print(f"Output dir: {OUTPUT_DIR}")

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://www.skyscanner.co.in"
SEARCH_PATH = "/g/radar/api/v2/web-unified-search/"
AUTOSUGGEST_PATH = "/g/autosuggest-search/api/v1/search-flight"

MIN_DELAY = 0.5
MAX_DELAY = 1.5
MAX_POLLS = 8
POLL_INTERVAL = 2

# Trip constraints
DEP_START = date(2026, 6, 20)
DEP_END = date(2026, 7, 15)
RET_START = date(2026, 7, 15)
RET_END = date(2026, 8, 10)
MIN_TRIP_DAYS = 10
MAX_TRIP_DAYS = 25

# Two-stage + adaptive search
COARSE_DAY_STEP = 3
REFINE_RADIUS_DAYS = 1
REFINE_RADIUS_CLUSTERED = 2
TOP_REGIONS = 10
TOP_REGIONS_EXPANDED = 20
CLUSTER_PRICE_SPREAD = 0.05
ONEWAY_SEED_COUNT = 20
ADAPTIVE_REFINE_BUDGET = 120
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Runtime limits and parallelism
MAX_TOTAL_API_CALLS = 5000
CONCURRENT_WORKERS = 4
MONTE_CARLO_SAMPLES = 100
MONTE_CARLO_VOLATILE_FRACTION = 0.6
HUB_GRAPH_HUBS = ["BOM", "DEL", "COK", "BLR"]
HUB_GRAPH_MAX_PAIRS = 12

ENTITY_CACHE_LOCK = threading.Lock()

# Fallback positioning estimates (used until real fares are fetched)
POSITIONING_ESTIMATES = {
    "AMD": 0,
    "BOM": 4000,
    "DEL": 6000,
    "PNQ": 4000,
    "BLR": 6000,
    "HYD": 5000,
    "MAA": 6000,
    "CCU": 7000,
    "GOI": 4000,
    "COK": 5500,
    "TRV": 6000,
    "NAG": 5000,
    "LKO": 5500,
    "IXC": 5500,
    "ATQ": 5000,
    "JAI": 4500,
    "CMB": 15000,
    "DXB": 12000,
    "KUL": 18000,
    "BKK": 16000,
}

ENTITY_CACHE = {
    "AMD": "95673366",
    "BOM": "95673320",
    "DEL": "95673498",
    "MRU": "128668851",
    "BLR": "95673351",
    "PNQ": "128668941",
    "HYD": "128668073",
    "MAA": "95673361",
    "CCU": "128668366",
    "GOI": "95790306",
    "DXB": "95673506",
    "KUL": "95673456",
    "BKK": "95673488",
    "CMB": "95673372",
}

INDIAN_ORIGINS = [
    "AMD", "BOM", "DEL", "PNQ", "BLR", "HYD", "MAA", "CCU", "GOI",
    "COK", "TRV", "NAG", "LKO", "IXC", "ATQ", "JAI",
]

INTERNATIONAL_GATEWAYS = ["CMB", "DXB", "KUL", "BKK"]

POSITIONING_HUBS = ["BOM", "DEL", "BLR", "COK", "PNQ", "HYD", "MAA", "GOI"]

# Scoring weights (INR)
SCORE_STOP_PENALTY = 1500
SCORE_OVERNIGHT_PENALTY = 3000
SCORE_LAYOVER_PER_HOUR = 150
SCORE_POSITIONING_FACTOR = 0.3
SCORE_ONEWAY_RISK = 2500
SCORE_GATEWAY_RISK = 4000


# ============================================================
# DATE HELPERS
# ============================================================

def daterange(start: date, end: date) -> List[date]:
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def valid_date_pairs(
    dep_dates: List[date],
    ret_dates: List[date],
    min_days: int = MIN_TRIP_DAYS,
    max_days: int = MAX_TRIP_DAYS,
) -> List[Tuple[date, date]]:
    pairs = []
    for dep, ret in product(dep_dates, ret_dates):
        trip_days = (ret - dep).days
        if min_days <= trip_days <= max_days:
            pairs.append((dep, ret))
    return pairs


def subsample_dates(dates: List[date], step: int) -> List[date]:
    return dates[::step] if step > 1 else dates


def expand_date(d: date, radius: int, lo: date, hi: date) -> List[date]:
    return [
        d + timedelta(days=offset)
        for offset in range(-radius, radius + 1)
        if lo <= d + timedelta(days=offset) <= hi
    ]


def time_bucket() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "night"


def weekday_label(d: date) -> str:
    return WEEKDAY_NAMES[d.weekday()]


def weekday_representatives(dates: List[date]) -> List[date]:
    """One representative date per weekday (middle occurrence in range)."""
    by_wd: Dict[int, List[date]] = defaultdict(list)
    for d in dates:
        by_wd[d.weekday()].append(d)
    reps = []
    for wd in range(7):
        if wd in by_wd:
            bucket = by_wd[wd]
            reps.append(bucket[len(bucket) // 2])
    return sorted(reps)


# ============================================================
# ENTITY RESOLVER
# ============================================================

def resolve_entity(iata: str) -> Optional[str]:
    code = iata.upper()
    if code in ENTITY_CACHE:
        return ENTITY_CACHE[code]

    url = f"{BASE_URL}{AUTOSUGGEST_PATH}/IN/en-GB/{code}"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    if item.get("PlaceId", "").upper() == code:
                        geo_id = item.get("GeoId", "")
                        if geo_id:
                            with ENTITY_CACHE_LOCK:
                                ENTITY_CACHE[code] = geo_id
                            print(f"  Resolved {code} -> {geo_id} ({item.get('PlaceName', '')})")
                            return geo_id
    except Exception as exc:
        print(f"  WARN: autosuggest failed for {code}: {exc}")
    return None


def resolve_destination_variants() -> List[Tuple[str, str]]:
    """Resolve MRU airport plus Mauritius city/group entities."""
    variants: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    mru = resolve_entity("MRU")
    if mru:
        variants.append(("MRU", mru))
        seen.add(mru)

    url = f"{BASE_URL}{AUTOSUGGEST_PATH}/IN/en-GB/Mauritius"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
            },
            timeout=10,
        )
        if resp.status_code == 200 and isinstance(resp.json(), list):
            for item in resp.json():
                geo_id = item.get("GeoId", "")
                name = item.get("PlaceName", "")
                place_id = item.get("PlaceId", "")
                if not geo_id or geo_id in seen:
                    continue
                if "mauritius" in name.lower() or place_id.upper() == "MRU":
                    label = place_id or name.replace(" ", "-")
                    variants.append((label, geo_id))
                    seen.add(geo_id)
                    print(f"  Destination variant: {label} -> {geo_id} ({name})")
    except Exception as exc:
        print(f"  WARN: Mauritius autosuggest failed: {exc}")

    if not variants and mru:
        variants.append(("MRU", mru))
    return variants


# ============================================================
# SEARCH BUDGET (global API call cap)
# ============================================================

class SearchBudget:
    def __init__(self, max_calls: int = MAX_TOTAL_API_CALLS):
        self.max_calls = max_calls
        self.used = 0
        self._lock = threading.Lock()
        self.exhausted = False

    def acquire(self) -> bool:
        with self._lock:
            if self.used >= self.max_calls:
                self.exhausted = True
                return False
            self.used += 1
            if self.used >= self.max_calls:
                self.exhausted = True
            return True

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_calls - self.used)


# ============================================================
# PRICE HISTORY
# ============================================================

class PriceHistory:
    def __init__(self, path: str = PRICE_HISTORY_PATH):
        self.path = path
        self.data = {"routes": {}, "runs": [], "all_time_best": None}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                pass

    def save(self):
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)

    def record(
        self,
        route_key: str,
        price: float,
        search_type: str = "roundtrip",
        dep_date: Optional[str] = None,
        carriers: Optional[str] = None,
    ) -> Optional[str]:
        """Record observation. Returns alert message if new all-time low."""
        with self._lock:
            bucket = time_bucket()
            ts = datetime.now().isoformat()
            routes = self.data.setdefault("routes", {})
            entry = routes.setdefault(
                route_key,
                {
                    "observations": [],
                    "min_price_seen": None,
                    "max_price_seen": None,
                    "avg_price": None,
                    "search_type": search_type,
                },
            )
            obs = {"timestamp": ts, "price": price, "time_bucket": bucket}
            if dep_date:
                obs["dep_weekday"] = weekday_label(date.fromisoformat(dep_date))
            entry["observations"].append(obs)
            prices = [o["price"] for o in entry["observations"]]
            entry["min_price_seen"] = min(prices)
            entry["max_price_seen"] = max(prices)
            entry["avg_price"] = round(sum(prices) / len(prices), 2)

            if carriers:
                for airline in {a.strip() for a in carriers.split(",") if a.strip()}:
                    self._record_airline_unlocked(airline, price, search_type)

            return self._check_all_time_low_unlocked(price, route_key, search_type)

    def _record_airline(self, airline: str, price: float, search_type: str):
        with self._lock:
            self._record_airline_unlocked(airline, price, search_type)

    def _record_airline_unlocked(self, airline: str, price: float, search_type: str):
        airlines = self.data.setdefault("airlines", {})
        entry = airlines.setdefault(
            airline,
            {
                "observations": 0,
                "min_price_seen": None,
                "max_price_seen": None,
                "avg_price": None,
                "search_types": {},
            },
        )
        entry["observations"] += 1
        entry["search_types"][search_type] = entry["search_types"].get(search_type, 0) + 1
        n = entry["observations"]
        if entry["min_price_seen"] is None:
            entry["min_price_seen"] = price
            entry["max_price_seen"] = price
            entry["avg_price"] = price
        else:
            entry["min_price_seen"] = min(entry["min_price_seen"], price)
            entry["max_price_seen"] = max(entry["max_price_seen"], price)
            entry["avg_price"] = round(
                (entry["avg_price"] * (n - 1) + price) / n, 2
            )

    def weekday_summary(self) -> Dict[str, dict]:
        """Min/avg fare by departure weekday from route observations."""
        by_wd: Dict[str, List[float]] = defaultdict(list)
        for entry in self.data.get("routes", {}).values():
            for obs in entry.get("observations", []):
                wd = obs.get("dep_weekday")
                if wd:
                    by_wd[wd].append(obs["price"])
        summary = {}
        for wd, prices in by_wd.items():
            summary[wd] = {
                "count": len(prices),
                "min": min(prices),
                "avg": round(sum(prices) / len(prices), 2),
            }
        return summary

    def cheapest_airlines(self, top_n: int = 5) -> List[Tuple[str, float]]:
        airlines = self.data.get("airlines", {})
        ranked = sorted(
            airlines.items(),
            key=lambda x: x[1].get("avg_price") or 999999,
        )
        return [(name, info["avg_price"]) for name, info in ranked[:top_n]]

    def volatile_routes(self, top_n: int = 40) -> List[Tuple[str, float]]:
        """Routes with highest price volatility — prioritize for monitoring."""
        scored = []
        for route, info in self.data.get("routes", {}).items():
            mn = info.get("min_price_seen")
            mx = info.get("max_price_seen")
            avg = info.get("avg_price")
            n_obs = len(info.get("observations", []))
            if mn and mx and avg and avg > 0 and n_obs >= 2:
                scored.append((route, (mx - mn) / avg))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_n]

    def weekly_best(self) -> Optional[dict]:
        week_ago = datetime.now() - timedelta(days=7)
        best_price = None
        best_route = None
        for route, info in self.data.get("routes", {}).items():
            for obs in info.get("observations", []):
                try:
                    ts = datetime.fromisoformat(obs["timestamp"])
                except Exception:
                    continue
                if ts >= week_ago:
                    p = obs["price"]
                    if best_price is None or p < best_price:
                        best_price = p
                        best_route = route
        if best_price is None:
            return None
        return {"route": best_route, "price": best_price}

    def _check_all_time_low_unlocked(
        self, price: float, route_key: str, search_type: str
    ) -> Optional[str]:
        best = self.data.get("all_time_best")
        if best is None or price < best.get("price", 999999999):
            self.data["all_time_best"] = {
                "price": price,
                "route": route_key,
                "search_type": search_type,
                "timestamp": datetime.now().isoformat(),
            }
            return (
                f"NEW ALL-TIME LOW: Rs.{price:,.0f} on {route_key} ({search_type})"
            )
        return None

    def check_effective_low(self, flight: "Flight") -> Optional[str]:
        """Alert when effective total (fare + positioning) hits a new low."""
        with self._lock:
            eff = flight.effective_total
            best = self.data.get("all_time_best_effective")
            prev = best.get("effective_total") if best else None
            if prev is None or eff < prev:
                self.data["all_time_best_effective"] = {
                    "effective_total": eff,
                    "price_raw": flight.price_raw,
                    "origin": flight.origin,
                    "departure_date": flight.departure_date,
                    "return_date": flight.return_date,
                    "search_type": flight.search_type,
                    "timestamp": datetime.now().isoformat(),
                }
                return (
                    f"NEW BEST FROM AMD: Rs.{eff:,.0f} effective "
                    f"({flight.origin} {flight.departure_date}-{flight.return_date})"
                )
            return None

    def parse_route_key(self, route_key: str) -> Optional[Tuple[str, str, date, date]]:
        """Parse 'ORIGIN->DEST|dep|ret' or 'ORIGIN->DEST|OW|dep|ret'."""
        try:
            if "|OW|" in route_key:
                left, _, rest = route_key.partition("|OW|")
                dep_s, ret_s = rest.split("|", 1)
            else:
                left, dep_s, ret_s = route_key.split("|", 2)
            origin, dest_label = left.split("->", 1)
            return origin, dest_label, date.fromisoformat(dep_s), date.fromisoformat(ret_s)
        except Exception:
            return None

    def record_run(self, phase: str, searches: int, flights: int):
        with self._lock:
            self.data.setdefault("runs", []).append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "time_bucket": time_bucket(),
                    "phase": phase,
                    "searches": searches,
                    "flights_found": flights,
                }
            )


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class Flight:
    itinerary_id: str
    origin: str
    destination: str
    dest_label: str
    departure_date: str
    return_date: str
    trip_days: int
    price_raw: float
    price_formatted: str
    positioning_cost: int
    effective_total: float
    outbound_duration: int
    return_duration: int
    total_duration: int
    outbound_stops: int
    return_stops: int
    total_stops: int
    outbound_carriers: str
    return_carriers: str
    outbound_departure: str
    outbound_arrival: str
    return_departure: str
    return_arrival: str
    search_type: str
    value_score: float
    positioning_source: str = "estimate"


@dataclass
class OneWayOption:
    itinerary_id: str
    origin: str
    destination: str
    travel_date: str
    price_raw: float
    price_formatted: str
    duration: int
    stops: int
    carriers: str
    departure: str
    arrival: str


# ============================================================
# SCORING
# ============================================================

def compute_value_score(
    price: float,
    total_stops: int,
    total_duration: int,
    positioning_cost: int,
    search_type: str,
) -> float:
    stop_penalty = SCORE_STOP_PENALTY * total_stops
    overnight_penalty = SCORE_OVERNIGHT_PENALTY if total_stops >= 2 else 0
    layover_penalty = max(0, total_duration - 960) * (SCORE_LAYOVER_PER_HOUR / 60)
    positioning_penalty = positioning_cost * SCORE_POSITIONING_FACTOR
    risk_penalty = 0
    if search_type == "oneway_combo":
        risk_penalty = SCORE_ONEWAY_RISK
    elif search_type == "international_gateway":
        risk_penalty = SCORE_GATEWAY_RISK
    return (
        price
        + stop_penalty
        + overnight_penalty
        + layover_penalty
        + positioning_penalty
        + risk_penalty
    )


# ============================================================
# SEARCH CLIENT
# ============================================================

class Searcher:
    def __init__(self, budget: Optional[SearchBudget] = None):
        self.session = requests.Session()
        self.search_count = 0
        self.fail_count = 0
        self.budget = budget
        self.positioning_cache: Dict[Tuple[str, str], float] = {}
        self._set_headers()

    def _can_search(self) -> bool:
        if self.budget is None:
            return True
        return self.budget.acquire()

    def _set_headers(self):
        vid = str(uuid.uuid4())
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
                "x-skyscanner-channelid": "website",
                "x-skyscanner-currency": "INR",
                "x-skyscanner-locale": "en-GB",
                "x-skyscanner-market": "IN",
                "x-skyscanner-viewid": vid,
                "x-skyscanner-trustedfunnelid": vid,
                "x-skyscanner-traveller-context": str(uuid.uuid4()),
            }
        )

    def _poll_search(self, url: str, data: dict) -> dict:
        ctx = data.get("context", {})
        status = ctx.get("status", "")
        sid = ctx.get("sessionId", "")
        polls = 0
        while status != "complete" and polls < MAX_POLLS:
            polls += 1
            time.sleep(POLL_INTERVAL)
            try:
                resp = self.session.get(f"{url}{sid}", timeout=30)
                if resp.status_code != 200:
                    break
                data = resp.json()
                ctx = data.get("context", {})
                status = ctx.get("status", "")
                sid = ctx.get("sessionId", "")
            except Exception:
                break
        return data

    def _execute(self, body: dict) -> dict:
        url = f"{BASE_URL}{SEARCH_PATH}"
        resp = self.session.post(url, json=body, timeout=30)
        if resp.status_code != 200:
            return {}
        gw = resp.headers.get("x-gateway-servedby", "")
        if gw:
            self.session.headers["x-gateway-servedby"] = gw
        data = resp.json()
        return self._poll_search(url, data)

    def _date_payload(self, d: date) -> dict:
        return {
            "@type": "date",
            "year": str(d.year),
            "month": str(d.month).zfill(2),
            "day": str(d.day).zfill(2),
        }

    def _fresh_session_ids(self):
        vid = str(uuid.uuid4())
        self.session.headers["x-skyscanner-viewid"] = vid
        self.session.headers["x-skyscanner-trustedfunnelid"] = vid

    def get_positioning_cost(self, hub: str, travel_date: date) -> Tuple[int, str]:
        if hub == "AMD":
            return 0, "home"
        key = (hub, str(travel_date))
        if key in self.positioning_cache:
            return int(self.positioning_cache[key]), "cached"
        fare = self.fetch_positioning_fare("AMD", hub, travel_date)
        if fare is not None:
            self.positioning_cache[key] = fare
            return int(fare), "live"
        est = POSITIONING_ESTIMATES.get(hub, 5000)
        return est, "estimate"

    def fetch_positioning_fare(
        self, origin: str, dest: str, travel_date: date
    ) -> Optional[float]:
        options = self.search_oneway(origin, dest, travel_date, quiet=True)
        if not options:
            return None
        return min(o.price_raw for o in options)

    def search_oneway(
        self,
        origin: str,
        dest: str,
        travel_date: date,
        dest_entity: Optional[str] = None,
        origin_entity: Optional[str] = None,
        quiet: bool = False,
    ) -> List[OneWayOption]:
        if not self._can_search():
            return []
        self.search_count += 1
        o_eid = origin_entity or resolve_entity(origin)
        d_eid = dest_entity or resolve_entity(dest)
        if not o_eid or not d_eid:
            self.fail_count += 1
            return []

        self._fresh_session_ids()
        body = {
            "cabinClass": "ECONOMY",
            "adults": 1,
            "childAges": [],
            "legs": [
                {
                    "legOrigin": {"@type": "entity", "entityId": o_eid},
                    "legDestination": {"@type": "entity", "entityId": d_eid},
                    "dates": self._date_payload(travel_date),
                }
            ],
        }

        try:
            data = self._execute(body)
            results = data.get("itineraries", {}).get("results", [])
            options = []
            for r in results:
                legs = r.get("legs", [])
                if not legs:
                    continue
                leg = legs[0]
                price = r.get("price", {})
                carriers = ", ".join(
                    c.get("name", "")
                    for c in leg.get("carriers", {}).get("marketing", [])
                )
                options.append(
                    OneWayOption(
                        itinerary_id=r.get("id", ""),
                        origin=origin,
                        destination=dest,
                        travel_date=str(travel_date),
                        price_raw=price.get("raw", 0),
                        price_formatted=price.get("formatted", ""),
                        duration=leg.get("durationInMinutes", 0),
                        stops=leg.get("stopCount", 0),
                        carriers=carriers,
                        departure=leg.get("departure", ""),
                        arrival=leg.get("arrival", ""),
                    )
                )
            if not quiet:
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            return options
        except Exception:
            self.fail_count += 1
            return []

    def search_roundtrip(
        self,
        origin: str,
        dest_entity: str,
        dest_label: str,
        dep: date,
        ret: date,
        search_type: str = "roundtrip",
        home_airport: str = "AMD",
    ) -> List[Flight]:
        if not self._can_search():
            return []
        self.search_count += 1
        o_eid = resolve_entity(origin)
        if not o_eid or not dest_entity:
            self.fail_count += 1
            return []

        self._fresh_session_ids()
        body = {
            "cabinClass": "ECONOMY",
            "adults": 1,
            "childAges": [],
            "legs": [
                {
                    "legOrigin": {"@type": "entity", "entityId": o_eid},
                    "legDestination": {"@type": "entity", "entityId": dest_entity},
                    "dates": self._date_payload(dep),
                },
                {
                    "legOrigin": {"@type": "entity", "entityId": dest_entity},
                    "legDestination": {"@type": "entity", "entityId": o_eid},
                    "dates": self._date_payload(ret),
                },
            ],
        }

        try:
            data = self._execute(body)
            results = data.get("itineraries", {}).get("results", [])
            trip_days = (ret - dep).days
            pos_cost, pos_source = self.get_positioning_cost(origin, dep)

            flights = []
            for r in results:
                legs = r.get("legs", [])
                if len(legs) < 2:
                    continue
                ob, ib = legs[0], legs[1]

                def carriers(leg):
                    return ", ".join(
                        c.get("name", "")
                        for c in leg.get("carriers", {}).get("marketing", [])
                    )

                price = r.get("price", {})
                p = price.get("raw", 0)
                os_ = ob.get("stopCount", 0)
                rs_ = ib.get("stopCount", 0)
                ts = os_ + rs_
                od = ob.get("durationInMinutes", 0)
                rd = ib.get("durationInMinutes", 0)
                td = od + rd

                if origin != home_airport:
                    amd_pos, _ = self.get_positioning_cost(origin, dep)
                    pos_cost = amd_pos
                    pos_source = "live" if (origin, str(dep)) in self.positioning_cache else "estimate"

                flights.append(
                    Flight(
                        itinerary_id=r.get("id", ""),
                        origin=origin,
                        destination=dest_label,
                        dest_label=dest_label,
                        departure_date=str(dep),
                        return_date=str(ret),
                        trip_days=trip_days,
                        price_raw=p,
                        price_formatted=price.get("formatted", ""),
                        positioning_cost=pos_cost,
                        effective_total=p + pos_cost,
                        outbound_duration=od,
                        return_duration=rd,
                        total_duration=td,
                        outbound_stops=os_,
                        return_stops=rs_,
                        total_stops=ts,
                        outbound_carriers=carriers(ob),
                        return_carriers=carriers(ib),
                        outbound_departure=ob.get("departure", ""),
                        outbound_arrival=ob.get("arrival", ""),
                        return_departure=ib.get("departure", ""),
                        return_arrival=ib.get("arrival", ""),
                        search_type=search_type,
                        value_score=compute_value_score(
                            p, ts, td, pos_cost, search_type
                        ),
                        positioning_source=pos_source,
                    )
                )
            return flights
        except Exception:
            self.fail_count += 1
            return []


# ============================================================
# SEARCH ORCHESTRATION
# ============================================================

def build_all_dep_ret_dates() -> Tuple[List[date], List[date]]:
    dep_dates = daterange(DEP_START, DEP_END)
    ret_dates = daterange(RET_START, RET_END)
    return dep_dates, ret_dates


def build_coarse_pairs() -> List[Tuple[date, date]]:
    dep_dates, ret_dates = build_all_dep_ret_dates()
    weekday_deps = weekday_representatives(dep_dates)
    weekday_rets = weekday_representatives(ret_dates)
    step_deps = subsample_dates(dep_dates, COARSE_DAY_STEP)
    step_rets = subsample_dates(ret_dates, COARSE_DAY_STEP)
    coarse_deps = sorted(set(weekday_deps + step_deps))
    coarse_rets = sorted(set(weekday_rets + step_rets))
    return valid_date_pairs(coarse_deps, coarse_rets)


def find_promising_regions(
    flights: List[Flight], top_n: int = TOP_REGIONS
) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], float]]:
    region_prices: Dict[Tuple[str, str], float] = {}
    for f in flights:
        key = (f.departure_date, f.return_date)
        region_prices[key] = min(region_prices.get(key, 999999999), f.price_raw)
    ranked = sorted(region_prices.items(), key=lambda x: x[1])
    regions = [key for key, _ in ranked[:top_n]]
    prices = dict(ranked[:top_n])
    return regions, prices


def adaptive_region_count(region_prices: Dict[Tuple[str, str], float]) -> int:
    """Expand to 20 regions only when top-10 prices cluster tightly."""
    if len(region_prices) < 2:
        return TOP_REGIONS
    top_prices = sorted(region_prices.values())[:TOP_REGIONS]
    if len(top_prices) < 2:
        return TOP_REGIONS
    spread = (max(top_prices) - min(top_prices)) / max(min(top_prices), 1)
    if spread <= CLUSTER_PRICE_SPREAD:
        print(f"  Clustered fares (spread {spread:.1%}) — expanding to {TOP_REGIONS_EXPANDED} regions")
        return TOP_REGIONS_EXPANDED
    return TOP_REGIONS


def build_refine_pairs(
    regions: List[Tuple[str, str]],
    region_prices: Dict[Tuple[str, str], float],
    budget: int = ADAPTIVE_REFINE_BUDGET,
) -> List[Tuple[date, date]]:
    """Adaptive refinement: tighter radius on expensive regions, wider on cheap clusters."""
    if not regions:
        return []

    min_price = min(region_prices.get(r, 999999999) for r in regions)
    weighted: List[Tuple[float, Tuple[date, date]]] = []

    for dep_str, ret_str in regions:
        price = region_prices.get((dep_str, ret_str), 999999999)
        dep = date.fromisoformat(dep_str)
        ret = date.fromisoformat(ret_str)
        radius = REFINE_RADIUS_CLUSTERED if price <= min_price * 1.03 else REFINE_RADIUS_DAYS
        dep_candidates = expand_date(dep, radius, DEP_START, DEP_END)
        ret_candidates = expand_date(ret, radius, RET_START, RET_END)
        weight = min_price / max(price, 1)
        for pair in valid_date_pairs(dep_candidates, ret_candidates):
            weighted.append((weight, pair))

    weighted.sort(key=lambda x: (-x[0], x[1]))
    seen: Set[Tuple[date, date]] = set()
    result: List[Tuple[date, date]] = []
    for _, pair in weighted:
        if pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
        if len(result) >= budget:
            break
    return result


def seed_oneway_targets(flights: List[Flight], top_n: int = ONEWAY_SEED_COUNT) -> List[Flight]:
    """Pick one-way decomposition targets from cheapest refined round trips."""
    refined = [
        f for f in flights
        if f.search_type in ("roundtrip", "roundtrip_refined")
    ]
    top = sorted(refined, key=lambda f: f.price_raw)[:top_n * 2]
    seen: Set[Tuple[str, str, str, str]] = set()
    targets: List[Flight] = []
    for f in top:
        key = (f.origin, f.departure_date, f.return_date, f.dest_label)
        if key in seen:
            continue
        seen.add(key)
        targets.append(f)
        if len(targets) >= top_n:
            break
    return targets


def emit_alert(message: str, alerts: List[str]):
    alerts.append(message)
    print(f"\n  *** ALERT: {message} ***", flush=True)


def save_alerts(alerts: List[str]):
    if not alerts:
        return
    existing = []
    if os.path.exists(ALERTS_PATH):
        try:
            with open(ALERTS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    existing.extend(
        {"timestamp": datetime.now().isoformat(), "message": m} for m in alerts
    )
    with open(ALERTS_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"  Alerts saved: {ALERTS_PATH}")


def process_search_result(
    flights: List[Flight],
    origin: str,
    dest_label: str,
    dep: date,
    ret: date,
    search_type: str,
    price_history: Optional[PriceHistory],
    alerts: Optional[List[str]],
) -> None:
    if not flights:
        return
    best_f = min(flights, key=lambda x: x.price_raw)
    route_key = f"{origin}->{dest_label}|{dep}|{ret}"
    if price_history:
        carriers = f"{best_f.outbound_carriers}, {best_f.return_carriers}"
        msg = price_history.record(
            route_key, best_f.price_raw, search_type,
            dep_date=str(dep), carriers=carriers,
        )
        if msg and alerts is not None:
            emit_alert(msg, alerts)
        eff_msg = price_history.check_effective_low(best_f)
        if eff_msg and alerts is not None:
            emit_alert(eff_msg, alerts)


def build_monte_carlo_combos(
    price_history: PriceHistory,
    valid_origins: List[str],
    dest_variants: List[Tuple[str, str]],
    full_pairs: List[Tuple[date, date]],
    dest_entity_map: Dict[str, str],
    n_samples: int = MONTE_CARLO_SAMPLES,
) -> List[Tuple[str, str, str, date, date]]:
    """Mix volatile routes from history with random exploration."""
    combos: List[Tuple[str, str, str, date, date]] = []
    seen: Set[Tuple[str, str, date, date]] = set()

    n_volatile = int(n_samples * MONTE_CARLO_VOLATILE_FRACTION)
    for route, vol in price_history.volatile_routes(n_volatile * 2):
        parsed = price_history.parse_route_key(route)
        if not parsed:
            continue
        origin, dest_label, dep, ret = parsed
        if origin not in valid_origins and not origin.startswith("AMD"):
            continue
        dest_eid = dest_entity_map.get(dest_label) or resolve_entity("MRU")
        key = (origin, dest_label, dep, ret)
        if key in seen:
            continue
        seen.add(key)
        combos.append((origin, dest_label, dest_eid, dep, ret))
        if len(combos) >= n_volatile:
            break

    pool = [
        (o, dl, de, d, r)
        for o, (dl, de), (d, r) in product(valid_origins, dest_variants, full_pairs)
    ]
    random.shuffle(pool)
    for item in pool:
        key = (item[0], item[1], item[3], item[4])
        if key in seen:
            continue
        seen.add(key)
        combos.append(item)
        if len(combos) >= n_samples:
            break

    print(
        f"  Monte Carlo: {len(combos)} combos "
        f"({min(len(combos), n_volatile)} volatile-biased + random)"
    )
    return combos


def run_monte_carlo_monitor(
    budget: SearchBudget,
    valid_origins: List[str],
    dest_variants: List[Tuple[str, str]],
    full_pairs: List[Tuple[date, date]],
    dest_entity_map: Dict[str, str],
    price_history: PriceHistory,
    workers: int,
    n_samples: int,
    alerts: List[str],
) -> List[Flight]:
    combos = build_monte_carlo_combos(
        price_history, valid_origins, dest_variants, full_pairs,
        dest_entity_map, n_samples,
    )
    return run_search_batch(
        Searcher(budget=budget),
        [],
        [],
        [],
        search_type="monte_carlo",
        label=f"Monte Carlo Monitor ({len(combos)} samples)",
        price_history=price_history,
        checkpoint_every=0,
        explicit_combos=combos,
        budget=budget,
        workers=workers,
        alerts=alerts,
    )


def run_hub_graph_search(
    searcher: Searcher,
    hubs: List[str],
    dest_eid: str,
    dest_label: str,
    date_pairs: List[Tuple[date, date]],
    price_history: Optional[PriceHistory] = None,
    alerts: Optional[List[str]] = None,
) -> List[Flight]:
    """
    Graph routing: AMD→hub→MRU→hub→AMD as four independent one-way legs.
    Finds cheapest hub path for Ahmedabad travelers.
    """
    print(f"\n--- Phase 7: Hub Graph (AMD→hub→MRU→hub→AMD) ---")
    results: List[Flight] = []

    for hub in hubs:
        for dep, ret in date_pairs[:HUB_GRAPH_MAX_PAIRS]:
            if searcher.budget and searcher.budget.exhausted:
                print("  Budget exhausted — stopping hub graph")
                return results

            amd_hub = searcher.search_oneway("AMD", hub, dep, quiet=True)
            hub_mru = searcher.search_oneway(
                hub, "MRU", dep, dest_entity=dest_eid, quiet=True
            )
            mru_hub = searcher.search_oneway(
                "MRU", hub, ret, origin_entity=dest_eid, quiet=True
            )
            hub_amd = searcher.search_oneway(hub, "AMD", ret, quiet=True)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            if not (amd_hub and hub_mru and mru_hub and hub_amd):
                continue

            l1 = min(amd_hub, key=lambda o: o.price_raw)
            l2 = min(hub_mru, key=lambda o: o.price_raw)
            l3 = min(mru_hub, key=lambda o: o.price_raw)
            l4 = min(hub_amd, key=lambda o: o.price_raw)
            total = l1.price_raw + l2.price_raw + l3.price_raw + l4.price_raw
            trip_days = (ret - dep).days
            ts = l1.stops + l2.stops + l3.stops + l4.stops
            td = l1.duration + l2.duration + l3.duration + l4.duration

            chain = Flight(
                itinerary_id=f"hub_{hub}_{dep}_{ret}",
                origin=f"AMD(via {hub} graph)",
                destination=dest_label,
                dest_label=dest_label,
                departure_date=str(dep),
                return_date=str(ret),
                trip_days=trip_days,
                price_raw=total,
                price_formatted=f"Rs.{total:,.0f}",
                positioning_cost=int(l1.price_raw),
                effective_total=total,
                outbound_duration=l1.duration + l2.duration,
                return_duration=l3.duration + l4.duration,
                total_duration=td,
                outbound_stops=l1.stops + l2.stops,
                return_stops=l3.stops + l4.stops,
                total_stops=ts,
                outbound_carriers=f"{l1.carriers}/{l2.carriers}",
                return_carriers=f"{l3.carriers}/{l4.carriers}",
                outbound_departure=l1.departure,
                outbound_arrival=l2.arrival,
                return_departure=l3.departure,
                return_arrival=l4.arrival,
                search_type="hub_graph",
                value_score=compute_value_score(
                    total, ts, td, int(l1.price_raw), "international_gateway"
                ),
                positioning_source="live",
            )
            results.append(chain)
            route_key = f"AMD~{hub}~{dest_label}|graph|{dep}|{ret}"
            if price_history:
                msg = price_history.record(
                    route_key, total, "hub_graph",
                    dep_date=str(dep), carriers=chain.outbound_carriers,
                )
                if msg and alerts is not None:
                    emit_alert(msg, alerts)
                eff_msg = price_history.check_effective_low(chain)
                if eff_msg and alerts is not None:
                    emit_alert(eff_msg, alerts)
            print(
                f"  AMD→{hub}→MRU→{hub}→AMD {dep}-{ret}: Rs.{total:,.0f}",
                flush=True,
            )

    return results


def run_search_batch(
    searcher: Searcher,
    origins: List[str],
    dest_variants: List[Tuple[str, str]],
    date_pairs: List[Tuple[date, date]],
    search_type: str,
    label: str,
    price_history: Optional[PriceHistory] = None,
    checkpoint_every: int = 100,
    start_index: int = 0,
    existing_flights: Optional[List[Flight]] = None,
    explicit_combos: Optional[List[Tuple[str, str, str, date, date]]] = None,
    budget: Optional[SearchBudget] = None,
    workers: int = 1,
    alerts: Optional[List[str]] = None,
) -> List[Flight]:
    if explicit_combos is not None:
        combos = explicit_combos
    else:
        combos = [
            (origin, dest_label, dest_eid, dep, ret)
            for origin, (dest_label, dest_eid), (dep, ret) in product(
                origins, dest_variants, date_pairs
            )
        ]
    total = len(combos)
    all_flights = list(existing_flights or [])
    t_start = time.time()
    budget = budget or searcher.budget

    print(f"\n--- {label} ({total} searches, workers={workers}) ---")
    if budget:
        print(f"  API budget: {budget.remaining()} / {budget.max_calls} remaining")
    if start_index:
        print(f"  Resuming from search #{start_index}")

    def do_one(combo):
        origin, dest_label, dest_eid, dep, ret = combo
        local = Searcher(budget=budget)
        flights = local.search_roundtrip(
            origin, dest_eid, dest_label, dep, ret, search_type=search_type
        )
        return flights, origin, dest_label, dep, ret, local.fail_count

    if workers <= 1:
        for i, combo in enumerate(combos):
            if i < start_index:
                continue
            if budget and budget.exhausted:
                print(f"\n  API budget exhausted at search {i+1}/{total}")
                break

            if i % 10 == 0:
                elapsed = time.time() - t_start
                rate = max((i + 1 - start_index) / max(elapsed, 1), 0.001)
                eta_m = (total - i) / rate / 60
                print(
                    f"\n  [{i+1}/{total}] {i/total*100:.0f}% | "
                    f"Flights: {len(all_flights)} | Fails: {searcher.fail_count} | "
                    f"Budget: {budget.remaining() if budget else '∞'} | ETA: {eta_m:.0f}m",
                    flush=True,
                )

            flights = searcher.search_roundtrip(
                combo[0], combo[2], combo[1], combo[3], combo[4],
                search_type=search_type,
            )
            all_flights.extend(flights)
            process_search_result(
                flights, combo[0], combo[1], combo[3], combo[4],
                search_type, price_history, alerts,
            )
            if flights:
                best = min(f.price_raw for f in flights)
                print(
                    f"  {combo[0]}->{combo[1]} {combo[3].strftime('%m/%d')}-"
                    f"{combo[4].strftime('%m/%d')} {len(flights):3d}fl Rs.{best:,.0f}",
                    flush=True,
                )
            else:
                print(
                    f"  {combo[0]}->{combo[1]} {combo[3].strftime('%m/%d')}-"
                    f"{combo[4].strftime('%m/%d')} FAIL",
                    flush=True,
                )

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            if checkpoint_every and (i + 1) % checkpoint_every == 0:
                save_checkpoint(
                    phase=label, index=i + 1, total=total,
                    flights=all_flights, searcher=searcher,
                )
    else:
        pending = combos[start_index:]
        done = start_index
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(do_one, c): c for c in pending}
            for fut in as_completed(futures):
                if budget and budget.exhausted:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    flights, origin, dest_label, dep, ret, fails = fut.result()
                except Exception:
                    searcher.fail_count += 1
                    continue
                searcher.fail_count += fails
                done += 1
                all_flights.extend(flights)
                process_search_result(
                    flights, origin, dest_label, dep, ret,
                    search_type, price_history, alerts,
                )
                if done % 10 == 0:
                    print(
                        f"  [{done}/{total}] Flights: {len(all_flights)} | "
                        f"Budget: {budget.remaining() if budget else '∞'}",
                        flush=True,
                    )

    return all_flights


def run_positioning_fares(
    searcher: Searcher,
    hubs: List[str],
    departure_dates: List[date],
) -> Dict[Tuple[str, str], float]:
    print(f"\n--- Phase 4: Real Positioning Fares ({len(hubs)} hubs) ---")
    for hub in hubs:
        for dep in departure_dates:
            key = (hub, str(dep))
            if key in searcher.positioning_cache:
                continue
            fare = searcher.fetch_positioning_fare("AMD", hub, dep)
            if fare is not None:
                searcher.positioning_cache[key] = fare
                print(f"  AMD->{hub} {dep}: Rs.{fare:,.0f}", flush=True)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return searcher.positioning_cache


def run_oneway_combos(
    searcher: Searcher,
    targets: List[Flight],
    dest_entity_map: Dict[str, str],
    price_history: Optional[PriceHistory] = None,
) -> List[Flight]:
    print(f"\n--- Phase 5: One-Way Combinations ({len(targets)} seeds from refined RT) ---")
    combined: List[Flight] = []

    for seed in targets:
        origin = seed.origin
        dest_label = seed.dest_label
        dest_eid = dest_entity_map.get(dest_label) or resolve_entity("MRU")
        dep = date.fromisoformat(seed.departure_date)
        ret = date.fromisoformat(seed.return_date)

        outbound = searcher.search_oneway(
            origin, "MRU", dep, dest_entity=dest_eid
        )
        inbound = searcher.search_oneway(
            "MRU", origin, ret, origin_entity=dest_eid
        )
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        if not outbound or not inbound:
            continue

        best_ob = min(outbound, key=lambda o: o.price_raw)
        best_ib = min(inbound, key=lambda o: o.price_raw)
        total_price = best_ob.price_raw + best_ib.price_raw
        rt_price = seed.price_raw
        if total_price >= rt_price:
            continue

        trip_days = (ret - dep).days
        pos_cost, pos_source = searcher.get_positioning_cost(origin, dep)
        ts = best_ob.stops + best_ib.stops
        td = best_ob.duration + best_ib.duration

        combo = Flight(
            itinerary_id=f"ow_{best_ob.itinerary_id}_{best_ib.itinerary_id}",
            origin=origin,
            destination=dest_label,
            dest_label=dest_label,
            departure_date=str(dep),
            return_date=str(ret),
            trip_days=trip_days,
            price_raw=total_price,
            price_formatted=f"Rs.{total_price:,.0f}",
            positioning_cost=pos_cost,
            effective_total=total_price + pos_cost,
            outbound_duration=best_ob.duration,
            return_duration=best_ib.duration,
            total_duration=td,
            outbound_stops=best_ob.stops,
            return_stops=best_ib.stops,
            total_stops=ts,
            outbound_carriers=best_ob.carriers,
            return_carriers=best_ib.carriers,
            outbound_departure=best_ob.departure,
            outbound_arrival=best_ob.arrival,
            return_departure=best_ib.departure,
            return_arrival=best_ib.arrival,
            search_type="oneway_combo",
            value_score=compute_value_score(
                total_price, ts, td, pos_cost, "oneway_combo"
            ),
            positioning_source=pos_source,
        )
        combined.append(combo)
        route_key = f"{origin}->{dest_label}|OW|{dep}|{ret}"
        if price_history:
            price_history.record(
                route_key, total_price, "oneway_combo",
                dep_date=str(dep),
                carriers=f"{best_ob.carriers}, {best_ib.carriers}",
            )
        savings = rt_price - total_price
        print(
            f"  {origin}->{dest_label} OW {dep}-{ret}: "
            f"Rs.{total_price:,.0f} (saves Rs.{savings:,.0f} vs RT) "
            f"({best_ob.carriers}/{best_ib.carriers})",
            flush=True,
        )

    return combined


def run_gateway_oneway_chains(
    searcher: Searcher,
    gateways: List[str],
    dest_eid: str,
    dest_label: str,
    date_pairs: List[Tuple[date, date]],
    price_history: Optional[PriceHistory] = None,
    max_pairs: int = 10,
) -> List[Flight]:
    """Full 4-leg chain: AMD→GW→MRU→GW→AMD via one-way searches."""
    print(f"\n--- Phase 6c: Full Gateway One-Way Chains ---")
    chained: List[Flight] = []

    for gateway in gateways:
        for dep, ret in date_pairs[:max_pairs]:
            amd_gw = searcher.search_oneway("AMD", gateway, dep, quiet=True)
            gw_mru = searcher.search_oneway(
                gateway, "MRU", dep, dest_entity=dest_eid, quiet=True
            )
            mru_gw = searcher.search_oneway(
                "MRU", gateway, ret, origin_entity=dest_eid, quiet=True
            )
            gw_amd = searcher.search_oneway(gateway, "AMD", ret, quiet=True)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            if not (amd_gw and gw_mru and mru_gw and gw_amd):
                continue

            l1 = min(amd_gw, key=lambda o: o.price_raw)
            l2 = min(gw_mru, key=lambda o: o.price_raw)
            l3 = min(mru_gw, key=lambda o: o.price_raw)
            l4 = min(gw_amd, key=lambda o: o.price_raw)
            total = l1.price_raw + l2.price_raw + l3.price_raw + l4.price_raw
            trip_days = (ret - dep).days
            ts = l1.stops + l2.stops + l3.stops + l4.stops
            td = l1.duration + l2.duration + l3.duration + l4.duration

            chain = Flight(
                itinerary_id=f"gw4_{gateway}_{dep}_{ret}",
                origin=f"AMD(via {gateway} 4-leg)",
                destination=dest_label,
                dest_label=dest_label,
                departure_date=str(dep),
                return_date=str(ret),
                trip_days=trip_days,
                price_raw=total,
                price_formatted=f"Rs.{total:,.0f}",
                positioning_cost=int(l1.price_raw),
                effective_total=total,
                outbound_duration=l1.duration + l2.duration,
                return_duration=l3.duration + l4.duration,
                total_duration=td,
                outbound_stops=l1.stops + l2.stops,
                return_stops=l3.stops + l4.stops,
                total_stops=ts,
                outbound_carriers=f"{l1.carriers}/{l2.carriers}",
                return_carriers=f"{l3.carriers}/{l4.carriers}",
                outbound_departure=l1.departure,
                outbound_arrival=l2.arrival,
                return_departure=l3.departure,
                return_arrival=l4.arrival,
                search_type="international_gateway",
                value_score=compute_value_score(
                    total, ts, td, int(l1.price_raw), "international_gateway"
                ),
                positioning_source="live",
            )
            chained.append(chain)
            route_key = f"AMD->{gateway}->{dest_label}|4leg|{dep}|{ret}"
            if price_history:
                price_history.record(
                    route_key, total, "international_gateway",
                    dep_date=str(dep),
                    carriers=f"{l1.carriers}, {l2.carriers}, {l3.carriers}, {l4.carriers}",
                )
            print(
                f"  AMD→{gateway}→MRU→{gateway}→AMD {dep}-{ret}: Rs.{total:,.0f}",
                flush=True,
            )

    return chained


def run_international_gateway_search(
    searcher: Searcher,
    gateways: List[str],
    dest_variants: List[Tuple[str, str]],
    date_pairs: List[Tuple[date, date]],
    price_history: Optional[PriceHistory] = None,
) -> List[Flight]:
    dest_label, dest_eid = dest_variants[0]

    gateway_flights = run_search_batch(
        searcher,
        gateways,
        dest_variants,
        date_pairs[:12],
        search_type="international_gateway",
        label="Phase 6: International Gateways",
        price_history=price_history,
        checkpoint_every=0,
    )

    print("\n--- Phase 6b: AMD -> Gateway -> Mauritius (RT positioning add-on) ---")
    chained: List[Flight] = []
    for gw_f in sorted(gateway_flights, key=lambda f: f.price_raw)[:8]:
        dep = date.fromisoformat(gw_f.departure_date)
        ret = date.fromisoformat(gw_f.return_date)
        amd_to_gw = searcher.fetch_positioning_fare("AMD", gw_f.origin, dep)
        gw_to_amd = searcher.fetch_positioning_fare(gw_f.origin, "AMD", ret)
        amd_out = amd_to_gw if amd_to_gw is not None else POSITIONING_ESTIMATES.get(gw_f.origin, 8000)
        amd_back = gw_to_amd if gw_to_amd is not None else POSITIONING_ESTIMATES.get(gw_f.origin, 8000)
        pos_int = int(amd_out + amd_back)
        pos_source = "live" if amd_to_gw is not None else "estimate"
        total = gw_f.price_raw + pos_int
        chained.append(
            Flight(
                itinerary_id=f"chain_{gw_f.itinerary_id}",
                origin=f"AMD(via {gw_f.origin})",
                destination=gw_f.destination,
                dest_label=gw_f.dest_label,
                departure_date=gw_f.departure_date,
                return_date=gw_f.return_date,
                trip_days=gw_f.trip_days,
                price_raw=total,
                price_formatted=f"Rs.{total:,.0f}",
                positioning_cost=pos_int,
                effective_total=total,
                outbound_duration=gw_f.outbound_duration,
                return_duration=gw_f.return_duration,
                total_duration=gw_f.total_duration,
                outbound_stops=gw_f.outbound_stops,
                return_stops=gw_f.return_stops,
                total_stops=gw_f.total_stops,
                outbound_carriers=gw_f.outbound_carriers,
                return_carriers=gw_f.return_carriers,
                outbound_departure=gw_f.outbound_departure,
                outbound_arrival=gw_f.outbound_arrival,
                return_departure=gw_f.return_departure,
                return_arrival=gw_f.return_arrival,
                search_type="international_gateway",
                value_score=compute_value_score(
                    total,
                    gw_f.total_stops,
                    gw_f.total_duration,
                    pos_int,
                    "international_gateway",
                ),
                positioning_source=pos_source,
            )
        )
        print(
            f"  AMD↔{gw_f.origin}↔MRU {gw_f.departure_date}-{gw_f.return_date}: "
            f"Rs.{total:,.0f} (pos out={amd_out:,.0f} back={amd_back:,.0f})",
            flush=True,
        )

    four_leg = run_gateway_oneway_chains(
        searcher, gateways, dest_eid, dest_label,
        date_pairs[:8], price_history=price_history,
    )
    return gateway_flights + chained + four_leg


# ============================================================
# CHECKPOINT
# ============================================================

def save_checkpoint(
    phase: str,
    index: int,
    total: int,
    flights: List[Flight],
    searcher: Searcher,
):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"checkpoint_{index}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "phase": phase,
                "progress": f"{index}/{total}",
                "flights_collected": len(flights),
                "fails": searcher.fail_count,
                "positioning_cache": {
                    f"{k[0]}|{k[1]}": v for k, v in searcher.positioning_cache.items()
                },
                "all_flights": [asdict(fl) for fl in flights],
            },
            f,
            indent=2,
        )
    print(f"\n  ** Checkpoint saved: {path}")


def load_latest_checkpoint() -> Optional[dict]:
    try:
        files = [
            f
            for f in os.listdir(OUTPUT_DIR)
            if f.startswith("checkpoint_") and f.endswith(".json")
        ]
        if not files:
            return None
        files.sort(key=lambda f: int(f.split("_")[1]))
        with open(os.path.join(OUTPUT_DIR, files[-1]), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def flights_from_checkpoint(data: dict) -> List[Flight]:
    flights = []
    for f_dict in data.get("all_flights", []):
        try:
            flights.append(Flight(**f_dict))
        except Exception:
            pass
    return flights


# ============================================================
# RESULTS
# ============================================================

def deduplicate(flights: List[Flight]) -> List[Flight]:
    seen = {}
    for f in flights:
        key = (f.itinerary_id, f.search_type, f.departure_date, f.return_date)
        if key not in seen or f.price_raw < seen[key].price_raw:
            seen[key] = f
    return list(seen.values())


def print_results(unique: List[Flight], valid_origins: List[str], elapsed: float):
    cheapest = sorted(unique, key=lambda f: f.price_raw)
    by_value = sorted(unique, key=lambda f: f.value_score)
    by_effective = sorted(unique, key=lambda f: f.effective_total)

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)

    print("\n--- TOP 50 CHEAPEST ---")
    print(
        f"{'#':>3} {'Price':>12} {'Type':>18} {'Origin':>12} "
        f"{'Depart':>10} {'Return':>10} {'Days':>4} {'Stops':>5}"
    )
    for i, f in enumerate(cheapest[:50]):
        print(
            f"{i+1:3d} {f.price_formatted:>12} {f.search_type:>18} {f.origin:>12} "
            f"{f.departure_date:>10} {f.return_date:>10} "
            f"{f.trip_days:4d} {f.total_stops:5d}"
        )

    print("\n--- TOP 20 BY VALUE SCORE ---")
    for i, f in enumerate(by_value[:20]):
        print(
            f"{i+1:3d} score={f.value_score:,.0f} Rs.{f.price_raw:,.0f} "
            f"{f.origin} {f.departure_date}-{f.return_date} "
            f"({f.search_type})"
        )

    print("\n--- TOP 20 AFTER POSITIONING (from AMD) ---")
    for i, f in enumerate(by_effective[:20]):
        print(
            f"{i+1:3d} fare={f.price_raw:,.0f} pos={f.positioning_cost:,} "
            f"({f.positioning_source}) total={f.effective_total:,.0f} "
            f"{f.origin} {f.departure_date}-{f.return_date}"
        )

    oneway = [f for f in cheapest if f.search_type == "oneway_combo"]
    if oneway:
        c = oneway[0]
        print(f"\n--- CHEAPEST ONE-WAY COMBO ---")
        print(
            f"  Rs.{c.price_raw:,.0f} | {c.origin} | {c.departure_date}-{c.return_date} | "
            f"{c.outbound_carriers} / {c.return_carriers}"
        )

    if cheapest:
        c = cheapest[0]
        print(f"\n--- CHEAPEST OVERALL ---")
        print(
            f"  {c.price_formatted} | {c.search_type} | {c.origin}->{c.destination} | "
            f"{c.departure_date} to {c.return_date} ({c.trip_days}d) | "
            f"{c.total_stops} stops"
        )

    for budget in [45000, 50000, 55000, 60000]:
        under = [f for f in cheapest if f.price_raw <= budget]
        print(f"\n--- UNDER Rs.{budget:,}: {len(under)} itineraries ---")
        for f in under[:3]:
            print(
                f"    Rs.{f.price_raw:,.0f} | {f.search_type} | {f.origin} | "
                f"{f.departure_date}-{f.return_date}"
            )


def export_results(
    unique: List[Flight],
    searcher: Searcher,
    total_searches: int,
    elapsed: float,
    price_history: PriceHistory,
):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    cheapest = sorted(unique, key=lambda f: f.price_raw)
    by_effective = sorted(unique, key=lambda f: f.effective_total)

    csv_path = os.path.join(OUTPUT_DIR, f"best_itineraries_{ts}.csv")
    if cheapest:
        fields = list(asdict(cheapest[0]).keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for fl in cheapest:
                w.writerow(asdict(fl))
        print(f"\n  CSV: {csv_path} ({len(cheapest)} flights)")

    json_path = os.path.join(OUTPUT_DIR, f"best_itineraries_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated": datetime.now().isoformat(),
                "config": {
                    "dep_range": f"{DEP_START} to {DEP_END}",
                    "ret_range": f"{RET_START} to {RET_END}",
                    "trip_days": f"{MIN_TRIP_DAYS}-{MAX_TRIP_DAYS}",
                    "origins": INDIAN_ORIGINS,
                    "gateways": INTERNATIONAL_GATEWAYS,
                },
                "search_stats": {
                    "total_searches": total_searches,
                    "failed": searcher.fail_count,
                    "unique_flights": len(unique),
                    "runtime_minutes": round(elapsed / 60, 1),
                },
                "positioning_cache": {
                    f"{k[0]}|{k[1]}": v for k, v in searcher.positioning_cache.items()
                },
                "top_50_cheapest": [asdict(f) for f in cheapest[:50]],
                "top_20_effective": [asdict(f) for f in by_effective[:20]],
                "price_history_summary": price_history.data.get("routes", {}),
                "weekday_summary": price_history.weekday_summary(),
                "airline_summary": price_history.data.get("airlines", {}),
            },
            f,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    print(f"  JSON: {json_path}")
    price_history.save()
    print(f"  Price history: {PRICE_HISTORY_PATH}")


# ============================================================
# MAIN PIPELINE
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Mauritius trip optimizer v3")
    parser.add_argument(
        "--phase",
        choices=[
            "all", "coarse", "refine", "positioning", "oneway",
            "gateway", "hub_graph", "monitor", "monte",
        ],
        default="all",
        help="Pipeline phase (monitor/monte = daily Monte Carlo sampling)",
    )
    parser.add_argument(
        "--skip-gateway",
        action="store_true",
        help="Skip international gateway searches",
    )
    parser.add_argument(
        "--skip-oneway",
        action="store_true",
        help="Skip one-way combination search",
    )
    parser.add_argument(
        "--skip-hub-graph",
        action="store_true",
        help="Skip AMD→hub→MRU graph routing",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from latest checkpoint if available",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=CONCURRENT_WORKERS,
        help=f"Parallel search threads (default: {CONCURRENT_WORKERS})",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=MAX_TOTAL_API_CALLS,
        help=f"Global API call cap (default: {MAX_TOTAL_API_CALLS})",
    )
    parser.add_argument(
        "--monte-samples",
        type=int,
        default=MONTE_CARLO_SAMPLES,
        help=f"Monte Carlo monitor sample count (default: {MONTE_CARLO_SAMPLES})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    t_start = time.time()
    alerts: List[str] = []
    budget = SearchBudget(max_calls=args.max_calls)
    workers = max(1, args.workers)

    print("=" * 70)
    print("MAURITIUS TRIP OPTIMIZER v3")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Phase: {args.phase} | Workers: {workers} | Budget: {args.max_calls} API calls")
    print("=" * 70)

    price_history = PriceHistory()
    searcher = Searcher(budget=budget)
    all_flights: List[Flight] = []

    if args.resume:
        cp = load_latest_checkpoint()
        if cp:
            all_flights = flights_from_checkpoint(cp)
            searcher.fail_count = cp.get("fails", 0)
            for k, v in cp.get("positioning_cache", {}).items():
                hub, d = k.split("|", 1)
                searcher.positioning_cache[(hub, d)] = v
            print(f"Resumed {len(all_flights)} flights from checkpoint")

    print("\n--- Entity Resolution ---")
    valid_origins = []
    for code in INDIAN_ORIGINS:
        eid = resolve_entity(code)
        if eid:
            print(f"  {code}: {eid}")
            valid_origins.append(code)
        else:
            print(f"  {code}: FAILED - skipping")

    for code in INTERNATIONAL_GATEWAYS:
        resolve_entity(code)

    dest_variants = resolve_destination_variants()
    dep_dates, ret_dates = build_all_dep_ret_dates()
    coarse_pairs = build_coarse_pairs()
    full_pairs = valid_date_pairs(dep_dates, ret_dates)

    print(f"\nDate space: {len(dep_dates)} departures × {len(ret_dates)} returns")
    print(f"  Valid pairs ({MIN_TRIP_DAYS}-{MAX_TRIP_DAYS} days): {len(full_pairs)}")
    print(f"  Coarse pairs (weekday reps + every {COARSE_DAY_STEP} days): {len(coarse_pairs)}")

    dest_entity_map = {label: eid for label, eid in dest_variants}

    is_monitor = args.phase in ("monitor", "monte")
    run_coarse = args.phase in ("all", "coarse") and not is_monitor
    run_refine = args.phase in ("all", "refine")
    run_positioning = args.phase in ("all", "positioning")
    run_oneway = args.phase in ("all", "oneway") and not args.skip_oneway
    run_gateway = args.phase in ("all", "gateway") and not args.skip_gateway
    run_hub = args.phase in ("all", "hub_graph") and not args.skip_hub_graph

    if is_monitor:
        mc_flights = run_monte_carlo_monitor(
            budget, valid_origins, dest_variants, full_pairs,
            dest_entity_map, price_history, workers, args.monte_samples, alerts,
        )
        all_flights.extend(mc_flights)
        price_history.record_run("monte_carlo", budget.used, len(mc_flights))
    elif run_coarse:
        coarse_flights = run_search_batch(
            searcher,
            valid_origins,
            dest_variants,
            coarse_pairs,
            search_type="roundtrip",
            label="Phase 1: Coarse Date Scan",
            price_history=price_history,
            budget=budget,
            workers=workers,
            alerts=alerts,
        )
        all_flights.extend(coarse_flights)
        price_history.record_run("coarse", budget.used, len(coarse_flights))

    refine_pairs = coarse_pairs
    region_prices: Dict[Tuple[str, str], float] = {}
    if run_refine and all_flights and not budget.exhausted:
        regions, region_prices = find_promising_regions(all_flights, TOP_REGIONS)
        n_regions = adaptive_region_count(region_prices)
        if n_regions > TOP_REGIONS:
            all_regions, all_prices = find_promising_regions(all_flights, n_regions)
            regions, region_prices = all_regions, all_prices
        refine_pairs = build_refine_pairs(regions, region_prices)
        print(f"\n  Refining {len(regions)} regions -> {len(refine_pairs)} date pairs (budget {ADAPTIVE_REFINE_BUDGET})")
        refine_flights = run_search_batch(
            searcher,
            valid_origins,
            dest_variants,
            refine_pairs,
            search_type="roundtrip_refined",
            label="Phase 2: Adaptive Local Refinement",
            price_history=price_history,
            budget=budget,
            workers=workers,
            alerts=alerts,
        )
        all_flights.extend(refine_flights)
        price_history.record_run("refine", budget.used, len(refine_flights))

    positioning_dates = sorted({dep for dep, _ in refine_pairs})
    if run_positioning and not budget.exhausted:
        run_positioning_fares(searcher, POSITIONING_HUBS, positioning_dates[:15])
        for f in all_flights:
            if f.origin == "AMD" or f.origin.startswith("AMD("):
                continue
            pos, src = searcher.get_positioning_cost(
                f.origin, date.fromisoformat(f.departure_date)
            )
            f.positioning_cost = pos
            f.positioning_source = src
            f.effective_total = f.price_raw + pos
            f.value_score = compute_value_score(
                f.price_raw, f.total_stops, f.total_duration, pos, f.search_type
            )

    if run_oneway and not budget.exhausted:
        oneway_targets = seed_oneway_targets(all_flights, ONEWAY_SEED_COUNT)
        print(f"  One-way seeds: {len(oneway_targets)} cheapest refined round trips")
        oneway_flights = run_oneway_combos(
            searcher,
            oneway_targets,
            dest_entity_map,
            price_history=price_history,
        )
        all_flights.extend(oneway_flights)
        price_history.record_run("oneway", budget.used, len(oneway_flights))

    if run_gateway and not budget.exhausted:
        gw_flights = run_international_gateway_search(
            searcher,
            INTERNATIONAL_GATEWAYS,
            dest_variants[:1],
            coarse_pairs[:10],
            price_history=price_history,
        )
        all_flights.extend(gw_flights)
        price_history.record_run("gateway", budget.used, len(gw_flights))

    if run_hub and not budget.exhausted and dest_variants:
        dest_label, dest_eid = dest_variants[0]
        hub_flights = run_hub_graph_search(
            searcher,
            HUB_GRAPH_HUBS,
            dest_eid,
            dest_label,
            refine_pairs[:HUB_GRAPH_MAX_PAIRS],
            price_history=price_history,
            alerts=alerts,
        )
        all_flights.extend(hub_flights)
        price_history.record_run("hub_graph", budget.used, len(hub_flights))

    unique = deduplicate(all_flights)
    elapsed = time.time() - t_start

    print(f"\n\nSearch complete: {elapsed/60:.1f} minutes")
    print(f"  API searches: {budget.used} / {budget.max_calls}")
    print(f"  Raw flights: {len(all_flights)}")
    print(f"  Unique flights: {len(unique)}")
    print(f"  Failed searches: {searcher.fail_count}")
    if budget.exhausted:
        print("  NOTE: API budget exhausted — later phases may have been skipped")

    print_results(unique, valid_origins, elapsed)
    export_results(unique, searcher, budget.used, elapsed, price_history)
    save_alerts(alerts)

    if price_history.data.get("routes"):
        print("\n--- PRICE TREND SUMMARY ---")
        for route, info in sorted(
            price_history.data["routes"].items(),
            key=lambda x: x[1].get("min_price_seen") or 999999,
        )[:10]:
            print(
                f"  {route}: min=Rs.{info.get('min_price_seen', 0):,.0f} "
                f"avg=Rs.{info.get('avg_price', 0):,.0f} "
                f"max=Rs.{info.get('max_price_seen', 0):,.0f} "
                f"({len(info.get('observations', []))} obs)"
            )

    wd_summary = price_history.weekday_summary()
    if wd_summary:
        print("\n--- WEEKDAY DEPARTURE PATTERNS ---")
        for wd in WEEKDAY_NAMES:
            if wd in wd_summary:
                s = wd_summary[wd]
                print(f"  {wd}: min=Rs.{s['min']:,.0f} avg=Rs.{s['avg']:,.0f} ({s['count']} obs)")

    cheap_airlines = price_history.cheapest_airlines(5)
    if cheap_airlines:
        print("\n--- CHEAPEST AIRLINES (by avg fare) ---")
        for name, avg in cheap_airlines:
            print(f"  {name}: avg Rs.{avg:,.0f}")

    weekly = price_history.weekly_best()
    if weekly:
        print(f"\n--- BEST THIS WEEK ---")
        print(f"  Rs.{weekly['price']:,.0f} on {weekly['route']}")

    all_time = price_history.data.get("all_time_best")
    if all_time:
        print(f"\n--- ALL-TIME LOW (fare) ---")
        print(f"  Rs.{all_time['price']:,.0f} on {all_time['route']} ({all_time.get('timestamp', '')[:10]})")

    all_time_eff = price_history.data.get("all_time_best_effective")
    if all_time_eff:
        print(f"\n--- ALL-TIME LOW (effective from AMD) ---")
        print(
            f"  Rs.{all_time_eff['effective_total']:,.0f} "
            f"{all_time_eff['origin']} {all_time_eff['departure_date']}-{all_time_eff['return_date']}"
        )

    volatile = price_history.volatile_routes(5)
    if volatile:
        print("\n--- MOST VOLATILE ROUTES (monitor these daily) ---")
        for route, vol in volatile:
            print(f"  {route}: volatility {vol:.1%}")

    print(f"\nCompleted: {datetime.now().isoformat()}")
    print(f"Total runtime: {elapsed/60:.1f} minutes")
    print(f"Output files in: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
