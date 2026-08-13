"""
Source connectors.

Each SOURCE points at a real, official RSS/Atom feed. Feeds are fetched with
httpx and parsed with feedparser (handles RSS 2.0, Atom and the usual quirks).
Every raw item is normalised to a common shape before summarisation.

Adding a national agency later = append one SOURCE row (most publish RSS).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    id: str
    country: str          # ISO-ish key used across the app ("EU", "DE", "FR"...)
    authority: str        # display name, e.g. "EMA", "EUR-Lex", "BfArM"
    feed_url: str
    default_subject: str   # fallback subject if the classifier is unsure


# Real, current official feeds. EMA exposes ~20 topic feeds; we start with the
# ones most relevant to regulatory affairs & pharmacovigilance teams.
SOURCES: list[Source] = [
    Source("ema-news", "EU", "EMA",
           "https://www.ema.europa.eu/en/news.xml", "GVP modules"),
    Source("ema-whats-new", "EU", "EMA",
           "https://www.ema.europa.eu/en/whats-new.xml", "GVP modules"),
    Source("ema-reg-guideline", "EU", "EMA",
           "https://www.ema.europa.eu/en/regulatory-and-procedural-guideline.xml", "GVP modules"),
    Source("ema-agendas", "EU", "EMA · committees",
           "https://www.ema.europa.eu/en/agendas-and-minutes.xml", "Signal management"),
    Source("ema-consultations", "EU", "EMA",
           "https://www.ema.europa.eu/en/public-consultations.xml", "GVP modules"),
    Source("ema-fees", "EU", "EMA",
           "https://www.ema.europa.eu/en/fees.xml", "Fees & guidance"),
    Source("ema-inspections", "EU", "EMA",
           "https://www.ema.europa.eu/en/inspections.xml", "GVP modules"),
    # EUR-Lex: Acts of the Official Journal (L series = legislation)
    Source("eurlex-oj-l", "EU", "EUR-Lex",
           "https://eur-lex.europa.eu/EN/display-feed.rss?rssId=165", "Falsified medicines"),
]


def item_id(source_id: str, link: str, title: str) -> str:
    """Stable dedup key for an item (link is usually unique; title backstops it)."""
    basis = f"{source_id}|{link or ''}|{title or ''}".encode("utf-8", "ignore")
    return hashlib.sha1(basis).hexdigest()[:16]


def fetch_raw_items(source: Source, timeout: float = 20.0) -> list[dict]:
    """
    Fetch and parse one feed. Returns a list of normalised raw items:
      {id, source_id, country, authority, title, link, description, published}
    Network + parse errors are swallowed per-source so one bad feed never breaks
    a whole ingestion run (the caller logs the miss).
    """
    import httpx
    import feedparser

    headers = {"User-Agent": "VigiRadar/0.1 (+https://vigiradar.com)"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        resp = client.get(source.feed_url)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)

    items: list[dict] = []
    for e in parsed.entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        desc = (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
        published = (getattr(e, "published", "") or getattr(e, "updated", "") or "").strip()
        if not title:
            continue
        items.append({
            "id": item_id(source.id, link, title),
            "source_id": source.id,
            "country": source.country,
            "authority": source.authority,
            "title": title,
            "link": link,
            "description": desc,
            "published": published,
        })
    return items
