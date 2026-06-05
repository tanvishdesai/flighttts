import json
import sys
from collections import defaultdict

# Load HAR file
with open(r"c:\Users\DELL\Desktop\code_playground\cheap flights\www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]
print(f"Total entries: {len(entries)}\n")

# Categorize requests
flight_related = []
analytics = []
static_resources = []
other = []

# Keywords for flight-search related
flight_keywords = [
    "flight", "search", "poll", "create", "session", "fare", "price",
    "itinerary", "result", "booking", "quote", "avail", "schedule",
    "graphql", "gateway", "api", "grpc", "funnel", "dayview",
    "refresh", "locale", "culture", "autosuggest", "indicate"
]

# Keywords for analytics/tracking
analytics_keywords = [
    "analytics", "tracking", "telemetry", "log", "metric", "beacon",
    "pixel", "tag", "gtm", "google", "facebook", "criteo", "doubleclick",
    "newrelic", "nr-data", "sentry", "bam.", "bat.", "ads", "adservice",
    "adsense", "googlead", "pubads", "segment", "amplitude", "hotjar",
    "mparticle", "optimizely", "launchdarkly", "event", "collect",
    "impression", "cm.", "match.", "usersync", "id5", "uidapi",
    "googlesyndication", "serving-sys", "demdex", "scorecardresearch"
]

# Static resource extensions
static_extensions = [".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ico", ".webp", ".ttf", ".map"]

for entry in entries:
    url = entry["request"]["url"]
    method = entry["request"]["method"]
    
    # Check if static resource
    is_static = any(url.split("?")[0].lower().endswith(ext) for ext in static_extensions)
    
    # Check if analytics
    is_analytics = any(kw in url.lower() for kw in analytics_keywords)
    
    # Check if flight-related
    is_flight = any(kw in url.lower() for kw in flight_keywords)
    
    if is_static and not is_flight:
        static_resources.append(entry)
    elif is_flight:
        flight_related.append(entry)
    elif is_analytics:
        analytics.append(entry)
    else:
        other.append(entry)

print(f"Flight-related requests: {len(flight_related)}")
print(f"Analytics/tracking requests: {len(analytics)}")
print(f"Static resources: {len(static_resources)}")
print(f"Other requests: {len(other)}")

print("\n" + "="*120)
print("FLIGHT-RELATED REQUESTS (DETAILED)")
print("="*120)

for i, entry in enumerate(flight_related):
    url = entry["request"]["url"]
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    mime = entry["response"]["content"].get("mimeType", "unknown")
    size = entry["response"]["content"].get("size", 0)
    
    print(f"\n--- Request #{i+1} ---")
    print(f"  Method: {method}")
    print(f"  URL: {url[:300]}")
    print(f"  Status: {status}")
    print(f"  Content-Type: {mime}")
    print(f"  Response Size: {size} bytes")
    
    # Query parameters
    if entry["request"].get("queryString"):
        print(f"  Query Params:")
        for qp in entry["request"]["queryString"]:
            print(f"    {qp['name']}: {qp['value'][:200]}")
    
    # Request headers (selected important ones)
    important_headers = ["content-type", "authorization", "x-api-key", "cookie", 
                         "x-skyscanner", "x-request-id", "x-session", "origin",
                         "referer", "accept", "x-csrf", "x-forwarded"]
    req_headers = entry["request"].get("headers", [])
    print(f"  Request Headers (important):")
    for h in req_headers:
        if any(ih in h["name"].lower() for ih in important_headers):
            val = h["value"][:300] if len(h["value"]) > 300 else h["value"]
            print(f"    {h['name']}: {val}")
    
    # Request body
    post_data = entry["request"].get("postData", {})
    if post_data:
        text = post_data.get("text", "")
        if text:
            print(f"  Request Body ({len(text)} chars):")
            print(f"    {text[:2000]}")
    
    # Response body preview
    resp_text = entry["response"]["content"].get("text", "")
    if resp_text and mime and ("json" in mime or "text" in mime):
        print(f"  Response Body Preview ({len(resp_text)} chars):")
        # Try to parse as JSON and show structure
        try:
            resp_json = json.loads(resp_text)
            if isinstance(resp_json, dict):
                print(f"    Top-level keys: {list(resp_json.keys())[:30]}")
                # Show deeper structure for important keys
                for key in resp_json:
                    val = resp_json[key]
                    if isinstance(val, dict):
                        print(f"    '{key}' sub-keys: {list(val.keys())[:20]}")
                    elif isinstance(val, list):
                        print(f"    '{key}': list of {len(val)} items")
                        if val and isinstance(val[0], dict):
                            print(f"      First item keys: {list(val[0].keys())[:15]}")
                    elif isinstance(val, str) and len(val) > 200:
                        print(f"    '{key}': string ({len(val)} chars)")
                    else:
                        print(f"    '{key}': {str(val)[:200]}")
            elif isinstance(resp_json, list):
                print(f"    Array of {len(resp_json)} items")
                if resp_json and isinstance(resp_json[0], dict):
                    print(f"    First item keys: {list(resp_json[0].keys())[:15]}")
        except:
            print(f"    (Raw text): {resp_text[:500]}")

print("\n" + "="*120)
print("OTHER (NON-STATIC, NON-ANALYTICS) REQUESTS")
print("="*120)

for i, entry in enumerate(other):
    url = entry["request"]["url"]
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    mime = entry["response"]["content"].get("mimeType", "unknown")
    print(f"  [{method}] {status} | {mime[:30]:30s} | {url[:150]}")

print("\n" + "="*120)
print("COOKIES FROM FLIGHT-RELATED REQUESTS")
print("="*120)

all_cookies = set()
for entry in flight_related:
    for cookie in entry["request"].get("cookies", []):
        all_cookies.add(cookie["name"])
    # Also check cookie header
    for h in entry["request"].get("headers", []):
        if h["name"].lower() == "cookie":
            pairs = h["value"].split("; ")
            for p in pairs:
                if "=" in p:
                    name = p.split("=")[0].strip()
                    all_cookies.add(name)

print(f"\nUnique cookies found: {len(all_cookies)}")
for c in sorted(all_cookies):
    print(f"  - {c}")

print("\n" + "="*120)
print("RESPONSE HEADERS FROM FLIGHT-RELATED REQUESTS") 
print("="*120)

for i, entry in enumerate(flight_related):
    url = entry["request"]["url"]
    resp_headers = entry["response"].get("headers", [])
    session_headers = [h for h in resp_headers if any(kw in h["name"].lower() for kw in ["session", "token", "auth", "csrf", "set-cookie", "x-sky"])]
    if session_headers:
        print(f"\n  Request #{i+1}: {url[:120]}")
        for h in session_headers:
            print(f"    {h['name']}: {h['value'][:200]}")
