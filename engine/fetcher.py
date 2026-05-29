"""
Price fetcher — calls each platform's own internal API directly using
product IDs parsed from the user-supplied listing URLs.

No search queries. No guessing. Each URL maps to one API call that returns
price, stock, and product name for that exact listing.

Platform strategies
───────────────────
Tokopedia  →  GraphQL API (gql.tokopedia.com)
               IDs parsed from URL: shop domain + numeric product_id (last segment)

Shopee     →  REST API (shopee.co.id/api/v4/item/get)
               IDs parsed from URL pattern: i.{shop_id}.{item_id}

BliBli     →  REST API (www.blibli.com/backend/product-detail/products/{sku}/sku)
               SKU parsed from URL: /is--{sku} or /ps--{sku}
               /is-- = individual SKU   → .../sku  endpoint
               /ps-- = product set SKU  → .../sku  endpoint (same, BliBli normalises)

All fetchers share the same retry wrapper and return the same listing dict shape.
"""

import re
import json
import time
import logging
from typing import Optional
from urllib.parse import urlparse

import requests

from config import Config, TRACKED_LISTINGS, DEMO_LISTINGS

logger = logging.getLogger(__name__)

# ── Shared HTTP headers ────────────────────────────────────────────────────────

HEADERS_BROWSER = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
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


# ── Price / stock helpers ─────────────────────────────────────────────────────

def _classify_stock(raw) -> str:
    """Normalise various stock representations to in-stock / low-stock / out-stock."""
    if raw is None:
        return "in-stock"
    if isinstance(raw, bool):
        return "in-stock" if raw else "out-stock"
    if isinstance(raw, (int, float)):
        if raw <= 0:   return "out-stock"
        if raw <= 5:   return "low-stock"
        return "in-stock"
    s = str(raw).lower()
    if any(w in s for w in ["out", "habis", "kosong", "0", "false", "unavailable"]):
        return "out-stock"
    if any(w in s for w in ["low", "sisa", "limited", "hampir"]):
        return "low-stock"
    return "in-stock"


def _classify_variant(name: str) -> str:
    n = (name or "").lower()
    if any(w in n for w in ["mario kart", "bundle", "paket", "world", "mk"]):
        return "With Mario Kart"
    return "Standard"


def _clean_price(raw) -> Optional[int]:
    """Convert any price representation to a plain IDR integer."""
    if raw is None:
        return None
    try:
        p = float(raw)
        # Shopee stores price as IDR * 100000
        if p > 1_000_000_000:
            p = p / 100000
        if 500_000 <= p <= 30_000_000:
            return int(round(p))
    except (TypeError, ValueError):
        pass
    return None


# ── URL parsers ───────────────────────────────────────────────────────────────

def _parse_tokopedia_url(url: str) -> Optional[dict]:
    """
    Extract shop_domain and product_id from a Tokopedia product URL.
    URL pattern: tokopedia.com/{shop_domain}/{slug}-{product_id}
    product_id is the last hyphen-separated numeric segment (≥15 digits).
    """
    path = urlparse(url).path.strip("/").split("/")
    if len(path) < 2:
        return None
    shop_domain = path[0]
    slug        = path[1]
    # product_id is last segment after final '-', must be a long integer
    product_id  = slug.rsplit("-", 1)[-1]
    if not product_id.isdigit() or len(product_id) < 10:
        return None
    return {"shop_domain": shop_domain, "product_id": product_id, "slug": slug}


def _parse_shopee_url(url: str) -> Optional[dict]:
    """
    Extract shop_id and item_id from a Shopee product URL.
    URL pattern: shopee.co.id/...-i.{shop_id}.{item_id}
    """
    m = re.search(r'i\.(\d+)\.(\d+)', url)
    if not m:
        return None
    return {"shop_id": m.group(1), "item_id": m.group(2)}


def _parse_blibli_url(url: str) -> Optional[dict]:
    """
    Extract SKU from a BliBli product URL.
    Patterns:
      /is--{sku}  — individual product SKU
      /ps--{sku}  — product set SKU
    """
    m = re.search(r'/(is|ps)--([^/?#]+)', url)
    if not m:
        return None
    return {"sku_type": m.group(1), "sku": m.group(2)}


# ── Platform fetchers ─────────────────────────────────────────────────────────

