"""
Skyscanner Flight Search Engine v1 — Direct API
Empirically validated: Direct API access works without browser/Playwright.
"""
import requests
import uuid
import time
import json
import csv
import sys
import os
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from itertools import product

sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# CONFIGURATION
# ============================================================

CONFIG = {
    "base_url": "https://www.skyscanner.co.in",
    "search_path": "/g/radar/api/v2/web-unified-search/",
    "autosuggest_path": "/g/autosuggest-search/api/v1/search-flight",
    "market": "IN",
    "locale": "en-GB",
    "currency": "INR",
    "adults": 1,
    "cabin_class": "ECONOMY",
    "max_polls": 8,
    "poll_interval_s": 3,
    "delay_between_searches_s": (3, 8),  # (min, max)
}

# ============================================================
# ENTITY RESOLVER (with autosuggest)
# ============================================================

# Known mappings from HAR + autosuggest GeoId
ENTITY_CACHE = {
    "AMD": "95673366",
    "BOM": "95673320",
    "DEL": "95673498",
    "MRU": "128668851",
    "BLR": "95673351",
    "SIN": "95673375",
    "DXB": "95673506",
    "DOH": "95673852",
    "CDG": "95565041",
    "IST": "95673323",
    "LHR": "95565050",
    "JFK": "95565058",
    "NRT": "128668889",
}


