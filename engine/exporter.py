"""
JSON exporter — writes the three static files consumed by the dashboard:

  dashboard/data/prices.json    ← latest listing snapshot (live table)
  dashboard/data/history.json   ← full price history (chart + history tab)
  dashboard/data/meta.json      ← last-updated, source, summary stats

The dashboard fetches these via a simple fetch('/data/prices.json') call.
On GitHub Pages they are served as static assets — no server needed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _write_json(filepath: str, data: object) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"Written: {filepath} ({os.path.getsize(filepath)} bytes)")


def _compute_stats(listings: list[dict]) -> dict:
    if not listings:
        return {"min": None, "max": None, "avg": None, "count": 0}
    prices = [l["price"] for l in listings]
    return {
        "min":   min(prices),
        "max":   max(prices),
        "avg":   round(sum(prices) / len(prices)),
        "count": len(listings),
    }


def export_all(
    listings:  list[dict],
    history:   list[dict],
    source:    str,
    data_dir:  str,
    is_demo:   bool = False,
    fetch_logs: Optional[list[dict]] = None,
) -> None:
    """
    Write all three JSON files to `data_dir`.
    Called once per fetch cycle by the main runner.
    """
    _ensure_dir(data_dir)

    now_utc = datetime.now(timezone.utc).isoformat()

    # ── prices.json ───────────────────────────────────────────────────────────
    prices_path = os.path.join(data_dir, "prices.json")
    _write_json(prices_path, {
        "updated_at": now_utc,
        "source":     source,
        "is_demo":    is_demo,
        "listings":   listings,
    })

    # ── history.json ──────────────────────────────────────────────────────────
    history_path = os.path.join(data_dir, "history.json")
    _write_json(history_path, {
        "updated_at": now_utc,
        "total":      len(history),
        "records":    history,
    })

    # ── meta.json ─────────────────────────────────────────────────────────────
    meta_path = os.path.join(data_dir, "meta.json")
    stats     = _compute_stats(listings)

    # Build per-platform breakdown
    platforms: dict[str, list[int]] = {}
    for l in listings:
        platforms.setdefault(l["platform"], []).append(l["price"])
    platform_stats = {
        p: {
            "count": len(prices),
            "min":   min(prices),
            "max":   max(prices),
            "avg":   round(sum(prices) / len(prices)),
        }
        for p, prices in platforms.items()
    }

    _write_json(meta_path, {
        "updated_at":     now_utc,
        "source":         source,
        "is_demo":        is_demo,
        "stats":          stats,
        "by_platform":    platform_stats,
        "history_count":  len(history),
        "recent_logs":    fetch_logs or [],
    })

    logger.info(
        f"Export complete → {data_dir}  "
        f"[{len(listings)} listings, {len(history)} history rows, demo={is_demo}]"
    )
