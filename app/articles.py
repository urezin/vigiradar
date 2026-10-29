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


def _claude(prompt: str, max_tokens: int = 3600) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, system=_SYSTEM,
                                 messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _parse_json(raw)


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


_DESKS = ("pharmacovigilance", "regulatory", "quality", "market-access", "clinical-trials")
_AUDIENCE["clinical-trials"] = "clinical operations and regulatory teams running trials"
_BEAT["clinical-trials"] = "clinical trials"

_OPINION_DESK = """Write a signed-editorial OPINION column (550-750 words) for the {beat} desk, headed "VigiEye View".
Take a clear, reasoned position on the most important theme running through these records for {audience}.
Argue from the facts in the records; acknowledge the strongest counter-argument; end with a concrete recommendation.
Pick a theme different from these recent columns: {avoid}

RECORDS:
{records}

Return ONLY JSON:
{{"title":"opinion headline, max 95 chars","dek":"one-sentence standfirst, max 200 chars",
"sections":[{{"heading":"...","paragraphs":["..."]}}],"takeaways":["2-3 things teams should do"]}}"""


def _src_list(rows: list[dict]) -> list[dict]:
    from .official import safe_url
    return [{"title": r["title"], "url": safe_url(r.get("url")), "publisher": r.get("publisher"), "date": r["date"]} for r in rows]


def _todays(prefix: str, today: str) -> list[dict]:
    pd.init()
    with store._engine.connect() as c:
        rows = c.execute(select(pd.analyses.c.body).where(pd.analyses.c.id.like(f"{prefix}:{today}%"))).all()
    out = []
    for (b,) in rows:
        try:
            out.append(json.loads(b))
        except Exception:
            pass
    return out


def generate_daily(target: int = 10, opinions: int = 4) -> dict:
    """VigiEye: `target` articles a day, about 60% analysis and 40% opinion, spread across the desks."""
    from . import news_pages as np_
    if not settings.anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}
    pd.init()
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=5)).isoformat()
    rows = [r for r in np_.stream(limit=1500) if r["date"] >= since and np_._real_summary(r)]
    done_sources = {s.get("url") for a in list_articles(300) for s in a.get("sources", [])[:1]}
    have = _todays("article", today)
    n_op = sum(1 for a in have if a.get("type") == "opinion")
    n_an = len(have) - n_op
    want_an, want_op = max(0, target - opinions - n_an), max(0, opinions - n_op)
    made, errors = [], []
    # analyses: round-robin over desks, strongest fresh lead each time
    order = [d for d in _DESKS] * 3
    for desk in order:
        if want_an <= 0:
            break
        cands = sorted([r for r in rows if r["section"] == desk and r.get("url") not in done_sources],
                       key=np_._score, reverse=True)
        if not cands:
            continue
        lead = cands[0]
        done_sources.add(lead.get("url"))
        related = [r for r in rows if r["section"] == desk and r is not lead][:8]
        records = "LEAD RECORD:\n" + _rec(lead) + "\nRELATED RECORDS:\n" + "\n".join(_rec(r) for r in related)
        try:
            j = _claude(_ANALYSIS.format(beat=_BEAT[desk], audience=_AUDIENCE[desk], records=records))
        except Exception as e:
            errors.append(f"{desk}: {e}")
            continue
        if not j or not j.get("title"):
            continue
        slug = f"{today}-{_slug(j['title'])}"
        _store({"slug": slug, "type": "analysis", "section": desk, "title": j["title"][:140],
                "dek": (j.get("dek") or "")[:260], "date": today, "sections": j.get("sections", [])[:7],
                "takeaways": j.get("takeaways", [])[:5], "model": settings.anthropic_model,
                "sources": _src_list([lead] + related[:5])})
        made.append(slug)
        want_an -= 1
    # opinions: one per desk with the most material
    desks = sorted(_DESKS, key=lambda d: -sum(1 for r in rows if r["section"] == d))
    recent = "; ".join(a["title"] for a in list_articles(30, kind="opinion")[:12]) or "none"
    for desk in desks:
        if want_op <= 0:
            break
        top = sorted([r for r in rows if r["section"] == desk], key=np_._score, reverse=True)[:12]
        if len(top) < 3:
            continue
        try:
            j = _claude(_OPINION_DESK.format(beat=_BEAT[desk], audience=_AUDIENCE[desk], avoid=recent,
                                             records="\n".join(_rec(r) for r in top)), max_tokens=2400)
        except Exception as e:
            errors.append(f"opinion {desk}: {e}")
            continue
        if j and j.get("title"):
            slug = f"{today}-view-{_slug(j['title'])}"
            _store({"slug": slug, "type": "opinion", "section": desk, "title": j["title"][:140],
                    "dek": (j.get("dek") or "")[:260], "date": today, "sections": j.get("sections", [])[:6],
                    "takeaways": j.get("takeaways", [])[:4], "model": settings.anthropic_model,
                    "sources": _src_list(top[:8])})
            made.append(slug)
            want_op -= 1
            recent += "; " + j["title"]
    return {"generated": made, "errors": errors[:5], "at": datetime.now(timezone.utc).isoformat()}


