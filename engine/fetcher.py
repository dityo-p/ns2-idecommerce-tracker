"""
Price fetcher — Playwright headless Chromium.

Architecture
────────────
One persistent browser instance, one fresh context per listing (isolated
cookies/cache), sequential fetching.

What changed from previous versions
────────────────────────────────────
- Seller name is ALWAYS from config — page content never overrides it.
- Wait strategy: domcontentloaded → then explicit wait for each price
  selector with a generous timeout, then a full-page text fallback.
- No polling loop (that broke Shopee). We let Playwright's built-in
  wait_for_selector handle the wait — it already polls internally.
- Per-selector timeout is generous (8 s each) rather than split across all.
- BliBli: added :has-text("Rp") pseudo-filter so we skip empty placeholders.
- Tokopedia: wait for load state "load" (not just domcontentloaded) before
  looking for price — gives the initial XHR time to complete.
"""

import re
import time
import logging
from typing import Optional

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS

logger = logging.getLogger(__name__)

# ── Timeouts ──────────────────────────────────────────────────────────────────
NAV_TIMEOUT_MS      = 45_000   # page.goto timeout
SELECTOR_TIMEOUT_MS = 8_000    # per selector attempt
POST_NAV_WAIT_MS    = 2_500    # settle time after navigation before querying
FALLBACK_WAIT_MS    = 4_000    # extra wait before full-body text fallback

# ── Confirmed selectors (2025) ────────────────────────────────────────────────
# Ordered: most specific / stable first.
# :has-text("Rp") ensures element is hydrated (non-empty placeholder).
PRICE_SELECTORS = {
    "Tokopedia": [
        '[data-testid="lblPDPDetailProductPrice"]:has-text("Rp")',
        '[data-testid="lblPDPDetailProductPrice"]',
        '[data-testid="price"]:has-text("Rp")',
        'h3[class*="price"]:has-text("Rp")',
        'div[class*="ProductPrice"]:has-text("Rp")',
    ],
    "Shopee": [
        'div[data-sqe="price"]:has-text("Rp")',
        'section[data-sqe="price"]:has-text("Rp")',
        'div[class*="product-price"]:has-text("Rp")',
        'span[class*="product-price"]:has-text("Rp")',
        'div[class*="Price"]:has-text("Rp")',
    ],
    "BliBli": [
        '[data-testid="product-detail-price"]:has-text("Rp")',
        'div[class*="product-detail__price"]:has-text("Rp")',
        'div[class*="product-price"]:has-text("Rp")',
        'span[class*="FinalPrice"]:has-text("Rp")',
        'div[class*="Price"]:has-text("Rp")',
        '[class*="price"]:has-text("Rp")',
    ],
}

NAME_SELECTORS = {
    "Tokopedia": ['[data-testid="lblPDPDetailProductName"]', 'h1'],
    "Shopee":    ['[data-sqe="name"] span', '[data-testid="item-title"]', 'h1'],
    "BliBli":    ['[data-testid="product-detail-name"]', 'h1'],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_idr(text: str, min_idr: int = 1_000_000, max_idr: int = 30_000_000) -> Optional[int]:
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


def _classify_stock(body_text: str) -> str:
    t = body_text.lower()
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


# ── Core page fetch ───────────────────────────────────────────────────────────

def _fetch_one_playwright(listing: dict, browser) -> dict:
    from playwright.sync_api import TimeoutError as PWTimeout

    url      = listing["url"]
    platform = listing["platform"]
    seller   = listing["seller"]   # always from config — never from page

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
        logger.info(f"   GET {url}")
        page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")

        # Wait for full page load (fires after images/scripts, before networkidle)
        # This gives the JS framework time to start the price XHR
        try:
            page.wait_for_load_state("load", timeout=15_000)
        except PWTimeout:
            pass  # non-fatal — continue anyway

        # Settle time for React hydration
        page.wait_for_timeout(POST_NAV_WAIT_MS)

        # ── Try each price selector ───────────────────────────────────────────
        price = None
        for selector in PRICE_SELECTORS.get(platform, []):
            try:
                el = page.wait_for_selector(
                    selector,
                    timeout=SELECTOR_TIMEOUT_MS,
                    state="visible",
                )
                if el:
                    text = el.inner_text()
                    p = _parse_idr(text)
                    if p:
                        logger.info(f"   price via '{selector}': {text.strip()!r} → Rp{p:,}")
                        price = p
                        break
            except PWTimeout:
                continue
            except Exception as e:
                logger.debug(f"   selector {selector!r}: {e}")
                continue

        # ── Fallback: scan visible page text ──────────────────────────────────
        if not price:
            logger.info("   no selector matched — waiting then scanning body text")
            page.wait_for_timeout(FALLBACK_WAIT_MS)
            body = page.inner_text("body")
            price = _parse_idr(body)
            if price:
                logger.info(f"   price via body scan: Rp{price:,}")

        if not price:
            # Last resort: grab all text nodes that contain "Rp" via JS
            try:
                rp_texts = page.evaluate("""
                    () => {
                        const walk = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT);
                        const results = [];
                        let node;
                        while ((node = walk.nextNode())) {
                            if (node.textContent.includes('Rp')) {
                                results.push(node.textContent.trim());
                            }
                        }
                        return results;
                    }
                """)
                for t in rp_texts:
                    p = _parse_idr(t)
                    if p:
                        price = p
                        logger.info(f"   price via JS tree walk: Rp{p:,}")
                        break
            except Exception:
                pass

        if not price:
            raise ValueError(
                f"No price found — platform={platform}, "
                f"page size={len(page.content()):,} bytes"
            )

        # ── Product name (display only) ───────────────────────────────────────
        name = None
        for sel in NAME_SELECTORS.get(platform, ["h1"]):
            try:
                el = page.wait_for_selector(sel, timeout=3_000, state="visible")
                if el:
                    t = el.inner_text().strip()
                    if len(t) > 5:
                        name = _clean_title(t)
                        break
            except (PWTimeout, Exception):
                continue

        name    = name or "Nintendo Switch 2"
        body    = page.inner_text("body")
        stock   = _classify_stock(body)
        variant = _classify_variant(name, url)

        logger.info(
            f"   ✓ seller={seller!r}  price=Rp{price:,}  "
            f"stock={stock}  variant={variant}"
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
