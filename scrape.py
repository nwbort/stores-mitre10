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
from playwright.sync_api import sync_playwright

URL = "https://www.mitre10.com.au/stores"
STORE_OFFERS_PATH = "/storelocatorstate/store/offers"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        captured = []

        def on_response(response):
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
        # Don't wait for "networkidle": analytics/challenge pages poll forever,
        # which caused Page.goto to time out. Wait for the API response instead.
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        deadline_ms = 60000
        waited = 0
        while not captured and waited < deadline_ms:
            page.wait_for_timeout(1000)
            waited += 1000

        title = page.title()
        browser.close()

        if not captured:
            if "just a moment" in title.lower():
                print("Error: blocked by a Cloudflare challenge page "
                      f"(title: {title!r}); store data was not loaded.", file=sys.stderr)
            else:
                print(f"Error: {STORE_OFFERS_PATH} was not called or returned no data "
                      f"(page title: {title!r}).", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(captured[0]))


if __name__ == "__main__":
    main()
