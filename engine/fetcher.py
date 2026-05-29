"""
Price fetcher — uses a three-tier query strategy per seller to maximise
the chance of finding real results from these specific stores.

Why three tiers?
  Tier 1 — inurl:store_slug + site:platform
    Most targeted. Works when the store has a product page indexed by Google.
    e.g. 'Nintendo Switch 2 inurl:psegameshop site:tokopedia.com'

  Tier 2 — store_slug unquoted + site:platform  (no inurl)
    Broader — catches cases where the slug appears in snippet but not URL.
    e.g. 'Nintendo Switch 2 psegameshop site:tokopedia.com'

  Tier 3 — store_slug + platform name only  (no site: operator)
    Widest net. Catches aggregator pages, review sites, and cached results
    that mention both the store slug and the platform.
    e.g. 'Nintendo Switch 2 psegameshop tokopedia harga'

Each result is strictly validated:
  - Must mention Nintendo Switch 2
  - Result URL must come from the correct platform domain  (tiers 1 & 2)
    OR the snippet must mention the platform  (tier 3)
  - The store slug (not display name) must appear in the URL or snippet
  - Must contain a parseable IDR price in range [1M–20M]

A result that passes all four checks is accepted and the remaining tiers
for that seller are skipped. If all three tiers fail, the seller is logged
as not found — the existing DB row is preserved (not overwritten with blank).
"""

import re
import time
import logging
from typing import Optional

import requests

from config import Config, OFFICIAL_SELLERS, DEMO_LISTINGS

logger = logging.getLogger(__name__)


# ── Platform map ──────────────────────────────────────────────────────────────

PLATFORM_DOMAINS = {
    "tokopedia.com": "Tokopedia",
    "shopee.co.id":  "Shopee",
    "blibli.com":    "BliBli",
}

PLATFORM_DOMAIN_MAP = {v: k for k, v in PLATFORM_DOMAINS.items()}   # reverse


# ── Query builder ─────────────────────────────────────────────────────────────

def _queries_for_seller(seller: dict) -> list[dict]:
    """
    Return three queries for one seller, from most-targeted to broadest.
    Each dict has keys: q (query string), tier (int), require_platform_in_url (bool).
    """
    slug     = seller["store_slug"]
    platform = seller["platform"]
    domain   = PLATFORM_DOMAIN_MAP.get(platform, "")

    return [
        {
            "tier": 1,
            "q": f"Nintendo Switch 2 inurl:{slug} site:{domain}",
            "require_platform_url": True,
        },
        {
            "tier": 2,
            "q": f"Nintendo Switch 2 {slug} site:{domain}",
            "require_platform_url": True,
        },
        {
            "tier": 3,
            "q": f"Nintendo Switch 2 {slug} {platform} harga",
            "require_platform_url": False,   # only require platform in snippet
        },
    ]


# ── Validation helpers ────────────────────────────────────────────────────────

def _detect_platform_from_url(url: str) -> Optional[str]:
    url_lower = url.lower()
    for domain, platform in PLATFORM_DOMAINS.items():
        if domain in url_lower:
            return platform
    return None


def _slug_in_result(url: str, snippet: str, slug: str) -> bool:
    """Return True if the store slug appears in the URL or snippet."""
    combined = (url + " " + snippet).lower()
    return slug.lower() in combined


