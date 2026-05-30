"""
Price fetcher — uses Serper /scrape and SerpApi html-output to fetch
product page content, bypassing the IP blocks on Tokopedia/Shopee/BliBli.

How it works
────────────
Both services route the request through their own infrastructure
(Google-indexed cache or residential proxies), so GitHub Actions'
datacenter IP never touches the Indonesian platform servers directly.

  Primary:  Serper.dev  POST https://scrape.serper.dev
            Sends the target URL; returns the page HTML/text.
            Uses the SERPER_KEY secret already in your repo.

  Fallback: SerpApi     GET  https://serpapi.com/search?engine=google&q=cache:{url}
            Fetches Google's cached copy of the page, which contains
            the full product data in __NEXT_DATA__ / JSON-LD / og: tags.
            Uses the SERPAPI_KEY secret already in your repo.

After fetching we extract the price using four strategies in order:
  1. __NEXT_DATA__ JSON blob    (Tokopedia, Shopee embed full product state here)
  2. JSON-LD structured data    (Schema.org Product / Offer — all three platforms)
  3. OpenGraph meta tags        (og:price:amount — all three platforms)
  4. Rp regex on visible text   (last resort)
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


# ── Proxy/cache fetchers ──────────────────────────────────────────────────────

def _fetch_via_serper(url: str, cfg: Config) -> str:
    """
    Fetch a URL through Serper's /scrape endpoint.
    Returns the page text content (may be Markdown or HTML depending on response).
    """
    resp = requests.post(
        "https://scrape.serper.dev",
        headers={
            "X-API-KEY":    cfg.serper_key,
            "Content-Type": "application/json",
        },
        json={"url": url},
        timeout=30,
    )
    if resp.status_code == 403:
        raise RuntimeError("Serper /scrape: 403 — key invalid or endpoint not available on your plan")
    if resp.status_code == 429:
        raise RuntimeError("Serper /scrape: 429 — rate limited")
    resp.raise_for_status()

    data = resp.json()
    # Serper /scrape returns {"text": "...", "markdown": "..."}
    # We prefer the raw text/html content
    return data.get("html") or data.get("markdown") or data.get("text") or ""


def _fetch_via_serpapi_cache(url: str, cfg: Config) -> str:
    """
    Fetch Google's cached copy of a URL via SerpApi.
    Uses engine=google with q=cache:{url} and output=html to get the raw HTML.
    Google's cache contains the full Next.js hydration data and JSON-LD.
    """
    resp = requests.get(
        "https://serpapi.com/search",
        params={
            "engine":   "google",
            "q":        f"cache:{url}",
            "api_key":  cfg.serpapi_key,
            "output":   "html",
            "num":      "1",
        },
        timeout=30,
    )
    if resp.status_code == 429:
        raise RuntimeError("SerpApi: 429 — rate limited or quota reached")
    resp.raise_for_status()

    # output=html returns the raw HTML of Google's cached page
    return resp.text


def _fetch_via_serpapi_json(url: str, cfg: Config) -> str:
    """
    Alternative SerpApi strategy: fetch the page via Google cache in JSON mode,
    then extract organic result snippets and any embedded product data.
    Used when html output doesn't contain enough price data.
    """
    resp = requests.get(
        "https://serpapi.com/search",
        params={
            "engine":   "google",
            "q":        f"cache:{url}",
            "api_key":  cfg.serpapi_key,
            "gl":       "id",
            "hl":       "id",
            "num":      "1",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # Combine all text fields that might contain price info
    parts = []
    for result in data.get("organic_results", []):
        parts.append(result.get("title", ""))
        parts.append(result.get("snippet", ""))
        price_info = result.get("price", "") or result.get("extracted_price", "")
        if price_info:
            parts.append(str(price_info))
    for block in data.get("answer_box", {}).values():
        if isinstance(block, str):
            parts.append(block)

    return "\n".join(parts)


def _fetch_content(url: str, cfg: Config) -> str:
    """
    Try Serper /scrape first, then SerpApi cache.
    Returns whatever content we get — HTML, markdown, or plain text.
    The price extractors handle all formats.
    """
    errors = []

    if cfg.serper_key:
        try:
            content = _fetch_via_serper(url, cfg)
            if content and len(content) > 200:
                logger.info("   source: Serper /scrape ✓")
                return content
            else:
                errors.append("Serper returned empty/short content")
        except Exception as exc:
            errors.append(f"Serper: {exc}")
            logger.warning(f"   Serper failed: {exc}")

    if cfg.serpapi_key:
        try:
            content = _fetch_via_serpapi_cache(url, cfg)
            if content and len(content) > 200:
                logger.info("   source: SerpApi cache ✓")
                return content
            else:
                errors.append("SerpApi cache returned empty content")
        except Exception as exc:
            errors.append(f"SerpApi cache: {exc}")
            logger.warning(f"   SerpApi cache failed: {exc}")

        # Last resort: SerpApi JSON mode
        try:
            content = _fetch_via_serpapi_json(url, cfg)
            if content and len(content) > 50:
                logger.info("   source: SerpApi JSON ✓")
                return content
        except Exception as exc:
            errors.append(f"SerpApi JSON: {exc}")
            logger.warning(f"   SerpApi JSON failed: {exc}")

    raise RuntimeError(
        "All fetch methods failed: " + " | ".join(errors)
    )


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


# ── Price extractors ──────────────────────────────────────────────────────────

def _price_from_next_data(content: str) -> Optional[int]:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        content, re.DOTALL
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    prices = []
    _collect_prices(data, prices)
    return _best_price(prices)


def _collect_prices(obj, out: list, depth: int = 0):
    if depth > 12:
        return
    PRICE_KEYS = {
        "price", "pricemin", "price_min", "saleprice", "pricewithcurrency",
        "displayprice", "normalprice", "regularprice", "listed", "offer",
        "amount", "harga", "pricedisplay",
    }
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in PRICE_KEYS:
                if isinstance(v, (int, float)) and v > 0:
                    out.append(float(v))
                elif isinstance(v, str):
                    cleaned = re.sub(r"[^\d]", "", v)
                    if cleaned:
                        out.append(float(cleaned))
            else:
                _collect_prices(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_prices(item, out, depth + 1)


def _best_price(candidates: list) -> Optional[int]:
    MIN_IDR, MAX_IDR = 1_000_000, 30_000_000
    normalised = []
    for v in candidates:
        if v > 1_000_000_000:
            v = v / 100000
        if MIN_IDR <= v <= MAX_IDR:
            normalised.append(int(round(v)))
    if not normalised:
        return None
    normalised.sort()
    return normalised[len(normalised) // 2]


def _price_from_json_ld(content: str) -> Optional[int]:
    for script in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        content, re.DOTALL
    ):
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue
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


def _price_from_og_meta(content: str) -> Optional[int]:
    m = re.search(
        r'<meta[^>]+property=["\']og:price:amount["\'][^>]+content=["\']([^"\']+)["\']',
        content, re.I
    )
    if m:
        try:
            price = float(re.sub(r"[^\d.]", "", m.group(1)))
            if 1_000_000 <= price <= 30_000_000:
                return int(round(price))
        except (ValueError, TypeError):
            pass
    return None


def _price_from_text_regex(content: str) -> Optional[int]:
    """
    Works on both HTML and plain text / markdown (Serper often returns markdown).
    """
    try:
        if "<html" in content.lower() or "<div" in content.lower():
            soup = BeautifulSoup(content, "lxml")
            text = soup.get_text(" ", strip=True)
        else:
            text = content
    except Exception:
        text = content

    candidates = []
    for m in re.finditer(r"Rp\.?\s*([\d]{1,2}(?:[.,]\d{3})+)", text):
        raw = re.sub(r"[.,]", "", m.group(1))
        try:
            v = int(raw)
            if 1_000_000 <= v <= 30_000_000:
                candidates.append(v)
        except ValueError:
            continue
    return _best_price([float(c) for c in candidates]) if candidates else None


# ── Stock / name helpers ──────────────────────────────────────────────────────

def _stock_from_content(content: str) -> str:
    text = content.lower()
    if any(w in text for w in ["habis", "out of stock", "sold out", "stok habis", "kosong"]):
        return "out-stock"
    if any(w in text for w in ["sisa", "limited", "hampir habis", "tersisa"]):
        return "low-stock"
    return "in-stock"


def _name_from_content(content: str, fallback: str) -> str:
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{5,120})["\']',
        content, re.I
    )
    if m:
        return m.group(1).strip()
    m = re.search(r'<title[^>]*>(.*?)</title>', content, re.I | re.DOTALL)
    if m:
        name = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(name) > 5:
            return name[:80]
    # Serper markdown: first H1 heading is usually the product title
    m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if m:
        return m.group(1).strip()[:80]
    return fallback


def _classify_variant(name: str) -> str:
    n = name.lower()
    if any(w in n for w in ["mario kart", "bundle", "paket", "world", "mk world"]):
        return "With Mario Kart"
    return "Standard"


def _clean_name(raw: str) -> str:
    name = re.sub(r'\s+', ' ', raw).strip()
    for sep in [' - ', ' | ', ' / ', ',']:
        if len(name) > 70 and sep in name:
            name = name.split(sep)[0].strip()
    return name[:80]


# ── Main per-listing fetcher ──────────────────────────────────────────────────

def _fetch_one(listing: dict, cfg: Config) -> dict:
    url      = listing["url"]
    platform = listing["platform"]
    seller   = listing["seller"]

    logger.info(f"   {url}")

    def call():
        return _fetch_content(url, cfg)

    content = _with_retry(call, cfg.retry_attempts, cfg.retry_backoff)

    logger.debug(f"   content length: {len(content):,} chars")

    price = (
        _price_from_next_data(content) or
        _price_from_json_ld(content)   or
        _price_from_og_meta(content)   or
        _price_from_text_regex(content)
    )

    if not price:
        raise ValueError(
            f"No price found in {len(content):,} chars of content"
        )

    stock   = _stock_from_content(content)
    name    = _clean_name(_name_from_content(content, f"Nintendo Switch 2 — {seller}"))
    variant = _classify_variant(name)

    logger.info(f"   ✓ Rp{price:,}  {stock}  {variant}")
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
    result        = FetchResult()
    result.source = "serper-serpapi"
    listings      = []
    fail_count    = 0

    if not cfg.serper_key and not cfg.serpapi_key:
        logger.warning("No API keys configured — using demo data.")
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

    total = len(TRACKED_LISTINGS)
    logger.info(
        f"\nFetch complete — {len(listings)}/{total} succeeded, "
        f"{fail_count} failed"
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
        result.error    = "All fetch attempts returned no price data"

    return result
