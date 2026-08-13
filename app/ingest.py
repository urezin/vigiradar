"""
Ingestion runner.

For each configured Source: fetch the feed, skip items already stored, summarise
the new ones (AI or heuristic), and persist. Returns a per-run stats dict.

Trigger it from the /admin/ingest endpoint or a scheduled job (Render Cron).
Designed to be safe to run repeatedly — dedup on item id makes it idempotent.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from . import store, sources, summarise

log = logging.getLogger("vigiradar.ingest")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _to_date(published: str) -> str:
    """Best-effort parse of an RSS date to YYYY-MM-DD; fall back to today (UTC)."""
    p = (published or "").strip()
    # RFC-822: "Wed, 12 Aug 2026 09:00:00 GMT"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", p)
    if m:
        day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        if mon in _MONTHS:
            return f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}"
    # ISO: "2026-08-12..."
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run_ingest(per_source: int = 15, only: list[str] | None = None) -> dict:
    """Run one ingestion pass. `per_source` caps new items summarised per feed
    (bounds AI token spend). `only` optionally restricts to given source ids."""
    store.init()
    stats = {"sources": 0, "fetched": 0, "new": 0, "errors": [], "by_source": {}}

    for src in sources.SOURCES:
        if only and src.id not in only:
            continue
        stats["sources"] += 1
        try:
            raw = sources.fetch_raw_items(src)
        except Exception as e:
            stats["errors"].append({"source": src.id, "error": str(e)})
            log.warning("fetch failed for %s: %s", src.id, e)
            continue

        stats["fetched"] += len(raw)
        new_here = 0
        for item in raw:
            if new_here >= per_source:
                break
            if store.exists(item["id"]):
                continue
            s = summarise.summarise_item(item, default_subject=src.default_subject)
            store.upsert({
                "id": item["id"],
                "country": item["country"],
                "authority": item["authority"],
                "subject": s["subject"],
                "impact": s["impact"],
                "title": item["title"],
                "summary": s["summary"],
                "source_url": item["link"],
                "published": item["published"],
                "date": _to_date(item["published"]),
                "mode": s.get("mode", "heuristic"),
            })
            new_here += 1

        stats["new"] += new_here
        stats["by_source"][src.id] = {"fetched": len(raw), "new": new_here}

    stats["total_in_store"] = store.count()
    return stats
