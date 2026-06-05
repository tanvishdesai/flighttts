import json
import base64

with open(r"c:\Users\DELL\Desktop\code_playground\cheap flights\www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]

# PART A: Decode JWT session token
print("="*80)
print("PART A: JWT SESSION TOKEN DECODE")
print("="*80)

jwt_token = None
for entry in entries:
    for h in entry["response"].get("headers", []):
        if h["name"] == "x-skyscanner-session-id-token":
            jwt_token = h["value"]
            break
    if jwt_token:
        break

if jwt_token:
    parts = jwt_token.split(".")
    # Decode header and payload
    for i, name in enumerate(["Header", "Payload"]):
        padded = parts[i] + "=" * (4 - len(parts[i]) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded)
            print(f"\n  {name}: {json.dumps(json.loads(decoded), indent=2)}")
        except:
            print(f"\n  {name}: [decode error]")

# PART B: PerimeterX / anti-bot requests  
print("\n\n" + "="*80)
print("PART B: ANTI-BOT / PERIMETERX / RECAPTCHA REQUESTS")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if any(kw in url.lower() for kw in ["px-cloud", "perimeterx", "recaptcha", "captcha", "rf8vapwa", "bot"]):
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        mime = entry["response"]["content"].get("mimeType", "")
        print(f"\n  [{method}] {status} | {size:>8d}B | {mime[:30]:30s} | {url[:200]}")
        
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"    Body: {post['text'][:500]}")
        
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            print(f"    Response ({len(resp_text)} chars): {resp_text[:300]}")

# PART C: /g/michelin/ endpoint (appears to be a search API too)
print("\n\n" + "="*80)
print("PART C: MICHELIN SEARCH ENDPOINT")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "michelin" in url:
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        
        print(f"\n  [{method}] {status} | {size}B | {url[:200]}")
        
        # ALL headers
        print(f"\n  Request Headers:")
        for h in entry["request"].get("headers", []):
            print(f"    {h['name']}: {h['value'][:200]}")
        
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"\n  Request Body:")
            body_text = post["text"]
            print(f"    {body_text[:3000]}")
        
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                print(f"\n  Response structure:")
                if isinstance(rj, dict):
                    print(f"    Top keys: {list(rj.keys())}")
                    for k, v in rj.items():
                        if isinstance(v, dict):
                            print(f"    '{k}': dict keys: {list(v.keys())[:20]}")
                        elif isinstance(v, list):
                            print(f"    '{k}': list of {len(v)}")
                            if v and isinstance(v[0], dict):
                                print(f"      First item: {list(v[0].keys())[:15]}")
                        else:
                            print(f"    '{k}': {str(v)[:150]}")
            except:
                pass

# PART D: price-insights / price-bands endpoint
print("\n\n" + "="*80)
print("PART D: PRICE INSIGHTS / PRICE BANDS ENDPOINT")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "price-insights" in url or "price-bands" in url or "pricecalendar" in url:
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        
        print(f"\n  [{method}] {status} | {size}B | {url[:200]}")
        
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"\n  Request Body:")
            print(f"    {post['text'][:2000]}")
        
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                print(f"\n  Response:")
                print(f"    {json.dumps(rj, indent=2)[:1500]}")
            except:
                print(f"    {resp_text[:500]}")

# PART E: ARS request endpoint 
print("\n\n" + "="*80)
print("PART E: ARS / AD REQUEST ENDPOINT")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "/g/ars/" in url:
        method = entry["request"]["method"]
        status = entry["response"]["status"]
        size = entry["response"]["content"].get("size", 0)
        
        print(f"\n  [{method}] {status} | {size}B | {url[:200]}")
        
        post = entry["request"].get("postData", {})
        if post and post.get("text"):
            print(f"\n  Request Body:")
            print(f"    {post['text'][:2000]}")

# PART F: Initial search POST body - detailed itinerary response sample
print("\n\n" + "="*80)
print("PART F: SAMPLE ITINERARY FROM FIRST POPULATED RESPONSE")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "web-unified-search" in url:
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                results = rj.get("itineraries", {}).get("results", [])
                if results:
                    # Print first result
                    print(f"\n  First itinerary result:")
                    print(f"    {json.dumps(results[0], indent=2)[:3000]}")
                    
                    # Print context
                    ctx = rj.get("context", {})
                    print(f"\n  Context: {json.dumps(ctx, indent=2)}")
                    
                    itctx = rj.get("itineraries", {}).get("context", {})
                    print(f"\n  Itineraries Context: {json.dumps(itctx, indent=2)}")
                    
                    # Print first agent
                    agents = rj.get("itineraries", {}).get("agents", [])
                    if agents:
                        print(f"\n  First agent: {json.dumps(agents[0], indent=2)}")
                    
                    break
            except:
                pass

# PART G: Context/status progression across polling requests
print("\n\n" + "="*80)
print("PART G: POLLING PROGRESSION (status across requests)")
print("="*80)

for i, entry in enumerate(entries):
    url = entry["request"]["url"]
    if "web-unified-search" in url:
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                ctx = rj.get("context", {})
                itctx = rj.get("itineraries", {}).get("context", {})
                results_count = len(rj.get("itineraries", {}).get("results", []))
                agents_count = len(rj.get("itineraries", {}).get("agents", []))
                method = entry["request"]["method"]
                size = entry["response"]["content"].get("size", 0)
                print(f"  [{method}] Size: {size:>10d}B | ctx.status: {ctx.get('status', 'N/A'):20s} | it.ctx.status: {itctx.get('status', 'N/A'):20s} | results: {results_count:3d} | agents: {agents_count:2d} | sessionId: {ctx.get('sessionId', 'N/A')[:60]}")
            except:
                pass

# PART H: culture-data-service response sample
print("\n\n" + "="*80)
print("PART H: CULTURE-DATA-SERVICE RESPONSE")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "culture-data-service" in url:
        resp_text = entry["response"]["content"].get("text", "")
        if resp_text:
            try:
                rj = json.loads(resp_text)
                print(f"  Keys: {list(rj.keys())[:15]}")
                print(f"  Sample: {json.dumps(rj, indent=2)[:1000]}")
            except:
                pass
