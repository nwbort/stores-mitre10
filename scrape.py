#!/usr/bin/env python3
"""
Scrape Mitre 10 store data.

The store list is loaded via a direct JSON API call to:
  /storelocatorstate/store/offers  ->  {"storeOffers": [...]}

We use Playwright so that any session cookies set during page init are
present when the API request fires. The site fronts this page with a
Cloudflare bot challenge ("Just a moment...") that flags plain headless
Chromium, so we apply some standard headless-detection countermeasures
(realistic UA/viewport, navigator.webdriver patch) and wait for the
challenge to clear before looking for the API response.
"""
import json
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.mitre10.com.au/stores"
STORE_OFFERS_PATH = "/storelocatorstate/store/offers"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-AU', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-AU",
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

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

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError as e:
            print(f"[warning] goto timed out: {e}", file=sys.stderr)

        # Wait out the Cloudflare "Just a moment..." challenge if present.
        try:
            page.wait_for_function(
                "document.title !== 'Just a moment...'", timeout=30000
            )
        except PlaywrightTimeoutError:
            print(f"[warning] still on challenge page: {page.title()}", file=sys.stderr)

        for _ in range(60):
            if captured:
                break
            page.wait_for_timeout(1000)

        if not captured:
            print(f"[debug] final page title: {page.title()}", file=sys.stderr)

        browser.close()

        if not captured:
            print(f"Error: {STORE_OFFERS_PATH} was not called or returned no data.", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(captured[0]))


if __name__ == "__main__":
    main()
