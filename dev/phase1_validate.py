"""
PHASE 1: Independent validation of all claims from HAR analysis.
PHASE 2: Discover ALL endpoints not yet investigated.
PHASE 3: Entity ID resolution - find autosuggest/geo endpoints.
"""
import json
from urllib.parse import urlparse, parse_qs, unquote
from collections import defaultdict

with open(r"www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]
print(f"Total HAR entries: {len(entries)}")

# ============================================================
# PHASE 1: VALIDATION
# ============================================================
print("\n" + "="*100)
print("PHASE 1: CLAIM VALIDATION")
print("="*100)

# CLAIM 1: web-unified-search is primary flight search endpoint
print("\n--- CLAIM 1: web-unified-search is the primary flight search endpoint ---")
search_endpoints = []
for entry in entries:
    url = entry["request"]["url"]
    resp_text = entry["response"]["content"].get("text", "")
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    size = entry["response"]["content"].get("size", 0)
    
    # Check if response contains flight-like data
    has_flight_data = False
    if resp_text:
        for marker in ['"itineraries"', '"itinerary"', '"flightNumber"', '"price":', '"legs":', 
                       '"departure":', '"arrival":', '"carriers":', '"segments":',
                       '"origin":', '"destination":', '"durationInMinutes"']:
            if marker in resp_text:
                has_flight_data = True
                break
    
    if has_flight_data and "skyscanner" in url:
        parsed = urlparse(url)
        search_endpoints.append({
            "url": url[:150],
            "method": method,
            "status": status,
            "size": size,
            "path": parsed.path,
            "has_itineraries": '"itineraries"' in resp_text,
            "has_results": '"results"' in resp_text,
            "has_flightNumber": '"flightNumber"' in resp_text,
            "has_pricingOptions": '"pricingOptions"' in resp_text,
        })

print(f"  Endpoints with flight-like data: {len(search_endpoints)}")
for ep in search_endpoints:
    print(f"    [{ep['method']}] {ep['status']} | {ep['size']:>10d}B | itineraries={ep['has_itineraries']} "
          f"results={ep['has_results']} flightNum={ep['has_flightNumber']} | {ep['path'][:80]}")

# CLAIM 2 & 3 & 4: Polling-based session search
print("\n--- CLAIMS 2-4: Polling-based search session ---")
wus_entries = []
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" in url:
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        started = entry.get("startedDateTime", "")
        resp_text = entry["response"]["content"].get("text", "")
        
        ctx_status = ""
        session_id = ""
        results_count = 0
        total_results = 0
        
        if resp_text:
            try:
                rj = json.loads(resp_text)
                ctx_status = rj.get("context", {}).get("status", "N/A")
                session_id = rj.get("context", {}).get("sessionId", "N/A")[:60]
                results_count = len(rj.get("itineraries", {}).get("results", []))
                total_results = rj.get("itineraries", {}).get("context", {}).get("totalResults", 0)
            except:
                pass
        
        post_body = ""
        pd = entry["request"].get("postData", {})
        if pd and pd.get("text"):
            post_body = pd["text"][:200]
        
        wus_entries.append({
            "method": method, "status": status, "time": started,
            "ctx_status": ctx_status, "session_id": session_id,
            "results": results_count, "total": total_results,
            "post_body": post_body
        })

print(f"  web-unified-search requests: {len(wus_entries)}")
for i, e in enumerate(wus_entries):
    print(f"    #{i+1} [{e['method']}] {e['status']} | {e['time'][:23]} | "
          f"status={e['ctx_status']:12s} | results={e['results']:3d} | total={e['total']:3d} | "
          f"sessionId={e['session_id'][:50]}")
    if e["post_body"]:
        print(f"         POST body: {e['post_body']}")

# CLAIM 5 & 6: No authentication / API key
print("\n--- CLAIMS 5-6: No auth / API key required ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    headers = {h["name"].lower(): h["value"] for h in entry["request"].get("headers", [])}
    
    auth_headers = {}
    for hname in ["authorization", "x-api-key", "api-key", "apikey", "token", 
                   "x-auth-token", "x-access-token", "bearer", "x-csrf-token",
                   "x-xsrf-token", "cookie"]:
        if hname in headers:
            auth_headers[hname] = headers[hname][:100]
    
    print(f"  [{entry['request']['method']}] Auth headers found: {auth_headers if auth_headers else 'NONE'}")

# CLAIM 7: No cookies on search requests
print("\n--- CLAIM 7: No cookies on search requests ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    
    cookies = entry["request"].get("cookies", [])
    cookie_header = ""
    for h in entry["request"].get("headers", []):
        if h["name"].lower() == "cookie":
            cookie_header = h["value"]
    
    print(f"  [{entry['request']['method']}] cookies array: {len(cookies)} items | "
          f"cookie header: {'YES (' + str(len(cookie_header)) + ' chars)' if cookie_header else 'ABSENT'}")
    if cookie_header:
        print(f"    Cookie value: {cookie_header[:300]}")

# CLAIM 8: PerimeterX is primary anti-bot
print("\n--- CLAIM 8: PerimeterX / HUMAN Security ---")
px_entries = []
for entry in entries:
    url = entry["request"]["url"]
    if any(kw in url.lower() for kw in ["rf8vapwa", "px-cloud", "perimeterx", "human"]):
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        px_entries.append({"method": method, "url": url[:200], "status": status, "size": size})

print(f"  PerimeterX-related requests: {len(px_entries)}")
for e in px_entries:
    print(f"    [{e['method']}] {e['status']} | {e['size']:>8d}B | {e['url']}")

# Check for OTHER anti-bot systems
print("\n  Other potential anti-bot systems:")
for entry in entries:
    url = entry["request"]["url"].lower()
    for kw in ["captcha", "recaptcha", "hcaptcha", "cloudflare", "akamai", "datadome", 
               "kasada", "shape", "imperva", "distil", "bot-detect", "challenge"]:
        if kw in url:
            print(f"    Found: {kw} -> {entry['request']['url'][:150]}")

# CLAIM 9: Entity IDs required
print("\n--- CLAIM 9: Entity IDs are required in search requests ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url or entry["request"]["method"] != "POST":
        continue
    pd = entry["request"].get("postData", {})
    if pd and pd.get("text"):
        body = pd["text"]
        print(f"  POST body contains entityId: {'entityId' in body}")
        print(f"  POST body contains IATA code: {'AMD' in body or 'MRU' in body}")
        print(f"  Full body:\n    {body}")

# CLAIM 10: Flight data completeness
print("\n--- CLAIM 10: Flight data completeness ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    resp_text = entry["response"]["content"].get("text", "")
    if not resp_text:
        continue
    try:
        rj = json.loads(resp_text)
        results = rj.get("itineraries", {}).get("results", [])
        if results and len(results) > 5:
            r = results[0]
            print(f"  First result keys: {list(r.keys())}")
            print(f"  Has price: {'price' in r}")
            print(f"  Has legs: {'legs' in r}")
            print(f"  Has pricingOptions: {'pricingOptions' in r}")
            
            if "legs" in r and r["legs"]:
                leg = r["legs"][0]
                print(f"  Leg keys: {list(leg.keys())}")
                print(f"  Has segments: {'segments' in leg}")
                print(f"  Has carriers: {'carriers' in leg}")
                print(f"  Has departure/arrival: {'departure' in leg and 'arrival' in leg}")
                
                if "segments" in leg and leg["segments"]:
                    seg = leg["segments"][0]
                    print(f"  Segment keys: {list(seg.keys())}")
                    print(f"  Has flightNumber: {'flightNumber' in seg}")
                    print(f"  Has marketingCarrier: {'marketingCarrier' in seg}")
            
            if "pricingOptions" in r and r["pricingOptions"]:
                po = r["pricingOptions"][0]
                print(f"  PricingOption keys: {list(po.keys())}")
                if "agents" in po:
                    print(f"  PricingOption.agents[0] keys: {list(po['agents'][0].keys()) if po['agents'] else 'EMPTY'}")
            
            break
    except:
        pass
