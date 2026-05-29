"""
Price fetcher — extracts prices from the raw HTML of each product page.

Why HTML parsing instead of internal APIs:
  - Tokopedia GQL (gql.tokopedia.com) blocks non-browser IPs: timeout/403
  - Shopee API (shopee.co.id/api/v4) requires CSRF cookies: 403
  - BliBli backend API (/backend/product-detail) blocks server IPs: 403

What we use instead:
  Every product page embeds its full data in the HTML as either:
    1. <script id="__NEXT_DATA__">  — Next.js hydration JSON (Tokopedia, Shopee)
    2. <script type="application/ld+json">  — Schema.org Product markup (all three)
    3. <meta property="og:price:amount">  — OpenGraph price meta tag (fallback)
    4. Regex on visible price text  — last resort

This approach works because:
  - Static HTML is served by CDN/edge nodes that don't check cookies
  - No JS execution required — prices are embedded for SEO/bots
  - Works from any IP including GitHub Actions runners
"""

import re
import json
import time
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS

logger = logging.getLogger(__name__)

# ── Request headers — mimic a real browser GET ───────────────────────────────
# Using a realistic Accept-Language (Indonesian) improves CDN routing
# and reduces the chance of being served a bot-detection page.

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT":             "1",
    "Connection":      "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-User":  "?1",
}


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def _with_retry(fn, attempts: int, backoff: float):
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            wait = backoff * (2 ** i)
            logger.warning(f"  attempt {i+1}/{attempts} failed: {exc} — retry in {wait:.0f}s")
            time.sleep(wait)
    raise last_exc


# ── HTML fetcher ──────────────────────────────────────────────────────────────

def _get_html(url: str, cfg: Config) -> str:
    """Fetch raw HTML for a product page with retry."""
    def call():
        resp = requests.get(
            url,
            headers={**HEADERS, "Referer": "https://www.google.com/"},
            timeout=cfg.request_timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    return _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)


# ── Price extractors (applied in order until one succeeds) ───────────────────

def _price_from_next_data(html: str, platform: str) -> Optional[int]:
    """
    Extract price from Next.js __NEXT_DATA__ hydration blob.
    Tokopedia and Shopee both embed their full product state here.
    """
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None

    # Flatten the entire JSON tree and search for numeric price values
    prices = []
    _collect_prices(data, prices, platform)
    return _best_price(prices)


def _collect_prices(obj, out: list, platform: str, depth: int = 0):
    """Recursively walk a JSON object and collect plausible IDR price values."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_low = k.lower()
            # Keys that commonly hold the display price
            if k_low in ("price", "pricemin", "price_min", "saleprice",
                         "pricewithcurrency", "displayprice", "normalPrice",
                         "regularPrice", "listed", "offer", "amount"):
                if isinstance(v, (int, float)) and v > 0:
                    out.append(float(v))
                elif isinstance(v, str):
                    cleaned = re.sub(r"[^\d]", "", v)
                    if cleaned:
                        out.append(float(cleaned))
            else:
                _collect_prices(v, out, platform, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_prices(item, out, platform, depth + 1)


def _best_price(candidates: list, min_idr: int = 1_000_000,
                max_idr: int = 30_000_000) -> Optional[int]:
    """
    From a list of raw numeric candidates, return the most likely IDR price.
    Shopee stores prices as IDR × 100000, so we normalise those.
    """
    normalised = []
    for v in candidates:
        # Shopee encodes price as micros (IDR * 100000)
        if v > 1_000_000_000:
            v = v / 100000
        if min_idr <= v <= max_idr:
            normalised.append(int(round(v)))
    if not normalised:
        return None
    # Return the median to avoid outliers (discount prices, shipping costs, etc.)
    normalised.sort()
    return normalised[len(normalised) // 2]


def _price_from_json_ld(html: str) -> Optional[int]:
    """
    Extract price from Schema.org JSON-LD Product markup.
    All three platforms include this for SEO.
    """
    for script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue
        # May be a single object or a list
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") in ("Product", "Offer"):
                offers = item.get("offers") or item
                if isinstance(offers, list):
                    offers = offers[0]
                price_raw = (
                    offers.get("price") or
                    offers.get("lowPrice") or
                    item.get("price")
                )
                if price_raw is not None:
                    try:
                        price = float(str(price_raw).replace(",", ""))
                        if 1_000_000 <= price <= 30_000_000:
                            return int(round(price))
                    except (ValueError, TypeError):
                        continue
    return None


def _price_from_og_meta(html: str) -> Optional[int]:
    """
    Extract price from OpenGraph meta tags.
    <meta property="og:price:amount" content="6299000">
    """
    m = re.search(
        r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I
    )
    if m:
        try:
            price = float(re.sub(r"[^\d.]", "", m.group(1)))
            if 1_000_000 <= price <= 30_000_000:
                return int(round(price))
        except (ValueError, TypeError):
            pass
    return None


def _price_from_text_regex(html: str) -> Optional[int]:
    """
    Last-resort: find 'Rp X.XXX.XXX' anywhere in the visible page text.
    Uses BeautifulSoup to strip tags first.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = html

    candidates = []
    for m in re.finditer(
        r"Rp\.?\s*([\d]{1,2}(?:[.,]\d{3})+(?:[.,]\d{2})?)",
        text
    ):
        raw = re.sub(r"[.,]", "", m.group(1))
        try:
            v = int(raw)
            if 1_000_000 <= v <= 30_000_000:
                candidates.append(v)
        except ValueError:
            continue
    return _best_price([float(c) for c in candidates]) if candidates else None


