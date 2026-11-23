"""Database models (SQLAlchemy 2.0). SQLite by default, PostgreSQL via DATABASE_URL."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Watch(Base):
    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    config: Mapped[dict] = mapped_column(JSON)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Last target value read from YAML; lets dashboard edits survive restarts until YAML changes.
    yaml_target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    offers: Mapped[list["OfferSnapshot"]] = relationship(back_populates="watch", cascade="all, delete-orphan")


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|partial|failed|skipped
    searches: Mapped[int] = mapped_column(Integer, default=0)
    offers_found: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class OfferSnapshot(Base):
    """Price history: one row per offer seen in a run (cheapest N per search are kept)."""

    __tablename__ = "offer_snapshots"
    __table_args__ = (
        Index("ix_offer_watch_found", "watch_id", "found_at"),
        Index("ix_offer_fingerprint", "watch_id", "fingerprint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    run_id: Mapped[int] = mapped_column(ForeignKey("search_runs.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(20))
    provider_offer_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(3))
    destination: Mapped[str] = mapped_column(String(3))
    departure_date: Mapped[dt.date] = mapped_column()
    return_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    airlines: Mapped[str] = mapped_column(String(300))
    stops: Mapped[int] = mapped_column(Integer)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baggage: Mapped[list | None] = mapped_column(JSON, nullable=True)
    segments: Mapped[list] = mapped_column(JSON)
    booking_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_url_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    found_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    watch: Mapped[Watch] = relationship(back_populates="offers")


class Alert(Base):
    """Sent alerts – used to avoid notifying twice about the same offer."""

    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("watch_id", "fingerprint", "total_amount", name="uq_alert_offer_price"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    reasons: Mapped[list] = mapped_column(JSON)
    message: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ProviderUsage(Base):
    """Monthly API call counter per provider (budget guard)."""

    __tablename__ = "provider_usage"
    __table_args__ = (UniqueConstraint("provider", "month", name="uq_usage_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    searches: Mapped[int] = mapped_column(Integer, default=0)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