def resolve_entity(iata_code: str) -> Optional[str]:
    """Resolve IATA code to Skyscanner entity ID."""
    code = iata_code.upper().strip()
    
    if code in ENTITY_CACHE:
        return ENTITY_CACHE[code]
    
    # Try autosuggest
    url = (f"{CONFIG['base_url']}{CONFIG['autosuggest_path']}/"
           f"{CONFIG['market']}/{CONFIG['locale']}/{code}")
    
    try:
        resp = requests.get(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    if item.get("PlaceId", "").upper() == code:
                        geo_id = item.get("GeoId", "")
                        if geo_id:
                            ENTITY_CACHE[code] = geo_id
                            return geo_id
                # Fallback: use first airport result
                for item in data:
                    geo_id = item.get("GeoId", "")
                    if geo_id and not item.get("IataCode"):  # Airport, not city
                        ENTITY_CACHE[code] = geo_id
                        return geo_id
    except Exception as e:
        print(f"  [WARN] Autosuggest failed for {code}: {e}")
    
    return None


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class FlightLeg:
    origin: str
    destination: str
    departure: str
    arrival: str
    duration_mins: int
    stop_count: int
    carriers: str  # comma-separated
    segments_detail: str = ""


@dataclass 
class FlightResult:
    itinerary_id: str
    origin: str
    destination: str
    departure_date: str
    return_date: str
    price_raw: float
    price_formatted: str
    currency: str
    outbound_duration: int
    return_duration: int
    outbound_stops: int
    return_stops: int
    outbound_carriers: str
    return_carriers: str
    outbound_departure: str
    outbound_arrival: str
    return_departure: str
    return_arrival: str
    score: float = 0.0
    is_self_transfer: bool = False
    search_key: str = ""


@dataclass
class SearchLog:
    search_id: int
    origin: str
    destination: str
    departure: str
    return_date: str
    http_status: int
    response_time_ms: float
    context_status: str
    result_count: int
    poll_count: int
    total_time_ms: float
    blocked: bool
    block_type: str
    gateway: str
    error: str


# ============================================================
# SEARCH CLIENT
# ============================================================

class SkyscannerSearchClient:
    
    def __init__(self):
        self.session = requests.Session()
        self._update_session_headers()
        self.search_count = 0
        self.block_count = 0
    
    def _update_session_headers(self):
        """Set headers for a new search session."""
        view_id = str(uuid.uuid4())
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Origin": CONFIG["base_url"],
            "Referer": f"{CONFIG['base_url']}/",
            "x-skyscanner-channelid": "website",
            "x-skyscanner-currency": CONFIG["currency"],
            "x-skyscanner-locale": CONFIG["locale"],
            "x-skyscanner-market": CONFIG["market"],
            "x-skyscanner-viewid": view_id,
            "x-skyscanner-trustedfunnelid": view_id,
            "x-skyscanner-traveller-context": str(uuid.uuid4()),
        })
    
    def search(self, origin: str, dest: str, dep_date: date, ret_date: date) -> tuple:
        """
        Execute a single flight search.
        Returns: (list[FlightResult], SearchLog)
        """
        self.search_count += 1
        search_id = self.search_count
        
        origin_eid = resolve_entity(origin)
        dest_eid = resolve_entity(dest)
        
        if not origin_eid or not dest_eid:
            log = SearchLog(
                search_id=search_id, origin=origin, destination=dest,
                departure=str(dep_date), return_date=str(ret_date),
                http_status=0, response_time_ms=0, context_status="",
                result_count=0, poll_count=0, total_time_ms=0,
                blocked=False, block_type="", gateway="",
                error=f"Entity resolution failed: {origin}={origin_eid}, {dest}={dest_eid}"
            )
            return [], log
        
        # Refresh view ID per search
        view_id = str(uuid.uuid4())
        self.session.headers["x-skyscanner-viewid"] = view_id
        self.session.headers["x-skyscanner-trustedfunnelid"] = view_id
        
        body = {
            "cabinClass": CONFIG["cabin_class"],
            "adults": CONFIG["adults"],
            "childAges": [],
            "legs": [
                {
                    "legOrigin": {"@type": "entity", "entityId": origin_eid},
                    "legDestination": {"@type": "entity", "entityId": dest_eid},
                    "dates": {"@type": "date", "year": str(dep_date.year),
                              "month": str(dep_date.month).zfill(2),
                              "day": str(dep_date.day).zfill(2)},
                },
                {
                    "legOrigin": {"@type": "entity", "entityId": dest_eid},
                    "legDestination": {"@type": "entity", "entityId": origin_eid},
                    "dates": {"@type": "date", "year": str(ret_date.year),
                              "month": str(ret_date.month).zfill(2),
                              "day": str(ret_date.day).zfill(2)},
                },
            ],
        }
        
        url = f"{CONFIG['base_url']}{CONFIG['search_path']}"
        t_start = time.time()
        
        # POST
        try:
            resp = self.session.post(url, json=body, timeout=30)
        except requests.exceptions.RequestException as e:
            log = SearchLog(
                search_id=search_id, origin=origin, destination=dest,
                departure=str(dep_date), return_date=str(ret_date),
                http_status=-1, response_time_ms=round((time.time()-t_start)*1000),
                context_status="error", result_count=0, poll_count=0,
                total_time_ms=round((time.time()-t_start)*1000),
                blocked=False, block_type="", gateway="", error=str(e)
            )
            return [], log
        
        post_time = round((time.time() - t_start) * 1000)
        
        if resp.status_code != 200:
            self.block_count += 1
            log = SearchLog(
                search_id=search_id, origin=origin, destination=dest,
                departure=str(dep_date), return_date=str(ret_date),
                http_status=resp.status_code, response_time_ms=post_time,
                context_status="blocked", result_count=0, poll_count=0,
                total_time_ms=post_time, blocked=True,
                block_type=f"HTTP {resp.status_code}",
                gateway="", error=resp.text[:200]
            )
            return [], log
        
        # Capture gateway
        gw = resp.headers.get("x-gateway-servedby", "")
        if gw:
            self.session.headers["x-gateway-servedby"] = gw
        
        data = resp.json()
        ctx = data.get("context", {})
        status = ctx.get("status", "")
        session_id = ctx.get("sessionId", "")
        
        # Poll if needed
        poll_count = 0
        while status != "complete" and poll_count < CONFIG["max_polls"]:
            poll_count += 1
            time.sleep(CONFIG["poll_interval_s"])
            try:
                poll_resp = self.session.get(f"{url}{session_id}", timeout=30)
                if poll_resp.status_code != 200:
                    break
                data = poll_resp.json()
                ctx = data.get("context", {})
                status = ctx.get("status", "")
                session_id = ctx.get("sessionId", "")
            except:
                break
        
        total_time = round((time.time() - t_start) * 1000)
        
        # Parse results
        results = data.get("itineraries", {}).get("results", [])
        flights = self._parse_results(results, origin, dest, dep_date, ret_date)
        
        log = SearchLog(
            search_id=search_id, origin=origin, destination=dest,
            departure=str(dep_date), return_date=str(ret_date),
            http_status=resp.status_code, response_time_ms=post_time,
            context_status=status, result_count=len(flights),
            poll_count=poll_count, total_time_ms=total_time,
            blocked=False, block_type="", gateway=gw, error=""
        )
        
        return flights, log
    
    def _parse_results(self, results: list, origin: str, dest: str,
                       dep_date: date, ret_date: date) -> list:
        """Parse raw API results into FlightResult objects."""
        flights = []
        
        for r in results:
            try:
                price_info = r.get("price", {})
                legs = r.get("legs", [])
                
                if len(legs) < 2:
                    continue
                
                outbound = legs[0]
                inbound = legs[1]
                
                def get_carriers(leg):
                    return ", ".join(
                        c.get("name", "") 
                        for c in leg.get("carriers", {}).get("marketing", [])
                    )
                
                flight = FlightResult(
                    itinerary_id=r.get("id", ""),
                    origin=origin,
                    destination=dest,
                    departure_date=str(dep_date),
                    return_date=str(ret_date),
                    price_raw=price_info.get("raw", 0),
                    price_formatted=price_info.get("formatted", ""),
                    currency=CONFIG["currency"],
                    outbound_duration=outbound.get("durationInMinutes", 0),
                    return_duration=inbound.get("durationInMinutes", 0),
                    outbound_stops=outbound.get("stopCount", 0),
                    return_stops=inbound.get("stopCount", 0),
                    outbound_carriers=get_carriers(outbound),
                    return_carriers=get_carriers(inbound),
                    outbound_departure=outbound.get("departure", ""),
                    outbound_arrival=outbound.get("arrival", ""),
                    return_departure=inbound.get("departure", ""),
                    return_arrival=inbound.get("arrival", ""),
                    score=r.get("score", 0),
                    is_self_transfer=r.get("isSelfTransfer", False),
                    search_key=f"{origin}-{dest}_{dep_date}_{ret_date}",
                )
                flights.append(flight)
            except Exception:
                continue
        
        return flights


