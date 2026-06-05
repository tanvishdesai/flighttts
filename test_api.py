"""
Skyscanner Direct API Client — Empirical Validation
Tests whether the search API works directly from Python requests.
"""
import requests
import uuid
import time
import json
import csv
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

# ============================================================
# ENTITY RESOLVER
# ============================================================

# Hardcoded from HAR extraction
KNOWN_ENTITIES = {
    "AMD": "95673366",
    "BOM": "95673320",
    "DEL": "95673498",
    "MRU": "128668851",
    "BLR": "95673351",
    "SIN": "95673375",
    "CPT": "95673380",
    "NBO": "95673395",
    "JNB": "95673415",
    "KUL": "95673456",
    "DXB": "95673506",
    "DOH": "95673852",
    "CDG": "95565041",
    "IST": "95673323",
    "PNQ": "128668941",
}


def resolve_entity_autosuggest(query, market="IN", locale="en-GB"):
    """
    Use the autosuggest API to resolve an airport/city name or IATA code to an entity ID.
    Endpoint discovered in JS: /g/autosuggest-search/api/v1/search-flight/{market}/{locale}/{query}
    """
    url = f"https://www.skyscanner.co.in/g/autosuggest-search/api/v1/search-flight/{market}/{locale}/{requests.utils.quote(query)}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    }
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return {
            "status": resp.status_code,
            "url": url,
            "response": resp.json() if resp.status_code == 200 else resp.text[:500],
            "headers": dict(resp.headers),
        }
    except Exception as e:
        return {"status": -1, "error": str(e), "url": url}


def resolve_entity(iata_code):
    """Resolve IATA code to entity ID. Uses cache first, then autosuggest."""
    if iata_code.upper() in KNOWN_ENTITIES:
        return KNOWN_ENTITIES[iata_code.upper()]
    
    # Try autosuggest
    result = resolve_entity_autosuggest(iata_code)
    if result["status"] == 200 and isinstance(result["response"], list):
        for item in result["response"]:
            if isinstance(item, dict):
                # Autosuggest returns PlaceId (IATA-like codes), not numeric entityId
                # The search API uses numeric entityIds, so we cannot resolve unknown airports
                # through autosuggest alone - PlaceId is a different ID system
                place_id = item.get("PlaceId", "")
                if place_id == iata_code.upper():
                    # PlaceId matches - we found the airport but need entityId
                    # For now, store the PlaceId and note it needs mapping
                    KNOWN_ENTITIES[iata_code.upper()] = place_id
                    return str(place_id)
    
    return None


# ============================================================
# SEARCH CLIENT
# ============================================================

@dataclass
class SearchResult:
    test_id: str
    origin: str
    destination: str
    departure: str
    return_date: str
    
    # POST results
    post_status: int = 0
    post_time_ms: float = 0
    post_session_id: str = ""
    post_context_status: str = ""
    post_error: str = ""
    
    # Polling results
    polls: int = 0
    final_status: str = ""
    total_results: int = 0
    total_time_ms: float = 0
    
    # Error tracking
    blocked: bool = False
    block_type: str = ""
    response_codes: str = ""
    
    # Gateway
    gateway: str = ""


