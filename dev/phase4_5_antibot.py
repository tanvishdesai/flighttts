"""
PHASE 4: Direct API feasibility - analyze minimal required request components.
PHASE 5: Anti-bot deep analysis.
"""
import json
from urllib.parse import urlparse
import base64

with open(r"www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]

# ============================================================
# PHASE 4: MINIMAL REQUEST ANALYSIS
# ============================================================
print("="*100)
print("PHASE 4: DIRECT API FEASIBILITY - MINIMAL REQUEST")
print("="*100)

# Analyze ALL headers sent on the POST request
print("\n--- POST /web-unified-search/ - Complete header analysis ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" in url and entry["request"]["method"] == "POST":
        headers = entry["request"].get("headers", [])
        print(f"\n  Total headers: {len(headers)}")
        
        # Categorize headers
        browser_std = []
        skyscanner_custom = []
        security_sec = []
        other = []
        
        for h in headers:
            name = h["name"].lower()
            if name.startswith("sec-"):
                security_sec.append(h)
            elif name.startswith("x-skyscanner"):
                skyscanner_custom.append(h)
            elif name.startswith(":") or name in ["accept", "accept-encoding", "accept-language", 
                                                     "content-type", "content-length", "origin",
                                                     "referer", "user-agent", "priority"]:
                browser_std.append(h)
            else:
                other.append(h)
        
        print(f"\n  BROWSER STANDARD ({len(browser_std)}):")
        for h in browser_std:
            print(f"    {h['name']}: {h['value'][:150]}")
        
        print(f"\n  SKYSCANNER CUSTOM ({len(skyscanner_custom)}):")
        for h in skyscanner_custom:
            print(f"    {h['name']}: {h['value'][:150]}")
        
        print(f"\n  SEC-* HEADERS ({len(security_sec)}):")
        for h in security_sec:
            print(f"    {h['name']}: {h['value'][:150]}")
        
        print(f"\n  OTHER ({len(other)}):")
        for h in other:
            print(f"    {h['name']}: {h['value'][:150]}")
        
        break

# Compare headers between POST and subsequent GETs
print("\n\n--- Header comparison: POST vs GET requests ---")
post_headers = set()
get_headers_list = []

for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    
    headers = {h["name"].lower() for h in entry["request"].get("headers", [])}
    method = entry["request"]["method"]
    
    if method == "POST":
        post_headers = headers
    else:
        get_headers_list.append(headers)

if get_headers_list:
    all_get_headers = get_headers_list[0]
    for gh in get_headers_list[1:]:
        all_get_headers = all_get_headers.intersection(gh)
    
    print(f"\n  Headers in POST but NOT in GET:")
    for h in sorted(post_headers - all_get_headers):
        print(f"    - {h}")
    
    print(f"\n  Headers in GET but NOT in POST:")
    for h in sorted(all_get_headers - post_headers):
        print(f"    + {h}")

# Analyze x-gateway-servedby
print("\n\n--- x-gateway-servedby analysis ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    
    method = entry["request"]["method"]
    
    # Check request headers
    req_gw = None
    for h in entry["request"].get("headers", []):
        if h["name"].lower() == "x-gateway-servedby":
            req_gw = h["value"]
    
    # Check response headers
    resp_gw = None
    for h in entry["response"].get("headers", []):
        if h["name"].lower() == "x-gateway-servedby":
            resp_gw = h["value"]
    
    print(f"  [{method}] Request x-gateway-servedby: {req_gw} | Response: {resp_gw}")

# Analyze UUIDs
print("\n\n--- UUID Analysis ---")
uuid_headers = ["x-skyscanner-viewid", "x-skyscanner-trustedfunnelid", "x-skyscanner-traveller-context"]
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    method = entry["request"]["method"]
    uuids = {}
    for h in entry["request"].get("headers", []):
        if h["name"].lower() in [u.lower() for u in uuid_headers]:
            uuids[h["name"]] = h["value"]
    
    print(f"  [{method}] UUIDs: {json.dumps(uuids, indent=4)}")

# Check if viewId and trustedFunnelId are always the same
print("\n  Are viewId and trustedFunnelId always identical?")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    headers = {h["name"].lower(): h["value"] for h in entry["request"].get("headers", [])}
    vid = headers.get("x-skyscanner-viewid", "")
    fid = headers.get("x-skyscanner-trustedfunnelid", "")
    print(f"    viewId == trustedFunnelId: {vid == fid}")

# ============================================================
# PHASE 5: ANTI-BOT DEEP ANALYSIS
# ============================================================
print("\n\n" + "="*100)
print("PHASE 5: ANTI-BOT DEEP ANALYSIS")
print("="*100)

# 5.1: PerimeterX request timing relative to search requests
print("\n--- 5.1: PerimeterX request timing ---")
px_times = []
search_times = []

for entry in entries:
    url = entry["request"]["url"]
    time = entry.get("startedDateTime", "")
    
    if "rf8vapwA" in url:
        px_times.append({"time": time, "url": url[:120], "method": entry["request"]["method"]})
    elif "web-unified-search" in url:
        search_times.append({"time": time, "url": url[:120], "method": entry["request"]["method"]})

print("  PerimeterX requests:")
for t in px_times:
    print(f"    {t['time'][:23]} [{t['method']}] {t['url']}")
print("  Search requests:")
for t in search_times:
    print(f"    {t['time'][:23]} [{t['method']}] {t['url']}")

# 5.2: Check if PX sets any response headers or cookies that are then sent on search requests
print("\n--- 5.2: PerimeterX response analysis ---")
for entry in entries:
    url = entry["request"]["url"]
    if "rf8vapwA" not in url:
        continue
    
    method = entry["request"]["method"]
    
    # Response headers
    resp_headers = entry["response"].get("headers", [])
    set_cookies = [h for h in resp_headers if h["name"].lower() == "set-cookie"]
    
    print(f"\n  [{method}] {url[:100]}")
    print(f"    Response headers ({len(resp_headers)}):")
    for h in resp_headers:
        print(f"      {h['name']}: {h['value'][:150]}")
    
    if set_cookies:
        print(f"    SET-COOKIE headers:")
        for sc in set_cookies:
            print(f"      {sc['value'][:200]}")

# 5.3: Check ALL set-cookie headers from skyscanner.co.in
print("\n\n--- 5.3: ALL Set-Cookie from skyscanner.co.in ---")
for entry in entries:
    url = entry["request"]["url"]
    parsed = urlparse(url)
    if "skyscanner.co.in" not in parsed.netloc:
        continue
    
    for h in entry["response"].get("headers", []):
        if h["name"].lower() == "set-cookie":
            print(f"  From {parsed.path[:60]}: {h['value'][:200]}")

# 5.4: Check if there's a _px cookie being set
print("\n\n--- 5.4: Search for _px cookies in ALL requests ---")
for entry in entries:
    # Check all cookie headers
    for h in entry["request"].get("headers", []):
        if h["name"].lower() == "cookie" and "_px" in h["value"]:
            url = entry["request"]["url"][:100]
            # Extract px cookies
            cookies = h["value"].split("; ")
            px_cookies = [c for c in cookies if "_px" in c.lower()]
            print(f"  URL: {url}")
            for c in px_cookies:
                print(f"    {c[:150]}")
    
    # Check cookie array
    for c in entry["request"].get("cookies", []):
        if "_px" in c.get("name", "").lower():
            print(f"  Cookie array: {c['name']}={c.get('value', '')[:100]}")

# 5.5: Analyze the collector payload structure
print("\n\n--- 5.5: PerimeterX collector payload analysis ---")
for entry in entries:
    url = entry["request"]["url"]
    if "rf8vapwA/xhr/api/v2/collector" not in url:
        continue
    
    method = entry["request"]["method"]
    pd = entry["request"].get("postData", {})
    body = pd.get("text", "")
    
    print(f"\n  [{method}] Collector request")
    print(f"    Content-Type: {pd.get('mimeType', 'N/A')}")
    print(f"    Body length: {len(body)}")
    
    # Check request headers for PX-specific headers
    for h in entry["request"].get("headers", []):
        if "px" in h["name"].lower() or "human" in h["name"].lower():
            print(f"    PX header: {h['name']}: {h['value'][:100]}")
    
    # Response
    resp_text = entry["response"]["content"].get("text", "")
    if resp_text:
        print(f"    Response: {resp_text[:300]}")
        try:
            rj = json.loads(resp_text)
            print(f"    Response keys: {list(rj.keys())}")
            for k, v in rj.items():
                print(f"      '{k}': {str(v)[:200]}")
        except:
            pass

# 5.6: Check response headers on search endpoints for PX enforcement
print("\n\n--- 5.6: Search endpoint response headers (PX enforcement?) ---")
for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    
    method = entry["request"]["method"]
    resp_headers = {h["name"].lower(): h["value"] for h in entry["response"].get("headers", [])}
    
    # Check for PX-related response headers
    px_resp = {k: v for k, v in resp_headers.items() 
               if any(x in k for x in ["px", "human", "challenge", "block", "captcha", "bot"])}
    
    print(f"  [{method}] PX-related response headers: {px_resp if px_resp else 'NONE'}")
    
    # Check status code
    print(f"  [{method}] Status: {entry['response']['status']}")

# 5.7: Timing analysis - did PX init before or after search started?
print("\n\n--- 5.7: Timeline of page load, PX init, and search ---")
timeline = []
for entry in entries:
    url = entry["request"]["url"]
    time = entry.get("startedDateTime", "")
    method = entry["request"]["method"]
    
    if "transport/flights" in url and "text/html" in entry["response"]["content"].get("mimeType", ""):
        timeline.append(("PAGE_LOAD", time, method, url[:80]))
    elif "rf8vapwA/init" in url:
        timeline.append(("PX_INIT", time, method, url[:80]))
    elif "rf8vapwA/xhr" in url:
        timeline.append(("PX_COLLECTOR", time, method, url[:80]))
    elif "px-cloud" in url:
        timeline.append(("PX_CLOUD", time, method, url[:80]))
    elif "web-unified-search" in url:
        timeline.append(("SEARCH_API", time, method, url[:80]))

timeline.sort(key=lambda x: x[1])
for event, time, method, url in timeline:
    print(f"  {time[:23]} | {event:15s} | [{method}] {url}")
