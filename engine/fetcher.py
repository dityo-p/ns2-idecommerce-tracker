"""
Price fetcher — queries SerpApi / Serper.dev, parses organic results,
and returns a clean list of listing dicts.

Architecture:
    fetch_all()
        ├── _fetch_serpapi(query)   → raw items[]
        ├── _fetch_serper(query)    → raw items[]
        └── _parse_items(items)     → listing dicts

Retry logic wraps every HTTP call with exponential backoff.
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests

from config import Config, SEARCH_QUERIES, SELLER_NAMES, DEMO_LISTINGS

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

PLATFORM_DOMAINS = {
    "tokopedia.com": "Tokopedia",
    "shopee.co.id":  "Shopee",
    "blibli.com":    "BliBli",
}

PLATFORM_FALLBACK_URLS = {
    "Tokopedia": "https://www.tokopedia.com/search?q=nintendo+switch+2",
    "Shopee":    "https://shopee.co.id/search?keyword=nintendo+switch+2",
    "BliBli":    "https://www.blibli.com/jual/nintendo-switch-2",
}


def _detect_platform(url: str, snippet: str = "") -> Optional[str]:
    combined = (url + " " + snippet).lower()
    for domain, platform in PLATFORM_DOMAINS.items():
        if domain in combined:
            return platform
    return None


def _detect_seller(title: str, snippet: str = "") -> Optional[str]:
    text = (title + " " + snippet).lower()
    for name in SELLER_NAMES:
        if name.lower() in text:
            return name
    return None


def _extract_price(text: str, min_idr: int, max_idr: int) -> Optional[int]:
    """
    Extract IDR price from a mixed text string.
    Handles: Rp 6.299.000 / Rp6299000 / 6,299,000 / 6.299.000
    """
    # Remove currency symbol variants
    cleaned = re.sub(r"[Rr][Pp]\.?\s*", "", text)
    # Find numeric blobs that look like prices
    candidates = re.findall(r"\d{1,2}[.,]\d{3}(?:[.,]\d{3})+|\d{6,10}", cleaned)
    for raw in candidates:
        numeric = int(re.sub(r"[.,]", "", raw))
        if min_idr <= numeric <= max_idr:
            return numeric
    return None


def _classify_stock(snippet: str) -> str:
    s = snippet.lower()
    if any(w in s for w in ["habis", "out of stock", "kosong", "sold out"]):
        return "out-stock"
    if any(w in s for w in ["sisa", "limited", "hampir habis", "segera habis"]):
        return "low-stock"
    return "in-stock"


def _classify_variant(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["mario kart", "bundle", "paket"]):
        return "With Mario Kart"
    return "Standard"


def _parse_items(items: list, default_platform: Optional[str], cfg: Config) -> list[dict]:
    """
    Convert raw search result items into listing dicts.
    Deduplicates by (seller, platform) — keeps highest-confidence price.
    """
    seen: dict[str, dict] = {}

    for item in items:
        title   = item.get("title")   or ""
        snippet = item.get("snippet") or item.get("description") or ""
        link    = item.get("link")    or item.get("url") or ""

        if not re.search(r"switch\s*2", title + snippet, re.I):
            continue

        price = _extract_price(title + " " + snippet, cfg.min_price_idr, cfg.max_price_idr)
        if not price:
            continue

        platform = _detect_platform(link, snippet) or default_platform
        if not platform:
            continue

        seller  = _detect_seller(title, snippet) or "Official Store"
        variant = _classify_variant(title + " " + snippet)
        stock   = _classify_stock(snippet)
        url     = link or PLATFORM_FALLBACK_URLS.get(platform, "")

        key = f"{seller}__{platform}"
        if key not in seen:
            seen[key] = {
                "seller":   seller,
                "platform": platform,
                "product":  "Nintendo Switch 2",
                "variant":  variant,
                "price":    price,
                "stock":    stock,
                "url":      url,
            }

    return list(seen.values())


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _with_retry(fn, attempts: int, backoff: float):
    """Call fn(), retrying up to `attempts` times with exponential backoff."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            wait = backoff * (2 ** i)
            logger.warning(f"Attempt {i+1}/{attempts} failed: {exc}. Retrying in {wait:.1f}s…")
            time.sleep(wait)
    raise last_exc


