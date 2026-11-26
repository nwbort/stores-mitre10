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
After a challenge, Magento's scripts have removed the x-magento-init blobs
from the live DOM by the time we can read it, so a script injected at
document start copies the blob somewhere safe first (see STASH_JS).
(The /storelocatorstate/store/offers API has its own challenge and returns
403 even from the cleared page, so it isn't used.)

Usage: scrape.py OUTPUT.json   (writes the JSON array of store objects)
"""
import asyncio
import html as htmllib
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
# Once the challenge clears, keep re-reading the page this long for the
# markers to show up before giving up on the attempt.
MARKERS_WAIT_SECONDS = 20
DEBUG_DIR = "debug"

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


# Runs in every new document before the page's own scripts. Magento's scripts
# remove the x-magento-init blobs from the DOM once they've processed them,
# which after a Cloudflare challenge happens before we can read the page. So
# copy the store locator blob, as the parser inserts it, into an inert
# <script type="application/json"> that nothing else touches. It runs in an
# isolated world, so the page's own JavaScript can't see it.
STASH_ID = "m10-scraper-markers"
STASH_JS = """
(() => {
  const ID = "%s";
  const stash = (el) => {
    if (document.getElementById(ID)) return;
    const text = el.textContent || "";
    if (text.indexOf("store-locator-search") < 0) return;
    try { JSON.parse(text); } catch (e) { return; }  // not fully parsed yet
    const copy = document.createElement("script");
    copy.type = "application/json";
    copy.id = ID;
    copy.textContent = text;
    document.documentElement.appendChild(copy);
  };
  const scan = () => document
    .querySelectorAll('script[type="text/x-magento-init"]').forEach(stash);
  const observer = new MutationObserver(() => {
    scan();
    if (document.getElementById(ID)) observer.disconnect();
  });
  observer.observe(document, { childList: true, subtree: true, characterData: true });
  document.addEventListener("DOMContentLoaded", scan, true);
})();
""" % STASH_ID


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
    Returns (tab, page HTML), with HTML None if the challenge never cleared."""
    log(f"Navigating to {url}")
    try:
        # Navigate the existing tab directly. browser.get() re-attaches to the
        # tab after navigating, which opens a new DevTools session and drops
        # the STASH_JS registration made on the old one.
        tab = browser.main_tab
        await with_timeout(tab.send(uc.cdp.page.navigate(url)), "page.navigate")
    except Exception as e:
        log(f"  navigation failed: {e}")
        return None, None

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
                return tab, html
            if time.time() >= next_click:
                await try_click_turnstile(tab)
                next_click = time.time() + TURNSTILE_CLICK_INTERVAL

        await tab.sleep(POLL_SECONDS)

    log(f"ERROR: Cloudflare challenge did not clear within {MAX_WAIT_SECONDS}s (last title {last_title!r})")
    return tab, None


def _is_store_list(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(v, dict) for v in value[:5])
        and all("latitude" in v and "name" in v for v in value[:5])
    )


def _find_markers_anywhere(text: str):
    """Find a JSON '"markers": [...]' array anywhere in text."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r'"markers"\s*:\s*\[', text):
        try:
            value, _ = decoder.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        if _is_store_list(value):
            return value
    return None


STASH_RE = re.compile(r'<script[^>]*id="%s"[^>]*>(.*?)</script>' % STASH_ID, re.S)


def extract_markers(html: str):
    """Return the store-locator-search markers list from the page, or None."""
    for blob in MAGENTO_INIT_RE.findall(html) + STASH_RE.findall(html):
        if '"store-locator-search"' not in blob:
            continue
        try:
            data = json.loads(blob)
            return data["*"]["Magento_Ui/js/core/app"]["components"]["store-locator-search"]["markers"]
        except (ValueError, KeyError, TypeError) as e:
            log(f"  store-locator-search blob found but not parsed: {e}")
    # Fall back to any markers array in the page: the blob may have moved to
    # another script, or to an HTML-escaped data-mage-init attribute.
    markers = _find_markers_anywhere(html)
    if markers is None and "&quot;markers&quot;" in html:
        markers = _find_markers_anywhere(htmllib.unescape(html))
    if markers is not None:
        log("  markers found outside the store-locator-search init blob")
    return markers


def describe_page(html: str) -> str:
    """One-line summary of a page, to help debug a missing markers list."""
    return (
        f"{len(html)} bytes, {html.count('x-magento-init')} x-magento-init scripts, "
        f"store-locator-search x{html.count('store-locator-search')}, "
        f"'markers' x{html.count('markers')}"
    )


def save_debug(html: str, name: str) -> None:
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, name)
        with open(path, "w") as f:
            f.write(html)
        log(f"  saved page to {path}")
    except OSError as e:
        log(f"  could not save debug page: {e}")


async def wait_for_markers(tab, html: str):
    """Re-read the page until the markers appear or MARKERS_WAIT_SECONDS pass.
    Returns (markers or None, the last HTML read)."""
    deadline = time.time() + MARKERS_WAIT_SECONDS
    while True:
        stores = extract_markers(html)
        if stores or time.time() >= deadline:
            return stores, html
        await tab.sleep(2)
        try:
            html = await with_timeout(tab.get_content(), "tab.get_content")
        except Exception as e:
            log(f"  page re-read failed: {e}")


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
        # Scripts added with addScriptToEvaluateOnNewDocument only run while
        # the Page domain is enabled.
        await with_timeout(browser.main_tab.send(uc.cdp.page.enable()), "page.enable")
        await with_timeout(
            browser.main_tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(
                STASH_JS, world_name="m10-scraper")),
            "addScriptToEvaluateOnNewDocument",
        )
        for attempt in range(1, PAGE_ATTEMPTS + 1):
            tab, html = await load_page(browser, PAGE_URL)
            stores = None
            if html:
                stores, html = await wait_for_markers(tab, html)
            if stores:
                log(f"Extracted {len(stores)} stores from {PAGE_URL}")
                with open(output_path, "w") as f:
                    json.dump(stores, f)
                return 0
            if html:
                log(f"  attempt {attempt}/{PAGE_ATTEMPTS}: no store markers in page ({describe_page(html)})")
                save_debug(html, f"stores-attempt{attempt}.html")
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
