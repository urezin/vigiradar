"""
Editorial takedowns: items and analysis pieces withdrawn from the public site.

An item listed here stays in the database (so ingestion never re-creates it) but is
hidden everywhere it is read: the item page, story API, news stream, feeds, sitemap,
JSON APIs and the monitoring record. Analysis articles are hidden by slug, or when
their text mentions one of TERMS.

To withdraw something, add it below with the date and reason, and redeploy.
"""
from __future__ import annotations

import json

# item id -> reason
ITEM_IDS: dict[str, str] = {
    "5ea634b8f35f079a": "2026-11-16: withdrawn after BS-MTI correction request (FSCA-BSMTI-26001); "
                        "IGJ listing attributed EPflex guidewire packaging issue to BS-MTI stent",
}

# analysis/opinion slugs to hide
ARTICLE_SLUGS: set[str] = set()

# lower-case phrases; any analysis article whose text contains one is hidden
TERMS: tuple[str, ...] = ("tumorstent innoflex",)


def item_blocked(item_id: str | None) -> bool:
    return bool(item_id) and item_id in ITEM_IDS


def text_blocked(*parts: str | None) -> bool:
    t = " ".join(p for p in parts if p).lower()
    return any(term in t for term in TERMS)


def article_blocked(a: dict) -> bool:
    if a.get("slug") in ARTICLE_SLUGS:
        return True
    try:
        return text_blocked(json.dumps(a, ensure_ascii=False))
    except Exception:
        return False
