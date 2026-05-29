"""
Price fetcher — fires one targeted search query per official seller,
then strictly validates that each result matches both the expected
seller name AND platform before accepting it.

No result is ever accepted with an unknown seller name. The "Official Store"
fallback from the old version has been removed entirely.

Architecture:
    fetch_all()
        └── for each seller in OFFICIAL_SELLERS:
                _fetch_query(seller-specific query)
                    ├── _fetch_serpapi()   primary
                    └── _fetch_serper()    fallback
                _parse_items(items, seller_config)
                    └── strict seller+platform guard — DROP if no match
"""

import re
import time
import logging
from typing import Optional

import requests

from config import Config, OFFICIAL_SELLERS, DEMO_LISTINGS

logger = logging.getLogger(__name__)


# ── Platform domain map ───────────────────────────────────────────────────────

PLATFORM_DOMAINS = {
    "tokopedia.com": "Tokopedia",
    "shopee.co.id":  "Shopee",
    "blibli.com":    "BliBli",
}


# ── Per-seller search query builder ──────────────────────────────────────────

def _build_query(seller: dict) -> str:
    """
    Build a precise Google query that targets one seller on one platform.

    Examples:
      'Nintendo Switch 2 "PS Enterprise" site:tokopedia.com'
      'Nintendo Switch 2 "Drakuli" site:shopee.co.id'
      'Nintendo Switch 2 "Gamestation" site:blibli.com'

    Quoting the seller name forces Google to require the exact string,
    eliminating results from unrelated stores.
    """
    platform_domains = {
        "Tokopedia": "tokopedia.com",
        "Shopee":    "shopee.co.id",
        "BliBli":    "blibli.com",
    }
    domain = platform_domains.get(seller["platform"], "")
    site_clause = f"site:{domain}" if domain else ""
    return f'Nintendo Switch 2 "{seller["name"]}" {site_clause}'.strip()


# ── Result parsers ────────────────────────────────────────────────────────────

def _detect_platform(url: str, snippet: str = "") -> Optional[str]:
    combined = (url + " " + snippet).lower()
    for domain, platform in PLATFORM_DOMAINS.items():
        if domain in combined:
            return platform
    return None


def _seller_mentioned(title: str, snippet: str, seller_name: str) -> bool:
    """Return True only if the seller name appears literally in title or snippet."""
    text = (title + " " + snippet).lower()
    return seller_name.lower() in text


def _extract_price(text: str, min_idr: int, max_idr: int) -> Optional[int]:
    """
    Extract an IDR price from mixed text.
    Handles: Rp 6.299.000 / Rp6299000 / 6,299,000 / 6.299.000
    """
    cleaned = re.sub(r"[Rr][Pp]\.?\s*", "", text)
    candidates = re.findall(r"\d{1,2}[.,]\d{3}(?:[.,]\d{3})+|\d{6,10}", cleaned)
    for raw in candidates:
        numeric = int(re.sub(r"[.,]", "", raw))
        if min_idr <= numeric <= max_idr:
            return numeric
    return None


def _classify_stock(snippet: str) -> str:
    s = snippet.lower()
    if any(w in s for w in ["habis", "out of stock", "kosong", "sold out", "stok habis"]):
        return "out-stock"
    if any(w in s for w in ["sisa", "limited", "hampir habis", "segera habis", "tersisa"]):
        return "low-stock"
    return "in-stock"


def _classify_variant(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["mario kart", "bundle", "paket", "world"]):
        return "With Mario Kart"
    return "Standard"


