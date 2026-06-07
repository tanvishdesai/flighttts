"""
Step 1: Validate autosuggest only (fast, no search calls).
Step 2: Single search with verbose output.
"""
import requests
import uuid
import time
import json
import sys
from datetime import date

sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# STEP 1: AUTOSUGGEST
# ============================================================
print("=" * 70)
print("STEP 1: AUTOSUGGEST ENDPOINT")
print("=" * 70)

queries = ["AMD", "BOM", "DEL", "MRU", "LHR", "JFK", "NRT"]
entity_cache = {}

for q in queries:
    url = f"https://www.skyscanner.co.in/g/autosuggest-search/api/v1/search-flight/IN/en-GB/{q}"
    try:
        t0 = time.time()
        resp = requests.get(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        }, timeout=10)
        elapsed = round((time.time() - t0) * 1000)
        
        print(f"\n  [{q}] Status: {resp.status_code} | {elapsed}ms")
        
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data[:5]:
                    pid = item.get("PlaceId", "")
                    name = item.get("PlaceName", "")
                    country = item.get("CountryName", "")
                    city_id = item.get("CityId", "")
                    iata = item.get("IataCode", "")
                    geoId = item.get("GeoId", "")
                    geoType = item.get("GeoContainerId", "")
                    geo = item.get("Location", "")
                    
                    # Print ALL keys for first result to understand format
                    if pid == q or (data.index(item) == 0):
                        print(f"    ALL KEYS: {list(item.keys())}")
                        print(f"    FULL: {json.dumps(item, ensure_ascii=False)}")
                    else:
                        print(f"    PlaceId={pid} Name={name} Country={country} CityId={city_id}")
                    
                    if pid == q:
                        entity_cache[q] = item
        else:
            print(f"    Response: {resp.text[:300]}")
    except Exception as e:
        print(f"    ERROR: {e}")
    
    time.sleep(0.5)

print(f"\n\nAutosuggest summary: resolved {len(entity_cache)} of {len(queries)} airports")

# ============================================================
# STEP 2: SINGLE SEARCH - VERBOSE
# ============================================================
print("\n" + "=" * 70)
print("STEP 2: SINGLE FLIGHT SEARCH (verbose)")
print("=" * 70)

# Use known entity IDs
ENTITIES = {
    "AMD": "95673366",
    "BOM": "95673320",
    "DEL": "95673498",
    "MRU": "128668851",
}

view_id = str(uuid.uuid4())
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Origin": "https://www.skyscanner.co.in",
    "Referer": "https://www.skyscanner.co.in/transport/flights/amd/mru/260708/260715/",
    "x-skyscanner-channelid": "website",
    "x-skyscanner-currency": "INR",
    "x-skyscanner-locale": "en-GB",
    "x-skyscanner-market": "IN",
    "x-skyscanner-viewid": view_id,
    "x-skyscanner-trustedfunnelid": view_id,
    "x-skyscanner-traveller-context": str(uuid.uuid4()),
}

body = {
    "cabinClass": "ECONOMY",
    "adults": 1,
    "childAges": [],
    "legs": [
        {
            "legOrigin": {"@type": "entity", "entityId": ENTITIES["AMD"]},
            "legDestination": {"@type": "entity", "entityId": ENTITIES["MRU"]},
            "dates": {"@type": "date", "year": "2026", "month": "07", "day": "08"},
        },
        {
            "legOrigin": {"@type": "entity", "entityId": ENTITIES["MRU"]},
            "legDestination": {"@type": "entity", "entityId": ENTITIES["AMD"]},
            "dates": {"@type": "date", "year": "2026", "month": "07", "day": "15"},
        },
    ],
}

url = "https://www.skyscanner.co.in/g/radar/api/v2/web-unified-search/"

print(f"\n  POST {url}")
print(f"  Body: {json.dumps(body, indent=2)[:500]}")
print(f"  Headers: {json.dumps({k:v for k,v in headers.items() if k.startswith('x-')}, indent=2)}")

