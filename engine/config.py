"""
Central config — reads from environment variables (or .env in dev).
Never commit real keys; use GitHub Actions Secrets in CI.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── API Keys ──────────────────────────────────────────────────────────────────
    # Both keys are already in your GitHub Secrets.
    # Serper is tried first (POST /scrape endpoint).
    # SerpApi is the fallback (Google cache fetch).
    serper_key:    Optional[str] = field(default_factory=lambda: os.getenv("SERPER_KEY"))
    serpapi_key:   Optional[str] = field(default_factory=lambda: os.getenv("SERPAPI_KEY"))

    # ── Database ──────────────────────────────────────────────
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "sqlite:///ns2_tracker.db"
    ))

    # ── Output paths ──────────────────────────────────────────
    data_dir: str = field(default_factory=lambda: os.getenv(
        "DATA_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard", "data")
    ))
    prices_json:  str = "prices.json"
    history_json: str = "history.json"
    meta_json:    str = "meta.json"

    # ── HTTP behaviour ────────────────────────────────────────
    request_timeout:    int   = 15
    retry_attempts:     int   = 3
    retry_backoff:      float = 2.0
    min_price_idr:      int   = 1_000_000
    max_price_idr:      int   = 30_000_000   # bumped — Switch 2 bundles can be high

    # ── Scheduler ─────────────────────────────────────────────
    fetch_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("FETCH_INTERVAL_MINUTES", "60"))
    )

    def has_serpapi(self) -> bool: return bool(self.serpapi_key)
    def has_serper(self)  -> bool: return bool(self.serper_key)
    def has_any_key(self) -> bool: return self.has_serpapi() or self.has_serper()


# ─────────────────────────────────────────────────────────────────────────────
# TRACKED LISTINGS
#
# Each entry is a specific product page URL supplied by the user.
# The fetcher extracts: price, stock status, product name, and seller name
# directly from the platform's own internal API — no search queries needed.
#
# Fields:
#   url          Exact product page URL (source of truth)
#   seller       Display name shown in dashboard
#   platform     Tokopedia | Shopee | BliBli
#
# To add a new listing: paste the product page URL and fill in seller/platform.
# To remove a listing: delete the entry.
# ─────────────────────────────────────────────────────────────────────────────

TRACKED_LISTINGS = [
    # ── Tokopedia ──────────────────────────────────────────────────────────────
    {
        "url":      "https://www.tokopedia.com/teknotrend/nintendo-switch-2-ns2-ns-2-console-new-model-1731484030680598466",
        "seller":   "TeknoTrend",
        "platform": "Tokopedia",
    },
    {
        "url":      "https://www.tokopedia.com/psenterprise/nintendo-switch-2-bonus-game-mario-kart-world-fisik-singapore-set-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2-layar-7-9-1080p-dock-4k-untuk-pengalaman-gaming-terbaik-1733000068976444710",
        "seller":   "PS Enterprise",
        "platform": "Tokopedia",
    },
    {
        "url":      "https://www.tokopedia.com/super-gameshop/nintendo-switch-2-console-switch-2-nintendo-swicth2-console-1733090951679083545",
        "seller":   "Super Gameshop",
        "platform": "Tokopedia",
    },

    # ── Shopee ────────────────────────────────────────────────────────────────
    {
        "url":      "https://shopee.co.id/Nintendo-Switch-2-Console-Bonus-Game-Mario-Kart-World-Cartridge-Bergaransi-switch-2-i.507067058.27123873650",
        "seller":   "Drakuli",
        "platform": "Shopee",
    },
    {
        "url":      "https://shopee.co.id/PROMO!-Nintendo-Switch2-Console-Nintendo-Switch-2-Nintendo-2-Switch-2-Switch2-i.12523743.24545210190",
        "seller":   "Supersonicgamestore",
        "platform": "Shopee",
    },

    # ── BliBli ────────────────────────────────────────────────────────────────
    {
        "url":      "https://www.blibli.com/p/nintendo-switch-2-console-bonus-game-mario-kart-world-switch-2-nintendo-2-nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2/is--PSP-60021-07373-00008",
        "seller":   "PS Enterprise",
        "platform": "BliBli",
    },
    {
        "url":      "https://www.blibli.com/p/nintendo-switch-2-switch2-ns2-ns-2-nintendo-2-nintendo2-console-mesin/is--MYB-34264-01338-00002",
        "seller":   "Mayora",
        "platform": "BliBli",
    },
    {
        "url":      "https://www.blibli.com/p/nintendo-switch2-console-nintendo-switch-2-nintendo-2-switch-2-switch2-ns2-nsw2/is--LIG-60027-03482-00004",
        "seller":   "Ligastore",
        "platform": "BliBli",
    },
    {
        "url":      "https://www.blibli.com/p/nintendo-switch-2-bundle-mario-kart-world/ps--SUS-34874-02516",
        "seller":   "Supersonic",
        "platform": "BliBli",
    },
]

# Fallback demo data — used only when all fetches fail
DEMO_LISTINGS = [
    {"seller": "TeknoTrend",        "platform": "Tokopedia", "product": "Nintendo Switch 2",              "variant": "Standard",        "price": 6299000, "stock": "in-stock",  "url": TRACKED_LISTINGS[0]["url"]},
    {"seller": "PS Enterprise",     "platform": "Tokopedia", "product": "Nintendo Switch 2 + Mario Kart", "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[1]["url"]},
    {"seller": "Super Gameshop",    "platform": "Tokopedia", "product": "Nintendo Switch 2",              "variant": "Standard",        "price": 6350000, "stock": "in-stock",  "url": TRACKED_LISTINGS[2]["url"]},
    {"seller": "Drakuli",           "platform": "Shopee",    "product": "Nintendo Switch 2 + Mario Kart", "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[3]["url"]},
    {"seller": "Supersonicgamestore","platform": "Shopee",   "product": "Nintendo Switch 2",              "variant": "Standard",        "price": 6399000, "stock": "low-stock", "url": TRACKED_LISTINGS[4]["url"]},
    {"seller": "PS Enterprise",     "platform": "BliBli",    "product": "Nintendo Switch 2 + Mario Kart", "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[5]["url"]},
    {"seller": "Mayora",            "platform": "BliBli",    "product": "Nintendo Switch 2",              "variant": "Standard",        "price": 6449000, "stock": "in-stock",  "url": TRACKED_LISTINGS[6]["url"]},
    {"seller": "Ligastore",         "platform": "BliBli",    "product": "Nintendo Switch 2",              "variant": "Standard",        "price": 6499000, "stock": "in-stock",  "url": TRACKED_LISTINGS[7]["url"]},
    {"seller": "Supersonic",        "platform": "BliBli",    "product": "Nintendo Switch 2 Bundle",       "variant": "With Mario Kart", "price": 6799000, "stock": "in-stock",  "url": TRACKED_LISTINGS[8]["url"]},
]