def _fetch_tokopedia(listing: dict, cfg: Config) -> dict:
    """
    Call Tokopedia's GraphQL API for one product listing.
    Uses the PDPGetLayoutQuery which powers the web product page.
    """
    parsed = _parse_tokopedia_url(listing["url"])
    if not parsed:
        raise ValueError(f"Cannot parse Tokopedia URL: {listing['url']}")

    gql_url = "https://gql.tokopedia.com/"
    headers = {
        **HEADERS_BROWSER,
        "Content-Type":        "application/json",
        "X-Source":            "tokopedia-lite",
        "X-Tkpd-App-Version":  "3.0",
        "X-Device":            "desktop-0.0",
        "X-Tkpd-UserId":       "0",
        "Referer":             "https://www.tokopedia.com/",
        "Origin":              "https://www.tokopedia.com",
        "Accept":              "*/*",
    }

    payload = [
        {
            "operationName": "PDPGetLayoutQuery",
            "variables": {
                "shopDomain":  parsed["shop_domain"],
                "productKey":  parsed["slug"],
                "layoutID":    "",
                "apiVersion":  1,
                "userLocation": {
                    "cityID":       "176",
                    "addressID":    "0",
                    "districtID":   "2274",
                    "postalCode":   "12950",
                    "latlon":       "",
                },
                "extParam": "",
            },
            "query": """
                query PDPGetLayoutQuery($shopDomain: String, $productKey: String, $layoutID: String, $apiVersion: Float, $userLocation: pdpUserLocation, $extParam: String) {
                  pdpGetLayout(shopDomain: $shopDomain, productKey: $productKey, layoutID: $layoutID, apiVersion: $apiVersion, userLocation: $userLocation, extParam: $extParam) {
                    basicInfo {
                      alias
                      stats { priceMin priceMax }
                      stock { useParallelStock stockQty }
                      txStats { transactionSuccess }
                    }
                  }
                }
            """,
        }
    ]

    def call():
        r = requests.post(gql_url, headers=headers, json=payload, timeout=cfg.request_timeout)
        r.raise_for_status()
        return r.json()

    data    = _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)
    basic   = data[0]["data"]["pdpGetLayout"]["basicInfo"]
    stats   = basic.get("stats", {})
    stock_d = basic.get("stock", {})

    name    = basic.get("alias") or listing["seller"] + " Nintendo Switch 2"
    price   = _clean_price(stats.get("priceMin") or stats.get("priceMax"))
    stock_q = stock_d.get("stockQty", 1)
    stock   = _classify_stock(stock_q)
    variant = _classify_variant(name)

    if not price:
        raise ValueError(f"No valid price in Tokopedia response for {listing['seller']}")

    return {
        "seller":   listing["seller"],
        "platform": listing["platform"],
        "product":  _clean_product_name(name),
        "variant":  variant,
        "price":    price,
        "stock":    stock,
        "url":      listing["url"],
    }


def _fetch_shopee(listing: dict, cfg: Config) -> dict:
    """
    Call Shopee's item detail API.
    Price is returned as IDR × 100000 — divide by 100000.
    """
    parsed = _parse_shopee_url(listing["url"])
    if not parsed:
        raise ValueError(f"Cannot parse Shopee URL: {listing['url']}")

    # v2 endpoint is less restricted than v4 and doesn't require CSRF cookies
    api_url = f"https://shopee.co.id/api/v2/item/get?itemid={parsed['item_id']}&shopid={parsed['shop_id']}"
    headers = {
        **HEADERS_BROWSER,
        "Referer":      "https://shopee.co.id/",
        "X-Api-Source": "pc",
        "If-None-Match": "",
    }

    def call():
        r = requests.get(api_url, headers=headers, timeout=cfg.request_timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("error") and body["error"] != 0:
            raise ValueError(f"Shopee API error {body['error']}: {body.get('error_msg','')}")
        return body

    body    = _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)
    item    = body.get("data") or {}
    name    = item.get("name") or ""
    price   = _clean_price(item.get("price") or item.get("price_min"))
    stock_v = item.get("stock") if item.get("stock") is not None else item.get("item_status", 1)
    stock   = _classify_stock(stock_v)
    variant = _classify_variant(name)

    if not price:
        raise ValueError(f"No valid price in Shopee response for {listing['seller']}")

    return {
        "seller":   listing["seller"],
        "platform": listing["platform"],
        "product":  _clean_product_name(name) or "Nintendo Switch 2",
        "variant":  variant,
        "price":    price,
        "stock":    stock,
        "url":      listing["url"],
    }


