# %% [markdown]
# # Mauritius Trip Optimizer — Skyscanner API
# 
# Finds the absolute cheapest flights to Mauritius from any major Indian airport.
# 
# **Search space:** 9 origins × 10 departure dates × 16 return dates = 1,440 searches
# 
# **Runtime:** ~4-6 hours on Kaggle (each search takes ~10-15s with API polling)
# 
# **No API key required.** No authentication. Just HTTP requests.

# %% [markdown]
# ## How to run on Kaggle
# 
# 1. Create a new Kaggle Notebook
# 2. Paste this entire script into a single code cell
# 3. **Turn ON Internet** (Settings → Internet → On)
# 4. Click **Run All**
# 5. Results will be saved to `/kaggle/working/results/`
# 6. Download the CSV/JSON files when done
# 
# **Note:** Kaggle notebooks have a 12-hour limit. This script takes ~4-6 hours.

# %%
"""
Skyscanner Mauritius Trip Optimizer
Full search-space optimization across Indian airports.

Search space: 9 origins × 10 departures × 16 returns = 1,440 searches
Estimated runtime: 4-6 hours
"""

import requests
import uuid
import time
import json
import csv
import sys
import os
from datetime import date, datetime, timedelta
from dataclasses import dataclass, asdict
from itertools import product
from collections import defaultdict
import random

# ============================================================
# DETECT ENVIRONMENT
# ============================================================
IS_KAGGLE = os.path.exists("/kaggle/working")
OUTPUT_DIR = "/kaggle/working/results" if IS_KAGGLE else "results"
os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"Environment: {'Kaggle' if IS_KAGGLE else 'Local'}")
print(f"Output dir: {OUTPUT_DIR}")

# ============================================================
# CONFIG — EDIT THESE IF NEEDED
# ============================================================

BASE_URL = "https://www.skyscanner.co.in"
SEARCH_PATH = "/g/radar/api/v2/web-unified-search/"
AUTOSUGGEST_PATH = "/g/autosuggest-search/api/v1/search-flight"

# Positioning costs from Ahmedabad (approximate one-way domestic fare)
POSITIONING_COSTS = {
    "AMD": 0,      # Home airport
    "BOM": 4000,    # Mumbai
    "DEL": 6000,    # Delhi
    "PNQ": 4000,    # Pune
    "BLR": 6000,    # Bangalore
    "HYD": 5000,    # Hyderabad
    "MAA": 6000,    # Chennai
    "CCU": 7000,    # Kolkata
    "GOI": 4000,    # Goa
}

# Known entity IDs (from HAR analysis + autosuggest)
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
}

# Origins to search
ORIGINS = ["AMD", "BOM", "DEL", "PNQ", "BLR", "HYD", "MAA", "CCU", "GOI"]

# Destination
DEST = "MRU"

# Departure dates: July 1-10, 2026 (must depart BEFORE July 11)
DEP_DATES = [date(2026, 7, d) for d in range(1, 11)]

# Return dates: July 16-31, 2026 (must return AFTER July 15)
RET_DATES = [date(2026, 7, d) for d in range(16, 32)]

# Delay between searches (seconds). Lower = faster but riskier.
# The API has been tested at 0.5-1.5s with zero blocks across 27 searches.
MIN_DELAY = 0.5
MAX_DELAY = 1.5

# Max polls per search (API uses async polling)
MAX_POLLS = 8
POLL_INTERVAL = 2  # seconds between polls


# ============================================================
# ENTITY RESOLVER
# ============================================================

def resolve_entity(iata):
    """Resolve IATA airport code to Skyscanner entity ID."""
    code = iata.upper()
    if code in ENTITY_CACHE:
        return ENTITY_CACHE[code]
    
    url = f"{BASE_URL}{AUTOSUGGEST_PATH}/IN/en-GB/{code}"
    try:
        resp = requests.get(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        }, timeout=10)
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
    except Exception as e:
        print(f"  WARN: autosuggest failed for {code}: {e}")
    return None


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class Flight:
    itinerary_id: str
    origin: str
    destination: str
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
    value_score: float  # price + 1000 * total_stops