def _extract_price(text: str, min_idr: int, max_idr: int) -> Optional[int]:
    cleaned    = re.sub(r"[Rr][Pp]\.?\s*", "", text)
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
    require_platform_url: bool,
    cfg: Config,
) -> Optional[dict]:
    """
    Scan items for one result that passes all four validation checks.
    Returns the first match, or None.
    """
    expected_platform = seller["platform"]
    slug              = seller["store_slug"]
    fallback_url      = seller["url"]

    for item in items:
        title   = item.get("title")   or ""
        snippet = item.get("snippet") or item.get("description") or ""
        link    = item.get("link")    or item.get("url") or ""
        full    = title + " " + snippet + " " + link

        # 1. Must mention Switch 2
        if not re.search(r"nintendo\s*switch\s*2|switch\s*2|ns\s*2|ns2", full, re.I):
            logger.debug(f"    skip (no Switch 2): {title[:55]}")
            continue

        # 2. Platform check
        url_platform = _detect_platform_from_url(link)
        if require_platform_url:
            if url_platform != expected_platform:
                logger.debug(f"    skip (platform {url_platform}≠{expected_platform}): {link[:55]}")
                continue
        else:
            # Tier 3: platform must appear somewhere in the full text
            if expected_platform.lower() not in full.lower():
                logger.debug(f"    skip (platform not in text): {title[:55]}")
                continue

        # 3. Store slug must appear in URL or snippet
        if not _slug_in_result(link, snippet, slug):
            logger.debug(f"    skip (slug '{slug}' not found): {link[:55]}")
            continue

        # 4. Must have a valid IDR price
        price = _extract_price(full, cfg.min_price_idr, cfg.max_price_idr)
        if not price:
            logger.debug(f"    skip (no price): {title[:55]}")
            continue

        variant = _classify_variant(full)
        stock   = _classify_stock(snippet)
        url     = link if (link and expected_platform.lower() in link.lower()) else fallback_url

        logger.info(
            f"    ✓ {seller['name']} @ {expected_platform}  "
            f"Rp{price:,}  {variant}  {stock}"
        )
        return {
            "seller":   seller["name"],
            "platform": expected_platform,
            "product":  "Nintendo Switch 2",
            "variant":  variant,
            "price":    price,
            "stock":    stock,
            "url":      url,
        }

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
    """Try SerpApi, fall back to Serper. Returns (items, source_name)."""
    if cfg.has_serpapi():
        try:
            items = _fetch_serpapi(query, cfg)
            logger.info(f"  SerpApi → {len(items)} results")
            return items, "serpapi"
        except Exception as exc:
            logger.warning(f"  SerpApi failed ({exc}), trying Serper…")
    if cfg.has_serper():
        items = _fetch_serper(query, cfg)
        logger.info(f"  Serper  → {len(items)} results")
        return items, "serper"
    raise RuntimeError("No API keys configured.")


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
    For each seller in OFFICIAL_SELLERS, try up to three query tiers until
    a valid result is found. Strictly validates seller slug + platform on
    every result — random stores are never accepted.
    """
    result = FetchResult()

    if not cfg.has_any_key():
        logger.warning("No API keys set — using demo data.")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = True
        result.is_demo  = True
        return result

    listings:    list[dict] = []
    source_used: str        = "unknown"

    for seller in OFFICIAL_SELLERS:
        logger.info(f"── {seller['name']} @ {seller['platform']} (slug: {seller['store_slug']}) ──")
        found = False

        for q_cfg in _queries_for_seller(seller):
            tier  = q_cfg["tier"]
            query = q_cfg["q"]
            logger.info(f"  Tier {tier}: {query}")
            result.queries_fired += 1

            try:
                items, source = _fetch_query(query, cfg)
                source_used   = source

                if not items:
                    logger.info(f"  Tier {tier}: 0 results — trying next tier")
                    continue

                listing = _parse_items_for_seller(
                    items, seller, q_cfg["require_platform_url"], cfg
                )

                if listing:
                    listings.append(listing)
                    found = True
                    break   # stop trying tiers for this seller
                else:
                    logger.info(f"  Tier {tier}: results returned but none passed validation")

            except Exception as exc:
                logger.error(f"  Tier {tier} query failed: {exc}")

        if not found:
            logger.warning(f"  ✗ {seller['name']}: not found across all tiers — DB row preserved")

    logger.info(
        f"\nFetch complete — {len(listings)}/{len(OFFICIAL_SELLERS)} sellers matched"
        f"  source={source_used}  queries_fired={result.queries_fired}"
    )

    if listings:
        result.listings = listings
        result.source   = source_used
        result.success  = True
    else:
        logger.error("Zero valid listings found. DB not overwritten.")
        result.listings = []
        result.source   = source_used
        result.success  = False
        result.error    = "No listings matched known sellers across all query tiers"

    return result
