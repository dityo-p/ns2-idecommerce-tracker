"""
Price fetcher — Playwright headless Chromium, one page per tracked listing.

Key design decisions
────────────────────
1. Seller name ALWAYS comes from config (listing["seller"]) — never from the page.
   This fixes the "wrong seller name" problem: the h1/h2 on a product page may
   show a different product title, but the seller is who we configured, full stop.

2. Price selector waits for the element to contain text matching "Rp [digits]",
   not just for the element to exist. This ensures React has fully hydrated
   before we read the price, fixing stale/cached prices.

3. Product name comes from the page (for display), but the seller association
   is locked to config. If name extraction fails we fall back to
   "Nintendo Switch 2" — never to a wrong seller name.

4. networkidle wait is avoided (too slow / unreliable on SPAs). Instead we
   wait for the specific price selector text to be non-empty with a 15s timeout.

Confirmed stable data-testid selectors (2025):
  Tokopedia  data-testid="lblPDPDetailProductPrice"  (price)
             data-testid="lblPDPDetailProductName"   (name)
  Shopee     data-sqe="price"  OR  class*="product-price"
  BliBli     data-testid="product-detail-price"  OR  class*="product-price"
"""

import re
import time
import logging
from typing import Optional

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS

logger = logging.getLogger(__name__)


# ── Per-platform selectors ────────────────────────────────────────────────────
# Each platform has a prioritised list of CSS selectors for the price element.
# The first one that contains "Rp" text wins.

PRICE_SELECTORS = {
    "Tokopedia": [
        '[data-testid="lblPDPDetailProductPrice"]',
        '[data-testid="price"]',
        'div[class*="ProductPrice"]',
        'h3[class*="price"]',
    ],
    "Shopee": [
        'div[data-sqe="price"]',
        'section[data-sqe="price"]',
        'div[class*="product-price"]',
        'span[class*="product-price"]',
        'div[class*="Price"] span',
    ],
    "BliBli": [
        '[data-testid="product-detail-price"]',
        'div[class*="product-detail__price"]',
        'div[class*="product-price"]',
        'span[class*="FinalPrice"]',
        'div[class*="Price"]',
    ],
}

NAME_SELECTORS = {
    "Tokopedia": ['[data-testid="lblPDPDetailProductName"]', 'h1'],
    "Shopee":    ['[data-sqe="name"] span', 'h1'],
    "BliBli":    ['[data-testid="product-detail-name"]', 'h1'],
}

NAV_TIMEOUT_MS   = 45_000   # 45 s navigation timeout
PRICE_TIMEOUT_MS = 15_000   # 15 s to wait for price element to appear


# ── Price parsing ─────────────────────────────────────────────────────────────

def _parse_idr(text: str, min_idr: int = 1_000_000, max_idr: int = 30_000_000) -> Optional[int]:
    """Extract the first plausible IDR value from a string."""
    cleaned = re.sub(r"Rp\.?\s*", "", text, flags=re.I)
    for m in re.finditer(r"\d{1,2}(?:[.,]\d{3})+|\d{6,10}", cleaned):
        try:
            value = int(re.sub(r"[.,]", "", m.group()))
            if min_idr <= value <= max_idr:
                return value
        except ValueError:
            continue
    return None


def _classify_variant(title: str, url: str) -> str:
    text = (title + " " + url).lower()
    if any(w in text for w in ["mario kart", "bundle", "paket", "world"]):
        return "With Mario Kart"
    return "Standard"


def _classify_stock(page_text: str) -> str:
    t = page_text.lower()
    if any(w in t for w in ["habis", "out of stock", "sold out", "stok habis", "kosong"]):
        return "out-stock"
    if any(w in t for w in ["sisa", "limited", "hampir habis", "tersisa"]):
        return "low-stock"
    return "in-stock"


