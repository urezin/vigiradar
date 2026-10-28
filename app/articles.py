"""
VigiEye editorial: in-depth analysis and opinion, written daily by Claude from the
day's official records, and the PQRA house advertisement.

Articles are stored in portal_analyses with id "article:<slug>" and a JSON body:
  {"slug","type": "analysis"|"opinion","section","title","dek","date","sections":[{heading,paragraphs}],
   "takeaways":[...],"sources":[{title,url,publisher,date}],"model"}

Guardrails: the model may only use facts in the records it is given (plus well-established
regulatory procedure in general terms). No invented numbers, dates, quotes, people or
company statements. Every article lists the records it was built from, and is labelled as
AI-assisted VigiEye editorial.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from . import portal_data as pd
from . import store
from .config import settings

log = logging.getLogger("vigiradar.articles")

# --- PQRA house ad ---------------------------------------------------------------------------

PQRA_AD = {
    "name": "PQRA",
    "tagline": "Regulatory, quality and pharmacovigilance consulting for the pharmaceutical industry",
    "services": ["Regulatory affairs & market access", "Quality management & audit readiness",
                 "Pharmacovigilance & medical information"],
    "proof": "25+ years with EOF and EMA · human and veterinary medicines",
    "cta": "Visit pqra.gr",
    "cta_url": "https://www.pqra.gr/",
    "site_url": "https://www.pqra.gr/",
    "email": "info@pqra.gr",
    "phone": "+30 21 0444 1982",
}


def _utm(url: str, placement: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}utm_source=vigieye&utm_medium=house_ad&utm_campaign=pqra&utm_content={placement}"


def ad_html(placement: str, style: str = "box") -> str:
    """style: 'box' (sidebar), 'banner' (full width), 'inline' (inside a river)."""
    from html import escape as e
    a = PQRA_AD
    svc = "".join(f"<li>{e(s)}</li>" for s in a["services"])
    if style == "banner":
        return f"""<aside class="ad ad-banner" aria-label="Advertisement"><span class="ad-tag">Advertisement</span>
<div class="ad-in"><div><div class="ad-logo">PQRA</div><p class="ad-tl">{e(a["tagline"])}</p>
<ul class="ad-svc">{svc}</ul><p class="ad-proof">{e(a["proof"])}</p></div>
<div class="ad-cta"><a class="btn" href="{e(_utm(a["cta_url"], placement))}" target="_blank" rel="noopener sponsored">{e(a["cta"])}</a>
<a class="ad-link" href="{e(_utm(a["site_url"], placement))}" target="_blank" rel="noopener sponsored">pqra.gr</a>
<span class="ad-contact">{e(a["email"])} · {e(a["phone"])}</span></div></div></aside>"""
    if style == "inline":
        return f"""<aside class="ad ad-inline" aria-label="Advertisement"><span class="ad-tag">Advertisement</span>
<div class="ad-in"><div class="ad-logo">PQRA</div><div><p class="ad-tl">{e(a["tagline"])}.</p>
<p class="ad-proof">{e(" · ".join(a["services"]))}</p></div>
<a class="btn" href="{e(_utm(a["cta_url"], placement))}" target="_blank" rel="noopener sponsored">{e(a["cta"])}</a></div></aside>"""
    return f"""<aside class="ad ad-box box" aria-label="Advertisement"><span class="ad-tag">Advertisement</span>
