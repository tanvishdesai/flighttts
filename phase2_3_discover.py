"""
PHASE 2: Discover ALL skyscanner.co.in endpoints not yet investigated.
PHASE 3: Entity ID resolution - find autosuggest/geo/place endpoints.
"""
import json
from urllib.parse import urlparse, parse_qs, unquote
from collections import defaultdict

with open(r"www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]

# ============================================================
# PHASE 2: ALL SKYSCANNER ENDPOINTS
# ============================================================
print("="*100)
print("PHASE 2: ALL SKYSCANNER.CO.IN ENDPOINTS (complete inventory)")
print("="*100)

sk_endpoints = defaultdict(list)
for entry in entries:
    url = entry["request"]["url"]
    parsed = urlparse(url)
    
    if "skyscanner.co.in" not in parsed.netloc:
        continue
    
    # Skip static assets
    path = parsed.path
    ext = path.split(".")[-1].lower() if "." in path.split("/")[-1] else ""
    if ext in ["js", "css", "png", "jpg", "jpeg", "gif", "svg", "woff", "woff2", "ico", "webp", "ttf", "map", "avif"]:
        continue
    
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    size = entry["response"]["content"].get("size", 0)
    mime = entry["response"]["content"].get("mimeType", "")
    
    key = f"{method} {path}"
    sk_endpoints[key].append({
        "url": url, "status": status, "size": size, "mime": mime,
        "entry": entry
    })

print(f"\nTotal unique Skyscanner endpoint paths: {len(sk_endpoints)}")
print()

for key in sorted(sk_endpoints.keys()):
    items = sk_endpoints[key]
    first = items[0]
    print(f"  [{len(items):2d}x] [{key.split()[0]:4s}] {first['status']:3d} | {first['size']:>10d}B | {first['mime'][:35]:35s} | {key[5:][:100]}")

# ============================================================
# PHASE 2B: DEEP DIVE on undiscovered endpoints
# ============================================================
print("\n\n" + "="*100)
print("PHASE 2B: DEEP DIVE on EACH SKYSCANNER ENDPOINT")
print("="*100)

already_analyzed = ["web-unified-search", "alternative-dates", "michelin", "price-insights", 
                    "pricecalendar", "ars/public", "culture-data-service", "saved-experience",
                    "tagging", "slipstream", "pixel", "rf8vapwA"]

for key in sorted(sk_endpoints.keys()):
    path = key.split(maxsplit=1)[1] if " " in key else key
    
    # Skip already analyzed
    if any(a in path for a in already_analyzed):
        continue
    
    items = sk_endpoints[key]
    entry = items[0]["entry"]
    
    print(f"\n{'='*80}")
    print(f"ENDPOINT: {key}")
    print(f"Count: {len(items)}x | Status: {items[0]['status']} | Size: {items[0]['size']}B")
    
    # Request headers
    headers = {h["name"]: h["value"] for h in entry["request"].get("headers", [])}
    important = {k: v[:150] for k, v in headers.items() 
                 if any(x in k.lower() for x in ["content-type", "x-sky", "authorization", "cookie", "x-api"])}
    if important:
        print(f"Important headers: {json.dumps(important, indent=2)}")
    
    # Query params
    qs = entry["request"].get("queryString", [])
    if qs:
        print(f"Query params:")
        for q in qs:
            print(f"  {q['name']}: {q['value'][:150]}")
    
    # POST body
    pd = entry["request"].get("postData", {})
    if pd and pd.get("text"):
        print(f"POST body ({len(pd['text'])} chars):")
        print(f"  {pd['text'][:1500]}")
    
    # Response body
    resp_text = entry["response"]["content"].get("text", "")
    if resp_text:
        print(f"Response ({len(resp_text)} chars):")
        try:
            rj = json.loads(resp_text)
            if isinstance(rj, dict):
                print(f"  Keys: {list(rj.keys())}")
                for k, v in rj.items():
                    if isinstance(v, dict):
                        print(f"  '{k}': dict keys={list(v.keys())[:15]}")
                    elif isinstance(v, list):
                        print(f"  '{k}': list of {len(v)}")
                        if v and isinstance(v[0], dict):
                            print(f"    First item keys: {list(v[0].keys())[:15]}")
                    else:
                        print(f"  '{k}': {str(v)[:150]}")
            else:
                print(f"  {str(rj)[:500]}")
        except:
            print(f"  (raw): {resp_text[:500]}")


# ============================================================
# PHASE 3: ENTITY ID RESOLUTION
# ============================================================
print("\n\n" + "="*100)
print("PHASE 3: ENTITY ID RESOLUTION - Search for autosuggest/geo/place endpoints")
print("="*100)

# Search ALL entries for entity-related patterns
entity_keywords = ["autosuggest", "suggest", "autocomplete", "geo", "place", "entity", 
                   "airport", "city", "location", "lookup", "search-box", "searchbox",
                   "typeahead", "complete", "hint", "resolve", "iata"]

print("\n--- Searching ALL URLs for entity-related patterns ---")
for entry in entries:
    url = entry["request"]["url"].lower()
    for kw in entity_keywords:
        if kw in url:
            print(f"  [{entry['request']['method']}] {entry['response']['status']} | "
                  f"Found '{kw}' in: {entry['request']['url'][:200]}")
            break

# Search response bodies for IATA codes and entity ID mappings
print("\n--- Searching response bodies for IATA/entity ID mappings ---")
iata_codes = ["AMD", "MRU", "BLR", "DEL", "BOM", "LHR", "JFK", "NRT"]
entity_mappings = {}

for entry in entries:
    resp_text = entry["response"]["content"].get("text", "")
    if not resp_text:
        continue
    
    for code in iata_codes:
        if f'"flightPlaceId":"{code}"' in resp_text or f'"displayCode":"{code}"' in resp_text:
            try:
                rj = json.loads(resp_text)
                resp_str = json.dumps(rj)
                # Search for entityId near the IATA code
                import re
                # Find entityId values near IATA codes
                for match in re.finditer(r'"entityId":\s*"(\d+)"', resp_str):
                    entity_id = match.group(1)
                    # Check if this entityId is near the IATA code
                    start = max(0, match.start() - 200)
                    end = min(len(resp_str), match.end() + 200)
                    context = resp_str[start:end]
                    if code in context:
                        if code not in entity_mappings:
                            entity_mappings[code] = set()
                        entity_mappings[code].add(entity_id)
            except:
                pass

print("\n  Entity ID mappings found in responses:")
for code, ids in sorted(entity_mappings.items()):
    print(f"    {code}: {sorted(ids)}")

# Search the initial HTML response for any embedded config with entity data
print("\n--- Searching initial HTML for embedded config/data ---")
for entry in entries:
    url = entry["request"]["url"]
    mime = entry["response"]["content"].get("mimeType", "")
    if "text/html" in mime and "skyscanner.co.in/transport/flights" in url:
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            # Search for entity-related config
            import re
            
            # Look for __NEXT_DATA__ or similar embedded JSON
            for pattern in [r'__NEXT_DATA__\s*=\s*({.*?})\s*</script>',
                           r'window\.__data\s*=\s*({.*?})\s*;',
                           r'"entityId":\s*"(\d+)"',
                           r'"skyId":\s*"([A-Z]{3})"',
                           r'"iata":\s*"([A-Z]{3})"']:
                matches = re.findall(pattern, resp_text[:50000])
                if matches:
                    print(f"  Pattern '{pattern[:40]}...' found {len(matches)} matches")
                    for m in matches[:5]:
                        print(f"    {str(m)[:200]}")
            
            # Search for autosuggest/geo API URLs in the HTML
            for pattern in [r'autosuggest[^"]*', r'geo[^"]*api[^"]*', r'place[^"]*api[^"]*',
                           r'/g/[a-z-]+/[^"]*']:
                matches = re.findall(pattern, resp_text[:100000])
                if matches:
                    unique = set(m[:100] for m in matches)
                    for m in sorted(unique)[:10]:
                        if "skyscanner" in m.lower() or m.startswith("/g/"):
                            print(f"  API pattern found: {m}")

# Search JS files for autosuggest endpoints
print("\n--- Searching JS chunks for autosuggest/entity API URLs ---")
for entry in entries:
    url = entry["request"]["url"]
    if ".js" not in url:
        continue
    resp_text = entry["response"]["content"].get("text", "")
    if not resp_text:
        continue
    
    # Search for API endpoint patterns
    import re
    for pattern in [r'autosuggest[^"\']*', r'/g/[a-z-]+/[^"\']{5,50}',
                   r'entityId[^"\']*', r'geo-service[^"\']*',
                   r'place-service[^"\']*', r'airport[^"\']*lookup']:
        matches = re.findall(pattern, resp_text[:500000])
        if matches:
            unique = set(m[:80] for m in matches if len(m) > 10)
            if unique:
                for m in sorted(unique)[:5]:
                    print(f"  In {url.split('/')[-1][:40]}: {m}")