def _clean_title(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip()
    for sep in [" - ", " | ", " / ", ","]:
        if len(name) > 70 and sep in name:
            name = name.split(sep)[0].strip()
    return name[:80]


# ── Playwright page loader ────────────────────────────────────────────────────

def _get_price_from_page(page, platform: str, cfg: Config) -> Optional[int]:
    """
    Try each CSS selector for the platform. For each candidate element,
    wait until it contains "Rp" text (confirming React has hydrated the price),
    then parse and return the IDR value.
    """  # noqa
    from playwright.sync_api import TimeoutError as PWTimeout

    selectors = PRICE_SELECTORS.get(platform, [])
    timeout_each = PRICE_TIMEOUT_MS // max(len(selectors), 1)

    for selector in selectors:
        try:
            # Wait for element to exist
            element = page.wait_for_selector(
                selector,
                timeout=timeout_each,
                state="visible",
            )
            if not element:
                continue

            # Poll until the text contains 'Rp' (React hasn't hydrated yet if empty)
            for _ in range(20):
                text = element.inner_text()
                if "Rp" in text or re.search(r"\d{6}", text):
                    price = _parse_idr(text, cfg.min_price_idr, cfg.max_price_idr)
                    if price:
                        logger.info(f"   ✓ price via '{selector}': {text.strip()!r} → Rp{price:,}")
                        return price
                page.wait_for_timeout(300)

        except PWTimeout:
            continue
        except Exception as e:
            logger.debug(f"   selector '{selector}' error: {e}")
            continue

    return None


def _get_name_from_page(page, platform: str) -> Optional[str]:
    """Extract product name from the page using stable selectors."""
    from playwright.sync_api import TimeoutError as PWTimeout

    selectors = NAME_SELECTORS.get(platform, ["h1"])
    for selector in selectors:
        try:
            element = page.wait_for_selector(selector, timeout=5_000, state="visible")
            if element:
                text = element.inner_text().strip()
                if len(text) > 5:
                    return _clean_title(text)
        except (PWTimeout, Exception):
            continue
    return None


def _fetch_one_playwright(listing: dict, browser) -> dict:
    """
    Open one product page, extract price + name. Seller comes from config.
    """
    url      = listing["url"]
    platform = listing["platform"]
    seller   = listing["seller"]   # ALWAYS from config — never from page

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="id-ID",
        extra_http_headers={"Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8"},
    )
    page = context.new_page()

    try:
        logger.info(f"   → {url}")
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        # Give the JS framework a moment to start hydrating
        page.wait_for_timeout(2000)

        price = _get_price_from_page(page, platform, Config())

        # Fallback: scan full body text for Rp pattern
        if not price:
            logger.info("   selectors failed — scanning full body text")
            page.wait_for_timeout(3000)
            body_text = page.inner_text("body")
            price = _parse_idr(body_text)
            if price:
                logger.info(f"   ✓ price via body scan: Rp{price:,}")

        if not price:
            raise ValueError(f"Price not found on page ({len(page.content()):,} bytes HTML)")

        # Product name from page (display only — seller identity is locked to config)
        name = _get_name_from_page(page, platform) or f"Nintendo Switch 2"

        # Stock and variant
        body_text = page.inner_text("body")
        stock   = _classify_stock(body_text)
        variant = _classify_variant(name, url)

        logger.info(
            f"   seller={seller!r}  product={name[:50]!r}  "
            f"price=Rp{price:,}  stock={stock}  variant={variant}"
        )

        return {
            "seller":   seller,    # from config
            "platform": platform,  # from config
            "product":  name,
            "variant":  variant,
            "price":    price,
            "stock":    stock,
            "url":      url,
        }

    finally:
        context.close()


# ── Retry ─────────────────────────────────────────────────────────────────────

def _fetch_with_retry(listing: dict, browser, cfg: Config) -> dict:
    last_exc = None
    for attempt in range(cfg.retry_attempts):
        try:
            return _fetch_one_playwright(listing, browser)
        except Exception as exc:
            last_exc = exc
            wait = cfg.retry_backoff * (2 ** attempt)
            logger.warning(
                f"   attempt {attempt+1}/{cfg.retry_attempts} failed: {exc}"
                + (f" — retry in {wait:.0f}s" if attempt < cfg.retry_attempts - 1 else "")
            )
            if attempt < cfg.retry_attempts - 1:
                time.sleep(wait)
    raise last_exc


# ── Public API ────────────────────────────────────────────────────────────────

class FetchResult:
    def __init__(self):
        self.listings:      list[dict]    = []
        self.source:        str           = "playwright"
        self.queries_fired: int           = 0
        self.success:       bool          = False
        self.error:         Optional[str] = None
        self.is_demo:       bool          = False


def fetch_all(cfg: Config) -> FetchResult:
    from playwright.sync_api import sync_playwright

    result     = FetchResult()
    listings   = []
    fail_count = 0

    logger.info("Launching headless Chromium…")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        try:
            for entry in TRACKED_LISTINGS:
                result.queries_fired += 1
                label = f"{entry['seller']} @ {entry['platform']}"
                logger.info(f"── {label}")
                try:
                    listing = _fetch_with_retry(entry, browser, cfg)
                    listings.append(listing)
                except Exception as exc:
                    fail_count += 1
                    logger.error(f"   ✗ {label}: {exc}")
        finally:
            browser.close()

    total = len(TRACKED_LISTINGS)
    logger.info(f"\nFetch complete — {len(listings)}/{total} OK, {fail_count} failed")

    if listings:
        result.listings = listings
        result.success  = True
    else:
        logger.error("All fetches failed — using demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = False
        result.is_demo  = True
        result.error    = "All Playwright fetches failed"

    return result
