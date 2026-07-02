#!/usr/bin/env python3
"""
Scrape Mitre 10 store data.

The store list is loaded via a direct JSON API call to:
  /storelocatorstate/store/offers  ->  {"storeOffers": [...]}

We use Playwright so that any session cookies set during page init are
present when the API request fires, and to handle bot-protection pages.
"""
import json
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.mitre10.com.au/stores"
STORE_OFFERS_PATH = "/storelocatorstate/store/offers"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        captured = []

        def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    print(f"[json-response] {response.status} {response.url}", file=sys.stderr)
            except Exception:
                pass
            if captured:
                return
            try:
                if STORE_OFFERS_PATH not in response.url:
                    return
                if response.status != 200:
                    return
                data = response.json()
                stores = data.get("storeOffers") or data.get("markers") or (
                    data if isinstance(data, list) else None
                )
                if stores and len(stores) > 0:
                    print(f"[captured] {len(stores)} stores from {response.url}", file=sys.stderr)
                    captured.append(stores)
            except Exception as e:
                print(f"[error parsing response] {e}", file=sys.stderr)

        page.on("response", on_response)

        # The page now has persistent background network activity (analytics,
        # chat widget, etc.) that never lets the network go fully idle, so
        # "networkidle" hangs until it times out. Wait only for the DOM instead,
        # then poll for the specific API response we actually need.
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError as e:
            print(f"[warning] goto timed out: {e}", file=sys.stderr)

        for _ in range(60):
            if captured:
                break
            page.wait_for_timeout(1000)

        if not captured:
            with open("debug_page.html", "w") as f:
                f.write(page.content())
            print(f"[debug] page title: {page.title()}", file=sys.stderr)
            print("[debug] saved rendered HTML to debug_page.html", file=sys.stderr)

        browser.close()

        if not captured:
            print(f"Error: {STORE_OFFERS_PATH} was not called or returned no data.", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(captured[0]))


if __name__ == "__main__":
    main()