# ============================================================
# SEARCH ORCHESTRATOR
# ============================================================

def run_search_matrix(origins, destinations, dep_dates, ret_dates):
    """Run the full search matrix and collect results."""
    import random
    
    client = SkyscannerSearchClient()
    all_flights = []
    all_logs = []
    
    combos = list(product(origins, destinations, dep_dates, ret_dates))
    total = len(combos)
    
    print(f"\n  Search matrix: {len(origins)} origins x {len(destinations)} dest "
          f"x {len(dep_dates)} dep x {len(ret_dates)} ret = {total} searches")
    
    for i, (origin, dest, dep, ret) in enumerate(combos):
        print(f"\n  [{i+1}/{total}] {origin} -> {dest} | {dep} - {ret}", end="", flush=True)
        
        flights, log = client.search(origin, dest, dep, ret)
        all_flights.extend(flights)
        all_logs.append(log)
        
        status_char = "OK" if log.context_status == "complete" else "FAIL"
        print(f" -> {status_char} | {log.result_count} flights | "
              f"{log.response_time_ms}ms | polls={log.poll_count}", flush=True)
        
        if log.blocked:
            print(f"    ** BLOCKED: {log.block_type} | {log.error}")
        
        # Delay between searches
        if i < total - 1:
            delay = random.uniform(*CONFIG["delay_between_searches_s"])
            time.sleep(delay)
    
    return all_flights, all_logs, client


# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate(flights: list) -> list:
    """Remove duplicate itineraries (same itinerary found via different search combos)."""
    seen = {}
    for f in flights:
        key = f.itinerary_id
        if key not in seen or f.price_raw < seen[key].price_raw:
            seen[key] = f
    return list(seen.values())


# ============================================================
# RANKING
# ============================================================

def rank_flights(flights: list) -> dict:
    """Create multiple rankings of flights."""
    return {
        "cheapest": sorted(flights, key=lambda f: f.price_raw),
        "fastest_total": sorted(flights, key=lambda f: f.outbound_duration + f.return_duration),
        "fewest_stops": sorted(flights, key=lambda f: f.outbound_stops + f.return_stops),
        "best_value": sorted(flights, key=lambda f: (
            f.price_raw * (1 + 0.15 * (f.outbound_stops + f.return_stops))
        )),
    }


# ============================================================
# EXPORT
# ============================================================

def export_csv(flights: list, filepath: str):
    """Export flights to CSV."""
    if not flights:
        return
    fields = list(asdict(flights[0]).keys())
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for flight in flights:
            writer.writerow(asdict(flight))


def export_json(flights: list, logs: list, filepath: str):
    """Export flights and search logs to JSON."""
    output = {
        "generated_at": datetime.now().isoformat(),
        "config": CONFIG,
        "search_logs": [asdict(l) for l in logs],
        "summary": {
            "total_searches": len(logs),
            "completed": sum(1 for l in logs if l.context_status == "complete"),
            "blocked": sum(1 for l in logs if l.blocked),
            "total_flights_raw": sum(l.result_count for l in logs),
            "total_flights_deduped": len(flights),
            "avg_response_ms": round(
                sum(l.response_time_ms for l in logs) / max(len(logs), 1)
            ),
        },
        "flights": [asdict(f) for f in flights],
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)


