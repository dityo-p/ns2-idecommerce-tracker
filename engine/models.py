"""
Database models for the NS2 Price Tracker engine.
Uses SQLite locally, PostgreSQL in production (same ORM, different URL).
"""

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
    platform   = Column(String(40),  nullable=False)   # Tokopedia | Shopee | BliBli
    base_url   = Column(String(500), nullable=True)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    listings   = relationship("Listing", back_populates="seller_rel", lazy="dynamic")

    def __repr__(self):
        return f"<Seller {self.name} @ {self.platform}>"


class Listing(Base):
    """
    Latest price snapshot per seller.
    One row per seller — upserted on every fetch cycle.
    """
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
    source      = Column(String(40),  nullable=True)   # serpapi | serper | scraper

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
    """
    Immutable append-only log — one row per seller per fetch cycle.
    Powers the history chart and CSV export.
    """
    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_history_seller", "seller_name"),
        Index("ix_history_ts",     "recorded_at"),
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
            "ts":          self.recorded_at.isoformat() if self.recorded_at else None,
            "seller":      self.seller_name,
            "platform":    self.platform,
            "product":     self.product,
            "variant":     self.variant or "Standard",
            "price":       int(self.price),
            "prev_price":  int(self.prev_price) if self.prev_price else None,
            "stock":       self.stock,
            "url":         self.url or "",
            "source":      self.source,
        }


class FetchLog(Base):
    """Audit log for every fetch run — useful for debugging and rate-limit tracking."""
    __tablename__ = "fetch_logs"

    id            = Column(Integer, primary_key=True)
    started_at    = Column(DateTime, default=datetime.utcnow)
    finished_at   = Column(DateTime, nullable=True)
    source        = Column(String(40),  nullable=True)    # serpapi | serper | demo
    queries_fired = Column(Integer,  default=0)
    listings_found= Column(Integer,  default=0)
    listings_new  = Column(Integer,  default=0)
    listings_updated = Column(Integer, default=0)
    success       = Column(Boolean,  default=False)
    error_message = Column(Text,     nullable=True)

    def duration_seconds(self):
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


def get_engine(db_url: str = "sqlite:///ns2_tracker.db"):
    """
    Return a SQLAlchemy engine.
    - SQLite: for local dev and GitHub Actions
    - PostgreSQL: for production (set DATABASE_URL env var)
    """
    kwargs = {}
    if db_url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(db_url, echo=False, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
