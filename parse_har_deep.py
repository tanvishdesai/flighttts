import json

with open(r"c:\Users\DELL\Desktop\code_playground\cheap flights\www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]

# Focus on the key API endpoints
print("="*120)
print("PART 1: ALL UNIQUE URL PATTERNS (excluding static assets)")
print("="*120)

from urllib.parse import urlparse
url_patterns = {}
for entry in entries:
    url = entry["request"]["url"]
    parsed = urlparse(url)
    path = parsed.path
    ext = path.split(".")[-1] if "." in path.split("/")[-1] else ""
    
    skip_ext = ["js", "css", "png", "jpg", "jpeg", "gif", "svg", "woff", "woff2", "ico", "webp", "ttf", "map", "avif"]
    if ext.lower() in skip_ext:
        continue
    
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    mime = entry["response"]["content"].get("mimeType", "")
    size = entry["response"]["content"].get("size", 0)
    
    # Normalize pattern: strip unique IDs from URLs like /v2/web-unified-search/XXXXXXX
    key = f"{method} {parsed.netloc}{parsed.path}"
    if key not in url_patterns:
        url_patterns[key] = {
            "method": method, "status": status, "mime": mime, "size": size,
            "full_url": url, "count": 0, "host": parsed.netloc
        }
    url_patterns[key]["count"] += 1

for key, info in sorted(url_patterns.items(), key=lambda x: x[0]):
    print(f"  [{info['count']:2d}x] [{info['method']:4s}] {info['status']:3d} | {info['size']:>10d}B | {info['mime'][:35]:35s} | {key[:120]}")


print("\n\n" + "="*120)
print("PART 2: DEEP DIVE - web-unified-search endpoints (THE MAIN SEARCH API)")
print("="*120)

for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" not in url:
        continue
    
    method = entry["request"]["method"]
    status = entry["response"]["status"]
    mime = entry["response"]["content"].get("mimeType", "")
    size = entry["response"]["content"].get("size", 0)
    
    print(f"\n{'='*80}")
    print(f"  Method: {method}")
    print(f"  URL: {url[:250]}")
    print(f"  Status: {status}")
    print(f"  Size: {size}")
    print(f"  Mime: {mime}")
    
    # ALL request headers
    print(f"\n  ALL Request Headers:")
    for h in entry["request"].get("headers", []):
        print(f"    {h['name']}: {h['value'][:200]}")
    
    # Request body
    post = entry["request"].get("postData", {})
    if post and post.get("text"):
        print(f"\n  Request Body ({len(post['text'])} chars):")
        print(f"    {post['text'][:3000]}")
    
    # Query params
    if entry["request"].get("queryString"):
        print(f"\n  Query Params:")
        for qp in entry["request"]["queryString"]:
            print(f"    {qp['name']}: {qp['value'][:200]}")
    
    # Cookies
    if entry["request"].get("cookies"):
        print(f"\n  Cookies ({len(entry['request']['cookies'])} total):")
        for c in entry["request"]["cookies"]:
            print(f"    {c['name']}: {str(c.get('value',''))[:100]}")
    
    # Response headers
    print(f"\n  Response Headers:")
    for h in entry["response"].get("headers", []):
        print(f"    {h['name']}: {h['value'][:200]}")
    
    # Response body structure
    resp_text = entry["response"]["content"].get("text", "")
    if resp_text:
        print(f"\n  Response Body ({len(resp_text)} chars):")
        try:
            rj = json.loads(resp_text)
            if isinstance(rj, dict):
                print(f"    Top keys: {list(rj.keys())}")
                for k, v in rj.items():
                    if isinstance(v, dict):
                        print(f"    '{k}': dict with keys: {list(v.keys())[:25]}")
                        # Go one more level for important keys
                        for sk, sv in v.items():
                            if isinstance(sv, dict):
                                print(f"      '{k}.{sk}': dict with keys: {list(sv.keys())[:20]}")
                            elif isinstance(sv, list):
                                print(f"      '{k}.{sk}': list of {len(sv)} items")
                                if sv and isinstance(sv[0], dict):
                                    print(f"        First item keys: {list(sv[0].keys())[:15]}")
                    elif isinstance(v, list):
                        print(f"    '{k}': list of {len(v)} items")
                    elif isinstance(v, str) and len(v) > 100:
                        print(f"    '{k}': str ({len(v)} chars)")
                    else:
                        print(f"    '{k}': {str(v)[:200]}")
        except:
            print(f"    [Non-JSON or parse error]: {resp_text[:500]}")

print("\n\n" + "="*120)
print("PART 3: DEEP DIVE - /g/conductor/v1/fps/search/ (Create search)")
print("="*120)

for entry in entries:
    url = entry["request"]["url"]
    if "conductor" in url or "fps/search" in url or ("create" in url.lower() and "search" in url.lower()):
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        print(f"\n  [{method}] {status} | {url[:200]}")
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"  Body: {post['text'][:2000]}")
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            print(f"  Response ({len(resp_text)} chars): {resp_text[:1000]}")

