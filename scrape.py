#!/usr/bin/env python3
"""
Scrape Mitre 10 store data.

The site loads store markers dynamically via AJAX (no longer embedded in
page HTML), so we use Playwright to:
  1. Intercept the JSON response that contains the markers array.
  2. Fall back to reading Magento's customer-data localStorage cache.
  3. Fall back to reading window/require variables after networkidle.
"""
import json
import sys
from playwright.sync_api import sync_playwright

URL = "https://www.mitre10.com.au/stores"

def looks_like_stores(obj):
    """Return True if obj appears to be a list of store marker records."""
    if not isinstance(obj, list) or len(obj) < 10:
        return False
    sample = obj[0]
    return isinstance(sample, dict) and any(
        k in sample for k in ("latitude", "longitude", "name", "id")
    )

def extract_from_cache(cache):
    """Walk a Magento customer-data cache dict looking for a markers array."""
    if not isinstance(cache, dict):
        return None
    for section in cache.values():
        if isinstance(section, dict):
            markers = section.get("markers")
            if looks_like_stores(markers):
                return markers
    return None

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        all_json = []  # (url, data) for every JSON response
        captured = []

        def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct or response.status != 200:
                    return
                data = response.json()
                all_json.append((response.url, data))
                print(f"[json] {response.status} {response.url} -> {str(data)[:120]}", file=sys.stderr)
                if captured:
                    return
                # Direct list of stores
                if looks_like_stores(data):
                    print(f"[intercepted] {response.url}", file=sys.stderr)
                    captured.append(data)
                    return
                # Wrapped: {"markers": [...], "stores": [...], etc.}
                if isinstance(data, dict):
                    for key in ("markers", "stores", "retailers", "items", "data"):
                        val = data.get(key)
                        if looks_like_stores(val):
                            print(f"[intercepted key={key}] {response.url}", file=sys.stderr)
                            captured.append(val)
                            return
            except Exception as e:
                pass

        page.on("response", on_response)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        # Extra settle time for lazy-loaded JS modules
        page.wait_for_timeout(3000)

        if captured:
            stores = captured[0]
        else:
            # Try localStorage (Magento customer-data sections)
            stores = page.evaluate("""() => {
                try {
                    var raw = localStorage.getItem('mage-cache-storage');
                    if (!raw) return null;
                    var cache = JSON.parse(raw);
                    for (var key in cache) {
                        var s = cache[key];
                        if (s && Array.isArray(s.markers) && s.markers.length > 10)
                            return s.markers;
                    }
                } catch(e) {}
                return null;
            }""")

        if not stores:
            # Try requirejs context: walk defined modules for a markers/stores array
            stores = page.evaluate("""() => {
                try {
                    var ctx = require && require.s && require.s.contexts
                              && require.s.contexts._ && require.s.contexts._.defined;
                    if (ctx) {
                        for (var k in ctx) {
                            var mod = ctx[k];
                            if (!mod) continue;
                            if (Array.isArray(mod) && mod.length > 10
                                && mod[0] && (mod[0].latitude || mod[0].longitude))
                                return mod;
                            if (mod && typeof mod.getList === 'function') {
                                var list = mod.getList()();
                                if (Array.isArray(list) && list.length > 10) return list;
                            }
                            if (mod && Array.isArray(mod.markers) && mod.markers.length > 10)
                                return mod.markers;
                        }
                    }
                } catch(e) {}
                return null;
            }""")

        if not stores:
            # Last resort: text/x-magento-init script tags (old approach, kept as fallback)
            stores = page.evaluate("""() => {
                var scripts = document.querySelectorAll('script');
                for (var i = 0; i < scripts.length; i++) {
                    var s = scripts[i];
                    if (s.getAttribute('type') !== 'text/x-magento-init') continue;
                    try {
                        var d = JSON.parse(s.textContent);
                        var c = d && d['*'] && d['*']['Magento_Ui/js/core/app']
                                && d['*']['Magento_Ui/js/core/app'].components;
                        if (c && c['store-locator-search'] && c['store-locator-search'].markers)
                            return c['store-locator-search'].markers;
                    } catch(e) {}
                }
                return null;
            }""")

        browser.close()

        if not stores:
            print(f"Error: could not find store markers via any method. "
                  f"Saw {len(all_json)} JSON responses total.", file=sys.stderr)
            # Dump localStorage keys for further diagnosis
            ls_keys = page.evaluate("() => Object.keys(localStorage)")
            print(f"localStorage keys: {ls_keys}", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(stores))


if __name__ == "__main__":
    main()
