"""
Extract precise entity ID to IATA mappings from flight response data.
Also extract the autosuggest API pattern from JS.
"""
import json
import re

with open(r"www.skyscanner.co.in.har", "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]

# 1. Extract entity-to-IATA from segment/origin/destination data in search results
print("="*80)
print("PRECISE ENTITY ID MAPPINGS FROM FLIGHT RESULTS")
print("="*80)

entity_map = {}  # entityId -> {iata, name, type, city, country}

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
        
        for itinerary in results:
            for leg in itinerary.get("legs", []):
                # Top-level origin/destination
                for place_key in ["origin", "destination"]:
                    place = leg.get(place_key, {})
                    eid = place.get("entityId")
                    iata = place.get("displayCode") or place.get("id")
                    name = place.get("name")
                    city = place.get("city")
                    country = place.get("country")
                    if eid and iata:
                        entity_map[eid] = {
                            "iata": iata, "name": name, 
                            "city": city, "country": country,
                            "type": "airport"
                        }
                
                # Segment-level data (more airports)
                for seg in leg.get("segments", []):
                    for place_key in ["origin", "destination"]:
                        place = seg.get(place_key, {})
                        eid = place.get("entityId")
                        iata = place.get("flightPlaceId") or place.get("displayCode")
                        name = place.get("name")
                        ptype = place.get("type")
                        country = place.get("country")
                        country_id = place.get("countryId")
                        
                        if eid and iata:
                            entity_map[eid] = {
                                "iata": iata, "name": name,
                                "type": ptype, "country": country,
                                "countryId": country_id
                            }
                        
                        # Also get parent (city)
                        parent = place.get("parent", {})
                        if parent:
                            p_eid = parent.get("entityId")
                            p_iata = parent.get("flightPlaceId") or parent.get("displayCode")
                            p_name = parent.get("name")
                            p_type = parent.get("type")
                            if p_eid and p_iata:
                                entity_map[p_eid] = {
                                    "iata": p_iata, "name": p_name,
                                    "type": p_type
                                }
    except:
        pass

print(f"\nTotal unique entity IDs found: {len(entity_map)}")
print(f"\n{'entityId':<15} {'IATA':<8} {'Type':<10} {'Name':<25} {'Country':<15}")
print("-" * 75)
for eid in sorted(entity_map.keys(), key=lambda x: int(x)):
    info = entity_map[eid]
    print(f"  {eid:<15} {info.get('iata',''):8} {info.get('type',''):10} "
          f"{info.get('name',''):25} {info.get('country','') or info.get('countryId',''):15}")

# 2. Extract autosuggest URL pattern from JS
print("\n\n" + "="*80)
print("AUTOSUGGEST API PATTERN FROM JS")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if ".js" not in url:
        continue
    resp_text = entry["response"]["content"].get("text", "")
    if not resp_text:
        continue
    
    # Find the autosuggest URL construction
    if "autosuggest-search/api" in resp_text:
        # Find context around the URL
        idx = resp_text.find("autosuggest-search/api")
        if idx > 0:
            context = resp_text[max(0,idx-200):idx+300]
            print(f"\n  From: {url.split('/')[-1][:50]}")
            print(f"  Context: {context}")

# 3. Search for skyId/IATA mappings in price-calendar endpoints
print("\n\n" + "="*80)
print("IATA CODE USAGE IN PRICE CALENDAR")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "pricecalendar" in url:
        pd = entry["request"].get("postData", {})
        if pd and pd.get("text"):
            print(f"\n  Body: {pd['text']}")

# 4. Check if the initial page HTML contains entity ID resolution data
print("\n\n" + "="*80)
print("HTML PAGE - EMBEDDED FLIGHT CONFIG/DATA")
print("="*80)

for entry in entries:
    url = entry["request"]["url"]
    if "transport/flights" in url and "text/html" in entry["response"]["content"].get("mimeType", ""):
        resp_text = entry["response"]["content"].get("text", "")
        
        # Search for a script tag containing search config
        # Try finding JSON-like config with entityId
        patterns = [
            r'"originEntityId":\s*"(\d+)"',
            r'"destinationEntityId":\s*"(\d+)"',
            r'"origin":\s*\{[^}]*"entityId":\s*"(\d+)"',
            r'"skyId":\s*"([A-Z]{3,4})"',
            r'"flightPlaceId":\s*"([A-Z]{3,4})"',
            r'"iata":\s*"([A-Z]{3,4})"',
            r'"origin":\s*\{[^}]*?"id":\s*"(\w+)"',
        ]
        
        for pat in patterns:
            matches = re.findall(pat, resp_text)
            if matches:
                print(f"  Pattern: {pat}")
                for m in matches[:5]:
                    print(f"    Match: {m}")
        
        # Find the data block that contains entity mappings
        # Look for window.__NEXT_DATA__ or similar
        for block_pat in [r'window\.__NEXT_DATA__\s*=\s*(\{.+?\})\s*;\s*</script>',
                          r'window\.__data\s*=\s*(\{.+?\})\s*;\s*</script>',
                          r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.+?)</script>']:
            matches = re.findall(block_pat, resp_text, re.DOTALL)
            if matches:
                print(f"\n  Found data block: {block_pat[:40]}")
                data_str = matches[0]
                print(f"    Size: {len(data_str)} chars")
                try:
                    data = json.loads(data_str)
                    # Recursively find entityId keys
                    def find_keys(obj, path=""):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k in ["entityId", "skyId", "iata", "originEntityId", "destinationEntityId"]:
                                    print(f"    {path}.{k}: {v}")
                                find_keys(v, f"{path}.{k}")
                        elif isinstance(obj, list) and len(obj) < 20:
                            for i, v in enumerate(obj):
                                find_keys(v, f"{path}[{i}]")
                    find_keys(data)
                except:
                    print(f"    (Could not parse as JSON)")
                    # Search within the raw string
                    for s_pat in [r'"entityId":"(\d+)"', r'"skyId":"([A-Z]{3,4})"']:
                        s_matches = re.findall(s_pat, data_str)
                        if s_matches:
                            print(f"    Sub-pattern {s_pat}: {s_matches[:10]}")

# 5. Look at the PricingOption agents detail  
print("\n\n" + "="*80)
print("PRICING OPTION DETAIL (booking links)")
print("="*80)

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
            if "pricingOptions" in r:
                print(f"\n  First itinerary pricingOptions:")
                for po in r["pricingOptions"][:2]:
                    print(f"    {json.dumps(po, indent=2)[:800]}")
            break
    except:
        pass