# ============================================================
# SEARCH CLIENT
# ============================================================

class Searcher:
    def __init__(self):
        self.session = requests.Session()
        self.search_count = 0
        self.fail_count = 0
        self._set_headers()
    
    def _set_headers(self):
        vid = str(uuid.uuid4())
        self.session.headers.update({
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
        })
    
    def search(self, origin, dest, dep, ret):
        """Execute one search. Returns list of Flight objects."""
        self.search_count += 1
        
        o_eid = resolve_entity(origin)
        d_eid = resolve_entity(dest)
        if not o_eid or not d_eid:
            self.fail_count += 1
            return []
        
        # Fresh UUIDs per search
        vid = str(uuid.uuid4())
        self.session.headers["x-skyscanner-viewid"] = vid
        self.session.headers["x-skyscanner-trustedfunnelid"] = vid
        
        body = {
            "cabinClass": "ECONOMY",
            "adults": 1,
            "childAges": [],
            "legs": [
                {
                    "legOrigin": {"@type": "entity", "entityId": o_eid},
                    "legDestination": {"@type": "entity", "entityId": d_eid},
                    "dates": {
                        "@type": "date",
                        "year": str(dep.year),
                        "month": str(dep.month).zfill(2),
                        "day": str(dep.day).zfill(2),
                    },
                },
                {
                    "legOrigin": {"@type": "entity", "entityId": d_eid},
                    "legDestination": {"@type": "entity", "entityId": o_eid},
                    "dates": {
                        "@type": "date",
                        "year": str(ret.year),
                        "month": str(ret.month).zfill(2),
                        "day": str(ret.day).zfill(2),
                    },
                },
            ],
        }
        
        url = f"{BASE_URL}{SEARCH_PATH}"
        
        try:
            resp = self.session.post(url, json=body, timeout=30)
            if resp.status_code != 200:
                self.fail_count += 1
                return []
            
            # Capture gateway for sticky routing
            gw = resp.headers.get("x-gateway-servedby", "")
            if gw:
                self.session.headers["x-gateway-servedby"] = gw
            
            data = resp.json()
            ctx = data.get("context", {})
            status = ctx.get("status", "")
            sid = ctx.get("sessionId", "")
            
            # Poll until complete
            polls = 0
            while status != "complete" and polls < MAX_POLLS:
                polls += 1
                time.sleep(POLL_INTERVAL)
                try:
                    r = self.session.get(f"{url}{sid}", timeout=30)
                    if r.status_code != 200:
                        break
                    data = r.json()
                    ctx = data.get("context", {})
                    status = ctx.get("status", "")
                    sid = ctx.get("sessionId", "")
                except:
                    break
            
            # Parse results
            results = data.get("itineraries", {}).get("results", [])
            trip_days = (ret - dep).days
            pos_cost = POSITIONING_COSTS.get(origin, 0)
            
            flights = []
            for r in results:
                try:
                    price = r.get("price", {})
                    legs = r.get("legs", [])
                    if len(legs) < 2:
                        continue
                    
                    ob, ib = legs[0], legs[1]
                    
                    def carriers(leg):
                        return ", ".join(
                            c.get("name", "")
                            for c in leg.get("carriers", {}).get("marketing", [])
                        )
                    
                    p = price.get("raw", 0)
                    os_ = ob.get("stopCount", 0)
                    rs_ = ib.get("stopCount", 0)
                    ts = os_ + rs_
                    od = ob.get("durationInMinutes", 0)
                    rd = ib.get("durationInMinutes", 0)
                    
                    flights.append(Flight(
                        itinerary_id=r.get("id", ""),
                        origin=origin,
                        destination=dest,
                        departure_date=str(dep),
                        return_date=str(ret),
                        trip_days=trip_days,
                        price_raw=p,
                        price_formatted=price.get("formatted", ""),
                        positioning_cost=pos_cost,
                        effective_total=p + pos_cost,
                        outbound_duration=od,
                        return_duration=rd,
                        total_duration=od + rd,
                        outbound_stops=os_,
                        return_stops=rs_,
                        total_stops=ts,
                        outbound_carriers=carriers(ob),
                        return_carriers=carriers(ib),
                        outbound_departure=ob.get("departure", ""),
                        outbound_arrival=ob.get("arrival", ""),
                        return_departure=ib.get("departure", ""),
                        return_arrival=ib.get("arrival", ""),
                        value_score=p + 1000 * ts,
                    ))
                except:
                    continue
            
            return flights
            
        except Exception as e:
            self.fail_count += 1
            return []


