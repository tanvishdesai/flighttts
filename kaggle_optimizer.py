# %% [markdown]
# # Mauritius Trip Optimizer — Skyscanner API (v2)
#
# Multi-phase search: coarse date scan → local refinement → real positioning fares
# → one-way combinations → international gateways → price monitoring.
#
# **No API key required.** Run locally or on Kaggle with Internet enabled.

# %%
"""
Skyscanner Mauritius Trip Optimizer v2
Broad date flexibility, two-stage search, one-way combos, and price tracking.
"""

import argparse
import csv
import json
import os
import random
import time
import uuid
from collections import defaultdict
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

# Two-stage search
COARSE_DAY_STEP = 3
REFINE_RADIUS_DAYS = 2
TOP_REGIONS = 20

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
# PRICE HISTORY
# ============================================================

class PriceHistory:
    def __init__(self, path: str = PRICE_HISTORY_PATH):
        self.path = path
        self.data = {"routes": {}, "runs": []}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                pass

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def record(self, route_key: str, price: float, search_type: str = "roundtrip"):
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
        entry["observations"].append(
            {"timestamp": ts, "price": price, "time_bucket": bucket}
        )
        prices = [o["price"] for o in entry["observations"]]
        entry["min_price_seen"] = min(prices)
        entry["max_price_seen"] = max(prices)
        entry["avg_price"] = round(sum(prices) / len(prices), 2)

    def record_run(self, phase: str, searches: int, flights: int):
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
    def __init__(self):
        self.session = requests.Session()
        self.search_count = 0
        self.fail_count = 0
        self.positioning_cache: Dict[Tuple[str, str], float] = {}
        self._set_headers()

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
        quiet: bool = False,
    ) -> List[OneWayOption]:
        self.search_count += 1
        o_eid = resolve_entity(origin)
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
    coarse_deps = subsample_dates(dep_dates, COARSE_DAY_STEP)
    coarse_rets = subsample_dates(ret_dates, COARSE_DAY_STEP)
    return valid_date_pairs(coarse_deps, coarse_rets)


def find_promising_regions(
    flights: List[Flight], top_n: int = TOP_REGIONS
) -> List[Tuple[str, str]]:
    region_prices: Dict[Tuple[str, str], float] = {}
    for f in flights:
        key = (f.departure_date, f.return_date)
        region_prices[key] = min(region_prices.get(key, 999999999), f.price_raw)
    ranked = sorted(region_prices.items(), key=lambda x: x[1])
    return [key for key, _ in ranked[:top_n]]