class SkyscannerAPIClient:
    BASE_URL = "https://www.skyscanner.co.in"
    SEARCH_PATH = "/g/radar/api/v2/web-unified-search/"
    
    def __init__(self, market="IN", locale="en-GB", currency="INR"):
        self.session = requests.Session()
        self.market = market
        self.locale = locale
        self.currency = currency
        
        view_id = str(uuid.uuid4())
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "x-skyscanner-channelid": "website",
            "x-skyscanner-currency": currency,
            "x-skyscanner-locale": locale,
            "x-skyscanner-market": market,
            "x-skyscanner-viewid": view_id,
            "x-skyscanner-trustedfunnelid": view_id,
            "x-skyscanner-traveller-context": str(uuid.uuid4()),
        })
    
    def _build_body(self, origin_eid, dest_eid, dep_date, ret_date, adults=1, cabin="ECONOMY"):
        legs = [
            {
                "legOrigin": {"@type": "entity", "entityId": origin_eid},
                "legDestination": {"@type": "entity", "entityId": dest_eid},
                "dates": {
                    "@type": "date",
                    "year": str(dep_date.year),
                    "month": str(dep_date.month).zfill(2),
                    "day": str(dep_date.day).zfill(2),
                },
            },
        ]
        if ret_date:
            legs.append({
                "legOrigin": {"@type": "entity", "entityId": dest_eid},
                "legDestination": {"@type": "entity", "entityId": origin_eid},
                "dates": {
                    "@type": "date",
                    "year": str(ret_date.year),
                    "month": str(ret_date.month).zfill(2),
                    "day": str(ret_date.day).zfill(2),
                },
            })
        return {"cabinClass": cabin, "adults": adults, "childAges": [], "legs": legs}
    
    def search(self, origin_iata, dest_iata, dep_date, ret_date, 
               max_polls=8, poll_interval=3, test_id=""):
        """Execute a full search with polling. Returns SearchResult."""
        
        result = SearchResult(
            test_id=test_id,
            origin=origin_iata,
            destination=dest_iata,
            departure=str(dep_date),
            return_date=str(ret_date),
        )
        
        origin_eid = resolve_entity(origin_iata)
        dest_eid = resolve_entity(dest_iata)
        
        if not origin_eid or not dest_eid:
            result.post_error = f"Entity resolution failed: {origin_iata}={origin_eid}, {dest_iata}={dest_eid}"
            return result
        
        body = self._build_body(origin_eid, dest_eid, dep_date, ret_date)
        
        # Refresh UUIDs per search
        view_id = str(uuid.uuid4())
        self.session.headers["x-skyscanner-viewid"] = view_id
        self.session.headers["x-skyscanner-trustedfunnelid"] = view_id
        
        url = f"{self.BASE_URL}{self.SEARCH_PATH}"
        response_codes = []
        start_total = time.time()
        
        # === STEP 1: POST to create search ===
        try:
            t0 = time.time()
            resp = self.session.post(url, json=body, timeout=30)
            result.post_time_ms = round((time.time() - t0) * 1000, 1)
            result.post_status = resp.status_code
            response_codes.append(resp.status_code)
            
            if resp.status_code != 200:
                result.post_error = resp.text[:500]
                result.blocked = resp.status_code in (403, 429, 503)
                result.block_type = f"HTTP {resp.status_code} on POST"
                result.response_codes = str(response_codes)
                return result
            
            data = resp.json()
            ctx = data.get("context", {})
            result.post_session_id = ctx.get("sessionId", "")[:60]
            result.post_context_status = ctx.get("status", "")
            
            # Capture gateway for sticky routing
            gw = resp.headers.get("x-gateway-servedby", "")
            if gw:
                result.gateway = gw
                self.session.headers["x-gateway-servedby"] = gw
            
            session_id = ctx.get("sessionId", "")
            status = ctx.get("status", "")
            results_count = len(data.get("itineraries", {}).get("results", []))
            
        except requests.exceptions.RequestException as e:
            result.post_error = str(e)
            result.response_codes = str(response_codes)
            return result
        
        # === STEP 2: Poll until complete ===
        poll_num = 0
        while status != "complete" and poll_num < max_polls:
            poll_num += 1
            time.sleep(poll_interval)
            
            poll_url = f"{self.BASE_URL}{self.SEARCH_PATH}{session_id}"
            try:
                resp = self.session.get(poll_url, timeout=30)
                response_codes.append(resp.status_code)
                
                if resp.status_code != 200:
                    result.blocked = resp.status_code in (403, 429, 503)
                    result.block_type = f"HTTP {resp.status_code} on poll #{poll_num}"
                    break
                
                data = resp.json()
                ctx = data.get("context", {})
                session_id = ctx.get("sessionId", "")
                status = ctx.get("status", "")
                results_count = len(data.get("itineraries", {}).get("results", []))
                
            except requests.exceptions.RequestException as e:
                result.post_error = f"Poll #{poll_num} error: {e}"
                break
        
        result.polls = poll_num
        result.final_status = status
        result.total_results = results_count
        result.total_time_ms = round((time.time() - start_total) * 1000, 1)
        result.response_codes = str(response_codes)
        
        return result


# ============================================================
# TEST RUNNER
# ============================================================

def run_autosuggest_test():
    """Test the autosuggest endpoint with multiple queries."""
    print("\n" + "=" * 80)
    print("TEST: AUTOSUGGEST ENDPOINT VALIDATION")
    print("=" * 80)
    
    test_queries = ["AMD", "BOM", "DEL", "MRU", "LHR", "JFK", "NRT",
                    "Ahmedabad", "Mumbai", "London Heathrow"]
    
    results = []
    for query in test_queries:
        print(f"\n  Testing: '{query}'...")
        result = resolve_entity_autosuggest(query)
        results.append({"query": query, **result})
        
        if result["status"] == 200:
            resp_data = result["response"]
            if isinstance(resp_data, list):
                print(f"    Status: {result['status']} | Results: {len(resp_data)}")
                for item in resp_data[:3]:
                    if isinstance(item, dict):
                        print(f"      {json.dumps({k: item[k] for k in list(item.keys())[:8]}, ensure_ascii=False)}")
            elif isinstance(resp_data, dict):
                print(f"    Status: {result['status']} | Keys: {list(resp_data.keys())[:10]}")
                # Try to find results in the dict
                for key in resp_data:
                    val = resp_data[key]
                    if isinstance(val, list) and val:
                        print(f"      '{key}': {len(val)} items")
                        for item in val[:3]:
                            if isinstance(item, dict):
                                print(f"        {json.dumps({k: item[k] for k in list(item.keys())[:8]}, ensure_ascii=False)}")
            else:
                print(f"    Status: {result['status']} | Type: {type(resp_data)}")
        else:
            print(f"    Status: {result['status']}")
            if "error" in result:
                print(f"    Error: {result['error']}")
            elif isinstance(result.get("response"), str):
                print(f"    Response: {result['response'][:200]}")
        
        time.sleep(1)  # Be polite
    
    return results


