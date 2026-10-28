"""
VigiEye stories: every news item gets its own article, written by VigiEye from scratch.

Input is the official primary source only (the regulator's notice page, guideline abstract or
SEC filing). The model is told to use only facts in that source plus well-established general
regulatory context, to write in English (sources can be in any EU language), and to say what
the item means for regulatory, PV, quality or market-access teams. Stored in `portal_stories`
keyed by "<u|n|c>:<item id>". Items without a story fall back to their short summary.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, String, Table, Text, insert, select

from . import store
from .config import settings
from .official import is_official

log = logging.getLogger("vigiradar.stories")
_meta = MetaData()

stories = Table(
    "portal_stories", _meta,
    Column("key", String(40), primary_key=True),
    Column("headline", Text, nullable=False),
    Column("dek", Text),
    Column("body", Text, nullable=False),            # json list of paragraphs
    Column("takeaways", Text),                       # json list of bullets
    Column("lang", String(8)),
    Column("model", String(60)),
    Column("created_at", String(40), nullable=False),
)

_SYSTEM = ("You are a senior reporter at VigiEye, an independent news service for pharmaceutical regulatory affairs, "
           "pharmacovigilance, quality and market-access professionals in Europe and the US. You write original news "
           "articles in clear British English. You never copy sentences from the source; you report its facts in your own "
           "words. You never invent facts, numbers, dates, names or quotes.")

_PROMPT = """Write a VigiEye news article about this official item.

Publisher: {publisher} ({country})
Date: {date}
Original title: {title}
Short summary already on file: {summary}

Text of the official source (may be in another language; may contain page navigation to ignore):
<<<
{source}
>>>

Rules:
- Use ONLY facts stated in the source text or summary above. You may add one or two sentences of general,
  well-established regulatory context (e.g. what a PRAC signal assessment or a Class II recall is), clearly as context.
- If the source text is thin, write a shorter article. Never pad with speculation.
- No quotes unless they appear verbatim in the source. No investment, medical or legal advice.
- English only, even when the source is not.

Return ONLY a JSON object:
{{"headline": "news headline, max 14 words, factual, no clickbait",
  "dek": "one-sentence standfirst, max 30 words",
  "body": ["3 to 5 short paragraphs of original reporting"],
  "takeaways": ["2 to 4 bullets: what this means for regulatory / PV / quality / market-access teams"],
  "newsworthy": true or false (false for staff news, events listings, website notices, page maintenance)}}"""


def init() -> None:
    _meta.create_all(store._engine)


def get(key: str) -> dict | None:
    init()
    with store._engine.connect() as c:
        r = c.execute(select(stories).where(stories.c.key == key)).first()
    if not r:
        return None
    d = dict(r._mapping)
    for k in ("body", "takeaways"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except Exception:
            d[k] = []
    return d


def existing_keys() -> set[str]:
    init()
    with store._engine.connect() as c:
        return {r[0] for r in c.execute(select(stories.c.key))}


def _source_text(url: str | None, limit: int = 7000) -> str:
    if not url or not is_official(url) or url.lower().endswith(".pdf"):
        return ""
    try:
        import httpx
        ua = ("VigiEye regulatory monitor (+https://vigi-eye.com; info@vigi-eye.com)" if "fda.gov" in url else
              "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.3")
        with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": ua}) as c:
            r = c.get(url)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return ""
        t = r.text
        m = re.search(r"<main\b.*?</main>|<article\b.*?</article>", t, re.S | re.I)
        t = m.group(0) if m else t
        t = re.sub(r"<(script|style|nav|header|footer|noscript)\b.*?</\1>", " ", t, flags=re.S | re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        return re.sub(r"\s+", " ", _html.unescape(t)).strip()[:limit]
    except Exception:
        return ""


def _key(r: dict) -> str | None:
    if r["kind"] == "official" and r.get("id"):
        return f"u:{r['id']}"
    if r["kind"] in ("notice", "guideline", "trial", "company") and r.get("id"):
        return f"n:{r['id']}" if r["kind"] != "company" else f"c:{r['id']}"
    return None


def write_one(r: dict) -> dict | None:
    import anthropic
    src = _source_text(r.get("url"))
    if not src and len(r.get("summary") or "") < 60:
        return None                      # nothing to report from
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.anthropic_model, max_tokens=1600, system=_SYSTEM,
        messages=[{"role": "user", "content": _PROMPT.format(
            publisher=r.get("publisher") or "", country=r.get("country") or "", date=r.get("date") or "",
            title=r.get("title") or "", summary=(r.get("summary") or "")[:1500], source=src or "(not available)")}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    from .articles import _parse_json
    try:
        data = _parse_json(text)
    except Exception:
        msg = client.messages.create(
            model=settings.anthropic_model, max_tokens=1600, system=_SYSTEM,
            messages=[{"role": "user", "content": _PROMPT.format(
                publisher=r.get("publisher") or "", country=r.get("country") or "", date=r.get("date") or "",
                title=r.get("title") or "", summary=(r.get("summary") or "")[:1500], source=src[:4000] or "(not available)")
                + "\n\nReturn strictly valid JSON (escape double quotes inside strings)."}])
        data = _parse_json("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
    body = [p.strip() for p in data.get("body") or [] if isinstance(p, str) and p.strip()]
    if not data.get("headline") or not body:
        return None
    return {"headline": data["headline"].strip()[:220], "dek": (data.get("dek") or "").strip()[:400],
            "body": body[:6], "takeaways": [t.strip() for t in data.get("takeaways") or [] if isinstance(t, str)][:5],
            "newsworthy": data.get("newsworthy", True) is not False}


def run(limit: int = 40, days: int = 21) -> dict:
    """Write stories for the most important recent items that don't have one yet."""
    from . import news_pages as np_
    init()
    stats = {"written": 0, "skipped": 0, "errors": 0, "ai": bool(settings.anthropic_api_key)}
    if not settings.anthropic_api_key:
        return stats
    have = existing_keys()
    since = (date.today() - timedelta(days=days)).isoformat()
    cand = [r for r in np_.stream(limit=2500) if r["date"] >= since and (_key(r) or "") not in have and _key(r)]
    cand.sort(key=np_._score, reverse=True)
    for r in cand[:limit]:
        k = _key(r)
        try:
            s = write_one(r)
        except Exception as e:
            log.warning("story failed for %s: %s", k, e)
            stats["errors"] += 1
            continue
        if not s:
            stats["skipped"] += 1
            s = {"headline": r["title"], "dek": "", "body": [], "takeaways": [], "newsworthy": False}
        with store._engine.begin() as c:
            c.execute(insert(stories).values(
                key=k, headline=s["headline"], dek=s["dek"], body=json.dumps(s["body"], ensure_ascii=False),
                takeaways=json.dumps(s["takeaways"], ensure_ascii=False),
                lang="en" if s["newsworthy"] else "skip", model=settings.anthropic_model,
                created_at=datetime.now(timezone.utc).isoformat()))
        stats["written"] += 1 if s["body"] else 0
    return stats


def headline_map() -> dict[str, dict]:
    """key -> {headline, dek, skip} for list pages."""
    init()
    with store._engine.connect() as c:
        return {r.key: {"headline": r.headline, "dek": r.dek, "skip": r.lang == "skip", "has_body": r.body not in ("[]", "")}
                for r in c.execute(select(stories.c.key, stories.c.headline, stories.c.dek, stories.c.lang, stories.c.body))}