def build_refine_pairs(regions: List[Tuple[str, str]]) -> List[Tuple[date, date]]:
    pairs: Set[Tuple[date, date]] = set()
    for dep_str, ret_str in regions:
        dep = date.fromisoformat(dep_str)
        ret = date.fromisoformat(ret_str)
        dep_candidates = expand_date(dep, REFINE_RADIUS_DAYS, DEP_START, DEP_END)
        ret_candidates = expand_date(ret, REFINE_RADIUS_DAYS, RET_START, RET_END)
        pairs.update(valid_date_pairs(dep_candidates, ret_candidates))
    return sorted(pairs)


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
) -> List[Flight]:
    combos = [
        (origin, dest_label, dest_eid, dep, ret)
        for origin, (dest_label, dest_eid), (dep, ret) in product(
            origins, dest_variants, date_pairs
        )
    ]
    total = len(combos)
    all_flights = list(existing_flights or [])
    t_start = time.time()

    print(f"\n--- {label} ({total} searches) ---")
    if start_index:
        print(f"  Resuming from search #{start_index}")

    for i, (origin, dest_label, dest_eid, dep, ret) in enumerate(combos):
        if i < start_index:
            continue

        if i % 10 == 0:
            elapsed = time.time() - t_start
            rate = max((i + 1 - start_index) / max(elapsed, 1), 0.001)
            eta_m = (total - i) / rate / 60
            print(
                f"\n  [{i+1}/{total}] {i/total*100:.0f}% | "
                f"Flights: {len(all_flights)} | Fails: {searcher.fail_count} | "
                f"ETA: {eta_m:.0f}m",
                flush=True,
            )

        flights = searcher.search_roundtrip(
            origin, dest_eid, dest_label, dep, ret, search_type=search_type
        )
        all_flights.extend(flights)

        if flights:
            best = min(f.price_raw for f in flights)
            route_key = f"{origin}->{dest_label}|{dep}|{ret}"
            if price_history:
                price_history.record(route_key, best, search_type)
            print(
                f"  {origin}->{dest_label} {dep.strftime('%m/%d')}-"
                f"{ret.strftime('%m/%d')} {len(flights):3d}fl Rs.{best:,.0f}",
                flush=True,
            )
        else:
            print(
                f"  {origin}->{dest_label} {dep.strftime('%m/%d')}-"
                f"{ret.strftime('%m/%d')} FAIL",
                flush=True,
            )

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        if checkpoint_every and (i + 1) % checkpoint_every == 0:
            save_checkpoint(
                phase=label,
                index=i + 1,
                total=total,
                flights=all_flights,
                searcher=searcher,
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
    origins: List[str],
    dest_variants: List[Tuple[str, str]],
    date_pairs: List[Tuple[date, date]],
    price_history: Optional[PriceHistory] = None,
    max_pairs: int = 30,
) -> List[Flight]:
    print(f"\n--- Phase 5: One-Way Combinations ---")
    limited_pairs = date_pairs[:max_pairs]
    combined: List[Flight] = []

    for origin in origins:
        for dest_label, dest_eid in dest_variants:
            for dep, ret in limited_pairs:
                outbound = searcher.search_oneway(
                    origin, "MRU", dep, dest_entity=dest_eid
                )
                inbound = searcher.search_oneway("MRU", origin, ret)
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

                if not outbound or not inbound:
                    continue

                best_ob = min(outbound, key=lambda o: o.price_raw)
                best_ib = min(inbound, key=lambda o: o.price_raw)
                total_price = best_ob.price_raw + best_ib.price_raw
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
                    price_history.record(route_key, total_price, "oneway_combo")
                print(
                    f"  {origin}->{dest_label} OW {dep}-{ret}: "
                    f"Rs.{total_price:,.0f} ({best_ob.carriers}/{best_ib.carriers})",
                    flush=True,
                )

    return combined


