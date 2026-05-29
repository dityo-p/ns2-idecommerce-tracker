"""
Central config — reads from environment variables (or .env file in dev).
Never commit real keys; use GitHub Actions Secrets in CI.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── API Keys ──────────────────────────────────────────────
    serpapi_key:   Optional[str] = field(default_factory=lambda: os.getenv("SERPAPI_KEY"))
    serper_key:    Optional[str] = field(default_factory=lambda: os.getenv("SERPER_KEY"))

    # ── Database ──────────────────────────────────────────────
    database_url:  str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///ns2_tracker.db"))

    # ── Output paths ──────────────────────────────────────────
    data_dir: str = field(default_factory=lambda: os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "data")
    ))
    prices_json:   str = "prices.json"
    history_json:  str = "history.json"
    meta_json:     str = "meta.json"

    # ── Fetch behaviour ───────────────────────────────────────
    request_timeout:    int   = 15
    retry_attempts:     int   = 3
    retry_backoff:      float = 2.0
    min_price_idr:      int   = 1_000_000
    max_price_idr:      int   = 20_000_000

    # ── Scheduler ─────────────────────────────────────────────
    fetch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("FETCH_INTERVAL_MINUTES", "60"))
    )

    def has_serpapi(self)  -> bool: return bool(self.serpapi_key)
    def has_serper(self)   -> bool: return bool(self.serper_key)
    def has_any_key(self)  -> bool: return self.has_serpapi() or self.has_serper()


# ── Official sellers ───────────────────────────────────────────────────────────
#
# Each seller has:
#   name         Display name shown in the dashboard
#   platform     Tokopedia | Shopee | BliBli
#   store_slug   The seller's store username/ID as it appears in their store URL.
#                Used to build inurl: queries that actually work on these platforms.
#   url          Direct store search URL (fallback link in dashboard)
#
# How to find store_slug:
#   Tokopedia → open the seller's store page → URL is tokopedia.com/{store_slug}
#   Shopee    → open the seller's store page → URL is shopee.co.id/{store_slug}
#   BliBli    → open the seller's store page → URL is blibli.com/merchant/{store_slug}
#
OFFICIAL_SELLERS = [
    {
        "name":       "PS Enterprise",
        "platform":   "Tokopedia",
        "store_slug": "psegameshop",
        "url":        "https://www.tokopedia.com/psegameshop",
    },
    {
        "name":       "GSShop",
        "platform":   "Tokopedia",
        "store_slug": "gsshop",
        "url":        "https://www.tokopedia.com/gsshop",
    },
    {
        "name":       "Drakuli",
        "platform":   "Shopee",
        "store_slug": "drakuli",
        "url":        "https://shopee.co.id/drakuli",
    },
    {
        "name":       "Supersonicgamestore",
        "platform":   "Shopee",
        "store_slug": "supersonicgamestore",
        "url":        "https://shopee.co.id/supersonicgamestore",
    },
    {
        "name":       "Gameku",
        "platform":   "Shopee",
        "store_slug": "gamekuid",
        "url":        "https://shopee.co.id/gamekuid",
    },
    {
        "name":       "Gamestation",
        "platform":   "BliBli",
        "store_slug": "gamestation",
        "url":        "https://www.blibli.com/merchant/gamestation",
    },
    {
        "name":       "iBox",
        "platform":   "BliBli",
        "store_slug": "ibox",
        "url":        "https://www.blibli.com/merchant/ibox",
    },
    {
        "name":       "iSolution",
        "platform":   "BliBli",
        "store_slug": "isolution",
        "url":        "https://www.blibli.com/merchant/isolution",
    },
    {
        "name":       "Wellcomm",
        "platform":   "Tokopedia",
        "store_slug": "wellcomm",
        "url":        "https://www.tokopedia.com/wellcomm",
    },
]

SELLER_NAMES = [s["name"] for s in OFFICIAL_SELLERS]

# Fallback demo data — used only when no API keys are configured
DEMO_LISTINGS = [
    {"seller": "PS Enterprise",       "platform": "Tokopedia", "product": "Nintendo Switch 2 Console",   "variant": "Standard",        "price": 6299000, "stock": "in-stock",  "url": "https://www.tokopedia.com/psegameshop"},
    {"seller": "GSShop",              "platform": "Tokopedia", "product": "Nintendo Switch 2 + Joy-Con", "variant": "Standard",        "price": 6399000, "stock": "in-stock",  "url": "https://www.tokopedia.com/gsshop"},
    {"seller": "Drakuli",             "platform": "Shopee",    "product": "Nintendo Switch 2",           "variant": "Standard",        "price": 6350000, "stock": "low-stock", "url": "https://shopee.co.id/drakuli"},
    {"seller": "Supersonicgamestore", "platform": "Shopee",    "product": "Nintendo Switch 2 Bundle",    "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": "https://shopee.co.id/supersonicgamestore"},
    {"seller": "Gamestation",         "platform": "BliBli",    "product": "Nintendo Switch 2",           "variant": "Standard",        "price": 6499000, "stock": "in-stock",  "url": "https://www.blibli.com/merchant/gamestation"},
    {"seller": "iBox",                "platform": "BliBli",    "product": "Nintendo Switch 2 Console",   "variant": "Standard",        "price": 6449000, "stock": "out-stock", "url": "https://www.blibli.com/merchant/ibox"},
]