def run_search_tests():
    """Run progressive search tests."""
    print("\n" + "=" * 80)
    print("TEST: DIRECT API SEARCH VALIDATION")
    print("=" * 80)
    
    from datetime import date
    
    # Test matrix
    departures = [date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
    returns = [date(2026, 7, 15), date(2026, 7, 16), date(2026, 7, 17)]
    
    all_combos = []
    for dep in departures:
        for ret in returns:
            all_combos.append(("AMD", "MRU", dep, ret))
    
    # Progressive tests
    tests = [
        ("Test A: 1 search", all_combos[:1]),
        ("Test B: 5 searches", all_combos[:5]),
        ("Test C: 9 searches (full AMD->MRU matrix)", all_combos[:9]),
    ]
    
    all_results = []
    client = SkyscannerAPIClient()
    search_num = 0
    first_block = None
    
    for test_name, combos in tests:
        print(f"\n{'=' * 70}")
        print(f"  {test_name}")
        print(f"{'=' * 70}")
        
        for origin, dest, dep, ret in combos:
            search_num += 1
            test_id = f"S{search_num:03d}"
            
            print(f"\n  [{test_id}] {origin} -> {dest} | {dep} - {ret}")
            
            result = client.search(origin, dest, dep, ret, test_id=test_id)
            all_results.append(result)
            
            status_icon = "[OK]" if result.final_status == "complete" else ("[BLOCKED]" if result.blocked else "[WARN]")
            print(f"    {status_icon} POST: {result.post_status} ({result.post_time_ms}ms) | "
                  f"Polls: {result.polls} | Status: {result.final_status} | "
                  f"Results: {result.total_results} | Total: {result.total_time_ms}ms")
            
            if result.blocked:
                if first_block is None:
                    first_block = search_num
                print(f"    ** BLOCKED: {result.block_type}")
            
            if result.post_error:
                print(f"    ** Error: {result.post_error[:200]}")
            
            print(f"    Response codes: {result.response_codes}")
            print(f"    Gateway: {result.gateway}")
            
            # Delay between searches
            if search_num < len(all_combos):
                delay = 5 + (search_num * 0.5)  # Increasing delay
                delay = min(delay, 15)
                print(f"    Waiting {delay:.0f}s...")
                time.sleep(delay)
    
    return all_results, first_block


def save_results(search_results, autosuggest_results):
    """Save all results to files."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save search results to CSV
    csv_path = f"test_results_{timestamp}.csv"
    if search_results:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(search_results[0]).keys()))
            writer.writeheader()
            for r in search_results:
                writer.writerow(asdict(r))
        print(f"\n  Search results saved to: {csv_path}")
    
    # Save everything to JSON
    json_path = f"test_results_{timestamp}.json"
    output = {
        "timestamp": timestamp,
        "search_results": [asdict(r) for r in search_results] if search_results else [],
        "autosuggest_results": autosuggest_results or [],
        "summary": {}
    }
    
    if search_results:
        completed = [r for r in search_results if r.final_status == "complete"]
        blocked = [r for r in search_results if r.blocked]
        output["summary"] = {
            "total_searches": len(search_results),
            "completed": len(completed),
            "blocked": len(blocked),
            "avg_results": sum(r.total_results for r in completed) / max(len(completed), 1),
            "avg_time_ms": sum(r.total_time_ms for r in completed) / max(len(completed), 1),
        }
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Full results saved to: {json_path}")
    
    return csv_path, json_path


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print("=" * 80)
    print("SKYSCANNER DIRECT API — EMPIRICAL VALIDATION")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 80)
    
    # Phase 1: Test autosuggest
    autosuggest_results = run_autosuggest_test()
    
    # Phase 2: Test search API
    search_results, first_block = run_search_tests()
    
    # Phase 3: Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    if search_results:
        completed = [r for r in search_results if r.final_status == "complete"]
        blocked = [r for r in search_results if r.blocked]
        failed = [r for r in search_results if r.post_error and not r.blocked]
        
        print(f"\n  Total searches: {len(search_results)}")
        print(f"  Completed:      {len(completed)}")
        print(f"  Blocked:        {len(blocked)}")
        print(f"  Other failures: {len(failed)}")
        
        if first_block:
            print(f"  First block at: search #{first_block}")
        else:
            print(f"  First block at: NEVER (all succeeded)")
        
        if completed:
            avg_results = sum(r.total_results for r in completed) / len(completed)
            avg_time = sum(r.total_time_ms for r in completed) / len(completed)
            print(f"  Avg results:    {avg_results:.0f}")
            print(f"  Avg total time: {avg_time:.0f}ms")
    
    # Save
    save_results(search_results, autosuggest_results)
    
    print(f"\nFinished: {datetime.now().isoformat()}")
