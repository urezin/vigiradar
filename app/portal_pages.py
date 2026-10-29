"""
VigiEye portal pages: the public EU + US pharma regulatory portal.

  /                      portal home (today's approvals, PDUFA dates, safety, news)
  /pdufa                 US PDUFA dates (company-announced, from SEC filings) + FDA AdComms
  /approvals             EU (CHMP opinions, EC decisions) + US (FDA) approvals
  /safety                PRAC referrals & recommendations, DHPCs, EU shortages, FDA MedWatch
  /drugs                 search EU + US products
  /drugs/{eu|us}/{slug}  product page with milestones and in-depth AI analysis
  /companies/{slug}      company page
  /insights              weekly in-depth reviews (AI, grounded on the week's records)
  /newsletter            daily newsletter sign-up + preview
  /api/ticker            upcoming dates for the embeddable ticker
  /embed/ticker.js       one-line scrolling ticker for any website (e.g. pqra.gr)
  /embed                 ticker demo + copy-paste snippet

Admin (X-Admin-Token): /admin/portal/ingest, /admin/portal/status,
/admin/portal/analyse, /admin/portal/send-daily
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import catalysts as cal
from . import portal_data as pd
from . import store
from .config import settings

log = logging.getLogger("vigiradar.portal")
router = APIRouter()

_e = cal._e
_fmt = cal._fmt
_base = cal._base

FLAG = {"EU": "🇪🇺", "US": "🇺🇸"}
REGION_NAME = {"EU": "EU", "US": "US"}

DISCLAIMER = ("Information from official EMA, FDA, Federal Register and SEC records. Dates marked "
              "<b>company-announced</b> come from the company's own SEC filing; dates marked <b>estimate</b> are "
              "calculated, not official. Not investment or medical advice; always confirm on the source.")


# --- shared bits ------------------------------------------------------------

_PCSS = """
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px;margin-top:22px}
@media(max-width:860px){.cols{grid-template-columns:1fr}}
.panel{border:1px solid var(--line);border-radius:14px;padding:16px 18px;background:var(--bg)}
.panel h2{font-size:17px;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.panel h2 a{font-size:13px;font-weight:600}
.ms{padding:10px 0;border-bottom:1px solid var(--line)}.ms:last-child{border-bottom:0}
.ms .top{font-size:12.5px;color:var(--mut);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.ms h3{font-size:15px;line-height:1.35;margin-top:3px}.ms h3 a{color:var(--ink)}
.ms p{font-size:13.5px;color:var(--mut);margin-top:2px}
.tag{font-size:11.5px;padding:1px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut);white-space:nowrap}
.tag.co{border-color:#f59e0b;color:#b45309}.tag.est{border-style:dashed}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 6px}
.tabs a{padding:7px 13px;border-radius:999px;border:1px solid var(--line);font-size:14px;color:var(--ink)}
.tabs a.on{background:var(--accent);border-color:var(--accent);color:#fff}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:18px}
@media(max-width:640px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:12px;padding:12px}
.kpi b{display:block;font-size:22px;color:#7dd3fc}.kpi span{font-size:12.5px;color:#cbd5e1}
.factgrid{display:grid;grid-template-columns:170px 1fr;gap:6px 14px;border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0;font-size:15px}
@media(max-width:560px){.factgrid{grid-template-columns:1fr}}
.factgrid dt{color:var(--mut)}
.analysis{border-left:3px solid var(--accent);padding:4px 0 4px 16px;margin:14px 0}
.analysis h3{font-size:16px;margin:14px 0 4px}.analysis p{margin:6px 0}
.note{font-size:12.5px;color:var(--mut);margin-top:10px}
.search{display:flex;gap:8px;margin:18px 0;flex-wrap:wrap}.search input{flex:1;min-width:200px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font:inherit;background:var(--bg);color:var(--ink)}
table.list{width:100%;border-collapse:collapse;font-size:14px}table.list td,table.list th{padding:9px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
table.list th{color:var(--mut);font-weight:600;font-size:12.5px}
"""


def _page(title, desc, path, body, ld=None):
    return cal._page(title, desc, f"{_base()}{path}", f"<style>{_PCSS}</style>{body}", ld)


def _source_tag(m: dict) -> str:
    st = m.get("source_type")
    if st == "company_filing":
        return '<span class="tag co" title="Date announced by the company in an SEC filing">company-announced</span>'
    if st == "estimate":
        return '<span class="tag est" title="Calculated, not an official date">estimate</span>'
    return ""


def _drug_href(m: dict) -> str | None:
    did = m.get("drug_id") or ""
    if did.startswith(("eu-", "us-")):
        return f"/drugs/{did[:2]}/{did[3:]}"
    return None


def _ms_item(m: dict, show_days: bool = False) -> str:
    href = _drug_href(m)
    title = _e(m.get("title"))
    title_html = f'<a href="{href}">{title}</a>' if href else title
    days = ""
    if show_days:
        try:
            n = (date.fromisoformat(m["date"]) - date.today()).days
            days = (f'<span class="cd {"hot" if n <= 7 else "soon" if n <= 30 else ""}">'
                    f'{"today" if n == 0 else ("in %d days" % n if n > 0 else "%d days ago" % -n)}</span>')
        except Exception:
            pass
    detail, co = _e(m.get("detail")), ""
    if m.get("company_slug") and m.get("company"):
        link = f'<a href="/companies/{_e(m["company_slug"])}">{_e(m["company"])}</a>'
        if _e(m["company"]) in detail:
            detail = detail.replace(_e(m["company"]), link, 1)   # link the existing mention
        else:
            co = " · " + link
    src = (f' · <a href="{_e(m.get("source_url"))}" target="_blank" rel="noopener nofollow">source</a>'
           if m.get("source_url") else "")
    return (f'<div class="ms"><div class="top"><b>{_e(_fmt(m["date"]))}</b>'
            f'<span>{FLAG.get(m.get("region"), "")} {_e(pd.KIND_LABEL.get(m.get("kind"), m.get("kind")))}</span>'
            f'{_source_tag(m)}{days}</div><h3>{title_html}</h3>'
            f'<p>{detail}{co}{src}</p></div>')


def _panel(title: str, items: list[dict], more: str | None = None, empty: str = "Nothing listed yet.",
           show_days: bool = False) -> str:
    inner = "".join(_ms_item(m, show_days) for m in items) or f'<p class="note">{_e(empty)}</p>'
    link = f'<a href="{more}">All →</a>' if more else ""
    return f'<section class="panel"><h2><span>{_e(title)}</span>{link}</h2>{inner}</section>'


def _today() -> str:
    return date.today().isoformat()


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _ahead(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _safe(fn, default):
    try:
        pd.init()
        return fn()
    except Exception as e:
        log.warning("[portal] query failed: %s", e)
        return default


def _relevant(r: dict) -> bool:
    from .sources import eurlex_relevant
    if "eur-lex" in (r.get("authority") or "").lower():
        # stored before the feed filter existed; also drop rows whose own summary says "no bearing"
        s = (r.get("summary") or "").lower()
        return eurlex_relevant(r.get("title", ""), "") and "no bearing" not in s and "no stated bearing" not in s
    return True


def _news(limit=8, country=None):
    from . import main as _main
    try:
        rows, _ = _main._current_updates(country=country)
        return [r for r in rows if _relevant(r)][:limit]
    except Exception:
        return []


def _news_html(rows):
    out = []
    for r in rows:
        out.append(f'<div class="ms"><div class="top"><b>{_e(_fmt(r.get("date", "")))}</b>'
                   f'<span>{FLAG.get(r.get("country"), r.get("country", ""))} {_e(r.get("authority") or r.get("auth"))}</span>'
                   f'<span class="tag">{_e(r.get("subject"))}</span></div>'
                   f'<h3>{_e(r.get("title"))}</h3><p>{_e(r.get("summary"))}'
                   + (f' · <a href="{_e(r.get("source_url"))}" target="_blank" rel="noopener nofollow">source</a>'
                      if r.get("source_url") else "") + '</p></div>')
    return "".join(out) or '<p class="note">No news yet.</p>'


def _newsletter_box(src: str) -> str:
    return f"""<form class="sub" data-src="{_e(src)}" onsubmit="return vgSubD(this)">
<input type="email" required placeholder="Work email" aria-label="Work email">
<button class="btn" type="submit">Get VigiEye Daily</button></form><div class="msg" aria-live="polite"></div>
<script>async function vgSubD(f){{const i=f.querySelector('input'),m=f.parentNode.querySelector('.msg');m.textContent='…';
try{{const r=await fetch('/newsletter/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{email:i.value,source:f.dataset.src,frequency:'daily'}})}});const j=await r.json();
m.textContent=j.message||j.error;if(r.ok)i.value='';}}catch(e){{m.textContent='Could not subscribe right now.';}}return false;}}</script>"""


# --- home -------------------------------------------------------------------

@router.get("/overview", response_class=HTMLResponse, include_in_schema=False)
def portal_home():
    t = _today()
    pdufa = _safe(lambda: pd.milestones_between(t, _ahead(120), ["pdufa"], limit=8), [])
    adcomm = _safe(lambda: pd.milestones_between(t, _ahead(90), ["adcomm"], limit=4), [])
    approvals = [m for m in _safe(lambda: pd.milestones_between(_ago(60), t, ["ec_decision", "fda_approval"], limit=200, desc=True), [])
                 if m["kind"] == "ec_decision" or m.get("impact") == "high"][:10]
    opinions = _safe(lambda: pd.milestones_between(_ago(60), t, ["chmp_opinion_pos", "chmp_opinion_neg"], limit=8, desc=True), [])
    expected = _safe(lambda: pd.milestones_between(t, _ahead(90), ["ec_expected"], limit=6), [])
    safety = _safe(lambda: pd.milestones_between(_ago(60), t, ["prac_recommendation", "referral_start", "dhpc"], limit=8, desc=True), [])
    cnt = _safe(pd.counts, {"drugs": {}, "milestones": {}})
    cal_events = [e for e in cal.load_events() if e["status"] != "past"]
    n_pdufa = len(_safe(lambda: pd.milestones_between(t, _ahead(365), ["pdufa"], limit=2000), []))
    n_eu = cnt["drugs"].get("EU", 0)
    n_us_appr = len(_safe(lambda: pd.milestones_between(_ago(90), t, ["fda_approval"], limit=2000), []))
    n_eu_appr = len(_safe(lambda: pd.milestones_between(_ago(90), t, ["ec_decision"], limit=2000), []))
    body = f"""<header class="hero"><div class="wrap">
<h1>EU &amp; US pharma regulatory intelligence, every day</h1>
<p>FDA and EU approvals, CHMP opinions, PDUFA dates, PRAC safety actions and regulatory news from EMA, FDA, the
Commission and national agencies. Every item links to its official source.</p>
<div class="kpis"><div class="kpi"><b>{n_pdufa}</b><span>upcoming PDUFA dates</span></div>
<div class="kpi"><b>{n_us_appr}</b><span>FDA approvals, last 90 days</span></div>
<div class="kpi"><b>{n_eu_appr}</b><span>EU approvals, last 90 days</span></div>
<div class="kpi"><b>{n_eu:,}</b><span>EU medicines tracked</span></div></div>
{_newsletter_box("home-hero")}
</div></header>
<main class="wrap">
<div class="cols">
{_panel("Upcoming PDUFA dates (US)", pdufa, "/pdufa", "PDUFA dates load after the first data refresh.", True)}
{_panel("Latest approvals (EU + US)", approvals, "/approvals")}
{_panel("CHMP opinions (EU)", opinions, "/approvals?tab=chmp")}
{_panel("Safety: PRAC, referrals, DHPCs", safety, "/safety")}
{_panel("Expected EU decisions", expected, "/approvals?tab=expected", show_days=True)}
{_panel("FDA advisory committees", adcomm, "/pdufa#adcomm", "No upcoming advisory committee meetings listed.", True)}
</div>
<div class="cols">
<section class="panel"><h2><span>Regulatory news</span><a href="/radar">All →</a></h2>{_news_html(_news(8))}</section>
<section class="panel"><h2><span>EU regulatory calendar</span><a href="/calendar">All →</a></h2>
{"".join(f'<div class="ms"><div class="top"><b>{_e(_fmt(e["date"]))}</b><span>{_e(e.get("body"))}</span>{cal._countdown(e)}</div><h3><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a></h3></div>' for e in cal_events[:8])}
</section></div>
<div class="lock"><h3>Monitor your own products and markets</h3>
<p>VigiEye subscribers get these updates filtered to their countries, subjects and products, with alerts, an audit trail and the compliance, medical-monitoring and literature modules.</p>
<a class="btn" href="/welcome#pricing">See plans</a> <a class="btn ghost" href="/hub">Open workspace</a></div>
<p class="note">{DISCLAIMER}</p>
</main>"""
    ld = [{"@context": "https://schema.org", "@type": "WebSite", "name": "VigiEye", "url": _base() + "/",
           "potentialAction": {"@type": "SearchAction", "target": _base() + "/drugs?q={query}",
                               "query-input": "required name=query"}}]
    return _page("VigiEye: EU & US pharma regulatory news, PDUFA dates, EMA & FDA approvals",
                 "Daily EU and US pharma regulatory intelligence: FDA and EMA approvals, CHMP opinions, PDUFA dates, "
                 "PRAC safety actions and regulatory news, each with its official source.", "/", body, ld)


# --- PDUFA ------------------------------------------------------------------

@router.get("/pdufa", response_class=HTMLResponse, include_in_schema=False)
def pdufa_page(past: int = 0):
    t = _today()
    up = _safe(lambda: pd.milestones_between(t, _ahead(540), ["pdufa"], limit=1000), [])
    recent = _safe(lambda: pd.milestones_between(_ago(120), _ago(1), ["pdufa"], limit=400, desc=True), [])
    adcomm = _safe(lambda: pd.milestones_between(_ago(14), _ahead(180), ["adcomm"], limit=100), [])
    rows = "".join(_ms_item(m, True) for m in up) or '<p class="note">No upcoming PDUFA dates found yet.</p>'
    body = f"""<header class="hero"><div class="wrap"><h1>PDUFA calendar: upcoming FDA decision dates</h1>
<p>FDA target action dates for new drugs and biologics, as announced by the companies in their SEC filings, plus FDA
advisory committee meetings from the Federal Register.</p>
<div class="stats"><span class="stat"><b>{len(up)}</b> upcoming PDUFA dates</span><span class="stat"><b>{len(adcomm)}</b> advisory committee meetings</span></div>
{_newsletter_box("pdufa")}</div></header>
<main class="wrap">
<div class="search"><input id="q" type="search" placeholder="Filter by drug, company or indication" aria-label="Filter"></div>
<section class="panel" id="list"><h2><span>Upcoming PDUFA dates</span></h2>{rows}</section>
<section class="panel" id="adcomm" style="margin-top:22px"><h2><span>FDA advisory committee meetings</span></h2>
{"".join(_ms_item(m, True) for m in adcomm) or '<p class="note">None listed.</p>'}</section>
<section class="panel" style="margin-top:22px"><h2><span>Recent PDUFA dates (last 120 days)</span><a href="/approvals?tab=us">FDA approvals →</a></h2>
{"".join(_ms_item(m) for m in recent[:60]) or '<p class="note">None.</p>'}
<p class="note">A past PDUFA date doesn't tell you the outcome. Check the FDA approvals list or the company's announcement.</p></section>
<p class="note">How these dates are found: FDA doesn't publish PDUFA goal dates. Companies announce them in press
releases filed with the SEC (Form 8-K). VigiEye reads those filings and links each date to the filing it came from.
The most recent filing wins when a date is extended.</p>
<p class="note">{DISCLAIMER}</p></main>
<script>(function(){{const q=document.getElementById('q');q.addEventListener('input',()=>{{const v=q.value.toLowerCase();
document.querySelectorAll('#list .ms').forEach(el=>{{el.style.display=!v||el.textContent.toLowerCase().includes(v)?'':'none';}});}});}})();</script>"""
    return _page("PDUFA Calendar 2026–2027: upcoming FDA decision dates | VigiEye",
                 "Upcoming PDUFA target action dates for FDA drug and biologic applications, sourced from company SEC "
                 "filings, plus FDA advisory committee meetings.", "/pdufa", body)


# --- approvals --------------------------------------------------------------

_TABS = [("all", "All"), ("eu", "EU approvals"), ("us", "FDA approvals"), ("chmp", "CHMP opinions"),
         ("expected", "Expected EU decisions"), ("negative", "Refusals & withdrawals")]


@router.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
def approvals_page(tab: str = "all", days: int = 120):
    t = _today()
    days = max(7, min(days, 400))
    kinds = {"all": ["ec_decision", "fda_approval"], "eu": ["ec_decision"], "us": ["fda_approval"],
             "chmp": ["chmp_opinion_pos", "chmp_opinion_neg"], "expected": ["ec_expected"],
             "negative": ["chmp_opinion_neg", "eu_refusal", "eu_withdrawal"]}.get(tab, ["ec_decision", "fda_approval"])
    if tab == "expected":
        rows = _safe(lambda: pd.milestones_between(t, _ahead(180), kinds, limit=500), [])
    else:
        rows = _safe(lambda: pd.milestones_between(_ago(days), t, kinds, limit=800, desc=True), [])
    tabs = "".join(f'<a class="{"on" if k == tab else ""}" href="/approvals?tab={k}">{_e(v)}</a>' for k, v in _TABS)
    body = f"""<header class="hero"><div class="wrap"><h1>EU &amp; FDA drug approvals and CHMP opinions</h1>
<p>New marketing authorisations from the European Commission and FDA, CHMP opinions and expected EU decisions,
straight from EMA and FDA data.</p>{_newsletter_box("approvals")}</div></header>
<main class="wrap"><div class="tabs">{tabs}</div>
<div class="search"><input id="q" type="search" placeholder="Filter by name, substance or company" aria-label="Filter"></div>
<section class="panel" id="list"><h2><span>{_e(dict(_TABS).get(tab, "All"))}: {len(rows)}</span></h2>
{"".join(_ms_item(m, tab == "expected") for m in rows) or '<p class="note">Nothing in this period yet.</p>'}</section>
<p class="note">EU dates are EMA's own records. FDA rows are original NDA/BLA approvals from Drugs@FDA, which excludes
generics (ANDAs). "Expected EC decision" is an estimate: the CHMP opinion date plus 67 days, the usual Commission
decision period.</p>
<p class="note">{DISCLAIMER}</p></main>
<script>(function(){{const q=document.getElementById('q');q.addEventListener('input',()=>{{const v=q.value.toLowerCase();
document.querySelectorAll('#list .ms').forEach(el=>{{el.style.display=!v||el.textContent.toLowerCase().includes(v)?'':'none';}});}});}})();</script>"""
    return _page("EU & FDA Drug Approvals and CHMP Opinions | VigiEye",
                 "Latest European Commission and FDA drug approvals, CHMP positive and negative opinions and expected "
                 "EU decisions, from official EMA and FDA data.", "/approvals" + ("" if tab == "all" else f"?tab={tab}"),
                 body)


# --- safety -----------------------------------------------------------------

@router.get("/safety-data", response_class=HTMLResponse, include_in_schema=False)
def safety_page():
    t = _today()
    prac = _safe(lambda: pd.milestones_between(_ago(365), t, ["prac_recommendation"], limit=60, desc=True), [])
    refs = _safe(lambda: pd.milestones_between(_ago(365), t, ["referral_start", "referral_opinion", "referral_ec"], limit=60, desc=True), [])
    dhpc = _safe(lambda: pd.milestones_between(_ago(180), t, ["dhpc"], limit=60, desc=True), [])
    short = _safe(lambda: pd.milestones_between(None, None, ["shortage"], limit=200, desc=True), [])
    us = [r for r in _news(60, "US") if "medwatch" in (r.get("authority") or "").lower()
          or "recall" in (r.get("title") or "").lower()][:20]
    signals = [r for r in _news(120, "EU") if r.get("subject") in ("Signal management", "PSUR / PBRER", "RMP & PASS")
               or "prac" in (r.get("title") or "").lower()][:12]
    body = f"""<header class="hero"><div class="wrap"><h1>Pharmacovigilance &amp; safety: PRAC, referrals, DHPCs, shortages</h1>
<p>EU safety actions from EMA's PRAC and CHMP, safety letters to healthcare professionals, ongoing EU shortages,
and FDA MedWatch alerts.</p>{_newsletter_box("safety")}</div></header>
<main class="wrap"><div class="cols">
{_panel("PRAC recommendations", prac)}
{_panel("Referrals (EU)", refs)}
{_panel("DHPCs: safety letters to HCPs", dhpc)}
{_panel("Ongoing EU shortages", short)}
</div>
<div class="cols"><section class="panel"><h2><span>PRAC &amp; EU pharmacovigilance news</span><a href="/radar">All news →</a></h2>{_news_html(signals)}</section>
<section class="panel"><h2><span>FDA MedWatch &amp; recalls (US)</span><a href="/radar">All news →</a></h2>{_news_html(us)}</section>
</div><div class="cols"><section class="panel"><h2><span>EU pharmacovigilance dates</span><a href="/calendar">Calendar →</a></h2>
{"".join(f'<div class="ms"><div class="top"><b>{_e(_fmt(e["date"]))}</b><span>{_e(e.get("body"))}</span>{cal._countdown(e)}</div><h3><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a></h3></div>' for e in [e for e in cal.load_events() if e["status"] != "past" and e.get("body") in ("PRAC", "CMDh")][:8]) or '<p class="note">None listed.</p>'}
</section></div><p class="note">{DISCLAIMER}</p></main>"""
    return _page("PRAC Recommendations, EU Referrals, DHPCs & Shortages | VigiEye",
                 "EU pharmacovigilance tracker: PRAC recommendations, safety referrals, DHPC letters, ongoing EU medicine "
                 "shortages and FDA MedWatch alerts, with official sources.", "/safety", body)


# --- drugs ------------------------------------------------------------------

def _flags(d: dict) -> list[str]:
    try:
        return json.loads(d.get("flags") or "[]")
    except Exception:
        return []


@router.get("/drugs", response_class=HTMLResponse, include_in_schema=False)
def drugs_search(q: str = "", region: str = ""):
    rows = []
    if q.strip():
        def run():
            from sqlalchemy import select, or_, func
            s = select(pd.drugs).where(or_(func.lower(pd.drugs.c.name).like(f"%{q.lower()}%"),
                                           func.lower(pd.drugs.c.inn).like(f"%{q.lower()}%"),
                                           func.lower(pd.drugs.c.company).like(f"%{q.lower()}%"),
                                           func.lower(pd.drugs.c.therapeutic_area).like(f"%{q.lower()}%")))
            if region in ("EU", "US"):
                s = s.where(pd.drugs.c.region == region)
            return pd._rows(s.order_by(pd.drugs.c.decision_date.desc()).limit(150))
        rows = _safe(run, [])
    trs = "".join(
        f'<tr><td>{FLAG.get(d["region"], "")}</td><td><a href="/drugs/{d["region"].lower()}/{_e(d["slug"])}"><b>{_e(d["name"])}</b></a><br>'
        f'<small>{_e(d.get("inn"))}</small></td><td>{_e(d.get("status"))}</td>'
        f'<td><a href="/companies/{_e(d.get("company_slug"))}">{_e(d.get("company"))}</a></td>'
        f'<td>{_e(d.get("decision_date") or d.get("opinion_date") or "")}</td></tr>' for d in rows)
    table = (f'<table class="list"><tr><th></th><th>Product</th><th>Status</th><th>Company</th><th>Date</th></tr>{trs}</table>'
             if rows else ('<p class="note">No matches.</p>' if q else
                           '<p class="note">Search all EU-authorised medicines and recent FDA approvals by brand, substance, company or therapeutic area.</p>'))
    body = f"""<main class="wrap"><div class="article" style="max-width:none">
<h1 style="font-size:30px">Drug database: EU &amp; US</h1>
<form class="search" method="get" action="/drugs"><input name="q" value="{_e(q)}" placeholder="e.g. semaglutide, Roche, haemophilia" aria-label="Search drugs">
<select name="region" aria-label="Region" style="padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)"><option value="">EU + US</option>
<option value="EU" {"selected" if region == "EU" else ""}>EU</option><option value="US" {"selected" if region == "US" else ""}>US</option></select>
<button class="btn" type="submit">Search</button></form>{table}</div></main>"""
    return _page("EU & US Drug Database: EMA and FDA status | VigiEye",
                 "Search EU-authorised medicines and FDA-approved drugs: status, company, CHMP opinion and approval dates, "
                 "with links to EMA and FDA records.", "/drugs", body)


@router.get("/drugs/{region}/{slug}", response_class=HTMLResponse, include_in_schema=False)
def drug_page(region: str, slug: str):
    region = region.upper()
    if region not in ("EU", "US"):
        raise HTTPException(404)
    d = _safe(lambda: pd.drug_get(region, slug), None)
    if not d:
        raise HTTPException(404, "Not found")
    ms = _safe(lambda: pd.milestones_for_drug(d), [])
    twins = _safe(lambda: pd.drugs_by_inn(d.get("inn") or "", d["id"]), [])
    flags = _flags(d)
    extra = {}
    try:
        extra = json.loads(d.get("extra") or "{}")
    except Exception:
        pass
    facts = [("Region", f'{FLAG[region]} {region}'), ("Active substance", d.get("inn")),
             ("Company", f'<a href="/companies/{_e(d.get("company_slug"))}">{_e(d.get("company"))}</a>' if d.get("company") else ""),
             ("Status", d.get("status")), ("Therapeutic area", d.get("therapeutic_area")),
             ("CHMP opinion", _fmt(d["opinion_date"]) if d.get("opinion_date") else ""),
             ("EU approval" if region == "EU" else "FDA approval", _fmt(d["decision_date"]) if d.get("decision_date") else ""),
             ("Designations", ", ".join(flags)),
             ("Application", extra.get("application_number") or extra.get("ema_product_number")),
             ("ATC", extra.get("atc"))]
    fg = "".join(f"<dt>{_e(k)}</dt><dd>{v if k == 'Company' else _e(v)}</dd>" for k, v in facts if v)
    an = _safe(lambda: pd.analysis_get(f"drug:{d['id']}"), None)
    an_html = (f'<h2>In-depth analysis</h2><div class="analysis">{an["body"]}</div>'
               f'<p class="note">AI-generated on {_e(an["created_at"][:10])} from the official records on this page '
               f'(model {_e(an.get("model"))}). It can contain errors: check the sources.</p>') if an else ""
    tw = "".join(f'<li><a href="/drugs/{t["region"].lower()}/{_e(t["slug"])}">{FLAG.get(t["region"], "")} {_e(t["name"])}</a>'
                 f' <small>{_e(t.get("status"))} · {_e(t.get("company"))}</small></li>' for t in twins)
    ind = _e(d.get("indication"))
    src = d.get("source_url") or ""
    body = f"""<main class="wrap"><div class="article">
<p><a href="/drugs">← Drug database</a></p>
<h1 style="font-size:32px;line-height:1.15;margin-top:8px">{_e(d["name"])} <small style="font-size:18px;color:var(--mut)">({_e(d.get("inn"))})</small></h1>
<div class="chips" style="margin-top:8px">{"".join(f'<span class="chip">{_e(f)}</span>' for f in flags)}</div>
<dl class="factgrid">{fg}<dt>Official record</dt><dd><a href="{_e(src)}" target="_blank" rel="noopener">{_e(cal._host(src))}</a></dd></dl>
{f'<h2>Indication</h2><p>{ind}</p>' if ind else ''}
<h2>Regulatory milestones</h2>{"".join(_ms_item(m) for m in ms) or '<p class="note">No milestones recorded in the last year.</p>'}
{an_html}
{f'<h2>Same substance in other markets</h2><ul style="margin-left:18px">{tw}</ul>' if tw else ''}
<div class="lock"><h3>Follow {_e(d["name"])}</h3><p>Get VigiEye Daily: approvals, opinions, safety actions and PDUFA dates every morning.</p>{_newsletter_box("drug:" + d["id"])}</div>
<p class="note">{DISCLAIMER}</p></div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "Drug", "name": d["name"],
           "nonProprietaryName": d.get("inn") or None, "url": f"{_base()}/drugs/{region.lower()}/{slug}",
           "manufacturer": {"@type": "Organization", "name": d.get("company")} if d.get("company") else None,
           "sameAs": src or None}]
    ld[0] = {k: v for k, v in ld[0].items() if v}
    status = d.get("status") or ""
    return _page(f"{d['name']} ({d.get('inn') or ''}): {region} regulatory status & milestones | VigiEye"[:110],
                 f"{d['name']} ({d.get('inn')}) by {d.get('company')}: {region} status {status}, approval and opinion dates, "
                 "safety actions and analysis, with official sources."[:160],
                 f"/drugs/{region.lower()}/{slug}", body, ld)


# --- companies --------------------------------------------------------------

@router.get("/companies/{cslug}", response_class=HTMLResponse, include_in_schema=False)
def company_page(cslug: str):
    ds = _safe(lambda: pd.company_drugs(cslug), [])
    ms = _safe(lambda: pd.company_milestones(cslug), [])
    if not ds and not ms:
        raise HTTPException(404, "Not found")
    names = sorted({x.get("company") for x in ds + ms if x.get("company")}, key=len)
    name = names[0] if names else cslug
    t = _today()
    up = [m for m in ms if m["date"] >= t]
    past = [m for m in ms if m["date"] < t]
    trs = "".join(
        f'<tr><td>{FLAG.get(x["region"], "")}</td><td><a href="/drugs/{x["region"].lower()}/{_e(x["slug"])}"><b>{_e(x["name"])}</b></a>'
        f'<br><small>{_e(x.get("inn"))}</small></td><td>{_e(x.get("status"))}</td><td>{_e(x.get("decision_date") or "")}</td></tr>'
        for x in ds[:200])
    body = f"""<main class="wrap"><div class="article" style="max-width:none">
<h1 style="font-size:32px">{_e(name)}</h1>
<p class="note">{len(ds)} products tracked · names used in filings: {_e(", ".join(names[:6]))}</p>
<div class="cols">{_panel("Upcoming milestones", up[:20], empty="No upcoming milestones listed.", show_days=True)}
{_panel("Recent milestones", past[:20], empty="No recent milestones.")}</div>
<h2 style="margin-top:24px">Products</h2>
{f'<table class="list"><tr><th></th><th>Product</th><th>Status</th><th>Approved</th></tr>{trs}</table>' if trs else '<p class="note">No products recorded.</p>'}
<p class="note">{DISCLAIMER}</p></div></main>"""
    return _page(f"{name}: EU & US approvals, PDUFA dates and pipeline milestones | VigiEye"[:110],
                 f"{name} regulatory tracker: EU and FDA approvals, CHMP opinions, PDUFA dates and safety actions, "
                 "from official records."[:160], f"/companies/{cslug}", body,
                 [{"@context": "https://schema.org", "@type": "Organization", "name": name,
                   "url": f"{_base()}/companies/{cslug}"}])


# --- insights (weekly in-depth review) --------------------------------------

@router.get("/insights", response_class=HTMLResponse, include_in_schema=False)
def insights_index():
    def run():
        from sqlalchemy import select
        return pd._rows(select(pd.analyses).where(pd.analyses.c.id.like("week:%"))
                        .order_by(pd.analyses.c.id.desc()).limit(60))
    rows = _safe(run, [])
    items = "".join(f'<div class="ms"><h3><a href="/insights/{_e(r["id"][5:])}">EU &amp; US regulatory week in review: {_e(r["id"][5:])}</a></h3>'
                    f'<p>Published {_e(r["created_at"][:10])}</p></div>' for r in rows) or \
        '<p class="note">The first weekly review appears after the first Monday run.</p>'
    body = f"""<header class="hero"><div class="wrap"><h1>Insights: weekly in-depth regulatory review</h1>
<p>Every Monday: what the past week's EU and US approvals, opinions, PDUFA outcomes and safety actions mean, and what to watch next.</p>
{_newsletter_box("insights")}</div></header><main class="wrap"><section class="panel" style="margin-top:22px">{items}</section></main>"""
    return _page("Insights: EU & US pharma regulatory week in review | VigiEye",
                 "Weekly in-depth analysis of EU and US pharma regulation: approvals, CHMP opinions, PDUFA outcomes, "
                 "PRAC safety actions and what to watch next.", "/insights", body)


@router.get("/insights/{week}", response_class=HTMLResponse, include_in_schema=False)
def insight_page(week: str):
    a = _safe(lambda: pd.analysis_get(f"week:{week}"), None)
    if not a:
        raise HTTPException(404)
    body = f"""<main class="wrap"><div class="article"><p><a href="/insights">← Insights</a></p>
<h1 style="font-size:30px;margin-top:8px">EU &amp; US regulatory week in review: {_e(week)}</h1>
<div class="analysis">{a["body"]}</div>
<p class="note">AI-generated on {_e(a["created_at"][:10])} from the week's official records on VigiEye (model {_e(a.get("model"))}). It can contain errors: check the linked sources.</p>
<div class="lock"><h3>Get this every week, and the daily briefing</h3>{_newsletter_box("insight:" + week)}</div></div></main>"""
    return _page(f"EU & US regulatory week in review {week} | VigiEye",
                 f"In-depth review of EU and US pharma regulatory events for {week}: approvals, opinions, PDUFA dates "
                 "and safety actions.", f"/insights/{week}", body,
                 [{"@context": "https://schema.org", "@type": "Article", "headline": f"EU & US regulatory week in review {week}",
                   "datePublished": a["created_at"][:10], "publisher": {"@type": "Organization", "name": "VigiEye"}}])


# --- newsletter -------------------------------------------------------------

from pydantic import BaseModel


class NewsIn(BaseModel):
    email: str
    source: str = "portal"
    frequency: str = "daily"


@router.post("/newsletter/subscribe")
def newsletter_subscribe(body: NewsIn):
    import re
    email = (body.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 320:
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    freq = "daily" if body.frequency == "daily" else "weekly"
    try:
        store.init()
        store.subscriber_add(email, body.source or "portal", freq)
    except Exception:
        return JSONResponse({"error": "Could not subscribe right now, please try again."}, status_code=500)
    return {"ok": True, "message": "You're in. VigiEye Daily arrives every weekday morning."
            if freq == "daily" else "You're in. The week-ahead email arrives every Monday."}


def render_daily(email: str = "") -> tuple[str, str, int]:
    """Daily newsletter. Returns (subject, html, item_count)."""
    t = _today()
    y = _ago(3)   # covers weekends: Monday's edition includes Fri–Sun
    groups = [
        ("New approvals", pd.milestones_between(y, t, ["ec_decision", "fda_approval"], limit=30, desc=True)),
        ("CHMP opinions", pd.milestones_between(y, t, ["chmp_opinion_pos", "chmp_opinion_neg"], limit=20, desc=True)),
        ("Safety: PRAC, referrals, DHPCs", pd.milestones_between(y, t, ["prac_recommendation", "referral_start", "dhpc"], limit=20, desc=True)),
        ("PDUFA dates in the next 14 days", pd.milestones_between(t, _ahead(14), ["pdufa"], limit=30)),
        ("FDA advisory committees, next 14 days", pd.milestones_between(t, _ahead(14), ["adcomm"], limit=10)),
    ]
    from . import news_pages as _np, articles as _arts
    news = sorted([r for r in _np.stream(limit=400) if (r.get("date") or "") >= y], key=_np._score, reverse=True)[:12]
    arts = [a for a in _arts.list_articles(20) if (a.get("date") or "") >= _ago(1)][:10]
    calnext = [e for e in cal.load_events() if e["status"] != "past" and e["days"] <= 7]
    n = sum(len(g[1]) for g in groups) + len(news) + len(calnext) + len(arts)

    def li(m):
        href = _drug_href(m)
        link = f'{_base()}{href}' if href else (m.get("source_url") or f"{_base()}/")
        tag = " (company-announced)" if m.get("source_type") == "company_filing" else (
            " (estimate)" if m.get("source_type") == "estimate" else "")
        return (f'<tr><td style="padding:8px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
                f'<b>{_e(_fmt(m["date"]))}</b> · {FLAG.get(m.get("region"), "")} {_e(pd.KIND_LABEL.get(m["kind"], ""))}{_e(tag)}<br>'
                f'<a href="{_e(link)}" style="color:#0284c7;font-weight:600">{_e(m["title"])}</a><br>'
                f'<span style="color:#64748b">{_e(m.get("detail"))}</span></td></tr>')

    sections = ""
    if arts:
        sections += ('<h3 style="font:600 16px system-ui;margin:22px 0 4px">Today\'s analysis &amp; opinion</h3><table style="width:100%;border-collapse:collapse">'
                     + "".join(f'<tr><td style="padding:9px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
                               f'<span style="font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#be123c">'
                               f'{"Opinion" if a.get("type") == "opinion" else "Analysis"}</span><br>'
                               f'<a href="{_base()}/analysis/{_e(a["slug"])}" style="color:#0284c7;font-weight:700;font-size:15px">{_e(a["title"])}</a><br>'
                               f'<span style="color:#64748b">{_e(a.get("dek"))}</span></td></tr>' for a in arts) + "</table>")
    for title, rows in groups:
        if rows:
            sections += (f'<h3 style="font:600 16px system-ui;margin:22px 0 4px">{_e(title)}</h3>'
                         f'<table style="width:100%;border-collapse:collapse">{"".join(li(m) for m in rows)}</table>')
    if news:
        sections += ('<h3 style="font:600 16px system-ui;margin:22px 0 4px">Top regulatory, safety &amp; quality news</h3><table style="width:100%;border-collapse:collapse">'
                     + "".join(f'<tr><td style="padding:8px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
                               f'{FLAG.get(r.get("country"), r.get("country", ""))} {_e(r.get("publisher"))} · {_e(_fmt(r.get("date", "")))}<br>'
                               f'<a href="{_base()}{_np._href(r)}" style="color:#0284c7;font-weight:600">{_e(r.get("title"))}</a><br>'
                               f'<span style="color:#64748b">{_e(r.get("summary"))}</span></td></tr>' for r in news) + "</table>")
    if calnext:
        sections += ('<h3 style="font:600 16px system-ui;margin:22px 0 4px">EU calendar, next 7 days</h3><table style="width:100%;border-collapse:collapse">'
                     + "".join(f'<tr><td style="padding:6px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui"><b>{_e(_fmt(e["date"]))}</b> · '
                               f'<a href="{_base()}/calendar/{_e(e["slug"])}" style="color:#0284c7">{_e(e.get("title"))}</a></td></tr>' for e in calnext) + "</table>")
    if not sections:
        sections = '<p style="font:14px system-ui;color:#64748b">A quiet day: no new approvals, opinions or safety actions recorded.</p>'
    unsub = (f"{_base()}/calendar/unsubscribe?email={cal._q(email)}&t={cal._unsub_token(email)}" if email else "#")
    html = f"""<div style="max-width:640px;margin:auto;font:15px system-ui;color:#0f172a">
<p style="font:700 20px system-ui;margin:0">👁️ VigiEye Daily</p>
<p style="color:#64748b;margin-top:4px">{_e(datetime.now(timezone.utc).strftime("%A %d %B %Y"))} · EU &amp; US pharma regulatory briefing</p>
{sections}
<p style="margin-top:22px"><a href="{_base()}/" style="color:#0284c7">Portal</a> · <a href="{_base()}/pdufa" style="color:#0284c7">PDUFA calendar</a> ·
<a href="{_base()}/approvals" style="color:#0284c7">Approvals</a> · <a href="{_base()}/safety" style="color:#0284c7">Safety</a> · <a href="{_base()}/insights" style="color:#0284c7">Insights</a></p>
<p style="font-size:12px;color:#94a3b8;margin-top:20px">Sources: EMA, FDA, Federal Register, SEC EDGAR. Company-announced dates come from company filings. Not investment or medical advice.
You subscribed at vigi-eye.com. <a href="{unsub}" style="color:#94a3b8">Unsubscribe</a></p></div>"""
    subj_bits = [f"{len(groups[0][1])} approvals" if groups[0][1] else "", f"{len(groups[1][1])} CHMP opinions" if groups[1][1] else "",
                 f"{len(groups[3][1])} PDUFA dates ahead" if groups[3][1] else ""]
    subject = "VigiEye Daily: " + (", ".join(b for b in subj_bits if b) or "EU & US regulatory briefing")
    return subject, html, n


@router.get("/newsletter", response_class=HTMLResponse, include_in_schema=False)
def newsletter_page():
    body = f"""<header class="hero"><div class="wrap"><h1>VigiEye Daily: the EU &amp; US pharma regulatory briefing</h1>
<p>Every weekday morning: new EU and FDA approvals, CHMP opinions, PRAC safety actions and DHPCs, the PDUFA dates
coming up in the next two weeks, and the day's regulatory news. Free.</p>{_newsletter_box("newsletter-page")}
<p style="margin-top:14px"><a href="/newsletter/preview" style="color:#7dd3fc">See today's edition →</a></p></div></header>"""
    return _page("VigiEye Daily: free EU & US pharma regulatory newsletter",
                 "Free daily email with new EU and FDA approvals, CHMP opinions, PRAC safety actions, upcoming PDUFA dates "
                 "and regulatory news.", "/newsletter", body)


@router.get("/newsletter/preview", response_class=HTMLResponse, include_in_schema=False)
def newsletter_preview():
    pd.init()
    _, html, _ = render_daily("")
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<meta name="robots" content="noindex"><title>VigiEye Daily preview</title>'
                        f'<body style="background:#fff;padding:16px">{html}</body>')


# --- ticker (embeddable, e.g. on pqra.gr) -------------------------------------

def ticker_items(limit: int = 20, region: str = "") -> list[dict]:
    t = _today()
    items = []
    for e in cal.load_events():
        if e["status"] == "past" or e["impact"] != "high" or e["days"] > 120:
            continue
        items.append({"date": e["date"], "region": "EU", "label": e.get("body") or "EU",
                      "title": e.get("title"), "url": f"{_base()}/calendar/{e['slug']}"})
    for m in _safe(lambda: pd.milestones_between(t, _ahead(90), ["pdufa", "adcomm", "ec_expected"], limit=200), []):
        items.append({"date": m["date"], "region": m["region"],
                      "label": {"pdufa": "PDUFA", "adcomm": "FDA AdComm", "ec_expected": "EC decision (est.)"}[m["kind"]],
                      "title": m["title"].replace(": PDUFA target action date", "").replace(": expected EC decision", ""),
                      "url": f"{_base()}{_drug_href(m)}" if _drug_href(m) else f"{_base()}/pdufa"})
    if region in ("EU", "US"):
        items = [i for i in items if i["region"] == region]
    items.sort(key=lambda i: i["date"])
    return items[:max(1, min(limit, 60))]


@router.get("/api/ticker")
def api_ticker(limit: int = 20, region: str = ""):
    return JSONResponse({"items": ticker_items(limit, region.upper()), "home": _base() + "/"},
                        headers={"Cache-Control": "public, max-age=900", "Access-Control-Allow-Origin": "*"})


_TICKER_JS = r"""/* VigiEye upcoming-dates ticker. Usage:
<script src="https://vigi-eye.com/embed/ticker.js" data-limit="20" data-region="" data-theme="dark" async></script> */
(function(){
  var me = document.currentScript || (function(){var s=document.getElementsByTagName('script');return s[s.length-1];})();
  var base = (me.src.match(/^https?:\/\/[^/]+/)||[''])[0];
  var lim = me.getAttribute('data-limit')||'20', reg = me.getAttribute('data-region')||'';
  var theme = me.getAttribute('data-theme')||'dark', speed = parseFloat(me.getAttribute('data-speed')||'40');
  var label = me.getAttribute('data-title')||'Upcoming regulatory dates';
  var utm = me.getAttribute('data-utm')||(location.hostname||'embed');
  var host = document.createElement('div'); host.className='vigieye-ticker';
  var target = me.getAttribute('data-target') && document.getElementById(me.getAttribute('data-target'));
  if (target) target.appendChild(host); else me.parentNode.insertBefore(host, me);
  var root = host.attachShadow ? host.attachShadow({mode:'open'}) : host;
  var dark = theme !== 'light';
  var css = ':host{display:block}*{box-sizing:border-box}'
   + '.bar{display:flex;align-items:stretch;font:500 14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
   + 'background:'+(dark?'#0b1626':'#f1f5f9')+';color:'+(dark?'#e2e8f0':'#0f172a')+';border-radius:10px;overflow:hidden;'
   + 'border:1px solid '+(dark?'#1e2d44':'#e2e8f0')+'}'
   + '.lab{flex:none;display:flex;align-items:center;gap:6px;padding:9px 12px;font-weight:700;white-space:nowrap;'
   + 'background:linear-gradient(92deg,#0ea5e9,#14b8a6);color:#fff;text-decoration:none}'
   + '.vp{flex:1;overflow:hidden;position:relative;mask-image:linear-gradient(90deg,transparent,#000 24px,#000 calc(100% - 24px),transparent)}'
   + '.track{display:inline-flex;white-space:nowrap;will-change:transform;animation:roll var(--d,60s) linear infinite}'
   + '.vp:hover .track,.vp:focus-within .track{animation-play-state:paused}'
   + '@keyframes roll{from{transform:translateX(0)}to{transform:translateX(-50%)}}'
   + '.it{display:inline-flex;align-items:center;gap:8px;padding:9px 18px;color:inherit;text-decoration:none;border-right:1px solid '+(dark?'#1e2d44':'#e2e8f0')+'}'
   + '.it:hover .t{text-decoration:underline}.d{font-weight:700;color:'+(dark?'#7dd3fc':'#0369a1')+'}'
   + '.k{font-size:12px;padding:1px 8px;border-radius:999px;background:'+(dark?'#13233a':'#e0f2fe')+';color:'+(dark?'#93c5fd':'#075985')+'}'
   + '.n{font-size:12px;opacity:.75}'
   + '@media (prefers-reduced-motion: reduce){.track{animation:none}.vp{overflow-x:auto}}'
   + '@media (max-width:560px){.lab span{display:none}}';
  function esc(s){return String(s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function fmt(d){try{var x=new Date(d+'T00:00:00');return x.toLocaleDateString(undefined,{day:'numeric',month:'short'});}catch(e){return d;}}
  function days(d){var n=Math.round((new Date(d+'T00:00:00')-new Date(new Date().toDateString()))/864e5);return n<=0?'today':(n===1?'tomorrow':'in '+n+' days');}
  function u(href){return href+(href.indexOf('?')<0?'?':'&')+'utm_source='+encodeURIComponent(utm)+'&utm_medium=ticker';}
  fetch(base+'/api/ticker?limit='+encodeURIComponent(lim)+'&region='+encodeURIComponent(reg)).then(function(r){return r.json();}).then(function(j){
    var items=(j.items||[]); if(!items.length){host.style.display='none';return;}
    var flag={EU:'🇪🇺',US:'🇺🇸'};
    var one=items.map(function(i){return '<a class="it" target="_blank" rel="noopener" href="'+esc(u(i.url))+'">'
      +'<span class="d">'+esc(fmt(i.date))+'</span><span class="k">'+(flag[i.region]||'')+' '+esc(i.label)+'</span>'
      +'<span class="t">'+esc(i.title)+'</span><span class="n">'+esc(days(i.date))+'</span></a>';}).join('');
    root.innerHTML='<style>'+css+'</style><div class="bar" role="region" aria-label="'+esc(label)+'">'
      +'<a class="lab" target="_blank" rel="noopener" href="'+esc(u(j.home||base+'/'))+'">👁️ <span>'+esc(label)+'</span></a>'
      +'<div class="vp"><div class="track">'+one+'<span aria-hidden="true" style="display:inline-flex">'+one+'</span></div></div></div>';
    var tr=root.querySelector('.track'); requestAnimationFrame(function(){var w=tr.scrollWidth/2;tr.style.setProperty('--d',Math.max(20,w/speed)+'s');});
  }).catch(function(){host.style.display='none';});
})();
"""


@router.get("/embed/ticker.js", include_in_schema=False)
def ticker_js():
    return Response(_TICKER_JS, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


@router.get("/embed", response_class=HTMLResponse, include_in_schema=False)
def embed_page():
    snippet = f'<script src="{_base()}/embed/ticker.js" data-limit="20" data-theme="dark" async></script>'
    body = f"""<main class="wrap"><div class="article"><h1 style="font-size:30px">Embed the VigiEye ticker</h1>
<p>A scrolling bar of upcoming EU and US regulatory dates (PDUFA dates, EC decisions, EMA committee meetings and
EU deadlines) for any website. It updates itself, pauses on hover, and respects reduced-motion settings.</p>
<h2>Live preview (dark)</h2><div style="margin:12px 0"><script src="/embed/ticker.js" data-theme="dark" async></script></div>
<h2>Light theme, EU only</h2><div style="margin:12px 0"><script src="/embed/ticker.js" data-theme="light" data-region="EU" async></script></div>
<h2>Copy this snippet</h2><pre style="white-space:pre-wrap;background:var(--soft);border:1px solid var(--line);padding:12px;border-radius:10px;font-size:13px">{_e(snippet)}</pre>
<p>Options: <code>data-theme</code> = dark or light; <code>data-region</code> = EU, US or empty for both; <code>data-limit</code> = number of items;
<code>data-speed</code> = pixels per second (default 40); <code>data-title</code> = label text.</p>
<h2>WordPress</h2><p>Edit the page, add a <b>Custom HTML</b> block where the bar should appear, and paste the snippet.
To show it on every page, use a header plugin such as WPCode ("Header &amp; Footer") and paste it in the body section.</p></div></main>"""
    return _page("Embed the VigiEye regulatory dates ticker", "Embeddable scrolling ticker of upcoming EU and US regulatory dates.",
                 "/embed", body)


# --- sitemap extension --------------------------------------------------------

def sitemap_urls() -> list[tuple[str, str, str]]:
    urls = [("/", "hourly", "1.0"), ("/analysis", "daily", "0.9"), ("/guidelines", "daily", "0.9"), ("/pdufa", "daily", "0.9"), ("/approvals", "daily", "0.9"),
            ("/safety", "daily", "0.9"), ("/insights", "weekly", "0.7"), ("/newsletter", "monthly", "0.6"),
            ("/drugs", "weekly", "0.6")]
    for region, slug, _ in _safe(pd.all_drug_ids, []):
        urls.append((f"/drugs/{region.lower()}/{slug}", "weekly", "0.5"))
    for c in _safe(pd.companies_with_activity, []):
        urls.append((f"/companies/{c}", "weekly", "0.4"))

    def weeks():
        from sqlalchemy import select
        return [r[0] for r in store._engine.connect().execute(
            select(pd.analyses.c.id).where(pd.analyses.c.id.like("week:%")))]
    for w in _safe(weeks, []):
        urls.append((f"/insights/{w[5:]}", "monthly", "0.6"))
    return urls


# --- in-depth analysis (AI, grounded) -------------------------------------------

_AN_SYSTEM = ("You are a senior EU/US regulatory affairs and pharmacovigilance analyst writing for regulatory, "
              "medical and business-development professionals. You only state facts given in the RECORDS. You may "
              "explain general, well-established regulatory procedure (e.g. what a CHMP opinion or PDUFA date means). "
              "Never invent trial results, prices, sales, probabilities of approval or dates. No investment advice.")

_AN_DRUG = """Write an in-depth regulatory analysis of this product for a professional reader.

RECORDS (official data):
{facts}

Return ONLY JSON: {{"sections":[{{"heading":"...","paragraphs":["...","..."]}}]}} with these headings in order:
"Where it stands", "Regulatory pathway and designations", "Safety and pharmacovigilance", "What to watch".
2-3 short paragraphs each, plain English. If the records don't cover a heading, say so in one sentence."""

_AN_WEEK = """Write the weekly in-depth review of EU and US pharma regulation for {week}.

RECORDS (official data from the week, plus dates coming up):
{facts}

Return ONLY JSON: {{"sections":[{{"heading":"...","paragraphs":["..."]}}]}} with these headings:
"The week in one minute", "EU approvals and CHMP opinions", "FDA approvals and PDUFA dates",
"Safety and pharmacovigilance", "What to watch next week".
Name specific products from the records. 2-4 short paragraphs each."""


def _render_sections(j: dict) -> str:
    out = []
    for s in (j.get("sections") or [])[:8]:
        out.append(f"<h3>{_e(s.get('heading'))}</h3>" +
                   "".join(f"<p>{_e(p)}</p>" for p in (s.get("paragraphs") or [])[:5]))
    return "".join(out)


def _claude_json(system: str, prompt: str, max_tokens: int = 1800) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(model=settings.anthropic_model, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def _drug_facts(d: dict) -> str:
    ms = pd.milestones_for_drug(d)
    lines = [f"Product: {d['name']} ({d.get('inn')}); region {d['region']}; company {d.get('company')}",
             f"Status: {d.get('status')}; therapeutic area: {d.get('therapeutic_area')}",
             f"Designations: {', '.join(_flags(d)) or 'none recorded'}",
             f"CHMP opinion date: {d.get('opinion_date') or '-'}; approval date: {d.get('decision_date') or '-'}",
             f"Indication: {(d.get('indication') or '-')[:2500]}", "Milestones:"]
    lines += [f"- {m['date']} {pd.KIND_LABEL.get(m['kind'], m['kind'])}: {m['title']}. {m.get('detail') or ''}"
              f" [{m.get('source_type')}]" for m in ms[:25]]
    twins = pd.drugs_by_inn(d.get("inn") or "", d["id"])
    if twins:
        lines.append("Same substance elsewhere: " + "; ".join(f"{t['name']} ({t['region']}, {t.get('status')})" for t in twins[:6]))
    return "\n".join(lines)


def analyse_drugs(limit: int = 30) -> dict:
    """Generate/refresh analyses for products with milestones in the last 120 / next 180 days."""
    pd.init()
    t = _today()
    ms = pd.milestones_between(_ago(120), _ahead(180), ["ec_decision", "fda_approval", "chmp_opinion_pos",
                                                         "chmp_opinion_neg", "ec_expected", "eu_refusal"], limit=3000)
    ids = []
    for m in sorted(ms, key=lambda m: abs((date.fromisoformat(m["date"]) - date.today()).days)):
        if m.get("drug_id") and m["drug_id"] not in ids:
            ids.append(m["drug_id"])
    done, skipped, errors = 0, 0, []
    for did in ids:
        if done >= limit:
            break
        region, slug = did[:2].upper(), did[3:]
        d = pd.drug_get(region, slug)
        if not d:
            continue
        facts = _drug_facts(d)
        h = hashlib.sha1(facts.encode()).hexdigest()
        prev = pd.analysis_get(f"drug:{did}")
        if prev and prev.get("facts_hash") == h:
            skipped += 1
            continue
        try:
            j = _claude_json(_AN_SYSTEM, _AN_DRUG.format(facts=facts))
            if not j:
                return {"error": "ANTHROPIC_API_KEY not set", "done": done}
            pd.analysis_put(f"drug:{did}", h, _render_sections(j), settings.anthropic_model)
            done += 1
        except Exception as e:
            errors.append(f"{did}: {e}")
    return {"candidates": len(ids), "generated": done, "unchanged": skipped, "errors": errors[:10]}


def analyse_week(week_end: date | None = None) -> dict:
    pd.init()
    end = week_end or (date.today() - timedelta(days=1))
    start = end - timedelta(days=6)
    iso = end.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    ms = pd.milestones_between(start.isoformat(), end.isoformat(), None, limit=400)
    nxt = pd.milestones_between((end + timedelta(days=1)).isoformat(), (end + timedelta(days=10)).isoformat(),
                                ["pdufa", "adcomm", "ec_expected"], limit=60)
    news = [r for r in _news(80) if start.isoformat() <= (r.get("date") or "") <= end.isoformat()][:40]
    calnext = [e for e in cal.load_events() if e["status"] != "past" and e["days"] <= 10]
    facts = "\n".join(
        [f"- {m['date']} [{m['region']}] {pd.KIND_LABEL.get(m['kind'], m['kind'])}: {m['title']}. {m.get('detail') or ''} [{m.get('source_type')}]"
         for m in ms if m["kind"] != "shortage"][:150]
        + ["COMING UP:"] + [f"- {m['date']} [{m['region']}] {pd.KIND_LABEL.get(m['kind'])}: {m['title']} [{m.get('source_type')}]" for m in nxt]
        + [f"- {e['date']} [EU] {e.get('body')}: {e.get('title')}" for e in calnext]
        + ["NEWS:"] + [f"- {r.get('date')} {r.get('authority')}: {r.get('title')}. {r.get('summary')}" for r in news])
    j = _claude_json(_AN_SYSTEM, _AN_WEEK.format(week=week, facts=facts), max_tokens=3000)
    if not j:
        return {"error": "ANTHROPIC_API_KEY not set"}
    pd.analysis_put(f"week:{week}", hashlib.sha1(facts.encode()).hexdigest(), _render_sections(j), settings.anthropic_model)
    return {"week": week, "records": len(ms), "news": len(news)}


# --- admin --------------------------------------------------------------------

_JOB = {"running": False, "started": None, "finished": None, "result": None, "step": None}


def _auth(tok: str):
    if not settings.admin_token or tok != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


def _run_job(steps: list[str]):
    _JOB.update(running=True, started=datetime.now(timezone.utc).isoformat(), finished=None, result={})
    try:
        for s in steps:
            _JOB["step"] = s
            if s == "ingest":
                _JOB["result"]["ingest"] = pd.run_all()
            elif s == "news":
                from . import ingest as ingest_mod, news as news_mod
                _JOB["result"]["news"] = ingest_mod.run_ingest(per_source=25)
                _JOB["step"] = "translate"
                try:
                    _JOB["result"]["translate"] = ingest_mod.translate_titles()
                except Exception as e:
                    _JOB["result"]["translate"] = {"error": f"{type(e).__name__}: {e}"}
                _JOB["step"] = "news"
                _JOB["result"]["press_company"] = news_mod.run_all()
                from . import guidelines as guide_mod
                _JOB["result"]["guidelines_safety"] = guide_mod.run_all()
                from . import stories as story_mod
                _JOB["step"] = "stories"
                _JOB["result"]["stories"] = story_mod.run(limit=30)
            elif s == "analyse":
                _JOB["result"]["analyse"] = analyse_drugs(40)
            elif s == "week":
                _JOB["result"]["week"] = analyse_week()
            elif s == "articles":
                from . import articles as art_mod, earnings as earn_mod
                _JOB["result"]["articles"] = art_mod.generate_daily(target=10)
                _JOB["step"] = "earnings"
                try:
                    _JOB["result"]["earnings_ingest"] = earn_mod.ingest()
                except Exception as e:
                    _JOB["result"]["earnings_ingest"] = {"error": f"{type(e).__name__}: {e}"}
                _JOB["step"] = "cp_articles"
                _JOB["result"]["cp_articles"] = art_mod.generate_cp_daily(target=10)
        _JOB["step"] = "indexnow"
        try:
            from . import indexnow as _inx
            _JOB["result"]["indexnow"] = _inx.ping_fresh(hours=4 if "articles" in steps else 3)
        except Exception as e:
            _JOB["result"]["indexnow"] = {"error": str(e)[:120]}
    except Exception as e:
        _JOB["result"]["error"] = f"{type(e).__name__}: {e}"
    finally:
        _JOB.update(running=False, finished=datetime.now(timezone.utc).isoformat(), step=None)


@router.get("/api/portal/stats")
def portal_stats():
    """Public data-freshness check: counts per record type and the last refresh outcome."""
    res = _JOB.get("result") or {}
    ing = res.get("ingest") or {}
    summary = {k: ({"error": v["error"]} if isinstance(v, dict) and v.get("error") else
                   {kk: vv for kk, vv in (v or {}).items() if kk in ("drugs", "milestones", "notices", "hits",
                                                                      "processed", "filings_with_dates", "ai_mode", "seconds")})
               for k, v in ing.items()}
    an = res.get("analyse") or {}
    nres = res.get("news") or {}

    def ncounts():
        from sqlalchemy import select, func
        from . import news as nw
        nw.init()
        with store._engine.connect() as c:
            return dict(c.execute(select(nw.news.c.kind, func.count()).group_by(nw.news.c.kind)).all()) | \
                {"official_updates": store.count()}
    return {"counts": _safe(pd.counts, {}), "news_counts": _safe(ncounts, {}),
            "news_last_run": {"new": nres.get("new"), "errors": (nres.get("errors") or [])[:10],
                              "press_company": res.get("press_company"),
                              "guidelines_safety": res.get("guidelines_safety")}, "last_run": {"started": _JOB.get("started"), "finished": _JOB.get("finished"),
            "running": _JOB.get("running"), "step": _JOB.get("step"), "ingest": summary,
            "analyse": {k: an.get(k) for k in ("candidates", "generated", "unchanged", "error") if k in an},
            "articles": res.get("articles"), "error": res.get("error")}}


@router.post("/admin/portal/run")
def admin_portal_run(steps: str = "ingest,news,analyse", x_admin_token: str = Header(default="")):
    """Start a background refresh (Render requests can't wait 15+ minutes). Poll /admin/portal/status."""
    _auth(x_admin_token)
    if _JOB["running"]:
        return {"status": "already running", "job": _JOB}
    wanted = [s for s in steps.split(",") if s in ("ingest", "news", "analyse", "week", "articles")]
    threading.Thread(target=_run_job, args=(wanted,), daemon=True).start()
    return {"status": "started", "steps": wanted}


@router.post("/admin/portal/tick")
def admin_portal_tick(owner: str = "", x_admin_token: str = Header(default="")):
    """Run whatever is due (see app/scheduler.py). Called hourly by GitHub; also wakes a sleeping server."""
    from . import scheduler
    _auth(x_admin_token)
    if owner and "@" in owner:
        scheduler._put("owner", {"email": owner.strip()})
    scheduler.start()
    return scheduler.tick()


@router.get("/admin/portal/schedule")
def admin_portal_schedule(x_admin_token: str = Header(default="")):
    from . import scheduler
    _auth(x_admin_token)
    return {"due": scheduler.due(), "news": scheduler._get("news"), "daily": scheduler._get("daily"),
            "last": scheduler._get("last_result"), "job": _JOB}


@router.post("/admin/portal/relay/{key}")
async def admin_relay(key: str, request: Request, x_admin_token: str = Header(default="")):
    """Store a JSON document fetched on a GitHub runner for a source that blocks this server (see app/relay.py)."""
    import json as _json
    from . import relay
    _auth(x_admin_token)
    if key not in relay.URLS:
        raise HTTPException(404)
    body = await request.body()
    data = _json.loads(body)
    pd.init()
    pd.analysis_put(f"relay:{key}", key, _json.dumps(data), "relay")
    return {"stored": key, "results": len(data.get("results", []) or [])}


@router.get("/admin/portal/status")
def admin_portal_status(x_admin_token: str = Header(default="")):
    _auth(x_admin_token)
    return {"job": _JOB, "counts": _safe(pd.counts, {})}


@router.post("/admin/portal/send-daily")
def admin_send_daily(dry_run: bool = True, x_admin_token: str = Header(default="")):
    from . import emailer
    _auth(x_admin_token)
    store.init()
    pd.init()
    emails = store.subscribers_active("daily")
    if dry_run:
        subj, _, n = render_daily("")
        return {"dry_run": True, "would_send_to": len(emails), "subject": subj, "items": n}
    sent, errors = 0, []
    for em in emails:
        subj, html, _ = render_daily(em)
        try:
            emailer.send(em, subj, html)
            sent += 1
        except Exception as ex:
            errors.append({"email": em, "error": str(ex)})
        time.sleep(0.6)   # stay inside Resend's default rate limit
    return {"sent": sent, "errors": errors[:20]}