# --- CompePharma editorial: earnings analysis, business analysis, opinion ----------------------------

_CP_SYSTEM = ("You are the editor of CompePharma, a business publication about the pharmaceutical industry (deals, launches, "
              "pricing, manufacturing, legal, earnings). You write sharp, well-structured financial and strategic journalism "
              "for industry executives, BD, commercial and finance teams. Absolute rule: use only facts in the material you are "
              "given; never invent numbers, quotes, people, guidance or analyst views. If a number is not in the material, do not "
              "state it. Describe, analyse and contextualise; never recommend buying or selling securities.")

_CP_EARN = """Write an EARNINGS ANALYSIS (700-950 words) of this drugmaker's quarterly results, based only on the results
press release it filed with the SEC (below). Cover: the quarter in numbers, what drove it (products, regions, pricing/volume
where stated), costs and margins if given, guidance, pipeline and regulatory catalysts mentioned, and what to watch next.
Be precise with figures and periods exactly as stated in the release; say "the company said" for its claims.

Company: {company} ({ticker}) · filed {date}
RELEASE:
<<<
{text}
>>>

Return ONLY JSON:
{{"title":"analytical headline, max 95 chars","dek":"one-sentence standfirst, max 200 chars",
"figures":[{{"label":"short label","value":"figure exactly as in the release"}}],
"sections":[{{"heading":"...","paragraphs":["..."]}}],"takeaways":["3-4 points for industry readers (not investment advice)"]}}
Give 3-6 figures. Use 4-6 sections."""

_CP_ANALYSIS = """Write an in-depth BUSINESS ANALYSIS (650-900 words) for CompePharma's {beat} desk, built around the lead story
and using the related stories for context: what happened, the strategic and commercial logic, competitive context, risks,
what to watch.

STORIES:
{records}

Return ONLY JSON:
{{"title":"specific headline, max 95 chars","dek":"one-sentence standfirst, max 200 chars",
"sections":[{{"heading":"...","paragraphs":["..."]}}],"takeaways":["3-4 implications for pharma executives"]}}"""

_CP_OPINION = """Write a signed-editorial OPINION column (550-750 words) headed "CompePharma View" on the most important
business theme in this material ({theme}). Take a clear position, argue from the facts given, acknowledge the strongest
counter-argument, end with what pharma leaders should do. Avoid these recent themes: {avoid}

MATERIAL:
{records}

Return ONLY JSON:
{{"title":"opinion headline, max 95 chars","dek":"one-sentence standfirst, max 200 chars",
"sections":[{{"heading":"...","paragraphs":["..."]}}],"takeaways":["2-3 things leaders should do"]}}"""

CP_BEATS = {"deals": "deals & M&A", "financing": "financing (IPOs, offerings, venture rounds, debt)", "launches": "approvals & launches", "pricing": "pricing & policy",
            "manufacturing": "manufacturing & supply", "legal": "legal", "marketing": "marketing & sales",
            "pipeline": "pipeline & R&D", "earnings": "earnings", "industry": "industry"}


