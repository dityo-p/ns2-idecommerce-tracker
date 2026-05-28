"""
Central config — reads from environment variables (or .env file in dev).
Never commit real keys; use GitHub Actions Secrets in CI.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()  # loads .env in local dev; no-op in CI where env vars are injected directly


@dataclass
class Config:
    # ── API Keys ──────────────────────────────────────────────
    serpapi_key:   Optional[str] = field(default_factory=lambda: os.getenv("SERPAPI_KEY"))
    serper_key:    Optional[str] = field(default_factory=lambda: os.getenv("SERPER_KEY"))

    # ── Database ──────────────────────────────────────────────
    # SQLite by default; override with postgresql://user:pass@host/db
    database_url:  str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///ns2_tracker.db"))

    # ── Output paths ──────────────────────────────────────────
    # These files are written after every fetch and served by GitHub Pages
    data_dir:      str = field(default_factory=lambda: os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "data")
    ))
    prices_json:   str = "prices.json"      # latest snapshot → dashboard live table
    history_json:  str = "history.json"     # full history log → chart + history tab
    meta_json:     str = "meta.json"        # last-updated, source, stats

    # ── Fetch behaviour ───────────────────────────────────────
    request_timeout:    int = 15        # seconds per HTTP request
    retry_attempts:     int = 3
    retry_backoff:      float = 2.0     # seconds, doubles each retry
    min_price_idr:      int = 1_000_000
    max_price_idr:      int = 20_000_000

    # ── Scheduler (when running as daemon) ────────────────────
    # Cron is preferred in GitHub Actions; this is for local daemon mode
    fetch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("FETCH_INTERVAL_MINUTES", "60"))
    )

    def has_serpapi(self)  -> bool: return bool(self.serpapi_key)
    def has_serper(self)   -> bool: return bool(self.serper_key)
    def has_any_key(self)  -> bool: return self.has_serpapi() or self.has_serper()


# ── Official sellers to track ──────────────────────────────────────────────────
OFFICIAL_SELLERS = [
    {"name": "PS Enterprise",       "platform": "Tokopedia", "url": "https://www.tokopedia.com/search?q=nintendo+switch+2+PS+Enterprise"},
    {"name": "GSShop",              "platform": "Tokopedia", "url": "https://www.tokopedia.com/search?q=nintendo+switch+2+GSShop"},
    {"name": "Drakuli",             "platform": "Shopee",    "url": "https://shopee.co.id/search?keyword=nintendo+switch+2+drakuli"},
    {"name": "Supersonicgamestore", "platform": "Shopee",    "url": "https://shopee.co.id/search?keyword=nintendo+switch+2+supersonic"},
    {"name": "Gameku",              "platform": "Shopee",    "url": "https://shopee.co.id/search?keyword=nintendo+switch+2+gameku"},
    {"name": "Gamestation",         "platform": "BliBli",    "url": "https://www.blibli.com/jual/nintendo-switch-2"},
    {"name": "iBox",                "platform": "BliBli",    "url": "https://www.blibli.com/jual/nintendo-switch-2"},
    {"name": "iSolution",           "platform": "BliBli",    "url": "https://www.blibli.com/jual/nintendo-switch-2"},
    {"name": "Wellcomm",            "platform": "Tokopedia", "url": "https://www.tokopedia.com/search?q=nintendo+switch+2+wellcomm"},
]

SELLER_NAMES = [s["name"] for s in OFFICIAL_SELLERS]

# ── Search queries fired per fetch cycle ──────────────────────────────────────
SEARCH_QUERIES = [
    {
        "q":        "Nintendo Switch 2 harga resmi site:tokopedia.com PS Enterprise OR GSShop OR Wellcomm",
        "platform": "Tokopedia",
    },
    {
        "q":        "Nintendo Switch 2 harga resmi site:shopee.co.id Drakuli OR Supersonicgamestore OR Gameku",
        "platform": "Shopee",
    },
    {
        "q":        "Nintendo Switch 2 harga resmi site:blibli.com Gamestation OR iBox OR iSolution",
        "platform": "BliBli",
    },
    {
        "q":        "Nintendo Switch 2 Indonesia harga resmi 2025 toko resmi Tokopedia Shopee BliBli",
        "platform": None,   # let URL detection decide
    },
]

# Fallback demo data used when no API keys are set (keeps dashboard non-empty)
DEMO_LISTINGS = [
    {"seller": "PS Enterprise",       "platform": "Tokopedia", "product": "Nintendo Switch 2 Console",   "variant": "Standard",       "price": 6299000, "stock": "in-stock",  "url": "https://www.tokopedia.com/search?q=nintendo+switch+2"},
    {"seller": "GSShop",              "platform": "Tokopedia", "product": "Nintendo Switch 2 + Joy-Con", "variant": "Standard",       "price": 6399000, "stock": "in-stock",  "url": "https://www.tokopedia.com/search?q=nintendo+switch+2"},
    {"seller": "Drakuli",             "platform": "Shopee",    "product": "Nintendo Switch 2",           "variant": "Standard",       "price": 6350000, "stock": "low-stock", "url": "https://shopee.co.id/search?keyword=nintendo+switch+2"},
    {"seller": "Supersonicgamestore", "platform": "Shopee",    "product": "Nintendo Switch 2 Bundle",    "variant": "With Mario Kart","price": 6799000, "stock": "in-stock",  "url": "https://shopee.co.id/search?keyword=nintendo+switch+2"},
    {"seller": "Gamestation",         "platform": "BliBli",    "product": "Nintendo Switch 2",           "variant": "Standard",       "price": 6499000, "stock": "in-stock",  "url": "https://www.blibli.com/jual/nintendo-switch-2"},
    {"seller": "iBox",                "platform": "BliBli",    "product": "Nintendo Switch 2 Console",   "variant": "Standard",       "price": 6449000, "stock": "out-stock", "url": "https://www.blibli.com/jual/nintendo-switch-2"},
]