# ============================================================
# MAIN EXECUTION
# ============================================================

t_start = time.time()
print("=" * 70)
print("MAURITIUS TRIP OPTIMIZER")
print(f"Started: {datetime.now().isoformat()}")
print("=" * 70)

# --- Phase 1: Entity Resolution ---
print("\n--- Phase 1: Entity Resolution ---")
valid_origins = []
for code in ORIGINS:
    eid = resolve_entity(code)
    if eid:
        print(f"  {code}: {eid}")
        valid_origins.append(code)
    else:
        print(f"  {code}: FAILED - skipping")

eid = resolve_entity(DEST)
print(f"  {DEST}: {eid}")

# --- Phase 2: Search ---
combos = list(product(valid_origins, DEP_DATES, RET_DATES))
total = len(combos)
print(f"\n--- Phase 2: Search ({total} combinations) ---")
print(f"  Origins: {valid_origins}")
print(f"  Departures: {DEP_DATES[0]} to {DEP_DATES[-1]}")
print(f"  Returns: {RET_DATES[0]} to {RET_DATES[-1]}")

searcher = Searcher()
all_flights = []
origin_stats = defaultdict(lambda: {"count": 0, "flights": 0, "min": 999999, "fails": 0})

for i, (origin, dep, ret) in enumerate(combos):
    # Progress report every 10 searches
    if i % 10 == 0:
        elapsed = time.time() - t_start
        rate = (i + 1) / max(elapsed, 1)
        eta_s = (total - i) / max(rate, 0.001)
        eta_m = eta_s / 60
        print(f"\n  [{i+1}/{total}] {i/total*100:.0f}% | "
              f"Flights: {len(all_flights)} | "
              f"Fails: {searcher.fail_count} | "
              f"ETA: {eta_m:.0f}m",
              flush=True)
    
    flights = searcher.search(origin, DEST, dep, ret)
    all_flights.extend(flights)
    
    # Track stats
    stats = origin_stats[origin]
    stats["count"] += 1
    stats["flights"] += len(flights)
    if flights:
        cheapest = min(f.price_raw for f in flights)
        stats["min"] = min(stats["min"], cheapest)
    else:
        stats["fails"] += 1
    
    # Print result
    if flights:
        best = min(f.price_raw for f in flights)
        print(f"  {origin}->MRU {dep.strftime('%m/%d')}-{ret.strftime('%m/%d')} "
              f"{len(flights):3d}fl Rs.{best:,.0f}", flush=True)
    else:
        print(f"  {origin}->MRU {dep.strftime('%m/%d')}-{ret.strftime('%m/%d')} FAIL", flush=True)
    
    # Delay between searches
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    
    # Save checkpoint every 100 searches
    if (i + 1) % 100 == 0:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cp = os.path.join(OUTPUT_DIR, f"checkpoint_{i+1}_{ts}.json")
        with open(cp, "w") as f:
            json.dump({
                "progress": f"{i+1}/{total}",
                "flights_collected": len(all_flights),
                "fails": searcher.fail_count,
                "origin_stats": {k: dict(v) for k, v in origin_stats.items()},
                "elapsed_min": round((time.time() - t_start) / 60, 1),
            }, f, indent=2)
        print(f"\n  ** Checkpoint saved: {cp}")

elapsed_total = time.time() - t_start
print(f"\n\n  Search complete: {elapsed_total/60:.1f} minutes")
print(f"  Total raw flights: {len(all_flights)}")
print(f"  Failed searches: {searcher.fail_count}/{total}")