def _parse_json(raw: str) -> dict:
    t = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        return json.loads(t)
    except Exception:
        # common model slip: raw newlines inside strings
        return json.loads(re.sub(r"(?<!\\)\n", " ", t))


def _cp_claude(prompt: str, max_tokens: int = 4200) -> dict | None:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    last = None
    for attempt in range(2):
        msg = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, system=_CP_SYSTEM,
                                     messages=[{"role": "user", "content": prompt + (
                                         "\n\nReturn strictly valid JSON (escape quotes inside strings)." if attempt else "")}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return _parse_json(raw)
        except Exception as e:
            last = e
    raise ValueError(f"invalid JSON from model: {last}")


def _cp_store(art: dict) -> None:
    art["site"] = "compepharma"
    pd.analysis_put(f"cparticle:{art['slug']}", hashlib.sha1(art["slug"].encode()).hexdigest(),
                    json.dumps(art, ensure_ascii=False), art.get("model", ""))


def cp_list(limit: int = 30, kind: str | None = None) -> list[dict]:
    pd.init()
    with store._engine.connect() as c:
        rows = c.execute(select(pd.analyses.c.body).where(pd.analyses.c.id.like("cparticle:%"))
                         .order_by(pd.analyses.c.created_at.desc()).limit(400)).all()
    out = []
    for (b,) in rows:
        try:
            a = json.loads(b)
        except Exception:
            continue
        if kind and a.get("type") != kind:
            continue
        out.append(a)
    out.sort(key=lambda a: (a.get("date", ""), a.get("created", "")), reverse=True)
    return out[:limit]


def cp_get(slug: str) -> dict | None:
    a = pd.analysis_get(f"cparticle:{slug}")
    try:
        return json.loads(a["body"]) if a else None
    except Exception:
        return None


def _biz_rows(days: int = 6) -> list[dict]:
    from . import news_pages as np_
    resp = np_.api_business_news(limit=400, days=days)
    return json.loads(resp.body).get("items", [])


def _brec(r: dict) -> str:
    return (f"- [{r['date']}] {r.get('publisher')}{(' (' + r['ticker'] + ')') if r.get('ticker') else ''}: {r['title']}. "
            f"{(r.get('summary') or '')[:800]}")


def generate_cp_daily(target: int = 10, earnings_n: int = 4, opinions: int = 3) -> dict:
    """CompePharma: `target` articles a day: earnings analyses from SEC results releases, business analyses, opinion."""
    from . import earnings as earn
    if not settings.anthropic_api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}
    pd.init()
    today = date.today().isoformat()
    have = _todays("cparticle", today)
    made, errors = [], []
    done_src = {s.get("url") for a in cp_list(400) for s in a.get("sources", [])[:1]}
    n_have = {"earnings": 0, "analysis": 0, "opinion": 0}
    for a in have:
        n_have[a.get("type", "analysis")] = n_have.get(a.get("type", "analysis"), 0) + 1
    # 1. earnings analyses: biggest new results releases first
    want_e = max(0, earnings_n - n_have["earnings"])
    covered = {(a.get("company") or "").lower() for a in cp_list(400) if a.get("type") == "earnings"}
    rel, seen_co = [], set()
    for r in earn.latest(days=10, limit=60):
        co = (r.get("company") or "").lower()
        if r["url"] in done_src or not r.get("text") or co in covered or co in seen_co:
            continue          # one earnings analysis per company per results release season
        seen_co.add(co)
        rel.append(r)
    for r in rel:
        if want_e <= 0:
            break
        try:
            j = _cp_claude(_CP_EARN.format(company=r["company"], ticker=r.get("ticker") or "", date=r["date"],
                                           text=r["text"][:16000]))
        except Exception as e:
            errors.append(f"earnings {r['company']}: {e}")
            continue
        if not j or not j.get("title"):
            continue
        slug = f"{today}-{_slug(j['title'])}"
        _cp_store({"slug": slug, "type": "earnings", "beat": "earnings", "title": j["title"][:140],
                   "dek": (j.get("dek") or "")[:260], "date": today, "company": r["company"], "ticker": r.get("ticker"),
                   "period": r.get("period"), "figures": [f for f in j.get("figures", []) if isinstance(f, dict)][:6],
                   "sections": j.get("sections", [])[:7], "takeaways": j.get("takeaways", [])[:5],
                   "model": settings.anthropic_model,
                   "sources": [{"title": f"{r['company']}: quarterly results release (SEC filing)", "url": r["url"],
                                "publisher": "SEC EDGAR", "date": r["date"]}]})
        made.append(slug)
        want_e -= 1
    # 2. business analyses
    biz = [b for b in _biz_rows() if b.get("summary")]
    want_o = max(0, opinions - n_have["opinion"])
    want_a = max(0, target - len(have) - len(made) - want_o)
    for beat in ["deals", "financing", "launches", "pricing", "manufacturing", "legal", "pipeline", "marketing", "industry"] * 2:
        if want_a <= 0:
            break
        cands = [b for b in biz if b["beat"] == beat and b.get("vigieye_url") not in done_src]
        cands.sort(key=lambda b: b.get("score") or 0, reverse=True)
        if not cands:
            continue
        lead = cands[0]
        done_src.add(lead.get("vigieye_url"))
        related = [b for b in biz if b["beat"] == beat and b is not lead][:7]
        try:
            j = _cp_claude(_CP_ANALYSIS.format(beat=CP_BEATS.get(beat, beat),
                                               records="LEAD:\n" + _brec(lead) + "\nRELATED:\n" + "\n".join(_brec(b) for b in related)))
        except Exception as e:
            errors.append(f"analysis {beat}: {e}")
            continue
        if not j or not j.get("title"):
            continue
        slug = f"{today}-{_slug(j['title'])}"
        _cp_store({"slug": slug, "type": "analysis", "beat": beat, "title": j["title"][:140], "dek": (j.get("dek") or "")[:260],
                   "date": today, "sections": j.get("sections", [])[:7], "takeaways": j.get("takeaways", [])[:5],
                   "model": settings.anthropic_model,
                   "sources": [{"title": b["title"], "url": b.get("source_url"), "publisher": b.get("publisher"), "date": b["date"],
                                "vigieye_url": b.get("vigieye_url")} for b in [lead] + related[:4]]})
        made.append(slug)
        want_a -= 1
    # 3. opinion: one on earnings season (if releases), the rest on business themes
    recent = "; ".join(a["title"] for a in cp_list(20, kind="opinion")[:10]) or "none"
    themes = []
    if rel:
        themes.append(("earnings season: what the latest results say about the industry",
                       "\n".join(f"- {r['company']} ({r.get('period') or 'latest quarter'}): {r.get('headline') or ''}. "
                                 f"{(r.get('text') or '')[:1200]}" for r in rel[:6])))
    for beat in ("pricing", "deals", "financing", "manufacturing", "launches", "legal"):
        rs = [b for b in biz if b["beat"] == beat][:10]
        if len(rs) >= 3:
            themes.append((CP_BEATS[beat], "\n".join(_brec(b) for b in rs)))
    for theme, material in themes:
        if want_o <= 0:
            break
        try:
            j = _cp_claude(_CP_OPINION.format(theme=theme, avoid=recent, records=material[:14000]), max_tokens=3200)
        except Exception as e:
            errors.append(f"opinion: {e}")
            continue
        if j and j.get("title"):
            slug = f"{today}-view-{_slug(j['title'])}"
            _cp_store({"slug": slug, "type": "opinion", "beat": "earnings" if theme.startswith("earnings") else
                       next((k for k, v in CP_BEATS.items() if v == theme), "industry"),
                       "title": j["title"][:140], "dek": (j.get("dek") or "")[:260], "date": today,
                       "sections": j.get("sections", [])[:6], "takeaways": j.get("takeaways", [])[:4],
                       "model": settings.anthropic_model, "sources": []})
            made.append(slug)
            want_o -= 1
            recent += "; " + j["title"]
    return {"generated": made, "errors": errors[:6], "at": datetime.now(timezone.utc).isoformat()}
