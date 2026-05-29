"""
Database models for the NS2 Price Tracker engine.
Uses SQLite locally, PostgreSQL in production (same ORM, different URL).
"""

import re
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Boolean, Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()


class Seller(Base):
    """Known official sellers we track."""
    __tablename__ = "sellers"
    __table_args__ = (UniqueConstraint('name', 'platform', name='uq_seller_name_platform'),)

    id         = Column(Integer, primary_key=True)
    name       = Column(String(120), nullable=False)
    platform   = Column(String(40),  nullable=False)
    base_url   = Column(String(500), nullable=True)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    listings   = relationship("Listing", back_populates="seller_rel", lazy="dynamic")

    def __repr__(self):
        return f"<Seller {self.name} @ {self.platform}>"


class Listing(Base):
    """Latest price snapshot per seller. One row per seller+platform, upserted each run."""
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("seller_name", "platform", name="uq_seller_platform"),
        Index("ix_listings_platform", "platform"),
        Index("ix_listings_price", "price"),
    )

    id          = Column(Integer, primary_key=True)
    seller_id   = Column(Integer, ForeignKey("sellers.id"), nullable=True)
    seller_name = Column(String(120), nullable=False)
    platform    = Column(String(40),  nullable=False)
    product     = Column(String(200), nullable=False)
    variant     = Column(String(100), nullable=True,  default="Standard")
    price       = Column(Float,       nullable=False)
    prev_price  = Column(Float,       nullable=True)
    stock       = Column(String(20),  nullable=False, default="in-stock")
    url         = Column(String(500), nullable=True)
    fetched_at  = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)
    source      = Column(String(40),  nullable=True)

    seller_rel  = relationship("Seller", back_populates="listings")

    def to_dict(self):
        return {
            "seller":     self.seller_name,
            "platform":   self.platform,
            "product":    self.product,
            "variant":    self.variant or "Standard",
            "price":      int(self.price),
            "prev_price": int(self.prev_price) if self.prev_price else None,
            "stock":      self.stock,
            "url":        self.url or "",
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source":     self.source,
        }

    def __repr__(self):
        return f"<Listing {self.seller_name} Rp{self.price:,.0f}>"


class PriceHistory(Base):
    """Immutable append-only log — one row per seller per fetch cycle."""
    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_history_seller",   "seller_name"),
        Index("ix_history_ts",       "recorded_at"),
        Index("ix_history_platform", "platform"),
    )

    id          = Column(Integer, primary_key=True)
    seller_name = Column(String(120), nullable=False)
    platform    = Column(String(40),  nullable=False)
    product     = Column(String(200), nullable=False)
    variant     = Column(String(100), nullable=True)
    price       = Column(Float,       nullable=False)
    prev_price  = Column(Float,       nullable=True)
    stock       = Column(String(20),  nullable=False)
    url         = Column(String(500), nullable=True)
    source      = Column(String(40),  nullable=True)
    recorded_at = Column(DateTime,    default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "ts":         self.recorded_at.isoformat() if self.recorded_at else None,
            "seller":     self.seller_name,
            "platform":   self.platform,
            "product":    self.product,
            "variant":    self.variant or "Standard",
            "price":      int(self.price),
            "prev_price": int(self.prev_price) if self.prev_price else None,
            "stock":      self.stock,
            "url":        self.url or "",
            "source":     self.source,
        }


class FetchLog(Base):
    """Audit log for every fetch run."""
    __tablename__ = "fetch_logs"

    id               = Column(Integer, primary_key=True)
    started_at       = Column(DateTime, default=datetime.utcnow)
    finished_at      = Column(DateTime, nullable=True)
    source           = Column(String(40),  nullable=True)
    queries_fired    = Column(Integer,  default=0)
    listings_found   = Column(Integer,  default=0)
    listings_new     = Column(Integer,  default=0)
    listings_updated = Column(Integer,  default=0)
    success          = Column(Boolean,  default=False)
    error_message    = Column(Text,     nullable=True)

    def duration_seconds(self):
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


def get_engine(db_url: str = "sqlite:///ns2_tracker.db"):
    """
    Return a SQLAlchemy engine.

    Before calling create_all, inspects the existing sellers table (if any)
    and drops it if it has the old single-column UNIQUE(name) constraint.
    This silently migrates stale GitHub Actions DB caches so they don't
    crash with IntegrityError when PS Enterprise is inserted for both
    Tokopedia and BliBli.

    Old schema:  name VARCHAR(120) NOT NULL UNIQUE   <- column-level, single col
    New schema:  CONSTRAINT uq_seller_name_platform UNIQUE (name, platform)
    """
    kwargs = {}
    if db_url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(db_url, echo=False, **kwargs)

    if db_url.startswith("sqlite"):
        _migrate_sellers_if_stale(engine)

    Base.metadata.create_all(engine)
    return engine


def _migrate_sellers_if_stale(engine) -> None:
    """
    Drop the sellers table when it has the old UNIQUE(name) constraint.
    Uses raw sqlite3 (bypassing SQLAlchemy ORM/session) so there is
    absolutely no autoflush or metadata-cache interference.
    """
    db_path = str(engine.url).replace("sqlite:///", "")
    import sqlite3, os
    if not os.path.exists(db_path):
        return   # fresh DB — nothing to migrate

    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sellers'"
        ).fetchone()
        con.close()

        if row is None:
            return   # sellers table doesn't exist yet

        sql_upper = (row[0] or "").upper()

        # Old schema has a column-level UNIQUE on name but no named CONSTRAINT block.
        # We detect "NOT NULL UNIQUE" near the name column definition.
        has_named_constraint = "CONSTRAINT" in sql_upper and "UQ_SELLER" in sql_upper
        has_column_unique    = "NOT NULL UNIQUE" in sql_upper

        if has_column_unique and not has_named_constraint:
            # Drop via raw sqlite3 — completely outside SQLAlchemy
            con = sqlite3.connect(db_path)
            con.execute("DROP TABLE sellers")
            con.commit()
            con.close()
    except Exception:
        pass   # non-fatal — if anything goes wrong, create_all will surface the real error


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
