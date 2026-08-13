"""
Storage — SQLite for the MVP.

One table, `updates`, holding the normalised + summarised feed items that power
/api/updates. Idempotent upsert keyed on the item id, so re-running ingestion
never creates duplicates. Swap for Postgres later without touching callers.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("VIGIRADAR_DB", os.path.join(os.path.dirname(__file__), "vigiradar.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id           TEXT PRIMARY KEY,
    country      TEXT NOT NULL,
    authority    TEXT NOT NULL,
    subject      TEXT NOT NULL,
    impact       TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    source_url   TEXT,
    published    TEXT,
    date         TEXT NOT NULL,      -- YYYY-MM-DD used for sorting/display
    mode         TEXT,               -- "llm" | "heuristic"
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_updates_date ON updates(date DESC);
CREATE INDEX IF NOT EXISTS idx_updates_country ON updates(country);
CREATE INDEX IF NOT EXISTS idx_updates_subject ON updates(subject);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def exists(item_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM updates WHERE id=?", (item_id,)).fetchone() is not None


def upsert(row: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT INTO updates
               (id, country, authority, subject, impact, title, summary, source_url, published, date, mode, ingested_at)
               VALUES (:id,:country,:authority,:subject,:impact,:title,:summary,:source_url,:published,:date,:mode,:ingested_at)
               ON CONFLICT(id) DO UPDATE SET
                 subject=excluded.subject, impact=excluded.impact,
                 summary=excluded.summary, mode=excluded.mode""",
            {**row, "ingested_at": now},
        )


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM updates").fetchone()[0]


def query(country: str | None = None, subject: str | None = None,
          impact: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT country, authority, subject, impact, title, summary, source_url, date FROM updates"
    where, params = [], []
    if country:
        where.append("country=?"); params.append(country)
    if subject:
        where.append("subject=?"); params.append(subject)
    if impact:
        where.append("impact=?"); params.append(impact)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, ingested_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def distinct_countries() -> list[str]:
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT country FROM updates ORDER BY country")]