# --- Phase 3: Deduplication ---
print("\n--- Phase 3: Deduplication ---")
seen = {}
for f in all_flights:
    key = f.itinerary_id
    if key not in seen or f.price_raw < seen[key].price_raw:
        seen[key] = f
unique = list(seen.values())
print(f"  Before: {len(all_flights)}")
print(f"  After:  {len(unique)}")


# --- Phase 4: Rankings ---
print("\n" + "=" * 70)
print("RESULTS")
print("=" * 70)

cheapest = sorted(unique, key=lambda f: f.price_raw)
by_value = sorted(unique, key=lambda f: f.value_score)
by_effective = sorted(unique, key=lambda f: f.effective_total)

# 1. Top 50 cheapest
print("\n--- TOP 50 CHEAPEST ---")
print(f"{'#':>3} {'Price':>12} {'Origin':>5} {'Depart':>10} {'Return':>10} "
      f"{'Days':>4} {'Stops':>5} {'Airline':<25}")
for i, f in enumerate(cheapest[:50]):
    print(f"{i+1:3d} {f.price_formatted:>12} {f.origin:>5} "
          f"{f.departure_date:>10} {f.return_date:>10} "
          f"{f.trip_days:4d} {f.total_stops:5d} "
          f"{f.outbound_carriers[:25]:<25}")

# 2. Top 20 after positioning
print("\n--- TOP 20 AFTER POSITIONING COSTS ---")
print(f"{'#':>3} {'Fare':>10} {'Pos':>6} {'Total':>12} {'Origin':>5} "
      f"{'Depart':>10} {'Return':>10} {'Stops':>5}")
for i, f in enumerate(by_effective[:20]):
    print(f"{i+1:3d} {f.price_raw:>10,.0f} {f.positioning_cost:>6,} "
          f"{f.effective_total:>12,.0f} {f.origin:>5} "
          f"{f.departure_date:>10} {f.return_date:>10} {f.total_stops:5d}")

# 3. Cheapest overall
if cheapest:
    c = cheapest[0]
    print(f"\n--- CHEAPEST OVERALL ---")
    print(f"  {c.price_formatted} | {c.origin}->{c.destination} | "
          f"{c.departure_date} to {c.return_date} ({c.trip_days}d) | "
          f"{c.total_stops} stops | {c.outbound_carriers} / {c.return_carriers}")

# 4. Cheapest non-stop
nonstop = [f for f in cheapest if f.total_stops == 0]
if nonstop:
    c = nonstop[0]
    print(f"\n--- CHEAPEST NON-STOP ---")
    print(f"  {c.price_formatted} | {c.origin}->{c.destination} | "
          f"{c.departure_date} to {c.return_date} ({c.trip_days}d) | "
          f"{c.outbound_carriers} / {c.return_carriers}")
else:
    print("\n--- CHEAPEST NON-STOP: None found ---")

# 5. Cheapest from AMD
amd_flights = [f for f in cheapest if f.origin == "AMD"]
if amd_flights:
    c = amd_flights[0]
    print(f"\n--- CHEAPEST FROM AHMEDABAD ---")
    print(f"  {c.price_formatted} | {c.departure_date} to {c.return_date} ({c.trip_days}d) | "
          f"{c.total_stops} stops | {c.outbound_carriers}")

# 6. Cheapest from any airport
if cheapest:
    c = cheapest[0]
    print(f"\n--- CHEAPEST FROM ANY AIRPORT ---")
    print(f"  {c.price_formatted} | {c.origin} | "
          f"{c.departure_date} to {c.return_date}")

# 7. Budget tiers
for budget in [50000, 55000, 60000]:
    under = [f for f in cheapest if f.price_raw <= budget]
    print(f"\n--- UNDER Rs.{budget:,} ---")
    if under:
        print(f"  {len(under)} itineraries found")
        for f in under[:5]:
            print(f"    Rs.{f.price_raw:,.0f} | {f.origin} | "
                  f"{f.departure_date}-{f.return_date} | "
                  f"{f.total_stops}stops | {f.outbound_carriers}")
    else:
        print(f"  None found")

