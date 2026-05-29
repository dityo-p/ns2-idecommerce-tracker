"""
Database store — handles all read/write operations.

Responsibilities:
  - Upsert latest listings (one row per seller+platform)
  - Append immutable price history rows
  - Write fetch audit logs
  - Seed official sellers table on first run
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import Seller, Listing, PriceHistory, FetchLog, get_engine, get_session
from config import Config, TRACKED_LISTINGS

logger = logging.getLogger(__name__)



# ── Schema migration ──────────────────────────────────────────────────────────

def migrate_sellers_table(engine) -> None:
    """
    Fix stale sellers table if it was cached from an older DB version.

    Old schema: UNIQUE(name)           -- breaks when same seller name appears
                                          on multiple platforms.
    New schema: UNIQUE(name, platform) -- correct.

    Detects the old constraint via sqlite_master, drops the table if stale,
    then calls create_all to rebuild with the correct schema.
    Seller rows are trivially re-seeded from TRACKED_LISTINGS.
    """
    from sqlalchemy import text
    from models import Base

    with engine.connect() as conn:
        try:
            row = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='sellers'"
            )).fetchone()

            if row is None:
                return   # table doesn't exist yet

            create_sql = (row[0] or "").upper()
            has_correct = (
                "UQ_SELLER_NAME_PLATFORM" in create_sql or
                ("UNIQUE" in create_sql and "PLATFORM" in create_sql.split("UNIQUE", 1)[-1])
            )

            if not has_correct:
                logger.warning(
                    "Stale sellers schema detected (UNIQUE on name only). "
                    "Dropping and recreating."
                )
                conn.execute(text("DROP TABLE sellers"))
                conn.commit()
                logger.info("Dropped stale sellers table.")

        except Exception as exc:
            logger.warning(f"Schema migration inspection failed (non-fatal): {exc}")

    # Always (re)create — no-op if table already correct
    try:
        Base.metadata.create_all(engine)
        logger.info("sellers table schema OK ✅")
    except Exception as exc:
        logger.warning(f"create_all after migration failed: {exc}")


# ── Seed ──────────────────────────────────────────────────────────────────────

def seed_sellers(session: Session) -> None:
    """
    Insert tracked listing sellers using raw INSERT OR IGNORE.
    This is immune to unique-constraint violations regardless of schema version,
    and avoids SQLAlchemy autoflush ordering issues.
    """
    from sqlalchemy import text
    from config import TRACKED_LISTINGS as _TL

    seen = set()
    with session.no_autoflush:
        for s in _TL:
            key = (s["seller"], s["platform"])
            if key in seen:
                continue
            seen.add(key)
            # INSERT OR IGNORE handles both old UNIQUE(name) and new UNIQUE(name,platform)
            # by simply skipping duplicates — no exception raised
            session.execute(text(
                "INSERT OR IGNORE INTO sellers (name, platform, base_url, is_active, created_at) "
                "VALUES (:name, :platform, :url, 1, datetime('now'))"
            ), {"name": s["seller"], "platform": s["platform"], "url": s.get("url", "")})
    session.commit()
    logger.info("Sellers table seeded.")

# ── Upsert listings ───────────────────────────────────────────────────────────

def upsert_listings(session: Session, listings: list[dict], source: str) -> tuple[int, int]:
    """
    For each listing:
      - If a row exists for (seller_name, platform): update price, prev_price, stock, fetched_at
      - Otherwise: insert new row

    Returns (new_count, updated_count).
    """
    new_count = updated_count = 0

    for raw in listings:
        existing: Optional[Listing] = (
            session.query(Listing)
            .filter_by(seller_name=raw["seller"], platform=raw["platform"])
            .first()
        )

        if existing:
            # Only record prev_price if price actually changed
            if existing.price != raw["price"]:
                existing.prev_price = existing.price
            existing.price      = raw["price"]
            existing.stock      = raw.get("stock", "in-stock")
            existing.product    = raw.get("product", existing.product)
            existing.variant    = raw.get("variant", existing.variant)
            existing.url        = raw.get("url", existing.url)
            existing.fetched_at = datetime.utcnow()
            existing.source     = source
            updated_count += 1
        else:
            seller_row = session.query(Seller).filter_by(name=raw["seller"]).first()
            session.add(Listing(
                seller_id   = seller_row.id if seller_row else None,
                seller_name = raw["seller"],
                platform    = raw["platform"],
                product     = raw.get("product", "Nintendo Switch 2"),
                variant     = raw.get("variant", "Standard"),
                price       = raw["price"],
                prev_price  = None,
                stock       = raw.get("stock", "in-stock"),
                url         = raw.get("url", ""),
                source      = source,
                fetched_at  = datetime.utcnow(),
            ))
            new_count += 1

    session.commit()
    logger.info(f"Listings upserted — new: {new_count}, updated: {updated_count}")
    return new_count, updated_count


# ── Append history ────────────────────────────────────────────────────────────

def append_history(session: Session, listings: list[dict], source: str) -> None:
    """
    Append one PriceHistory row per listing.
    This table is append-only — never updated, never deleted (unless user clears).
    """
    now = datetime.utcnow()
    for raw in listings:
        session.add(PriceHistory(
            seller_name = raw["seller"],
            platform    = raw["platform"],
            product     = raw.get("product", "Nintendo Switch 2"),
            variant     = raw.get("variant", "Standard"),
            price       = raw["price"],
            prev_price  = raw.get("prev_price"),
            stock       = raw.get("stock", "in-stock"),
            url         = raw.get("url", ""),
            source      = source,
            recorded_at = now,
        ))
    session.commit()
    logger.info(f"Appended {len(listings)} history rows.")


# ── Fetch log ─────────────────────────────────────────────────────────────────

def start_fetch_log(session: Session) -> FetchLog:
    log = FetchLog(started_at=datetime.utcnow())
    session.add(log)
    session.commit()
    return log


def finish_fetch_log(
    session: Session,
    log: FetchLog,
    source: str,
    queries_fired: int,
    listings_found: int,
    new_count: int,
    updated_count: int,
    success: bool,
    error: Optional[str] = None,
) -> None:
    log.finished_at      = datetime.utcnow()
    log.source           = source
    log.queries_fired    = queries_fired
    log.listings_found   = listings_found
    log.listings_new     = new_count
    log.listings_updated = updated_count
    log.success          = success
    log.error_message    = error
    session.commit()


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_latest_listings(session: Session) -> list[dict]:
    rows = session.query(Listing).order_by(Listing.price).all()
    return [r.to_dict() for r in rows]


def get_history(session: Session, limit: int = 500) -> list[dict]:
    rows = (
        session.query(PriceHistory)
        .order_by(PriceHistory.recorded_at.desc())
        .limit(limit)
        .all()
    )
    return [r.to_dict() for r in rows]


def get_fetch_logs(session: Session, limit: int = 20) -> list[dict]:
    rows = (
        session.query(FetchLog)
        .order_by(FetchLog.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "started_at":    r.started_at.isoformat() if r.started_at else None,
            "finished_at":   r.finished_at.isoformat() if r.finished_at else None,
            "source":        r.source,
            "queries_fired": r.queries_fired,
            "listings_found":r.listings_found,
            "new":           r.listings_new,
            "updated":       r.listings_updated,
            "success":       r.success,
            "error":         r.error_message,
            "duration_s":    r.duration_seconds(),
        }
        for r in rows
    ]


def clear_history(session: Session) -> int:
    deleted = session.query(PriceHistory).delete()
    session.commit()
    logger.info(f"Cleared {deleted} history rows.")
    return deleted