# ── Stock extractor ───────────────────────────────────────────────────────────

def _stock_from_html(html: str) -> str:
    """Detect out-of-stock / low-stock signals from the page HTML."""
    text = html.lower()
    if any(w in text for w in [
        "habis", "out of stock", "sold out", "stok habis",
        "kosong", "unavailable", "tidak tersedia"
    ]):
        return "out-stock"
    if any(w in text for w in [
        "sisa", "limited", "hampir habis", "tersisa", "segera habis"
    ]):
        return "low-stock"
    return "in-stock"


# ── Name / variant extractor ──────────────────────────────────────────────────

def _name_from_html(html: str, fallback: str) -> str:
    """Extract product name from <title>, og:title, or JSON-LD."""
    # og:title is usually cleanest
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{5,120})["\']',
        html, re.I
    )
    if m:
        return m.group(1).strip()
    # <title> tag
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.DOTALL)
    if m:
        name = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(name) > 5:
            return name[:80]
    return fallback


def _classify_variant(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ["mario kart", "bundle", "paket", "world", "mk world"]):
        return "With Mario Kart"
    return "Standard"


def _clean_name(raw: str) -> str:
    """Trim over-long marketplace titles."""
    name = re.sub(r'\s+', ' ', raw).strip()
    for sep in [' - ', ' | ', ' / ', ',']:
        if len(name) > 70 and sep in name:
            name = name.split(sep)[0].strip()
    return name[:80]


# ── Main per-listing fetcher ──────────────────────────────────────────────────

def _fetch_one(listing: dict, cfg: Config) -> dict:
    """
    Fetch one listing's product page and extract price + stock.
    Tries four extraction strategies in order.
    """
    url      = listing["url"]
    platform = listing["platform"]
    seller   = listing["seller"]

    logger.info(f"   GET {url[:80]}")
    html = _get_html(url, cfg)

    # Try extraction strategies in order of reliability
    price = (
        _price_from_next_data(html, platform) or
        _price_from_json_ld(html)             or
        _price_from_og_meta(html)             or
        _price_from_text_regex(html)
    )

    if not price:
        raise ValueError(f"No price found in page HTML ({len(html)} bytes)")

    stock   = _stock_from_html(html)
    raw_name = _name_from_html(html, f"Nintendo Switch 2 — {seller}")
    name    = _clean_name(raw_name)
    variant = _classify_variant(name)

    logger.info(
        f"   ✓ price=Rp{price:,}  stock={stock}  variant={variant}"
    )
    return {
        "seller":   seller,
        "platform": platform,
        "product":  name or "Nintendo Switch 2",
        "variant":  variant,
        "price":    price,
        "stock":    stock,
        "url":      url,
    }


# ── Public API ────────────────────────────────────────────────────────────────

class FetchResult:
    def __init__(self):
        self.listings:      list[dict]    = []
        self.source:        str           = "unknown"
        self.queries_fired: int           = 0
        self.success:       bool          = False
        self.error:         Optional[str] = None
        self.is_demo:       bool          = False


def fetch_all(cfg: Config) -> FetchResult:
    """
    Fetch price + stock for every URL in TRACKED_LISTINGS by parsing
    the raw HTML of each product page. No internal API calls needed.
    Falls back to DEMO_LISTINGS only if ALL fetches fail.
    """
    result        = FetchResult()
    result.source = "html-parse"
    listings      = []
    fail_count    = 0

    for entry in TRACKED_LISTINGS:
        result.queries_fired += 1
        label = f"{entry['seller']} @ {entry['platform']}"
        logger.info(f"── {label}")
        try:
            listing = _fetch_one(entry, cfg)
            listings.append(listing)
        except Exception as exc:
            fail_count += 1
            logger.error(f"   ✗ {label}: {exc}")

    total = len(TRACKED_LISTINGS)
    logger.info(
        f"\nFetch complete — {len(listings)}/{total} OK, "
        f"{fail_count}/{total} failed"
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
        result.error    = "All HTML fetches returned no price"

    return result