def run_international_gateway_search(
    searcher: Searcher,
    gateways: List[str],
    dest_variants: List[Tuple[str, str]],
    date_pairs: List[Tuple[date, date]],
    price_history: Optional[PriceHistory] = None,
) -> List[Flight]:
    gateway_flights = run_search_batch(
        searcher,
        gateways,
        dest_variants,
        date_pairs[:20],
        search_type="international_gateway",
        label="Phase 6: International Gateways",
        price_history=price_history,
        checkpoint_every=0,
    )

    print("\n--- Phase 6b: AMD -> Gateway -> Mauritius chains ---")
    chained: List[Flight] = []
    for gw_f in sorted(gateway_flights, key=lambda f: f.price_raw)[:10]:
        dep = date.fromisoformat(gw_f.departure_date)
        amd_to_gw = searcher.fetch_positioning_fare("AMD", gw_f.origin, dep)
        if amd_to_gw is None:
            amd_to_gw = POSITIONING_ESTIMATES.get(gw_f.origin, 8000)
        pos_source = "live" if amd_to_gw != POSITIONING_ESTIMATES.get(gw_f.origin) else "estimate"
        pos_int = int(amd_to_gw)
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
            f"  AMD->{gw_f.origin}->{gw_f.destination} "
            f"{gw_f.departure_date}-{gw_f.return_date}: Rs.{total:,.0f}",
            flush=True,
        )
    return gateway_flights + chained


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
    parser = argparse.ArgumentParser(description="Mauritius trip optimizer v2")
    parser.add_argument(
        "--phase",
        choices=["all", "coarse", "refine", "positioning", "oneway", "gateway", "monitor"],
        default="all",
        help="Which pipeline phase to run (default: all)",
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
        "--resume",
        action="store_true",
        help="Resume from latest checkpoint if available",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    t_start = time.time()

    print("=" * 70)
    print("MAURITIUS TRIP OPTIMIZER v2")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Phase: {args.phase}")
    print("=" * 70)

    price_history = PriceHistory()
    searcher = Searcher()
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
    print(f"  Coarse pairs (every {COARSE_DAY_STEP} days): {len(coarse_pairs)}")

    run_coarse = args.phase in ("all", "coarse", "monitor")
    run_refine = args.phase in ("all", "refine")
    run_positioning = args.phase in ("all", "positioning")
    run_oneway = args.phase in ("all", "oneway") and not args.skip_oneway
    run_gateway = args.phase in ("all", "gateway") and not args.skip_gateway

    if run_coarse:
        coarse_flights = run_search_batch(
            searcher,
            valid_origins,
            dest_variants,
            coarse_pairs,
            search_type="roundtrip",
            label="Phase 1: Coarse Date Scan",
            price_history=price_history,
        )
        all_flights.extend(coarse_flights)
        price_history.record_run("coarse", searcher.search_count, len(coarse_flights))

    refine_pairs = coarse_pairs
    if run_refine and all_flights:
        regions = find_promising_regions(all_flights)
        refine_pairs = build_refine_pairs(regions)
        print(f"\n  Refining {len(regions)} regions -> {len(refine_pairs)} date pairs")
        refine_flights = run_search_batch(
            searcher,
            valid_origins,
            dest_variants,
            refine_pairs,
            search_type="roundtrip_refined",
            label="Phase 2: Local Refinement",
            price_history=price_history,
        )
        all_flights.extend(refine_flights)
        price_history.record_run("refine", searcher.search_count, len(refine_flights))

    positioning_dates = sorted({dep for dep, _ in refine_pairs})
    if run_positioning:
        run_positioning_fares(searcher, POSITIONING_HUBS, positioning_dates[:15])
        for f in all_flights:
            if f.origin != "AMD":
                pos, src = searcher.get_positioning_cost(f.origin, date.fromisoformat(f.departure_date))
                f.positioning_cost = pos
                f.positioning_source = src
                f.effective_total = f.price_raw + pos
                f.value_score = compute_value_score(
                    f.price_raw, f.total_stops, f.total_duration, pos, f.search_type
                )

    if run_oneway:
        top_origins = valid_origins[:8]
        oneway_flights = run_oneway_combos(
            searcher,
            top_origins,
            dest_variants[:1],
            refine_pairs[:30],
            price_history=price_history,
        )
        all_flights.extend(oneway_flights)
        price_history.record_run("oneway", searcher.search_count, len(oneway_flights))

    if run_gateway:
        gw_flights = run_international_gateway_search(
            searcher,
            INTERNATIONAL_GATEWAYS,
            dest_variants[:1],
            coarse_pairs[:15],
            price_history=price_history,
        )
        all_flights.extend(gw_flights)
        price_history.record_run("gateway", searcher.search_count, len(gw_flights))

    unique = deduplicate(all_flights)
    elapsed = time.time() - t_start

    print(f"\n\nSearch complete: {elapsed/60:.1f} minutes")
    print(f"  API searches: {searcher.search_count}")
    print(f"  Raw flights: {len(all_flights)}")
    print(f"  Unique flights: {len(unique)}")
    print(f"  Failed searches: {searcher.fail_count}")

    print_results(unique, valid_origins, elapsed)
    export_results(unique, searcher, searcher.search_count, elapsed, price_history)

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

    print(f"\nCompleted: {datetime.now().isoformat()}")
    print(f"Total runtime: {elapsed/60:.1f} minutes")
    print(f"Output files in: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