print("\n\n" + "="*120)
print("PART 4: DEEP DIVE - alternative-dates endpoint")
print("="*120)

for entry in entries:
    url = entry["request"]["url"]
    if "alternative-dates" in url:
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        mime = entry["response"]["content"].get("mimeType", "")
        print(f"\n  [{method}] {status} | {size}B | {url[:200]}")
        
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"\n  Request Body:")
            print(f"    {post['text'][:3000]}")
        
        # ALL request headers
        print(f"\n  Request Headers:")
        for h in entry["request"].get("headers", []):
            print(f"    {h['name']}: {h['value'][:200]}")
        
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                print(f"\n  Response keys: {list(rj.keys()) if isinstance(rj, dict) else 'list'}")
                if isinstance(rj, dict):
                    for k, v in rj.items():
                        if isinstance(v, dict):
                            print(f"    '{k}': {list(v.keys())[:20]}")
                        elif isinstance(v, list):
                            print(f"    '{k}': list of {len(v)}")
                        else:
                            print(f"    '{k}': {str(v)[:150]}")
            except:
                print(f"  Response (raw): {resp_text[:500]}")

print("\n\n" + "="*120)
print("PART 5: /g/radar/ or /radar/ endpoints")
print("="*120)

for entry in entries:
    url = entry["request"]["url"]
    if "/g/radar/" in url or "/radar/" in url:
        if "web-unified-search" in url or "alternative-dates" in url:
            continue  # already covered
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        mime = entry["response"]["content"].get("mimeType", "")
        print(f"\n  [{method}] {status} | {size:>8d}B | {mime[:30]:30s} | {url[:200]}")
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"    Body: {post['text'][:500]}")

print("\n\n" + "="*120)
print("PART 6: Cookies analysis")
print("="*120)

# Get ALL cookies from ALL requests
all_cookies = {}
for entry in entries:
    for h in entry["request"].get("headers", []):
        if h["name"].lower() == "cookie":
            pairs = h["value"].split("; ")
            for p in pairs:
                if "=" in p:
                    name = p.split("=", 1)[0].strip()
                    value = p.split("=", 1)[1].strip()
                    if name not in all_cookies:
                        all_cookies[name] = value

for name in sorted(all_cookies.keys()):
    val = all_cookies[name]
    print(f"  {name}: {val[:120]}{'...' if len(val) > 120 else ''}")

print(f"\n  Total unique cookies: {len(all_cookies)}")


print("\n\n" + "="*120)
print("PART 7: Set-Cookie headers in responses")
print("="*120)

for entry in entries:
    for h in entry["response"].get("headers", []):
        if h["name"].lower() == "set-cookie":
            url = entry["request"]["url"]
            parsed = urlparse(url)
            print(f"  From: {parsed.netloc}{parsed.path[:60]}")
            print(f"    {h['value'][:200]}")
