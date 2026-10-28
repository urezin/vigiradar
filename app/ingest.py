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

from . import store, sources, scrapers, summarise

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
            if not s.get("relevant", True):
                s["mode"] = "irrelevant"     # stored so it is not re-summarised, hidden by store.query
            title = item["title"]
            if s.get("headline") and s["headline"].lower() != title.lower():
                title = f'{s["headline"]} ({src.authority})' if src.language != "en" else title
            store.upsert({
                "id": item["id"],
                "country": item["country"],
                "authority": item["authority"],
                "subject": s["subject"],
                "impact": s["impact"],
                "title": title,
                "summary": s["summary"],
                "source_url": item["link"],
                "published": item["published"],
                "date": _to_date(item["published"]),
                "mode": s.get("mode", "heuristic"),
            })
            new_here += 1

        stats["new"] += new_here
        stats["by_source"][src.id] = {"fetched": len(raw), "new": new_here}

    # National-agency scrapers (HTML) — same normalised item shape as RSS.
    for scr in scrapers.SCRAPERS:
        if only and scr.id not in only:
            continue
        stats["sources"] += 1
        try:
            raw = scrapers.fetch_raw_items(scr)
        except Exception as e:
            stats["errors"].append({"source": scr.id, "error": str(e)})
            log.warning("scrape failed for %s: %s", scr.id, e)
            continue

        stats["fetched"] += len(raw)
        new_here = 0
        if scr.seed_hidden and not store.authority_seen(scr.authority):
            # first run: remember what is already listed, hidden, so only later items appear as news
            for item in raw:
                store.upsert({"id": item["id"], "country": item["country"], "authority": item["authority"],
                              "subject": scr.default_subject, "impact": "low", "title": item["title"], "summary": "",
                              "source_url": item["link"], "published": item["published"],
                              "date": _to_date(item["published"]), "mode": "seed"})
            stats["by_source"][scr.id] = {"fetched": len(raw), "seeded": len(raw), "scraper": True}
            continue
        for item in raw:
            if new_here >= per_source:
                break
            if store.exists(item["id"]):
                continue
            s = summarise.summarise_item(item, default_subject=scr.default_subject)
            if not s.get("relevant", True):
                s["mode"] = "irrelevant"
            title = item["title"]
            if s.get("headline") and getattr(scr, "language", "en") != "en" and s["headline"].lower() != title.lower():
                title = f'{s["headline"]} ({scr.authority})'
            store.upsert({
                "id": item["id"], "country": item["country"], "authority": item["authority"],
                "subject": s["subject"], "impact": s["impact"], "title": title,
                "summary": s["summary"], "source_url": item["link"], "published": item["published"],
                "date": _to_date(item["published"]), "mode": s.get("mode", "heuristic"),
            })
            new_here += 1

        stats["new"] += new_here
        stats["by_source"][scr.id] = {"fetched": len(raw), "new": new_here,
                                      "scraper": True, "verified": scr.verified}

    stats["total_in_store"] = store.count()
    return stats


# --- English titles for items from non-English agencies --------------------------------

_NON_EN = {"FR", "ES", "IT", "DE", "NL", "SE", "DK", "FI", "EE", "CZ", "SK", "SI", "HU", "GR", "PL", "PT", "RO",
           "AT", "BE", "BG", "HR", "LV", "LT", "LU", "NO", "IS"}

_TR_PROMPT = """Translate these titles from European medicines agencies into English news headlines (faithful, max 16 words,
no added facts; keep product names). Also mark relevant=false for items that are not about medicines, medical devices,
clinical trials, pharmacovigilance, quality/GMP/GDP, pricing/reimbursement or pharma compliance (e.g. staff news, events,
website notices, building closures, job vacancies).

{items}

Return ONLY a JSON array: [{{"i": 0, "en": "...", "relevant": true}}, ...]"""


def translate_titles(limit: int = 200, batch: int = 25) -> dict:
    """Give every stored item from a non-English agency an English headline (marks rows with mode suffix '~')."""
    import json
    from sqlalchemy import select, update, and_, not_
    from .config import settings
    stats = {"translated": 0, "irrelevant": 0, "errors": 0}
    if not settings.anthropic_api_key:
        return stats
    store.init()
    u = store.updates
    with store._engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            select(u.c.id, u.c.title, u.c.authority, u.c.mode, u.c.country)
            .where(and_(u.c.country.in_(sorted(_NON_EN)), not_(u.c.mode.like("%~")), u.c.mode != "irrelevant", u.c.mode != "seed"))
            .order_by(u.c.date.desc()).limit(limit))]
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    for k in range(0, len(rows), batch):
        chunk = rows[k:k + batch]
        items = "\n".join(f'{i}. [{r["authority"]}] {r["title"]}' for i, r in enumerate(chunk))
        try:
            msg = client.messages.create(model=settings.anthropic_model, max_tokens=3000,
                                         messages=[{"role": "user", "content": _TR_PROMPT.format(items=items)}])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            arr = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        except Exception as e:
            log.warning("title translation failed: %s", e)
            stats["errors"] += 1
            continue
        by_i = {a.get("i"): a for a in arr if isinstance(a, dict)}
        with store._engine.begin() as c:
            for i, r in enumerate(chunk):
                a = by_i.get(i) or {}
                en = (a.get("en") or "").strip()
                mode = (r.get("mode") or "heuristic")[:14] + "~"
                vals = {"mode": mode}
                if a.get("relevant") is False:
                    vals["mode"] = "irrelevant"
                    stats["irrelevant"] += 1
                elif en and en.lower() != (r["title"] or "").lower() and not (r["title"] or "").endswith(f'({r["authority"]})'):
                    vals["title"] = f'{en} ({r["authority"]})'[:500]
                    stats["translated"] += 1
                c.execute(update(u).where(u.c.id == r["id"]).values(**vals))
    return stats
