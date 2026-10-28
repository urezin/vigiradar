"""
VigiEye news portal pages.

  /                           news-portal home (lead story, four sections, industry headlines, data rails)
  /news                       all news
  /news/{section}             pharmacovigilance | regulatory | market-access | compliance | industry
  /news/item/{u|c}/{id}       article page for an official notice (u) or company release (c)
  /feed.xml                   RSS of VigiEye's own briefs (official + company)
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from . import catalysts as cal
from . import news as nw
from . import portal_data as pd
from . import portal_pages as pp
from . import store

router = APIRouter()
_e, _fmt, _base = cal._e, cal._fmt, cal._base

FLAG = {"EU": "🇪🇺", "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱",
        "IE": "🇮🇪", "SE": "🇸🇪", "BE": "🇧🇪", "INT": "🌐"}
SEC_COLOR = {"pharmacovigilance": "#e11d48", "regulatory": "#0284c7", "market-access": "#7c3aed",
             "compliance": "#b45309", "industry": "#0f766e"}


# --- the unified stream -----------------------------------------------------------

def _official(limit: int = 800) -> list[dict]:
    try:
        store.init()
        rows = store.query(limit=limit) if store.count() else []
    except Exception:
        rows = []
    out = []
    for r in rows:
        if not pp._relevant(r):
            continue
        out.append({"kind": "official", "id": r.get("id"), "section": nw.section_for_update(r),
                    "publisher": r.get("authority") or "", "country": r.get("country"), "title": r.get("title") or "",
                    "summary": r.get("summary") or "", "url": r.get("source_url") or "", "date": r.get("date") or "",
                    "impact": r.get("impact") or "med", "company": None, "ticker": None})
    return out


def _company_press(limit: int = 800) -> list[dict]:
    try:
        rows = nw.latest(limit=limit)
    except Exception:
        rows = []
    return [{**r, "impact": "high" if r["kind"] == "company" else "med"} for r in rows]


def stream(section: str | None = None, kinds: tuple = ("official", "company", "press"), limit: int = 60,
           offset: int = 0) -> list[dict]:
    rows = []
    if "official" in kinds:
        rows += _official()
    if "company" in kinds or "press" in kinds:
        rows += [r for r in _company_press() if r["kind"] in kinds]
    if section:
        rows = [r for r in rows if r["section"] == section]
    rows.sort(key=lambda r: (r["date"], r["kind"] != "press", r["impact"] == "high"), reverse=True)
    return rows[offset: offset + limit]


def _href(r: dict) -> str:
    if r["kind"] == "official" and r.get("id"):
        return f"/news/item/u/{r['id']}"
    if r["kind"] == "company":
        return f"/news/item/c/{r['id']}"
    return r.get("url") or "#"


def _badge(r: dict) -> str:
    sec = r.get("section") or "industry"
    s = f'<a class="sec" style="color:{SEC_COLOR.get(sec)}" href="/news/{sec}">{_e(nw.SECTIONS.get(sec, sec))}</a>'
    if r["kind"] == "official":
        src = f'{FLAG.get(r.get("country"), "")} {_e(r.get("publisher"))}'
        kind = '<span class="kb off">Official</span>'
    elif r["kind"] == "company":
        src = _e(r.get("publisher")) + (f' ({_e(r["ticker"])})' if r.get("ticker") else "")
        kind = '<span class="kb co">Company release</span>'
    else:
        src = _e(r.get("publisher"))
        kind = '<span class="kb pr">Press</span>'
    return f'<div class="meta2">{s}<span>·</span>{kind}<span>{src}</span><span>·</span><span>{_e(_fmt(r["date"]))}</span></div>'


def _card(r: dict, summary: bool = True, big: bool = False) -> str:
    ext = r["kind"] == "press"
    tgt = ' target="_blank" rel="noopener nofollow"' if ext else ""
    title = _e(r["title"]) + (' <span class="ext">↗</span>' if ext else "")
    summ = (f'<p>{_e(r.get("summary"))}</p>' if summary and r.get("summary") else "")
    cls = "story big" if big else "story"
    return f'<article class="{cls}">{_badge(r)}<h3><a href="{_e(_href(r))}"{tgt}>{title}</a></h3>{summ}</article>'


_NCSS = """
.ticker-strip{background:#050c17;padding:8px 0;border-bottom:1px solid #1e2d44}
.secnav{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 4px}
.secnav a{padding:6px 12px;border-radius:999px;border:1px solid var(--line);font-size:13.5px;color:var(--ink);font-weight:600}
.secnav a.on{color:#fff;border-color:transparent}
.lead{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:26px;margin-top:18px}
@media(max-width:860px){.lead{grid-template-columns:1fr}}
.story{padding:12px 0;border-bottom:1px solid var(--line)}.story:last-child{border-bottom:0}
.story h3{font-size:16px;line-height:1.35;margin-top:3px}.story h3 a{color:var(--ink)}
.story.big h3{font-size:26px;line-height:1.2;letter-spacing:-.01em}.story.big p{font-size:16.5px}
.story p{color:var(--mut);font-size:14px;margin-top:4px}
.meta2{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12.5px;color:var(--mut)}
.meta2 .sec{font-weight:700;text-transform:uppercase;letter-spacing:.04em;font-size:11.5px}
.kb{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line)}
.kb.off{border-color:#38bdf8;color:#0284c7}.kb.co{border-color:#f59e0b;color:#b45309}.kb.pr{color:var(--mut)}
.ext{font-size:12px;color:var(--mut)}
.grid4{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px;margin-top:26px}
@media(max-width:860px){.grid4{grid-template-columns:1fr}}
.block{border-top:3px solid var(--accent);padding-top:10px}
.block h2{font-size:19px;display:flex;justify-content:space-between;align-items:baseline}
.block h2 a{font-size:13px;font-weight:600}
.split{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:26px;margin-top:28px}
@media(max-width:860px){.split{grid-template-columns:1fr}}
.rail .panel{margin-bottom:18px}
.heads{columns:2;column-gap:26px}@media(max-width:640px){.heads{columns:1}}
.heads .story{break-inside:avoid}
.pager{display:flex;gap:10px;margin:18px 0}
"""


def _ticker() -> str:
    return ('<div class="ticker-strip"><div class="wrap"><script src="/embed/ticker.js" data-theme="dark" '
            'data-limit="24" data-title="Coming up" async></script></div></div>')


def _secnav(active: str | None = None) -> str:
    links = [f'<a class="{"on" if active is None else ""}" href="/news" style="{"background:var(--ink)" if active is None else ""}">All news</a>']
    for k, v in nw.SECTIONS.items():
        on = k == active
        links.append(f'<a class="{"on" if on else ""}" href="/news/{k}" '
                     f'style="{"background:" + SEC_COLOR[k] if on else "color:" + SEC_COLOR[k]}">{_e(v)}</a>')
    return f'<nav class="secnav" aria-label="Sections">{"".join(links)}</nav>'


def _page(title, desc, path, body, ld=None):
    return pp._page(title, desc, path, f"<style>{_NCSS}</style>{body}", ld)


def _rail_panel(title: str, items: list[dict], more: str, show_days: bool = False) -> str:
    if not items:
        return ""
    return pp._panel(title, items, more, show_days=show_days)


# --- home -------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home():
    t = date.today().isoformat()
    all_rows = stream(limit=600)
    briefs = [r for r in all_rows if r["kind"] in ("official", "company") and r.get("summary")]
    lead_pool = [r for r in briefs if r["impact"] == "high"] or briefs
    lead = lead_pool[0] if lead_pool else None
    top = [r for r in briefs if r is not lead][:6]
    used = {id(lead)} | {id(r) for r in top}

    def sec_items(k, n=5):
        rows = [r for r in all_rows if r["section"] == k and id(r) not in used and r["kind"] != "press"]
        if len(rows) < n:   # top up with press headlines on the same topic
            rows += [r for r in all_rows if r["section"] == k and r["kind"] == "press"][: n - len(rows)]
        return rows[:n]

    blocks = []
    for k in ("pharmacovigilance", "regulatory", "market-access", "compliance"):
        items = sec_items(k)
        if not items:
            continue
        blocks.append(f'<section class="block" style="border-color:{SEC_COLOR[k]}"><h2><span>{_e(nw.SECTIONS[k])}</span>'
                      f'<a href="/news/{k}">More →</a></h2>{"".join(_card(r) for r in items)}</section>')
    press = [r for r in all_rows if r["kind"] == "press"][:14]
    company = [r for r in all_rows if r["kind"] == "company"][:6]

    pdufa = pp._safe(lambda: pd.milestones_between(t, (date.today() + timedelta(days=60)).isoformat(), ["pdufa"], limit=7), [])
    expected = pp._safe(lambda: pd.milestones_between(t, (date.today() + timedelta(days=60)).isoformat(), ["ec_expected"], limit=5), [])
    approvals = [m for m in pp._safe(lambda: pd.milestones_between((date.today() - timedelta(days=45)).isoformat(), t,
                                                                    ["ec_decision", "fda_approval", "chmp_opinion_pos", "chmp_opinion_neg"],
                                                                    limit=200, desc=True), [])
                 if m["kind"] != "fda_approval" or m.get("impact") == "high"][:7]
    safety = pp._safe(lambda: pd.milestones_between((date.today() - timedelta(days=90)).isoformat(), t,
                                                     ["dhpc", "prac_recommendation", "referral_start"], limit=6, desc=True), [])
    adcomm = pp._safe(lambda: pd.milestones_between(t, (date.today() + timedelta(days=60)).isoformat(), ["adcomm"], limit=4), [])
    cal_next = [e for e in cal.load_events() if e["status"] != "past"][:6]

    lead_html = _card(lead, big=True) if lead else ""
    lead_box = ""
    if lead:
        lead_box = f"""<div class="lead"><div>{lead_html}</div><div><h2 style="font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)">Top stories</h2>
{"".join(_card(r, summary=False) for r in top)}</div></div>"""

    rail = "".join(x for x in [
        _rail_panel("PDUFA dates ahead", pdufa, "/pdufa", True),
        _rail_panel("Expected EU decisions", expected, "/approvals?tab=expected", True),
        _rail_panel("FDA advisory committees", adcomm, "/pdufa#adcomm", True),
    ] if x)
    cal_html = "".join(f'<div class="ms"><div class="top"><b>{_e(_fmt(e["date"]))}</b><span>{_e(e.get("body"))}</span>{cal._countdown(e)}</div>'
                       f'<h3><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a></h3></div>' for e in cal_next)
    if cal_html:
        rail += f'<section class="panel"><h2><span>EU regulatory calendar</span><a href="/calendar">All →</a></h2>{cal_html}</section>'

    data_row = "".join(x for x in [_rail_panel("Approvals & CHMP opinions", approvals, "/approvals"),
                                   _rail_panel("Safety actions (EMA data)", safety, "/safety")] if x)

    empty_note = "" if (lead or blocks or press) else \
        '<p class="note" style="margin-top:22px">The news feeds are loading for the first time. Check back in a few minutes.</p>'
    body = f"""{_ticker()}
<main class="wrap">
{_secnav()}
{empty_note}{lead_box}
<div class="grid4">{"".join(blocks)}</div>
<div class="split">
<div>{f'<section class="block" style="border-color:#f59e0b"><h2><span>Company announcements</span><a href="/news/industry">More →</a></h2>{"".join(_card(r) for r in company)}</section>' if company else ""}
{f'<section class="block" style="border-color:var(--mut);margin-top:24px"><h2><span>Industry headlines</span><a href="/news/industry">More →</a></h2><div class="heads">{"".join(_card(r, summary=False) for r in press)}</div><p class="note">Headlines link to the publisher. VigiEye does not reproduce their articles.</p></section>' if press else ""}
</div>
<aside class="rail">{rail}</aside></div>
{f'<div class="cols">{data_row}</div>' if data_row else ""}
<div class="lock"><h3>VigiEye Daily: the EU &amp; US regulatory briefing, every weekday</h3>
<p>Pharmacovigilance, regulatory, market access and compliance news, with approvals and PDUFA dates, in one email.</p>{pp._newsletter_box("home")}</div>
<p class="note">{pp.DISCLAIMER}</p>
</main>"""
    ld = [{"@context": "https://schema.org", "@type": "WebSite", "name": "VigiEye", "url": _base() + "/",
           "potentialAction": {"@type": "SearchAction", "target": _base() + "/drugs?q={query}",
                               "query-input": "required name=query"}},
          {"@context": "https://schema.org", "@type": "NewsMediaOrganization", "name": "VigiEye", "url": _base() + "/"}]
    return _page("VigiEye: biotech regulatory, pharmacovigilance, market access & compliance news",
                 "Daily EU and US biotech news for regulatory, pharmacovigilance, market access and compliance teams, "
                 "with PDUFA dates, EMA and FDA approvals and official sources.", "/", body, ld)


# --- section pages ------------------------------------------------------------------

_SECTION_RAIL = {
    "pharmacovigilance": lambda t: [("DHPCs & PRAC (EMA data)", ["dhpc", "prac_recommendation", "referral_start"], -120, "/safety"),
                                    ("Ongoing EU shortages", ["shortage"], None, "/safety")],
    "regulatory": lambda t: [("PDUFA dates ahead", ["pdufa"], 60, "/pdufa"),
                             ("Expected EU decisions", ["ec_expected"], 90, "/approvals?tab=expected"),
                             ("CHMP opinions", ["chmp_opinion_pos", "chmp_opinion_neg"], -60, "/approvals?tab=chmp")],
    "market-access": lambda t: [("EU approvals (new to HTA)", ["ec_decision"], -60, "/approvals?tab=eu"),
                                ("Expected EU decisions", ["ec_expected"], 90, "/approvals?tab=expected")],
    "compliance": lambda t: [("EU referrals", ["referral_start", "referral_opinion", "referral_ec"], -365, "/safety")],
    "industry": lambda t: [("PDUFA dates ahead", ["pdufa"], 60, "/pdufa"),
                           ("Latest approvals", ["ec_decision", "fda_approval"], -30, "/approvals")],
}


def _rail_for(section: str) -> str:
    t = date.today()
    out = []
    for title, kinds, days, more in _SECTION_RAIL.get(section, lambda t: [])(t):
        if days is None:
            rows = pp._safe(lambda: pd.milestones_between(None, None, kinds, limit=6, desc=True), [])
        elif days > 0:
            rows = pp._safe(lambda: pd.milestones_between(t.isoformat(), (t + timedelta(days=days)).isoformat(), kinds, limit=6), [])
        else:
            rows = pp._safe(lambda: pd.milestones_between((t + timedelta(days=days)).isoformat(), t.isoformat(), kinds, limit=6, desc=True), [])
        if kinds == ["ec_decision", "fda_approval"]:
            rows = [m for m in rows if m["kind"] != "fda_approval" or m.get("impact") == "high"]
        out.append(_rail_panel(title, rows, more, show_days=days is not None and days > 0))
    return "".join(out)


def _list_page(section: str | None, page: int):
    per = 40
    rows = stream(section=section, limit=per + 1, offset=(page - 1) * per)
    more = len(rows) > per
    rows = rows[:per]
    title = nw.SECTIONS.get(section, "All news") if section else "All news"
    blurb = nw.SECTION_BLURB.get(section, "Pharmacovigilance, regulatory, market access and compliance news from EU and US regulators, HTA bodies and the biotech industry.")
    items = "".join(_card(r) for r in rows) or '<p class="note">No stories in this section yet.</p>'
    base = f"/news/{section}" if section else "/news"
    pager = '<div class="pager">' + (f'<a class="btn ghost" href="{base}?page={page - 1}">← Newer</a>' if page > 1 else "") + \
            (f'<a class="btn ghost" href="{base}?page={page + 1}">Older →</a>' if more else "") + "</div>"
    rail = _rail_for(section or "industry")
    body = f"""{_ticker()}<main class="wrap">{_secnav(section)}
<h1 style="font-size:30px;margin-top:14px">{_e(title)}</h1><p class="note" style="font-size:15px">{_e(blurb)}</p>
<div class="split" style="margin-top:12px"><div>{items}{pager}</div><aside class="rail">{rail}
<section class="panel"><h2><span>VigiEye Daily</span></h2><p class="note">This section and the day's data, every weekday morning.</p>{pp._newsletter_box("section:" + (section or "all"))}</section></aside></div>
<p class="note">{pp.DISCLAIMER}</p></main>"""
    seo = {"pharmacovigilance": "Pharmacovigilance & drug safety news: PRAC, DHPCs, MHRA, FDA safety | VigiEye",
           "regulatory": "Biotech regulatory news: FDA, EMA, CHMP, MHRA approvals and guidance | VigiEye",
           "market-access": "Market access & HTA news: EU HTA, G-BA, HAS, ICER, pricing and reimbursement | VigiEye",
           "compliance": "Pharma compliance & enforcement news: GMP, warning letters, fraud, DOJ | VigiEye",
           "industry": "Biotech & pharma industry news: company announcements and headlines | VigiEye"}
    return _page(seo.get(section, "Biotech regulatory news: pharmacovigilance, regulatory, market access, compliance | VigiEye"),
                 blurb[:155], base + (f"?page={page}" if page > 1 else ""), body)


@router.get("/news", response_class=HTMLResponse, include_in_schema=False)
def news_all(page: int = 1):
    return _list_page(None, max(1, page))


@router.get("/news/{section}", response_class=HTMLResponse, include_in_schema=False)
def news_section(section: str, page: int = 1):
    if section not in nw.SECTIONS:
        raise HTTPException(404)
    return _list_page(section, max(1, page))


# --- article pages ------------------------------------------------------------------

@router.get("/news/item/{kind}/{nid}", response_class=HTMLResponse, include_in_schema=False)
def news_item(kind: str, nid: str):
    if kind == "u":
        try:
            store.init()
            u = store.get_update(nid)
        except Exception:
            u = None
        if not u:
            raise HTTPException(404)
        r = {"kind": "official", "id": u["id"], "section": nw.section_for_update(u), "publisher": u.get("authority"),
             "country": u.get("country"), "title": u.get("title"), "summary": u.get("summary"),
             "url": u.get("source_url"), "date": u.get("date"), "impact": u.get("impact"), "company": None, "ticker": None}
    elif kind == "c":
        n = nw.get(nid)
        if not n or n["kind"] != "company":
            raise HTTPException(404)
        r = {**n, "impact": "high"}
    else:
        raise HTTPException(404)
    related = [x for x in stream(section=r["section"], limit=12) if x.get("id") != r.get("id")][:8]
    src_label = "Read the official notice" if r["kind"] == "official" else "Read the company's press release (SEC filing)"
    summary = r.get("summary") or ""
    note = ("Summary written by VigiEye from the official notice." if r["kind"] == "official" else
            "Brief written by VigiEye from the company's press release filed with the SEC. Claims are the company's own.")
    if not summary:
        note = "Headline as published by the company. Open the filing for details."
    body = f"""{_ticker()}<main class="wrap"><div class="split"><div class="article" style="max-width:none">
{_secnav(r["section"])}
{_badge(r)}
<h1 style="font-size:32px;line-height:1.18;margin-top:8px">{_e(r["title"])}</h1>
{f'<p class="detail" style="margin-top:14px">{_e(summary)}</p>' if summary else ""}
<p style="margin-top:18px"><a class="btn" href="{_e(r.get("url"))}" target="_blank" rel="noopener">{_e(src_label)} ↗</a></p>
<p class="note">{_e(note)} AI-assisted; check the source before acting.</p>
<h2>More in {_e(nw.SECTIONS.get(r["section"], ""))}</h2>{"".join(_card(x, summary=False) for x in related)}
</div><aside class="rail">{_rail_for(r["section"])}
<section class="panel"><h2><span>VigiEye Daily</span></h2>{pp._newsletter_box("item")}</section></aside></div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "NewsArticle", "headline": r["title"][:110],
           "datePublished": r.get("date"), "description": summary[:300],
           "publisher": {"@type": "Organization", "name": "VigiEye", "url": _base() + "/"},
           "isBasedOn": r.get("url"), "articleSection": nw.SECTIONS.get(r["section"], "")}]
    return _page(f"{r['title'][:90]} | VigiEye", (summary or r["title"])[:155], f"/news/item/{kind}/{nid}", body, ld)


# --- RSS of VigiEye's own briefs -------------------------------------------------------

@router.get("/feed.xml", include_in_schema=False)
def rss():
    rows = [r for r in stream(limit=80) if r["kind"] != "press"]
    items = "".join(
        f"<item><title>{_e(r['title'])}</title><link>{_e(_base() + _href(r))}</link>"
        f"<guid isPermaLink=\"true\">{_e(_base() + _href(r))}</guid><pubDate>{_e(r['date'])}</pubDate>"
        f"<category>{_e(nw.SECTIONS.get(r['section'], ''))}</category><description>{_e(r.get('summary') or '')}</description></item>"
        for r in rows)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>VigiEye</title>'
           f'<link>{_base()}/</link><description>EU &amp; US biotech regulatory, PV, market access and compliance news</description>{items}</channel></rss>')
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def sitemap_urls() -> list[tuple[str, str, str]]:
    urls = [("/news", "hourly", "0.9")] + [(f"/news/{k}", "hourly", "0.9") for k in nw.SECTIONS]
    for r in stream(limit=600):
        if r["kind"] in ("official", "company") and r.get("id"):
            urls.append((_href(r), "monthly", "0.6"))
    return urls
