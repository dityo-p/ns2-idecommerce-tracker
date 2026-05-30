"""
Price fetcher — uses Playwright (headless Chromium) to load each product
page with full JavaScript execution, then extracts the price from the
rendered DOM.

Why Playwright:
  Tokopedia, Shopee, and BliBli are fully client-side rendered (React/Next.js).
  The price is injected into the DOM by JavaScript after page load — it is
  never present in static HTML. No HTTP-based approach (direct fetch, Serper
  scrape, SerpApi cache, Google Shopping) can see it. A real browser is the
  only solution that works reliably.

How it runs:
  GitHub Actions provides Ubuntu runners with Chromium available via
  `playwright install chromium`. The browser runs headless (no display needed).
  Each product URL is opened, we wait for the price element to appear, then
  extract the text and parse the IDR amount.

Platform-specific CSS selectors (tried in order until one matches):
  Tokopedia  →  data-testid="lblPDPDetailProductPrice"  (stable test ID)
  Shopee     →  data-sqe="price" or class*="price"
  BliBli     →  data-testid="product-detail-price" or class*="product-price"

Fallback:  regex scan of the entire page text for "Rp X.XXX.XXX" pattern.
"""

import re
import time
import logging
from typing import Optional

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS

logger = logging.getLogger(__name__)


# ── CSS selector lists per platform ──────────────────────────────────────────
# Ordered by specificity — most stable / specific first.

PRICE_SELECTORS = {
    "Tokopedia": [
        '[data-testid="lblPDPDetailProductPrice"]',
        '[data-testid="price"]',
        'div[class*="ProductPrice"]',
        'h3[class*="price"]',
        'span[class*="price"]',
    ],
    "Shopee": [
        'div[data-sqe="price"]',
        'div[class*="product-price"]',
        'span[class*="product-price"]',
        '[class*="ProductPrice"]',
        'div[class*="price"] span',
    ],
    "BliBli": [
        '[data-testid="product-detail-price"]',
        'div[class*="product-detail__price"]',
        'div[class*="product-price"]',
        'span[class*="FinalPrice"]',
        'span[class*="price"]',
    ],
}

# How long to wait for the price element before falling back to full-page text
PRICE_WAIT_MS   = 12_000   # 12 seconds
NAV_TIMEOUT_MS  = 30_000   # 30 seconds navigation timeout


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_idr(text: str, min_idr: int = 1_000_000, max_idr: int = 30_000_000) -> Optional[int]:
    """
    Extract the first plausible IDR price from a string.
    Handles: Rp6.299.000 / Rp 6.299.000 / 6,299,000 / 6299000
    """
    # Remove currency symbol
    cleaned = re.sub(r"Rp\.?\s*", "", text, flags=re.I)
    # Find all numeric blobs
    for m in re.finditer(r"[\d]{1,2}(?:[.,]\d{3})+|\d{6,10}", cleaned):
        try:
            value = int(re.sub(r"[.,]", "", m.group()))
            if min_idr <= value <= max_idr:
                return value
        except ValueError:
            continue
    return None


def _classify_variant(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["mario kart", "bundle", "paket", "world", "mk world"]):
        return "With Mario Kart"
    return "Standard"


def _classify_stock(page_text: str) -> str:
    t = page_text.lower()
    if any(w in t for w in ["habis", "out of stock", "sold out", "stok habis", "kosong", "unavailable"]):
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


# ── Playwright page fetcher ───────────────────────────────────────────────────

def _fetch_one_playwright(listing: dict, browser) -> dict:
    """
    Open one product URL in a new browser page, wait for the price to render,
    extract and return the listing dict.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    url      = listing["url"]
    platform = listing["platform"]
    seller   = listing["seller"]
    selectors = PRICE_SELECTORS.get(platform, [])

    logger.info(f"   opening: {url}")

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="id-ID",
        extra_http_headers={
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        },
    )
    page = context.new_page()

    try:
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        # Try each CSS selector for the price element
        price = None
        for selector in selectors:
            try:
                element = page.wait_for_selector(
                    selector,
                    timeout=PRICE_WAIT_MS // len(selectors),
                    state="visible",
                )
                if element:
                    raw_text = element.inner_text()
                    price = _parse_idr(raw_text)
                    if price:
                        logger.info(f"   price via selector '{selector}': Rp{price:,}")
                        break
            except PWTimeout:
                continue
            except Exception:
                continue

        # Fallback: scan the full page text
        if not price:
            logger.info("   selectors failed — scanning full page text")
            # Wait a bit more for JS to finish rendering
            page.wait_for_timeout(3000)
            full_text = page.inner_text("body")
            price = _parse_idr(full_text)
            if price:
                logger.info(f"   price via full text scan: Rp{price:,}")

        if not price:
            raise ValueError("No price found after full DOM scan")

        # Get title and stock from page
        full_text = page.inner_text("body") if "full_text" not in dir() else full_text
        try:
            title_el  = page.query_selector("h1, h2")
            title_raw = title_el.inner_text() if title_el else f"Nintendo Switch 2 — {seller}"
        except Exception:
            title_raw = f"Nintendo Switch 2 — {seller}"

        title   = _clean_title(title_raw)
        variant = _classify_variant(title + " " + url)
        stock   = _classify_stock(page.inner_text("body"))

        logger.info(f"   ✓ Rp{price:,}  {stock}  {variant}")

        return {
            "seller":   seller,
            "platform": platform,
            "product":  title or "Nintendo Switch 2",
            "variant":  variant,
            "price":    price,
            "stock":    stock,
            "url":      url,
        }

    finally:
        context.close()


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
    """
    Launch a single headless Chromium instance, fetch all tracked listings
    sequentially (one browser context per listing), then close the browser.
    """
    from playwright.sync_api import sync_playwright

    result     = FetchResult()
    listings   = []
    fail_count = 0

    logger.info("Launching headless Chromium via Playwright…")

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

                for attempt in range(cfg.retry_attempts):
                    try:
                        listing = _fetch_one_playwright(entry, browser)
                        listings.append(listing)
                        break
                    except Exception as exc:
                        wait = cfg.retry_backoff * (2 ** attempt)
                        logger.warning(
                            f"   attempt {attempt+1}/{cfg.retry_attempts} failed: {exc}"
                            + (f" — retry in {wait:.0f}s" if attempt < cfg.retry_attempts - 1 else "")
                        )
                        if attempt < cfg.retry_attempts - 1:
                            time.sleep(wait)
                        else:
                            fail_count += 1
                            logger.error(f"   ✗ {label}: all retries failed")
        finally:
            browser.close()

    total = len(TRACKED_LISTINGS)
    logger.info(
        f"\nFetch complete — {len(listings)}/{total} OK, {fail_count} failed"
    )

    if listings:
        result.listings = listings
        result.success  = True
    else:
        logger.error("All fetches failed — falling back to demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = False
        result.is_demo  = True
        result.error    = "All Playwright fetches failed"

    return result
