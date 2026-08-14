"""
Storage — persistent database via SQLAlchemy Core.

Uses DATABASE_URL when set (Render Postgres in production) and falls back to a
local SQLite file otherwise, so the same code runs locally and in the cloud with
durable history (no more resets on redeploy). Table + query interface is
unchanged, so callers (ingest, main) don't care which backend is behind it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text, Float,
                        select, func, insert, delete, or_)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # Render/Heroku hand out postgres:// ; SQLAlchemy wants postgresql://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    path = os.getenv("VIGIRADAR_DB", os.path.join(os.path.dirname(__file__), "vigieye.db"))
    return f"sqlite:///{path}"


_engine = create_engine(_database_url(), future=True, pool_pre_ping=True)
_meta = MetaData()

updates = Table(
    "updates", _meta,
    Column("id", String(32), primary_key=True),
    Column("country", String(8), nullable=False),
    Column("authority", String(120), nullable=False),
    Column("subject", String(80), nullable=False),
    Column("impact", String(8), nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("source_url", Text),
    Column("published", Text),
    Column("date", String(10), nullable=False),
    Column("mode", String(16)),
    Column("ingested_at", String(40), nullable=False),
)

# Medicine price / reimbursement rows (per country). Full-refresh dataset.
prices = Table(
    "prices", _meta,
    Column("id", String(32), primary_key=True),
    Column("country", String(8), nullable=False),
    Column("product", Text, nullable=False),
    Column("form", Text),
    Column("presentation", Text),
    Column("cip13", String(20)),
    Column("price_eur", Float),
    Column("price_with_fee_eur", Float),
    Column("reimbursement", String(16)),
    Column("status", String(120)),
    Column("source_url", Text),
    Column("updated_at", String(40), nullable=False),
)


def init() -> None:
    _meta.create_all(_engine)


def exists(item_id: str) -> bool:
    with _engine.connect() as c:
        return c.execute(select(updates.c.id).where(updates.c.id == item_id)).first() is not None


def upsert(row: dict) -> None:
    """Idempotent by id — delete-then-insert works on both SQLite and Postgres."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {**row, "ingested_at": now}
    with _engine.begin() as c:
        c.execute(delete(updates).where(updates.c.id == payload["id"]))
        c.execute(insert(updates).values(**payload))


def count() -> int:
    with _engine.connect() as c:
        return c.execute(select(func.count()).select_from(updates)).scalar_one()


def query(country: str | None = None, subject: str | None = None,
          impact: str | None = None, limit: int = 200) -> list[dict]:
    stmt = select(updates.c.country, updates.c.authority, updates.c.subject,
                  updates.c.impact, updates.c.title, updates.c.summary,
                  updates.c.source_url, updates.c.date)
    if country:
        stmt = stmt.where(updates.c.country == country)
    if subject:
        stmt = stmt.where(updates.c.subject == subject)
    if impact:
        stmt = stmt.where(updates.c.impact == impact)
    stmt = stmt.order_by(updates.c.date.desc(), updates.c.ingested_at.desc()).limit(limit)
    with _engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def distinct_countries() -> list[str]:
    with _engine.connect() as c:
        return [r[0] for r in c.execute(select(updates.c.country).distinct().order_by(updates.c.country))]


# --- prices -----------------------------------------------------------------

def prices_replace_country(country: str, rows: list[dict]) -> int:
    """Full refresh for one country: drop its rows, then bulk-insert `rows`.
    De-dups on id within the batch (a CIP can appear twice) so the PK holds."""
    now = datetime.now(timezone.utc).isoformat()
    seen, payload = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        payload.append({**r, "updated_at": now})
    with _engine.begin() as c:
        c.execute(delete(prices).where(prices.c.country == country))
        for i in range(0, len(payload), 1000):     # chunked bulk insert
            chunk = payload[i:i + 1000]
            if chunk:
                c.execute(insert(prices), chunk)
    return len(payload)


def prices_count(country: str | None = None) -> int:
    stmt = select(func.count()).select_from(prices)
    if country:
        stmt = stmt.where(prices.c.country == country)
    with _engine.connect() as c:
        return c.execute(stmt).scalar_one()


def prices_query(country: str | None = None, search: str | None = None,
                 limit: int = 200) -> list[dict]:
    stmt = select(prices.c.country, prices.c.product, prices.c.form,
                  prices.c.presentation, prices.c.cip13, prices.c.price_eur,
                  prices.c.price_with_fee_eur, prices.c.reimbursement,
                  prices.c.status, prices.c.source_url)
    if country:
        stmt = stmt.where(prices.c.country == country)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(prices.c.product.ilike(like),
                              prices.c.presentation.ilike(like)))
    stmt = stmt.order_by(prices.c.product).limit(limit)
    with _engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def prices_countries() -> list[str]:
    with _engine.connect() as c:
        return [r[0] for r in c.execute(
            select(prices.c.country).distinct().order_by(prices.c.country))]
