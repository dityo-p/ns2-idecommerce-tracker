"""
Price fetcher — uses Google Shopping via SerpApi / Serper to get
structured price data for each tracked listing.

Why Google Shopping instead of page fetching:
  All three platforms (Tokopedia, Shopee, BliBli) render prices via
  JavaScript. No static HTML fetcher — direct, Serper scrape, or
  SerpApi cache — ever sees the price. Google Shopping, however,
  indexes these product pages with full price data via its own crawler
  (which has elevated platform access), and returns it as structured
  JSON through the API. No IP blocking, no JS rendering needed.

Strategy per listing:
  1. Build a targeted Google Shopping query:
       "Nintendo Switch 2 {shop_slug} site:{platform_domain}"
     e.g. "Nintendo Switch 2 teknotrend site:tokopedia.com"
  2. Call SerpApi engine=google_shopping (primary) or Serper shopping
     endpoint (fallback). Both return structured shopping_results[].
  3. Find the result whose link most closely matches our tracked URL.
     If no link match, accept the first result with a valid IDR price.
  4. Use shopping_results[].extracted_price — already a clean float.

Credit usage:
  9 listings × 2 runs/day × 30 days = 540 queries/month.
  SerpApi free tier:  100/month  (fallback only — Serper used first)
  Serper free tier: 2,500/month  (primary — well within limit)
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS, PLATFORM_DOMAIN

logger = logging.getLogger(__name__)


# ── Query builder ─────────────────────────────────────────────────────────────

def _build_query(listing: dict) -> str:
    """
    Build a Google Shopping query that targets one specific seller
    on one specific platform.
    """
    slug   = listing["shop_slug"]
    domain = PLATFORM_DOMAIN[listing["platform"]]
    return f'Nintendo Switch 2 {slug} site:{domain}'


# ── API callers ───────────────────────────────────────────────────────────────

def _serpapi_shopping(query: str, cfg: Config) -> list:
    """Call SerpApi Google Shopping engine. Returns shopping_results list."""
    resp = requests.get(
        "https://serpapi.com/search",
        params={
            "engine":  "google_shopping",
            "q":       query,
            "api_key": cfg.serpapi_key,
            "gl":      "id",
            "hl":      "id",
            "num":     "10",
        },
        timeout=cfg.request_timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise ValueError(f"SerpApi error: {data['error']}")
    return data.get("shopping_results") or []


def _serper_shopping(query: str, cfg: Config) -> list:
    """
    Call Serper Google Shopping endpoint. Returns shopping results list.
    Serper's /shopping endpoint mirrors Google Shopping and returns:
      title, source, price, link, rating, imageUrl
    """
    resp = requests.post(
        "https://google.serper.dev/shopping",
        headers={
            "X-API-KEY":    cfg.serper_key,
            "Content-Type": "application/json",
        },
        json={
            "q":   query,
            "gl":  "id",
            "hl":  "id",
            "num": 10,
        },
        timeout=cfg.request_timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("shopping") or []


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


# ── Result picking ────────────────────────────────────────────────────────────

def _normalise_results(raw: list, source: str) -> list:
    """
    Normalise SerpApi and Serper results to a common shape:
    [{"title", "source", "price_str", "price", "link", "in_stock"}, ...]
    """
    out = []
    for r in raw:
        # SerpApi uses "extracted_price"; Serper uses "price" as string
        price_float = r.get("extracted_price")
        if price_float is None:
            price_str = str(r.get("price") or "")
            digits = re.sub(r"[^\d]", "", price_str)
            price_float = float(digits) if digits else None

        if price_float is None:
            continue

        # Shopee encodes in micros
        if price_float > 1_000_000_000:
            price_float /= 100_000

        if not (1_000_000 <= price_float <= 30_000_000):
            continue

        out.append({
            "title":    r.get("title") or "",
            "source":   r.get("source") or "",
            "price":    int(round(price_float)),
            "link":     r.get("link") or r.get("product_link") or "",
            "in_stock": "out of stock" not in (r.get("title") or "").lower()
                        and "out of stock" not in (r.get("extensions") or []),
        })
    return out


def _best_result(results: list, listing: dict) -> Optional[dict]:
    """
    Pick the best result for a listing.
    Preference order:
      1. Result whose link contains the exact product URL path
      2. Result whose link contains the shop slug
      3. Result whose source field contains the seller name
      4. First result (already filtered to correct domain by site: operator)
    """
    slug        = listing["shop_slug"].lower()
    seller      = listing["seller"].lower()
    target_url  = listing["url"]
    target_path = urlparse(target_url).path.lower()

    # Score each result
    def score(r):
        link   = r["link"].lower()
        source = r["source"].lower()
        if target_path and target_path[:40] in link:
            return 3
        if slug in link or slug in source:
            return 2
        if seller.replace(" ", "").lower() in source.replace(" ", "").lower():
            return 1
        return 0

    scored = sorted(results, key=score, reverse=True)
    return scored[0] if scored else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _classify_variant(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["mario kart", "bundle", "paket", "world", "mk"]):
        return "With Mario Kart"
    return "Standard"


def _classify_stock(result: dict) -> str:
    title = (result.get("title") or "").lower()
    if any(w in title for w in ["habis", "out of stock", "kosong", "sold out"]):
        return "out-stock"
    return "in-stock"


def _clean_title(raw: str) -> str:
    name = re.sub(r'\s+', ' ', raw).strip()
    for sep in [' - ', ' | ', ' / ', ',']:
        if len(name) > 70 and sep in name:
            name = name.split(sep)[0].strip()
    return name[:80]


# ── Per-listing fetcher ───────────────────────────────────────────────────────

def _fetch_one(listing: dict, cfg: Config) -> dict:
    query = _build_query(listing)
    logger.info(f"   query: {query}")

    raw_results = []
    source_used = "unknown"

    # Primary: Serper /shopping (2,500 free/month)
    if cfg.has_serper():
        try:
            def call_serper():
                return _serper_shopping(query, cfg)
            raw = _with_retry(call_serper, cfg.retry_attempts, cfg.retry_backoff)
            raw_results = _normalise_results(raw, "serper")
            source_used = "serper"
            logger.info(f"   Serper: {len(raw)} raw → {len(raw_results)} with price")
        except Exception as exc:
            logger.warning(f"   Serper failed: {exc}")

    # Fallback: SerpApi /search?engine=google_shopping (100 free/month)
    if not raw_results and cfg.has_serpapi():
        try:
            def call_serpapi():
                return _serpapi_shopping(query, cfg)
            raw = _with_retry(call_serpapi, cfg.retry_attempts, cfg.retry_backoff)
            raw_results = _normalise_results(raw, "serpapi")
            source_used = "serpapi"
            logger.info(f"   SerpApi: {len(raw)} raw → {len(raw_results)} with price")
        except Exception as exc:
            logger.warning(f"   SerpApi failed: {exc}")

    if not raw_results:
        raise ValueError("No shopping results with a valid price returned by either API")

    best = _best_result(raw_results, listing)
    if not best:
        raise ValueError("Could not match any result to this seller/listing")

    price   = best["price"]
    title   = _clean_title(best["title"]) or "Nintendo Switch 2"
    variant = _classify_variant(title)
    stock   = _classify_stock(best)

    logger.info(
        f"   ✓ Rp{price:,}  stock={stock}  variant={variant}  "
        f"source={source_used}  matched='{best['title'][:50]}'"
    )

    return {
        "seller":   listing["seller"],
        "platform": listing["platform"],
        "product":  title,
        "variant":  variant,
        "price":    price,
        "stock":    stock,
        "url":      listing["url"],   # always use our canonical URL
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
    result     = FetchResult()
    listings   = []
    fail_count = 0

    if not cfg.has_any_key():
        logger.warning("No API keys — using demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = True
        result.is_demo  = True
        return result

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

    # Determine dominant source
    result.source = "serper" if cfg.has_serper() else "serpapi"

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
        result.error    = "All Google Shopping queries returned no usable price data"

    return result