def _parse_items_for_seller(
    items: list,
    seller: dict,
    cfg: Config,
) -> Optional[dict]:
    """
    Search `items` for a result that:
      1. Mentions Nintendo Switch 2
      2. Mentions the exact seller name
      3. Comes from the seller's expected platform domain
      4. Contains a valid IDR price

    Returns the best matching listing dict, or None if nothing qualifies.
    The first qualifying result wins (results are already ranked by Google).
    """
    expected_platform = seller["platform"]
    seller_name       = seller["name"]
    fallback_url      = seller["url"]

    for item in items:
        title   = item.get("title")   or ""
        snippet = item.get("snippet") or item.get("description") or ""
        link    = item.get("link")    or item.get("url") or ""
        full    = title + " " + snippet

        # Must mention Switch 2
        if not re.search(r"switch\s*2|nintendo\s+switch\s+2", full, re.I):
            logger.debug(f"  skip (no Switch 2 mention): {title[:60]}")
            continue

        # Must come from the right platform domain
        detected_platform = _detect_platform(link, snippet)
        if detected_platform and detected_platform != expected_platform:
            logger.debug(f"  skip (wrong platform {detected_platform}≠{expected_platform}): {link[:60]}")
            continue

        # Seller name must appear literally in the result
        if not _seller_mentioned(title, snippet, seller_name):
            logger.debug(f"  skip (seller '{seller_name}' not mentioned): {title[:60]}")
            continue

        # Must have a parseable IDR price
        price = _extract_price(full, cfg.min_price_idr, cfg.max_price_idr)
        if not price:
            logger.debug(f"  skip (no price found): {title[:60]}")
            continue

        variant = _classify_variant(full)
        stock   = _classify_stock(snippet)
        url     = link if link else fallback_url

        logger.info(
            f"  ✓ matched: seller={seller_name}  platform={expected_platform}"
            f"  price=Rp{price:,}  variant={variant}  stock={stock}"
        )
        return {
            "seller":   seller_name,
            "platform": expected_platform,
            "product":  "Nintendo Switch 2",
            "variant":  variant,
            "price":    price,
            "stock":    stock,
            "url":      url,
        }

    logger.warning(f"  no qualifying result for {seller_name} @ {expected_platform}")
    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _with_retry(fn, attempts: int, backoff: float):
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
    """Try SerpApi first, fall back to Serper.dev. Returns (items, source_name)."""
    if cfg.has_serpapi():
        try:
            items = _fetch_serpapi(query, cfg)
            logger.info(f"  SerpApi: {len(items)} results")
            return items, "serpapi"
        except Exception as exc:
            logger.warning(f"  SerpApi failed ({exc}), trying Serper…")

    if cfg.has_serper():
        items = _fetch_serper(query, cfg)
        logger.info(f"  Serper: {len(items)} results")
        return items, "serper"

    raise RuntimeError("No API keys configured. Set SERPAPI_KEY or SERPER_KEY.")


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
    Fire one targeted query per seller in OFFICIAL_SELLERS.
    Each result is strictly validated against the expected seller name
    and platform — random stores are never accepted.

    Falls back to DEMO_LISTINGS only when no API keys are configured.
    """
    result = FetchResult()

    if not cfg.has_any_key():
        logger.warning("No API keys set — using demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = True
        result.is_demo  = True
        return result

    listings:     list[dict] = []
    source_used:  str        = "unknown"

    for seller in OFFICIAL_SELLERS:
        query = _build_query(seller)
        result.queries_fired += 1
        logger.info(f"Querying: {seller['name']} @ {seller['platform']}  →  {query}")

        try:
            items, source = _fetch_query(query, cfg)
            source_used   = source
            listing       = _parse_items_for_seller(items, seller, cfg)
            if listing:
                listings.append(listing)
        except Exception as exc:
            logger.error(f"  query failed for {seller['name']}: {exc}")

    logger.info(
        f"Fetch complete — {len(listings)}/{len(OFFICIAL_SELLERS)} sellers found"
        f"  source={source_used}"
    )

    if listings:
        result.listings = listings
        result.source   = source_used
        result.success  = True
    else:
        # All queries returned zero valid results — don't pollute DB with random data
        logger.error("Zero valid listings found. Keeping existing DB data (no overwrite).")
        result.listings = []
        result.source   = source_used
        result.success  = False
        result.error    = "No listings matched known sellers — DB not overwritten"

    return result
