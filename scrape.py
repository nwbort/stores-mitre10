#!/usr/bin/env python3
"""
Scrape Mitre 10 store data.

The store list is loaded via a direct JSON API call to:
  /storelocatorstate/store/offers  ->  {"storeOffers": [...]}

The site fronts this page with a Cloudflare Turnstile bot challenge that
flags plain Playwright/Chromium (stuck on a "Just a moment..." page,
challenge never clears). We use patchright, a drop-in Playwright
replacement that patches the CDP-level signals Cloudflare detects, and
retry a few times since even a good bypass can be probabilistic.
"""
import json
import sys
import tempfile
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.mitre10.com.au/stores"
STORE_OFFERS_PATH = "/storelocatorstate/store/offers"
MAX_ATTEMPTS = 3


def try_scrape(p):
    with tempfile.TemporaryDirectory() as user_data_dir:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            no_viewport=True,
        )
        try:
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
                    if stores:
                        print(f"[captured] {len(stores)} stores from {response.url}", file=sys.stderr)
                        captured.append(stores)
                except Exception as e:
                    print(f"[error parsing response] {e}", file=sys.stderr)

            page.on("response", on_response)

            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError as e:
                print(f"[warning] goto timed out: {e}", file=sys.stderr)

            try:
                page.wait_for_function(
                    "document.title !== 'Just a moment...'", timeout=30000
                )
            except PlaywrightTimeoutError:
                print(f"[warning] still on challenge page: {page.title()}", file=sys.stderr)

            for _ in range(45):
                if captured:
                    break
                page.wait_for_timeout(1000)

            return captured[0] if captured else None
        finally:
            context.close()


def main():
    with sync_playwright() as p:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[attempt {attempt}/{MAX_ATTEMPTS}]", file=sys.stderr)
            stores = try_scrape(p)
            if stores:
                print(json.dumps(stores))
                return

        print(
            f"Error: {STORE_OFFERS_PATH} was not called or returned no data "
            f"after {MAX_ATTEMPTS} attempts.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
