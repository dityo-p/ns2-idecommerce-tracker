"""
Main runner — entry point for the price engine.

Usage:
    python main.py              # run once (default, used by GitHub Actions)
    python main.py --daemon     # run continuously on a schedule
    python main.py --export-only  # re-export existing DB data without fetching
    python main.py --clear-history  # wipe history table then exit
    python main.py --status     # print last N fetch logs and exit

Environment variables (set in .env or GitHub Secrets):
    SERPAPI_KEY            SerpApi key (primary)
    SERPER_KEY             Serper.dev key (fallback)
    DATABASE_URL           SQLAlchemy URL (default: sqlite:///ns2_tracker.db)
    DATA_DIR               Output dir for JSON files (default: dashboard/data)
    FETCH_INTERVAL_MINUTES Daemon interval in minutes (default: 60)
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from config import Config
from models import get_engine, get_session
from fetcher import fetch_all
from store import (
    seed_sellers, upsert_listings, append_history,
    start_fetch_log, finish_fetch_log,
    get_latest_listings, get_history, get_fetch_logs, clear_history,
)
from exporter import export_all

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("engine.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("runner")


# ── Core run cycle ────────────────────────────────────────────────────────────

def run_once(cfg: Config) -> bool:
    """
    One full fetch → store → export cycle.
    Returns True on success (even if demo data was used).
    """
    engine  = get_engine(cfg.database_url)
    session = get_session(engine)

    # Ensure sellers table is populated
    seed_sellers(session)

    log = start_fetch_log(session)
    logger.info("=" * 60)
    logger.info(f"Fetch cycle started at {datetime.utcnow().isoformat()}Z")
    logger.info(f"API keys — SerpApi: {'✓' if cfg.has_serpapi() else '✗'}  Serper: {'✓' if cfg.has_serper() else '✗'}")

    # 1. Fetch
    result = fetch_all(cfg)

    new_count = updated_count = 0

    if result.listings:
        # 2. Upsert latest listings
        new_count, updated_count = upsert_listings(session, result.listings, result.source)

        # 3. Append history (skip demo data — no point logging fake prices)
        if not result.is_demo:
            append_history(session, result.listings, result.source)

    # 4. Read back from DB (ensures we serve committed, consistent data)
    db_listings = get_latest_listings(session)
    db_history  = get_history(session, limit=1000)
    db_logs     = get_fetch_logs(session, limit=10)

    # 5. Write JSON files
    export_all(
        listings   = db_listings,
        history    = db_history,
        source     = result.source,
        data_dir   = cfg.data_dir,
        is_demo    = result.is_demo,
        fetch_logs = db_logs,
    )

    # 6. Finalise audit log
    finish_fetch_log(
        session,
        log,
        source         = result.source,
        queries_fired  = result.queries_fired,
        listings_found = len(result.listings),
        new_count      = new_count,
        updated_count  = updated_count,
        success        = result.success,
        error          = result.error,
    )

    session.close()

    logger.info(
        f"Cycle complete — source={result.source}  "
        f"listings={len(result.listings)}  new={new_count}  updated={updated_count}  "
        f"demo={result.is_demo}"
    )
    logger.info("=" * 60)
    return result.success


def run_daemon(cfg: Config) -> None:
    """Run fetch cycles on a fixed interval until interrupted."""
    interval = cfg.fetch_interval_minutes * 60
    logger.info(f"Daemon mode — interval: {cfg.fetch_interval_minutes} min")
    while True:
        try:
            run_once(cfg)
        except Exception as exc:
            logger.error(f"Unhandled exception in run cycle: {exc}", exc_info=True)
        logger.info(f"Next fetch in {cfg.fetch_interval_minutes} min…")
        time.sleep(interval)


def export_only(cfg: Config) -> None:
    """Re-export current DB data without fetching."""
    engine  = get_engine(cfg.database_url)
    session = get_session(engine)
    db_listings = get_latest_listings(session)
    db_history  = get_history(session, limit=1000)
    db_logs     = get_fetch_logs(session, limit=10)
    export_all(
        listings   = db_listings,
        history    = db_history,
        source     = "db-reexport",
        data_dir   = cfg.data_dir,
        is_demo    = False,
        fetch_logs = db_logs,
    )
    session.close()
    logger.info("Re-export complete.")


def print_status(cfg: Config) -> None:
    engine  = get_engine(cfg.database_url)
    session = get_session(engine)
    logs    = get_fetch_logs(session, limit=10)
    listings = get_latest_listings(session)
    session.close()

    print(f"\n{'─'*62}")
    print(f"  NS2 Price Tracker — Status")
    print(f"  DB: {cfg.database_url}")
    print(f"{'─'*62}")
    print(f"  Current listings ({len(listings)}):")
    for l in listings:
        print(f"    {l['seller']:<24} {l['platform']:<12} Rp {l['price']:>10,.0f}  {l['stock']}")
    print(f"\n  Recent fetch logs:")
    for log in logs:
        status = "✓" if log["success"] else "✗"
        ts = (log["started_at"] or "")[:19]
        print(
            f"    {status} {ts}  source={log['source'] or '?':<10}  "
            f"found={log['listings_found']}  "
            f"duration={log['duration_s'] or '?'}s"
        )
        if log["error"]:
            print(f"        error: {log['error']}")
    print(f"{'─'*62}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="NS2 Price Tracker Engine")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--daemon",        action="store_true", help="Run continuously on schedule")
    group.add_argument("--export-only",   action="store_true", help="Re-export DB without fetching")
    group.add_argument("--clear-history", action="store_true", help="Wipe price history table")
    group.add_argument("--status",        action="store_true", help="Print latest fetch logs")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg  = Config()

    if args.daemon:
        run_daemon(cfg)

    elif args.export_only:
        export_only(cfg)

    elif args.clear_history:
        engine  = get_engine(cfg.database_url)
        session = get_session(engine)
        n = clear_history(session)
        session.close()
        print(f"Cleared {n} history rows.")

    elif args.status:
        print_status(cfg)

    else:
        # Default: run once (used by GitHub Actions cron)
        success = run_once(cfg)
        sys.exit(0 if success else 1)