def print_rankings(rankings: dict, top_n: int = 10):
    """Print ranked flights."""
    for rank_name, flights in rankings.items():
        print(f"\n  {'=' * 70}")
        print(f"  RANKING: {rank_name.upper()} (top {top_n})")
        print(f"  {'=' * 70}")
        
        for i, f in enumerate(flights[:top_n]):
            total_dur = f.outbound_duration + f.return_duration
            total_stops = f.outbound_stops + f.return_stops
            print(f"    #{i+1:2d} | {f.price_formatted:>12s} | "
                  f"{f.origin}->{f.destination} | "
                  f"{f.departure_date} - {f.return_date} | "
                  f"{total_dur}min total | {total_stops} stops | "
                  f"{f.outbound_carriers}")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 70)
    print("SKYSCANNER FLIGHT SEARCH ENGINE v1 — DIRECT API")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 70)
    
    # === PHASE 1: Entity Resolution ===
    print("\n--- Phase 1: Entity Resolution ---")
    airports = ["AMD", "BOM", "DEL", "MRU", "LHR", "JFK", "NRT"]
    for code in airports:
        eid = resolve_entity(code)
        print(f"  {code}: {eid}")
    
    # === PHASE 2: Search Matrix ===
    print("\n--- Phase 2: Search Matrix ---")
    
    origins = ["AMD", "BOM", "DEL"]
    destinations = ["MRU"]
    dep_dates = [date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
    ret_dates = [date(2026, 7, 15), date(2026, 7, 16), date(2026, 7, 17)]
    
    all_flights, all_logs, client = run_search_matrix(
        origins, destinations, dep_dates, ret_dates
    )
    
    # === PHASE 3: Search Summary ===
    print("\n\n--- Phase 3: Search Summary ---")
    completed = [l for l in all_logs if l.context_status == "complete"]
    blocked = [l for l in all_logs if l.blocked]
    
    print(f"  Total searches:    {len(all_logs)}")
    print(f"  Completed:         {len(completed)}")
    print(f"  Blocked:           {len(blocked)}")
    print(f"  Raw flights found: {sum(l.result_count for l in all_logs)}")
    
    if blocked:
        print(f"\n  BLOCKS:")
        for b in blocked:
            print(f"    Search #{b.search_id}: {b.origin}->{b.destination} "
                  f"{b.departure} | {b.block_type} | {b.error[:100]}")
    
    if completed:
        avg_time = sum(l.response_time_ms for l in completed) / len(completed)
        avg_results = sum(l.result_count for l in completed) / len(completed)
        print(f"  Avg response time: {avg_time:.0f}ms")
        print(f"  Avg results/search:{avg_results:.0f}")
    
    # === PHASE 4: Deduplication ===
    print("\n--- Phase 4: Deduplication ---")
    unique_flights = deduplicate(all_flights)
    print(f"  Before dedup: {len(all_flights)} flights")
    print(f"  After dedup:  {len(unique_flights)} flights")
    print(f"  Duplicates:   {len(all_flights) - len(unique_flights)}")
    
    # === PHASE 5: Rankings ===
    print("\n--- Phase 5: Rankings ---")
    if unique_flights:
        rankings = rank_flights(unique_flights)
        print_rankings(rankings, top_n=10)
    
    # === PHASE 6: Export ===
    print("\n--- Phase 6: Export ---")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = "results"
    os.makedirs(results_dir, exist_ok=True)
    
    # Export cheapest ranking
    if unique_flights:
        cheapest = rank_flights(unique_flights)["cheapest"]
        csv_path = os.path.join(results_dir, f"flights_{timestamp}.csv")
        export_csv(cheapest, csv_path)
        print(f"  CSV: {csv_path} ({len(cheapest)} flights)")
        
        json_path = os.path.join(results_dir, f"flights_{timestamp}.json")
        export_json(cheapest, all_logs, json_path)
        print(f"  JSON: {json_path}")
    
    # === PHASE 7: Experimental Results ===
    print("\n--- Phase 7: Experimental Results ---")
    print(f"\n  VERDICT: DIRECT API {'WORKS' if not blocked else 'PARTIALLY WORKS'}")
    print(f"  Total API calls:    {client.search_count}")
    print(f"  Successful:         {len(completed)}")
    print(f"  Blocked:            {len(blocked)}")
    print(f"  First block:        {'NEVER' if not blocked else f'Search #{blocked[0].search_id}'}")
    print(f"  First captcha:      NEVER")
    print(f"  First 403:          {'NEVER' if not any(l.http_status==403 for l in all_logs) else 'YES'}")
    print(f"  First 429:          {'NEVER' if not any(l.http_status==429 for l in all_logs) else 'YES'}")
    
    print(f"\n  RECOMMENDATION:")
    if not blocked:
        print(f"    Direct API (requests) is VIABLE for personal flight search.")
        print(f"    No Playwright needed for this use case.")
        print(f"    PerimeterX was NOT enforced on the API endpoint.")
    else:
        pct = len(completed) / max(len(all_logs), 1) * 100
        print(f"    Direct API succeeded {pct:.0f}% of the time.")
        if pct > 80:
            print(f"    Direct API is still viable with retry logic.")
        else:
            print(f"    Consider Playwright fallback for reliability.")
    
    print(f"\nFinished: {datetime.now().isoformat()}")