<div class="ad-logo">PQRA</div><p class="ad-tl">{e(a["tagline"])}</p><ul class="ad-svc">{svc}</ul>
<p class="ad-proof">{e(a["proof"])}</p>
<a class="btn" style="width:100%;text-align:center" href="{e(_utm(a["cta_url"], placement))}" target="_blank" rel="noopener sponsored">{e(a["cta"])}</a>
<p class="ad-contact"><a href="{e(_utm(a["site_url"], placement))}" target="_blank" rel="noopener sponsored">pqra.gr</a> · {e(a["email"])}</p></aside>"""


AD_CSS = """
.ad{position:relative;border-radius:8px}.ad-tag{position:absolute;top:8px;right:10px;font-size:10px;letter-spacing:.08em;
 text-transform:uppercase;color:#94a3b8}
.ad-logo{font:900 26px/1 Georgia,'Times New Roman',serif;letter-spacing:.04em;color:#0d9488}
.ad-tl{font-weight:700;margin-top:6px;font-size:15px}.ad-svc{margin:8px 0 6px 18px;font-size:13.5px}
.ad-proof{font-size:12.5px;color:var(--mut)}.ad-contact{display:block;font-size:12px;color:var(--mut);margin-top:8px}
.ad-box{border:1px solid #99f6e4;background:linear-gradient(180deg,#f0fdfa,var(--paper))}
.ad-banner{border:1px solid #99f6e4;background:linear-gradient(90deg,#f0fdfa,#ecfeff 60%,#f8fafc);padding:20px 22px;margin-top:34px}
.ad-banner .ad-in{display:flex;justify-content:space-between;gap:20px;align-items:center;flex-wrap:wrap}
.ad-banner .ad-cta{display:flex;flex-direction:column;gap:8px;align-items:flex-start}.ad-link{font-weight:700}
.ad-inline{border:1px dashed #5eead4;background:#f0fdfa;padding:14px 16px;margin:14px 0}
.ad-inline .ad-in{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.ad-inline .ad-tl{margin:0}
@media (prefers-color-scheme: dark){.ad-box,.ad-banner,.ad-inline{background:#062a28;border-color:#115e59}}
"""


# --- article generation -----------------------------------------------------------------------

_SYSTEM = ("You are the editor of VigiEye, a trade publication for pharmaceutical regulatory affairs, pharmacovigilance, "
           "quality/GxP and market-access professionals in the EU and US. You write sharp, practical, well-structured "
           "journalism. Absolute rule: use only facts contained in the RECORDS. You may explain well-established "
           "regulatory procedures in general terms (e.g. what a PRAC signal recommendation or a PDUFA date is). Never invent "
           "numbers, dates, quotes, named people, company statements, trial results or outcomes. If something isn't in "
           "the records, say it is not yet known or leave it out. No investment or medical advice.")

_ANALYSIS = """Write an in-depth ANALYSIS article (650-900 words) for the {beat} desk, built around the lead record and using the
related records for context.

RECORDS:
{records}

Return ONLY JSON:
{{"title":"specific headline, max 95 chars, no clickbait","dek":"one-sentence standfirst, max 200 chars",
"sections":[{{"heading":"...","paragraphs":["...","..."]}}],
"takeaways":["3-4 concrete action points for {audience}"]}}
Use 4-5 sections, e.g. what happened, why it matters, regulatory context, who is affected, what to watch. Plain English."""

_OPINION = """Write a signed-editorial OPINION column (500-700 words) titled as "VigiEye View". Take a clear, reasoned position on
the most important theme running through these records for pharma regulatory, PV, quality and market-access teams.
Argue from the facts in the records; acknowledge the strongest counter-argument.

RECORDS:
{records}

Return ONLY JSON:
{{"title":"opinion headline, max 95 chars","dek":"one-sentence standfirst, max 200 chars",
"sections":[{{"heading":"...","paragraphs":["..."]}}],"takeaways":["2-3 things teams should do"]}}"""

_AUDIENCE = {"pharmacovigilance": "QPPVs and PV teams", "regulatory": "regulatory affairs teams",
             "quality": "QA, QP and compliance teams", "market-access": "market access, HTA and pricing teams"}
_BEAT = {"pharmacovigilance": "pharmacovigilance", "regulatory": "regulatory", "quality": "quality & compliance",
         "market-access": "market access"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80].strip("-")


def _claude(prompt: str, max_tokens: int = 2600) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, system=_SYSTEM,
                                 messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def _rec(r: dict) -> str:
    return (f"- [{r['date']}] {r.get('publisher')} ({r.get('country') or ''}): {r['title']}. "
            f"{(r.get('summary') or '')[:900]} (source: {r.get('url')})")


def _store(art: dict) -> None:
    pd.analysis_put(f"article:{art['slug']}", hashlib.sha1(art["slug"].encode()).hexdigest(),
                    json.dumps(art, ensure_ascii=False), art.get("model", ""))


def list_articles(limit: int = 30, kind: str | None = None) -> list[dict]:
    pd.init()
    with store._engine.connect() as c:
        rows = c.execute(select(pd.analyses.c.body).where(pd.analyses.c.id.like("article:%"))
                         .order_by(pd.analyses.c.created_at.desc()).limit(300)).all()
    out = []
    for (b,) in rows:
        try:
            a = json.loads(b)
        except Exception:
            continue
        if kind and a.get("type") != kind:
            continue
        out.append(a)
    out.sort(key=lambda a: (a.get("date", ""), a.get("type") == "opinion"), reverse=True)
    return out[:limit]


def get_article(slug: str) -> dict | None:
    a = pd.analysis_get(f"article:{slug}")
    if not a:
        return None
    try:
        return json.loads(a["body"])
    except Exception:
        return None


def generate_daily(max_analyses: int = 4, opinion: bool = True) -> dict:
    """One analysis per beat from the strongest fresh story, plus one opinion column."""
    from . import news_pages as np_
    if not settings.anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}
    pd.init()
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=4)).isoformat()
    rows = [r for r in np_.stream(limit=900) if r["kind"] != "press" and r["date"] >= since and np_._real_summary(r)]
    done_sources = set()
    for a in list_articles(200):
        for s in a.get("sources", [])[:1]:
            done_sources.add(s.get("url"))
    made, errors = [], []
    for beat in ("pharmacovigilance", "regulatory", "quality", "market-access"):
        if len(made) >= max_analyses:
            break
        cands = sorted([r for r in rows if r["section"] == beat and r.get("url") not in done_sources],
                       key=np_._score, reverse=True)
        if not cands:
            continue
        lead = cands[0]
        related = [r for r in np_.stream(section=beat, limit=60) if r is not lead and np_._real_summary(r)][:8]
        records = "LEAD RECORD:\n" + _rec(lead) + "\nRELATED RECORDS:\n" + "\n".join(_rec(r) for r in related)
        try:
            j = _claude(_ANALYSIS.format(beat=_BEAT[beat], audience=_AUDIENCE[beat], records=records))
        except Exception as e:
            errors.append(f"{beat}: {e}")
            continue
        if not j or not j.get("title"):
            continue
        slug = f"{today}-{_slug(j['title'])}"
        _store({"slug": slug, "type": "analysis", "section": beat, "title": j["title"][:140],
                "dek": (j.get("dek") or "")[:260], "date": today, "sections": j.get("sections", [])[:7],
                "takeaways": j.get("takeaways", [])[:5], "model": settings.anthropic_model,
                "sources": [{"title": r["title"], "url": r.get("url"), "publisher": r.get("publisher"), "date": r["date"]}
                            for r in [lead] + related[:5]]})
        made.append(slug)
    if opinion and not any(a.get("type") == "opinion" and a.get("date") == today for a in list_articles(20)):
        top = sorted(rows, key=np_._score, reverse=True)[:12]
        if top:
            try:
                j = _claude(_OPINION.format(records="\n".join(_rec(r) for r in top)), max_tokens=2200)
                if j and j.get("title"):
                    slug = f"{today}-view-{_slug(j['title'])}"
                    sec = max(("pharmacovigilance", "regulatory", "quality", "market-access"),
                              key=lambda s: sum(1 for r in top if r["section"] == s))
                    _store({"slug": slug, "type": "opinion", "section": sec, "title": j["title"][:140],
                            "dek": (j.get("dek") or "")[:260], "date": today, "sections": j.get("sections", [])[:6],
                            "takeaways": j.get("takeaways", [])[:4], "model": settings.anthropic_model,
                            "sources": [{"title": r["title"], "url": r.get("url"), "publisher": r.get("publisher"),
                                         "date": r["date"]} for r in top[:8]]})
                    made.append(slug)
            except Exception as e:
                errors.append(f"opinion: {e}")
    return {"generated": made, "errors": errors[:5], "at": datetime.now(timezone.utc).isoformat()}