# 8. Price heatmap
print(f"\n--- PRICE HEATMAP (Min fare, all origins, in thousands) ---")
heatmap = {}
for f in unique:
    key = (f.departure_date, f.return_date)
    if key not in heatmap or f.price_raw < heatmap[key]:
        heatmap[key] = f.price_raw

ret_strs = sorted(set(f.return_date for f in unique))
dep_strs = sorted(set(f.departure_date for f in unique))

hdr = f"{'Dep\\Ret':>12}"
for rd in ret_strs:
    hdr += f" {rd[5:]:>8}"
print(hdr)

for dd in dep_strs:
    row = f"{dd[5:]:>12}"
    for rd in ret_strs:
        price = heatmap.get((dd, rd), None)
        if price:
            row += f" {price/1000:>7.1f}k"
        else:
            row += f" {'---':>8}"
    print(row)

# 9. Airport comparison
print(f"\n--- AIRPORT COMPARISON ---")
print(f"{'Airport':>8} {'Min Fare':>12} {'Avg Fare':>12} "
      f"{'Itineraries':>12} {'Min+Pos':>12}")
for origin in sorted(valid_origins):
    flights_from = [f for f in unique if f.origin == origin]
    if flights_from:
        mn = min(f.price_raw for f in flights_from)
        avg = sum(f.price_raw for f in flights_from) / len(flights_from)
        pos = POSITIONING_COSTS.get(origin, 0)
        print(f"{origin:>8} {mn:>12,.0f} {avg:>12,.0f} "
              f"{len(flights_from):>12,} {mn+pos:>12,.0f}")


# --- Phase 5: Exports ---
print(f"\n--- Phase 5: Exports ---")
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