def _fetch_blibli(listing: dict, cfg: Config) -> dict:
    """
    Call BliBli's product-detail API using the SKU from the URL.
    Both /is-- (individual) and /ps-- (product set) use the same endpoint.
    """
    parsed = _parse_blibli_url(listing["url"])
    if not parsed:
        raise ValueError(f"Cannot parse BliBli URL: {listing['url']}")

    sku     = parsed["sku"]
    api_url = f"https://www.blibli.com/backend/product-detail/products/{sku}/sku"
    headers = {
        **HEADERS_BROWSER,
        "Referer":      "https://www.blibli.com/",
        "Origin":       "https://www.blibli.com",
        "Accept":       "application/json, text/plain, */*",
        "channel-id":   "web",
    }

    def call():
        r = requests.get(api_url, headers=headers, timeout=cfg.request_timeout)
        r.raise_for_status()
        return r.json()

    body = _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)
    data = body.get("data") or {}

    # BliBli nests price under data.sku.price or data.price
    sku_data = data.get("sku") or {}
    price_d  = sku_data.get("price") or data.get("price") or {}
    name     = data.get("name") or sku_data.get("name") or ""
    price    = _clean_price(
        price_d.get("listed") or price_d.get("offer") or
        data.get("priceDisplay") or data.get("price")
    )
    stock_v  = sku_data.get("stock") or data.get("stock")
    stock    = _classify_stock(stock_v)
    variant  = _classify_variant(name)

    if not price:
        raise ValueError(f"No valid price in BliBli response for {listing['seller']}")

    return {
        "seller":   listing["seller"],
        "platform": listing["platform"],
        "product":  _clean_product_name(name) or "Nintendo Switch 2",
        "variant":  variant,
        "price":    price,
        "stock":    stock,
        "url":      listing["url"],
    }


def _clean_product_name(raw: str) -> str:
    """Shorten overly long product names to something dashboard-friendly (≤60 chars)."""
    if not raw:
        return "Nintendo Switch 2"
    # Strip excessive repetition common in Indonesian marketplace titles
    name = re.sub(r'\s+', ' ', raw).strip()
    # If name is very long, extract a clean prefix up to the first '/'  or '-' or ','
    if len(name) > 60:
        for sep in ['/', ' - ', ',', '|']:
            if sep in name:
                name = name.split(sep)[0].strip()
                break
    return name[:80]


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _fetch_one(listing: dict, cfg: Config) -> dict:
    """Route one listing to the correct platform fetcher."""
    platform = listing["platform"]
    if platform == "Tokopedia":
        return _fetch_tokopedia(listing, cfg)
    if platform == "Shopee":
        return _fetch_shopee(listing, cfg)
    if platform == "BliBli":
        return _fetch_blibli(listing, cfg)
    raise ValueError(f"Unknown platform: {platform}")


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
    Fetch price + stock for every URL in TRACKED_LISTINGS by calling
    each platform's own API directly. No search queries, no guessing.

    Falls back to DEMO_LISTINGS only when ALL fetches fail.
    Partial results (some listings succeed, some fail) are still returned —
    failed listings retain their last-known value from the DB.
    """
    result = FetchResult()
    result.source = "direct-api"

    listings:   list[dict] = []
    fail_count: int        = 0

    for entry in TRACKED_LISTINGS:
        result.queries_fired += 1
        label = f"{entry['seller']} @ {entry['platform']}"
        logger.info(f"── Fetching {label}")
        logger.info(f"   {entry['url'][:80]}")

        try:
            listing = _fetch_one(entry, cfg)
            listings.append(listing)
            logger.info(
                f"   ✓ price=Rp{listing['price']:,}  "
                f"stock={listing['stock']}  variant={listing['variant']}"
            )
        except Exception as exc:
            fail_count += 1
            logger.error(f"   ✗ failed: {exc}")

    total = len(TRACKED_LISTINGS)
    logger.info(
        f"\nFetch complete — {len(listings)}/{total} succeeded, "
        f"{fail_count}/{total} failed"
    )

    if listings:
        result.listings = listings
        result.success  = True
    else:
        logger.error("All fetches failed — falling back to demo data")
        result.listings = DEMO_LISTINGS
        result.source   = "demo"
        result.success  = False
        result.is_demo  = True
        result.error    = "All platform API calls failed"

    return result
