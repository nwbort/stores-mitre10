#!/usr/bin/env python3
"""
Scrape Mitre 10 store data.

The store list is embedded in the /stores page as a Magento init blob:
  <script type="text/x-magento-init">{"*": {"Magento_Ui/js/core/app":
    {"components": {"store-locator-search": {"markers": [...]}}}}}</script>

The site sits behind a Cloudflare managed challenge, so headless Playwright
only ever sees the "Just a moment..." interstitial. As in tribunal-tracker,
we drive a real Chrome (headful, under xvfb-run) via nodriver, load the
store locator page until the challenge clears, and read the markers from it.
(The /storelocatorstate/store/offers API has its own challenge and returns
403 even from the cleared page, so it isn't used.)

Usage: scrape.py OUTPUT.json   (writes the JSON array of store objects)
"""
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from shutil import which

import nodriver as uc

BASE_URL = "https://www.mitre10.com.au"
PAGE_URL = f"{BASE_URL}/stores"

# Markers of the interstitial itself. ("challenge-platform" alone isn't one:
# Cloudflare injects a /cdn-cgi/challenge-platform/ script into real pages too.)
CHALLENGE_TITLES = ("Just a moment", "Attention Required")
CHALLENGE_MARKERS = (
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "Verifying you are human",
)

MAX_WAIT_SECONDS = 90
POLL_SECONDS = 0.5
TURNSTILE_CLICK_INTERVAL = 3
CDP_CALL_TIMEOUT_SECONDS = 30
TURNSTILE_FIND_TIMEOUT_SECONDS = 5
PAGE_ATTEMPTS = 2

MAGENTO_INIT_RE = re.compile(
    r'<script[^>]*type="text/x-magento-init"[^>]*>(.*?)</script>', re.S
)

# nodriver's default args, which help the browser look like a normal user
# session rather than an automated one.
CHROME_ARGS = [
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--no-sandbox",  # CI runs as root
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def looks_like_challenge(html: str, title: str = "") -> bool:
    if any(t in title for t in CHALLENGE_TITLES):
        return True
    return any(marker in html for marker in CHALLENGE_MARKERS)


async def with_timeout(coro, what: str, seconds: float = CDP_CALL_TIMEOUT_SECONDS):
    """Await coro, turning a hang into a TimeoutError after seconds."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"{what} did not respond within {seconds}s")


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = which(candidate)
        if path:
            return path
    raise FileNotFoundError("Could not find a Chrome/Chromium binary")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_chrome(chrome_path: str, port: int, user_data_dir: str):
    args = [
        chrome_path,
        *CHROME_ARGS,
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    # Chrome ignores the proxy environment variables, so pass any HTTPS proxy
    # on explicitly (not needed on GitHub's runners).
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")
    log(f"Launching Chrome: {chrome_path} (port {port})")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_devtools(port: int, timeout: float = 30.0) -> bool:
    # nodriver's own launcher only waits ~2.5s for the port, which can lose a
    # race against a slow Chrome start on the runner, so we wait ourselves.
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                log(f"DevTools ready: {json.load(r).get('Browser')}")
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def try_click_turnstile(tab) -> bool:
    """Best-effort click of the Turnstile 'Verify you are human' checkbox."""
    for text in ("Verify you are human", "Verify you are a human", "challenges.cloudflare.com"):
        try:
            el = await with_timeout(
                tab.find(text, best_match=True, timeout=3),
                "tab.find",
                TURNSTILE_FIND_TIMEOUT_SECONDS,
            )
            if el:
                await with_timeout(el.mouse_click(), "mouse_click")
                log(f"  clicked element matching {text!r}")
                return True
        except Exception:
            pass
    return False


async def load_page(browser, url: str):
    """Load url and wait for the Cloudflare challenge to clear.
    Returns the page HTML, or None if the challenge never cleared."""
    log(f"Navigating to {url}")
    try:
        tab = await with_timeout(browser.get(url), "browser.get")
    except Exception as e:
        log(f"  navigation failed: {e}")
        return None

    deadline = time.time() + MAX_WAIT_SECONDS
    next_click = time.time() + TURNSTILE_CLICK_INTERVAL
    last_title = None
    while time.time() < deadline:
        try:
            ready = await with_timeout(tab.evaluate("document.readyState"), "tab.evaluate")
            html = await with_timeout(tab.get_content(), "tab.get_content") if ready == "complete" else None
        except Exception as e:
            log(f"  page check failed: {e}")
            await tab.sleep(POLL_SECONDS)
            continue

        if html is not None:
            title = await with_timeout(tab.evaluate("document.title"), "tab.evaluate")
            if title != last_title:
                log(f"  {len(html)} bytes, title={title!r}")
                last_title = title
            if not looks_like_challenge(html, title):
                return html
            if time.time() >= next_click:
                await try_click_turnstile(tab)
                next_click = time.time() + TURNSTILE_CLICK_INTERVAL

        await tab.sleep(POLL_SECONDS)

    log(f"ERROR: Cloudflare challenge did not clear within {MAX_WAIT_SECONDS}s (last title {last_title!r})")
    return None


def extract_markers(html: str):
    """Return the store-locator-search markers list from the page, or None."""
    for blob in MAGENTO_INIT_RE.findall(html):
        if '"store-locator-search"' not in blob:
            continue
        try:
            data = json.loads(blob)
            return data["*"]["Magento_Ui/js/core/app"]["components"]["store-locator-search"]["markers"]
        except (ValueError, KeyError, TypeError) as e:
            log(f"  store-locator-search blob found but not parsed: {e}")
    return None


async def scrape(output_path: str) -> int:
    chrome_path = find_chrome()
    port = free_port()
    user_data_dir = tempfile.mkdtemp(prefix="mitre10-chrome-")
    proc = launch_chrome(chrome_path, port, user_data_dir)
    if not wait_for_devtools(port):
        log("ERROR: Chrome DevTools endpoint never became ready")
        proc.terminate()
        return 3

    browser = await uc.start(host="127.0.0.1", port=port, browser_executable_path=chrome_path)
    try:
        for attempt in range(1, PAGE_ATTEMPTS + 1):
            html = await load_page(browser, PAGE_URL)
            stores = extract_markers(html) if html else None
            if stores:
                log(f"Extracted {len(stores)} stores from {PAGE_URL}")
                with open(output_path, "w") as f:
                    json.dump(stores, f)
                return 0
            if html:
                log(f"  attempt {attempt}/{PAGE_ATTEMPTS}: no store-locator-search markers in page")
        return 1
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def main() -> int:
    if len(sys.argv) != 2:
        log("Usage: scrape.py OUTPUT.json")
        return 2
    return uc.loop().run_until_complete(scrape(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