def _fetch_serpapi(query: str, cfg: Config) -> list:
    def call():
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={"q": query, "gl": "id", "hl": "id", "num": 10, "api_key": cfg.serpapi_key},
            timeout=cfg.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise ValueError(f"SerpApi error: {data['error']}")
        return data.get("organic_results") or []

    return _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)


def _fetch_serper(query: str, cfg: Config) -> list:
    def call():
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": cfg.serper_key, "Content-Type": "application/json"},
            json={"q": query, "gl": "id", "hl": "id", "num": 10},
            timeout=cfg.request_timeout,
        )
        resp.raise_for_status()
        return resp.json().get("organic") or []

    return _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)


def _fetch_query(query: str, cfg: Config) -> tuple[list, str]:
    """
    Try SerpApi first, fall back to Serper.dev.
    Returns (items, source_name).
    """
    if cfg.has_serpapi():
        try:
            items = _fetch_serpapi(query, cfg)
            logger.info(f"SerpApi returned {len(items)} results for: {query[:60]}")
            return items, "serpapi"
        except Exception as exc:
            logger.warning(f"SerpApi failed ({exc}), trying Serper fallback…")

    if cfg.has_serper():
        items = _fetch_serper(query, cfg)
        logger.info(f"Serper returned {len(items)} results for: {query[:60]}")
        return items, "serper"

    raise RuntimeError("No API keys configured. Set SERPAPI_KEY or SERPER_KEY.")


# ── Public API ────────────────────────────────────────────────────────────────

class FetchResult:
    def __init__(self):
        self.listings:       list[dict] = []
        self.source:         str        = "unknown"
        self.queries_fired:  int        = 0
        self.success:        bool       = False
        self.error:          Optional[str] = None
        self.is_demo:        bool       = False


def fetch_all(cfg: Config) -> FetchResult:
    """
    Fire all SEARCH_QUERIES, parse results, deduplicate, and return a FetchResult.
    Falls back to DEMO_LISTINGS if no keys are configured or all queries fail.
    """
    result = FetchResult()

    if not cfg.has_any_key():
        logger.warning("No API keys set — using demo data.")
        result.listings  = DEMO_LISTINGS
        result.source    = "demo"
        result.success   = True
        result.is_demo   = True
        return result

    seen:    dict[str, dict] = {}
    source_used = "unknown"

    for q_cfg in SEARCH_QUERIES:
        result.queries_fired += 1
        try:
            items, source = _fetch_query(q_cfg["q"], cfg)
            source_used = source
            parsed = _parse_items(items, q_cfg.get("platform"), cfg)
            for listing in parsed:
                key = f"{listing['seller']}__{listing['platform']}"
                if key not in seen:
                    seen[key] = listing
            logger.info(f"Query {result.queries_fired}: {len(parsed)} listings parsed (running total: {len(seen)})")
        except Exception as exc:
            logger.error(f"Query failed: {exc}")

    # If we still have < 3 listings, fire a broad fallback query
    if len(seen) < 3:
        logger.warning(f"Only {len(seen)} listings found — firing broad fallback query…")
        fallback_q = "Nintendo Switch 2 Indonesia harga resmi toko resmi 2025"
        try:
            items, source = _fetch_query(fallback_q, cfg)
            source_used = source
            result.queries_fired += 1
            for listing in _parse_items(items, None, cfg):
                key = f"{listing['seller']}__{listing['platform']}"
                if key not in seen:
                    seen[key] = listing
        except Exception as exc:
            logger.error(f"Fallback query failed: {exc}")

    if seen:
        result.listings = list(seen.values())
        result.source   = source_used
        result.success  = True
        logger.info(f"Fetch complete: {len(result.listings)} listings from {source_used}")
    else:
        logger.error("All queries failed — falling back to demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = False
        result.is_demo  = True
        result.error    = "All API queries returned no results"

    return result
