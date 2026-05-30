"""
Central config — reads from environment variables (or .env in dev).
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── API Keys ──────────────────────────────────────────────────────────────
    # Both are already in your GitHub Secrets.
    # SerpApi  → Google Shopping engine (primary, most reliable)
    # Serper   → Google Shopping fallback
    serpapi_key: Optional[str] = field(default_factory=lambda: os.getenv("SERPAPI_KEY"))
    serper_key:  Optional[str] = field(default_factory=lambda: os.getenv("SERPER_KEY"))

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "sqlite:///ns2_tracker.db"
    ))

    # ── Output paths ──────────────────────────────────────────────────────────
    data_dir: str = field(default_factory=lambda: os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "data")
    ))
    prices_json:  str = "prices.json"
    history_json: str = "history.json"
    meta_json:    str = "meta.json"

    # ── Fetch behaviour ───────────────────────────────────────────────────────
    request_timeout:    int   = 20
    retry_attempts:     int   = 3
    retry_backoff:      float = 2.0
    min_price_idr:      int   = 1_000_000
    max_price_idr:      int   = 30_000_000

    # ── Scheduler ─────────────────────────────────────────────────────────────
    fetch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("FETCH_INTERVAL_MINUTES", "60"))
    )

    def has_serpapi(self) -> bool: return bool(self.serpapi_key)
    def has_serper(self)  -> bool: return bool(self.serper_key)
    def has_any_key(self) -> bool: return self.has_serpapi() or self.has_serper()


# ── Platform domain map ───────────────────────────────────────────────────────
PLATFORM_DOMAIN = {
    "Tokopedia": "tokopedia.com",
    "Shopee":    "shopee.co.id",
    "BliBli":    "blibli.com",
}

# ─────────────────────────────────────────────────────────────────────────────
# TRACKED LISTINGS
#
# url          Exact product page (used as canonical link in dashboard)
# seller       Display name in dashboard
# platform     Tokopedia | Shopee | BliBli
# shop_slug    The store username / SKU fragment used to target
#              the Google Shopping query to this exact seller.
#              Tokopedia/Shopee → store username slug in the URL
#              BliBli           → first segment of the SKU (e.g. PSP-60021)
# ─────────────────────────────────────────────────────────────────────────────

TRACKED_LISTINGS = [
    # ── Tokopedia ─────────────────────────────────────────────────────────────
    {
        "url":       "https://www.tokopedia.com/teknotrend/nintendo-switch-2-ns2-ns-2-console-new-model-1731484030680598466",
        "seller":    "TeknoTrend",
        "platform":  "Tokopedia",
        "shop_slug": "teknotrend",
    },
    {
        "url":       "https://www.tokopedia.com/psenterprise/nintendo-switch-2-bonus-game-mario-kart-world-fisik-singapore-set-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2-layar-7-9-1080p-dock-4k-untuk-pengalaman-gaming-terbaik-1733000068976444710",
        "seller":    "PS Enterprise",
        "platform":  "Tokopedia",
        "shop_slug": "psenterprise",
    },
    {
        "url":       "https://www.tokopedia.com/super-gameshop/nintendo-switch-2-console-switch-2-nintendo-swicth2-console-1733090951679083545",
        "seller":    "Super Gameshop",
        "platform":  "Tokopedia",
        "shop_slug": "super-gameshop",
    },

    # ── Shopee ────────────────────────────────────────────────────────────────
    {
        "url":       "https://shopee.co.id/Nintendo-Switch-2-Console-Bonus-Game-Mario-Kart-World-Cartridge-Bergaransi-switch-2-i.507067058.27123873650",
        "seller":    "Drakuli",
        "platform":  "Shopee",
        "shop_slug": "drakuli",
    },
    {
        "url":       "https://shopee.co.id/PROMO!-Nintendo-Switch2-Console-Nintendo-Switch-2-Nintendo-2-Switch-2-Switch2-i.12523743.24545210190",
        "seller":    "Supersonicgamestore",
        "platform":  "Shopee",
        "shop_slug": "supersonicgamestore",
    },

    # ── BliBli ────────────────────────────────────────────────────────────────
    {
        "url":       "https://www.blibli.com/p/nintendo-switch-2-console-bonus-game-mario-kart-world-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2/is--PSP-60021-07373-00008",
        "seller":    "PS Enterprise",
        "platform":  "BliBli",
        "shop_slug": "PSP-60021-07373",
    },
    {
        "url":       "https://www.blibli.com/p/nintendo-switch-2-switch2-ns2-ns-2-nintendo-2-nintendo2-console-mesin/is--MYB-34264-01338-00002",
        "seller":    "Mayora",
        "platform":  "BliBli",
        "shop_slug": "MYB-34264-01338",
    },
    {
        "url":       "https://www.blibli.com/p/nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2/is--LIG-60027-03482-00004",
        "seller":    "Ligastore",
        "platform":  "BliBli",
        "shop_slug": "LIG-60027-03482",
    },
    {
        "url":       "https://www.blibli.com/p/nintendo-switch-2-bundle-mario-kart-world/ps--SUS-34874-02516",
        "seller":    "Supersonic",
        "platform":  "BliBli",
        "shop_slug": "SUS-34874",
    },
]

# Demo data — shown when no API keys are configured
DEMO_LISTINGS = [
    {"seller": "TeknoTrend",         "platform": "Tokopedia", "product": "Nintendo Switch 2 Console",        "variant": "Standard",        "price": 6299000, "stock": "in-stock",  "url": TRACKED_LISTINGS[0]["url"]},
    {"seller": "PS Enterprise",      "platform": "Tokopedia", "product": "Nintendo Switch 2 + Mario Kart",   "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[1]["url"]},
    {"seller": "Super Gameshop",     "platform": "Tokopedia", "product": "Nintendo Switch 2 Console",        "variant": "Standard",        "price": 6350000, "stock": "in-stock",  "url": TRACKED_LISTINGS[2]["url"]},
    {"seller": "Drakuli",            "platform": "Shopee",    "product": "Nintendo Switch 2 + Mario Kart",   "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[3]["url"]},
    {"seller": "Supersonicgamestore","platform": "Shopee",    "product": "Nintendo Switch 2 Console",        "variant": "Standard",        "price": 6399000, "stock": "low-stock", "url": TRACKED_LISTINGS[4]["url"]},
    {"seller": "PS Enterprise",      "platform": "BliBli",    "product": "Nintendo Switch 2 + Mario Kart",   "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[5]["url"]},
    {"seller": "Mayora",             "platform": "BliBli",    "product": "Nintendo Switch 2 Console",        "variant": "Standard",        "price": 6449000, "stock": "in-stock",  "url": TRACKED_LISTINGS[6]["url"]},
    {"seller": "Ligastore",          "platform": "BliBli",    "product": "Nintendo Switch 2 Console",        "variant": "Standard",        "price": 6499000, "stock": "in-stock",  "url": TRACKED_LISTINGS[7]["url"]},
    {"seller": "Supersonic",         "platform": "BliBli",    "product": "Nintendo Switch 2 Bundle",         "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[8]["url"]},
]