# CSV
csv_path = os.path.join(OUTPUT_DIR, f"best_itineraries_{ts}.csv")
if cheapest:
    fields = list(asdict(cheapest[0]).keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for fl in cheapest:
            w.writerow(asdict(fl))
    print(f"  CSV: {csv_path} ({len(cheapest)} flights)")

# JSON
json_path = os.path.join(OUTPUT_DIR, f"best_itineraries_{ts}.json")
with open(json_path, "w", encoding="utf-8") as f:
    json.dump({
        "generated": datetime.now().isoformat(),
        "search_stats": {
            "total_searches": total,
            "failed": searcher.fail_count,
            "total_flights_raw": len(all_flights),
            "unique_flights": len(unique),
            "runtime_minutes": round(elapsed_total / 60, 1),
        },
        "origin_stats": {k: dict(v) for k, v in origin_stats.items()},
        "top_50_cheapest": [asdict(f) for f in cheapest[:50]],
        "top_20_effective": [asdict(f) for f in by_effective[:20]],
        "cheapest_nonstop": asdict(nonstop[0]) if nonstop else None,
        "cheapest_from_amd": asdict(amd_flights[0]) if amd_flights else None,
        "all_flights": [asdict(f) for f in cheapest],
    }, f, indent=2, ensure_ascii=False, default=str)
print(f"  JSON: {json_path}")


# --- Phase 6: Final Answers ---
print(f"\n" + "=" * 70)
print("FINAL ANSWERS")
print("=" * 70)

if cheapest:
    c = cheapest[0]
    print(f"\n1. ABSOLUTE CHEAPEST WAY TO MAURITIUS:")
    print(f"   {c.price_formatted} from {c.origin}")
    print(f"   Depart {c.departure_date}, Return {c.return_date} ({c.trip_days} days)")
    print(f"   {c.total_stops} stops | {c.outbound_carriers} / {c.return_carriers}")

if unique:
    best_airport = min(valid_origins,
                       key=lambda o: min((f.price_raw for f in unique if f.origin == o),
                                         default=999999))
    best_price = min(f.price_raw for f in unique if f.origin == best_airport)
    print(f"\n2. BEST INDIAN AIRPORT: {best_airport} (from Rs.{best_price:,.0f})")

amd_min = min((f.price_raw for f in unique if f.origin == "AMD"), default=999999)
overall_min = cheapest[0].price_raw if cheapest else 999999
savings = amd_min - overall_min
pos_cost = POSITIONING_COSTS.get(cheapest[0].origin, 0) if cheapest else 0
net_savings = savings - pos_cost
print(f"\n3. REPOSITIONING VALUE:")
print(f"   AMD cheapest:     Rs.{amd_min:,.0f}")
print(f"   Overall cheapest: Rs.{overall_min:,.0f} from {cheapest[0].origin if cheapest else '?'}")
print(f"   Gross savings:    Rs.{savings:,.0f}")
print(f"   Positioning cost: Rs.{pos_cost:,}")
print(f"   Net savings:      Rs.{net_savings:,.0f}")
print(f"   Worth it:         {'YES' if net_savings > 0 else 'NO'}")

# Date analysis
if cheapest:
    by_dep = defaultdict(list)
    by_ret = defaultdict(list)
    for f in unique:
        by_dep[f.departure_date].append(f.price_raw)
        by_ret[f.return_date].append(f.price_raw)
    
    dep_mins = {d: min(ps) for d, ps in by_dep.items()}
    ret_mins = {d: min(ps) for d, ps in by_ret.items()}
    
    cheapest_dep = min(dep_mins, key=dep_mins.get)
    costliest_dep = max(dep_mins, key=dep_mins.get)
    cheapest_ret = min(ret_mins, key=ret_mins.get)
    costliest_ret = max(ret_mins, key=ret_mins.get)
    
    flex_savings = dep_mins[costliest_dep] - dep_mins[cheapest_dep]
    print(f"\n4. DATE FLEXIBILITY SAVINGS: Rs.{flex_savings:,.0f}")
    print(f"\n5. CHEAPEST DEPARTURE: {cheapest_dep} (from Rs.{dep_mins[cheapest_dep]:,.0f})")
    print(f"   Costliest departure: {costliest_dep} (from Rs.{dep_mins[costliest_dep]:,.0f})")
    print(f"\n6. CHEAPEST RETURN: {cheapest_ret} (from Rs.{ret_mins[cheapest_ret]:,.0f})")
    print(f"   Costliest return: {costliest_ret} (from Rs.{ret_mins[costliest_ret]:,.0f})")

# Airline analysis
airline_prices = defaultdict(list)
for f in unique:
    airline_prices[f.outbound_carriers].append(f.price_raw)

airline_avg = {a: sum(ps)/len(ps) for a, ps in airline_prices.items() if len(ps) >= 5}
if airline_avg:
    cheapest_airline = min(airline_avg, key=airline_avg.get)
    print(f"\n7. CHEAPEST AIRLINE (avg): {cheapest_airline} "
          f"(avg Rs.{airline_avg[cheapest_airline]:,.0f})")
    print(f"   Top 5 airlines by average price:")
    for a in sorted(airline_avg, key=airline_avg.get)[:5]:
        print(f"     {a}: avg Rs.{airline_avg[a]:,.0f} "
              f"({len(airline_prices[a])} itineraries)")

# Personal recommendation
if by_effective:
    rec = by_effective[0]
    print(f"\n8. RECOMMENDED BOOKING (minimum effective cost from AMD):")
    print(f"   International fare: Rs.{rec.price_raw:,.0f}")
    print(f"   Positioning:        Rs.{rec.positioning_cost:,} (AMD->{rec.origin})")
    print(f"   TOTAL:              Rs.{rec.effective_total:,.0f}")
    print(f"   Route:              {rec.origin}->{rec.destination}")
    print(f"   Dates:              {rec.departure_date} to {rec.return_date} ({rec.trip_days} days)")
    print(f"   Stops:              {rec.total_stops}")
    print(f"   Airlines:           {rec.outbound_carriers} / {rec.return_carriers}")

print(f"\nCompleted: {datetime.now().isoformat()}")
print(f"Total runtime: {(time.time()-t_start)/60:.1f} minutes")
print(f"\nOutput files in: {OUTPUT_DIR}/")
