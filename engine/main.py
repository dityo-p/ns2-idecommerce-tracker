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



def diagnose(cfg: Config) -> None:
    """
    Print a full diagnostic report — useful when the dashboard shows demo data.
    Checks keys, data_dir, DB, and JSON file contents.
    """
    import json, os
    W = "\033[93m"  # yellow
    G = "\033[92m"  # green
    R = "\033[91m"  # red
    B = "\033[94m"  # blue
    E = "\033[0m"   # reset

    print(f"\n{B}{'═'*60}")
    print(f"  NS2 Price Tracker — Diagnostics")
    print(f"{'═'*60}{E}\n")

    # 1. API Keys
    print(f"{B}[1] API Keys{E}")
    if cfg.serpapi_key:
        masked = cfg.serpapi_key[:6] + "…" + cfg.serpapi_key[-4:]
        print(f"  {G}✅ SERPAPI_KEY set{E}  ({masked})")
    else:
        print(f"  {R}❌ SERPAPI_KEY not set{E}")

    if cfg.serper_key:
        masked = cfg.serper_key[:6] + "…" + cfg.serper_key[-4:]
        print(f"  {G}✅ SERPER_KEY set{E}  ({masked})")
    else:
        print(f"  {R}❌ SERPER_KEY not set{E}")

    if not cfg.serpapi_key and not cfg.serper_key:
        print(f"\n  {W}→ FIX: Add SERPAPI_KEY or SERPER_KEY as a GitHub Secret{E}")
        print(f"  {W}  Settings → Secrets and variables → Actions → New repository secret{E}\n")
    else:
        print()

    # 2. Database
    print(f"{B}[2] Database{E}")
    print(f"  URL: {cfg.database_url}")
    db_path = cfg.database_url.replace("sqlite:///", "")
    if cfg.database_url.startswith("sqlite"):
        if os.path.exists(db_path):
            size = os.path.getsize(db_path)
            print(f"  {G}✅ DB file exists{E}  ({size:,} bytes  →  {db_path})")
        else:
            print(f"  {W}⚠️  DB file not found yet — will be created on first run{E}")
            print(f"  Path: {os.path.abspath(db_path)}")

    # 3. Output directory
    print(f"\n{B}[3] Data output directory{E}")
    data_dir = os.path.abspath(cfg.data_dir)
    print(f"  Resolved path: {data_dir}")
    if os.path.isdir(data_dir):
        print(f"  {G}✅ Directory exists{E}")
        for fname in ["prices.json", "history.json", "meta.json"]:
            fpath = os.path.join(data_dir, fname)
            if os.path.exists(fpath):
                size = os.path.getsize(fpath)
                try:
                    with open(fpath) as f:
                        data = json.load(f)
                    is_demo = data.get("is_demo", False)
                    updated = data.get("updated_at", "unknown")
                    count   = len(data.get("listings", data.get("records", [])))
                    demo_flag = f"  {W}[DEMO]{E}" if is_demo else f"  {G}[LIVE]{E}"
                    print(f"  {G}✅{E} {fname:<18} {size:>6,} bytes  updated={updated[:19]}{demo_flag}  items={count}")
                except Exception as e:
                    print(f"  {W}⚠️{E}  {fname:<18} {size:>6,} bytes  (parse error: {e})")
            else:
                print(f"  {R}❌{E} {fname} — not found")
    else:
        print(f"  {R}❌ Directory does not exist: {data_dir}{E}")
        print(f"  {W}→ FIX: Create it with:  mkdir -p \"{data_dir}\"{E}")

    # 4. Recent fetch logs
    print(f"\n{B}[4] Recent fetch history{E}")
    try:
        db_engine = get_engine(cfg.database_url)
        session   = get_session(db_engine)
        logs      = get_fetch_logs(session, limit=5)
        listings  = get_latest_listings(session)
        session.close()
        if logs:
            for log in logs:
                icon = G+"✅"+E if log["success"] else R+"❌"+E
                ts   = (log["started_at"] or "")[:19]
                print(f"  {icon} {ts}  source={log['source'] or '?':<10} found={log['listings_found']}  demo={'yes' if log['source']=='demo' else 'no'}")
                if log["error"]:
                    print(f"       error: {log['error']}")
        else:
            print(f"  {W}No fetch history yet — run  python main.py  first{E}")
        if listings:
            print(f"\n  Stored listings ({len(listings)}):")
            for l in listings[:6]:
                src_flag = f"  {W}[demo]{E}" if l.get("source") == "demo" else ""
                print(f"    {l['seller']:<24} {l['platform']:<12} Rp {l['price']:>10,.0f}{src_flag}")
    except Exception as e:
        print(f"  {R}Could not read DB: {e}{E}")

    # 5. Summary verdict
    print(f"\n{B}[5] Verdict{E}")
    has_key = cfg.serpapi_key or cfg.serper_key
    data_dir_exists = os.path.isdir(os.path.abspath(cfg.data_dir))
    prices_path = os.path.join(os.path.abspath(cfg.data_dir), "prices.json")
    prices_is_live = False
    if os.path.exists(prices_path):
        try:
            with open(prices_path) as f:
                prices_is_live = not json.load(f).get("is_demo", True)
        except Exception:
            pass

    if has_key and prices_is_live:
        print(f"  {G}🎉 Everything looks good — dashboard should show live data!{E}")
    else:
        issues = []
        if not has_key:
            issues.append("No API key → engine outputs demo data")
        if not data_dir_exists:
            issues.append("Data directory missing → JSON not written")
        if not prices_is_live:
            issues.append("prices.json contains demo data → set a real API key and re-run")
        for i in issues:
            print(f"  {R}⚠️  {i}{E}")
        print(f"\n  {W}Quick fix steps:{E}")
        if not has_key:
            print(f"  {W}  1. Get a free key at serper.dev (2,500/month){E}")
            print(f"  {W}  2. In GitHub: Settings → Secrets → Actions → New secret{E}")
            print(f"  {W}     Name: SERPER_KEY   Value: your-key-here{E}")
            print(f"  {W}  3. Re-run the workflow: Actions → Fetch prices → Run workflow{E}")

    print(f"\n{B}{'═'*60}{E}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="NS2 Price Tracker Engine")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--daemon",        action="store_true", help="Run continuously on schedule")
    group.add_argument("--export-only",   action="store_true", help="Re-export DB without fetching")
    group.add_argument("--clear-history", action="store_true", help="Wipe price history table")
    group.add_argument("--status",        action="store_true", help="Print latest fetch logs")
    group.add_argument("--diagnose",      action="store_true", help="Full diagnostic report")
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

    elif args.diagnose:
        diagnose(cfg)

    else:
        # Default: run once (used by GitHub Actions cron)
        success = run_once(cfg)
        sys.exit(0 if success else 1)