try:
    t0 = time.time()
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    elapsed = round((time.time() - t0) * 1000)
    
    print(f"\n  POST Response:")
    print(f"    Status: {resp.status_code}")
    print(f"    Time: {elapsed}ms")
    print(f"    Content-Type: {resp.headers.get('content-type', 'N/A')}")
    print(f"    Content-Length: {resp.headers.get('content-length', len(resp.content))}")
    print(f"    Gateway: {resp.headers.get('x-gateway-servedby', 'N/A')}")
    
    # Print interesting response headers
    for h in ["x-skyscanner-session-id-token", "x-gateway-servedby", "set-cookie"]:
        if h in resp.headers:
            val = resp.headers[h]
            print(f"    {h}: {val[:100]}...")
    
    if resp.status_code == 200:
        data = resp.json()
        ctx = data.get("context", {})
        print(f"\n    context.status: {ctx.get('status')}")
        print(f"    context.sessionId: {ctx.get('sessionId', '')[:80]}...")
        
        itin = data.get("itineraries", {})
        results = itin.get("results", [])
        agents = itin.get("agents", {})
        print(f"    itineraries.results: {len(results)}")
        print(f"    itineraries.agents: {len(agents)}")
        
        session_id = ctx.get("sessionId", "")
        gateway = resp.headers.get("x-gateway-servedby", "")
        
        if gateway:
            headers["x-gateway-servedby"] = gateway
        
        # POLLING
        poll = 0
        max_polls = 8
        status = ctx.get("status", "")
        
        while status != "complete" and poll < max_polls:
            poll += 1
            print(f"\n  --- Poll #{poll} (waiting 3s) ---")
            time.sleep(3)
            
            poll_url = f"{url}{session_id}"
            t0 = time.time()
            resp = requests.get(poll_url, headers=headers, timeout=30)
            elapsed = round((time.time() - t0) * 1000)
            
            print(f"    Status: {resp.status_code} | Time: {elapsed}ms | Size: {len(resp.content)}B")
            
            if resp.status_code != 200:
                print(f"    ERROR RESPONSE: {resp.text[:500]}")
                break
            
            data = resp.json()
            ctx = data.get("context", {})
            status = ctx.get("status", "")
            session_id = ctx.get("sessionId", "")
            results = data.get("itineraries", {}).get("results", [])
            
            print(f"    context.status: {status}")
            print(f"    results: {len(results)}")
            
            if results and poll == 1:
                # Print first result detail
                r = results[0]
                print(f"    Sample result:")
                print(f"      Price: {r.get('price', {}).get('formatted', 'N/A')} ({r.get('price', {}).get('raw', 'N/A')})")
                for j, leg in enumerate(r.get("legs", [])):
                    orig = leg.get("origin", {}).get("displayCode", "?")
                    dest = leg.get("destination", {}).get("displayCode", "?")
                    stops = leg.get("stopCount", "?")
                    dur = leg.get("durationInMinutes", "?")
                    dep = leg.get("departure", "?")
                    arr = leg.get("arrival", "?")
                    carriers = [c.get("name","") for c in leg.get("carriers",{}).get("marketing",[])]
                    print(f"      Leg {j+1}: {orig}->{dest} | {dep[:16]}->{arr[:16]} | "
                          f"{dur}min | {stops} stops | {','.join(carriers)}")
        
        print(f"\n  FINAL: status={status} results={len(results)} polls={poll}")
        
        if status == "complete":
            print(f"\n  SUCCESS: Direct API works! {len(results)} flights found.")
            
            # Print top 5 cheapest
            sorted_results = sorted(results, key=lambda x: x.get("price", {}).get("raw", 999999))
            print(f"\n  Top 5 cheapest flights:")
            for i, r in enumerate(sorted_results[:5]):
                price = r.get("price", {}).get("formatted", "N/A")
                legs = r.get("legs", [])
                info = []
                for leg in legs:
                    orig = leg.get("origin", {}).get("displayCode", "?")
                    dest = leg.get("destination", {}).get("displayCode", "?")
                    stops = leg.get("stopCount", 0)
                    dur = leg.get("durationInMinutes", 0)
                    carriers = [c.get("name","") for c in leg.get("carriers",{}).get("marketing",[])]
                    info.append(f"{orig}->{dest} {dur}min {stops}stop {','.join(carriers)}")
                print(f"    #{i+1} {price}: {' | '.join(info)}")
        else:
            print(f"\n  INCOMPLETE: status={status} after {poll} polls")
    
    else:
        print(f"\n  FAILED: HTTP {resp.status_code}")
        print(f"  Response body: {resp.text[:1000]}")

except Exception as e:
    print(f"\n  EXCEPTION: {type(e).__name__}: {e}")

print(f"\nDone at {time.strftime('%H:%M:%S')}")
