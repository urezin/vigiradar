"""
VigiEye news portal pages, laid out like a trade news site (Fierce-style): lead story,
top stories, a "Latest" river, section rows of image cards, and a sidebar.

Beats: Regulatory, Pharmacovigilance, Quality & compliance, Market access (+ Industry).

  /                           home
  /news, /news/{section}      section fronts (+ ?page=)
  /news/item/{u|c}/{id}       article page: official notice (u) or company release (c)
  /guidelines                 ICH / FDA / EMA / MHRA guidelines tracker
  /safety                     pharmacovigilance & safety hub
  /thumb/{section}/{seed}.svg generated story artwork (no third-party images are used)
  /feed.xml                   RSS of VigiEye's own briefs
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import catalysts as cal
from . import news as nw
from . import portal_data as pd
from . import portal_pages as pp
from . import store

router = APIRouter()
_e, _fmt, _base = cal._e, cal._fmt, cal._base

FLAG = {"EU": "🇪🇺", "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱",
        "IE": "🇮🇪", "SE": "🇸🇪", "BE": "🇧🇪", "INT": "🌐", "CA": "🇨🇦"}
SEC_COLOR = {"regulatory": "#0369a1", "pharmacovigilance": "#be123c", "quality": "#b45309",
             "market-access": "#6d28d9", "industry": "#0f766e"}
SEC_ABBR = {"regulatory": "REG", "pharmacovigilance": "PV", "quality": "QA", "market-access": "MA", "industry": "BIO"}
_PRIORITY_PUB = ("ema", "fda", "mhra", "prac", "european commission", "hma", "cmdh", "who")
ALL_KINDS = ("official", "notice", "guideline", "company", "press")


# --- the unified stream -----------------------------------------------------------------

def _clean(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(x or ""))).strip()


def _official(limit: int = 800) -> list[dict]:
    try:
        store.init()
        rows = store.query(limit=limit) if store.count() else []
    except Exception:
        rows = []
    out = []
    for r in rows:
        r = {**r, "title": _clean(r.get("title")), "summary": _clean(r.get("summary"))}
        if not pp._relevant(r) or nw.is_junk(r) or r.get("authority") == "FDA CDER":   # page-update feed, removed
            continue
        if r.get("authority") == "US Department of Justice":
            from .sources import feed_relevant
            if not feed_relevant("doj", r["title"], r["summary"]):
                continue          # stored before the health-care filter existed
        out.append({"kind": "official", "id": r.get("id"), "section": nw.section_for_update(r),
                    "publisher": r.get("authority") or "", "country": r.get("country"), "title": r["title"],
                    "summary": r["summary"], "url": r.get("source_url") or "", "date": r.get("date") or "",
                    "impact": r.get("impact") or "med", "company": None, "ticker": None, "tags": None})
    return out


def _company_press(limit: int = 1200) -> list[dict]:
    try:
        rows = nw.latest(limit=limit)
    except Exception:
        rows = []
    out = []
    for r in rows:
        out.append({**r, "section": nw.norm_section(r.get("section")), "title": _clean(r.get("title")),
                    "summary": _clean(r.get("summary")),
                    "impact": "high" if r["kind"] in ("company", "notice") else "med"})
    return out


def stream(section: str | None = None, kinds: tuple = ALL_KINDS, limit: int = 60, offset: int = 0) -> list[dict]:
    rows = []
    if "official" in kinds:
        rows += _official()
    if any(k in kinds for k in ("company", "press", "notice", "guideline")):
        rows += [r for r in _company_press() if r["kind"] in kinds]
    if section:
        rows = [r for r in rows if r["section"] == section]
    rows.sort(key=lambda r: (r["date"], r["kind"] != "press", r["impact"] == "high"), reverse=True)
    # the same notice often arrives through two feeds (e.g. MHRA news + MHRA alerts): keep one
    seen, out = set(), []
    for r in rows:
        k = re.sub(r"[^a-z0-9]+", "", (r.get("title") or "").lower())[:90]
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out[offset: offset + limit]


def _real_summary(r: dict) -> bool:
    sm, ti = (r.get("summary") or "").strip().lower(), (r.get("title") or "").strip().lower()
    return len(sm) > 70 and sm != ti and not sm.startswith(ti[:40])


def _score(r: dict) -> float:
    """Editorial weight for lead/top-story selection."""
    pub = (r.get("publisher") or "").lower()
    s = {"company": 3, "notice": 2.5, "official": 2, "guideline": 1.5, "press": 0}.get(r["kind"], 0)
    s += 3 if any(p in pub for p in _PRIORITY_PUB) else 0
    s += 2 if r.get("country") in ("EU", "US", "UK") else (-1.5 if r.get("country") in ("CA", "INT") else 0)
    s += 2 if _real_summary(r) else -2
    s += 1.5 if r.get("impact") == "high" else 0
    s += 1 if r["section"] in ("pharmacovigilance", "regulatory", "quality", "market-access") else -1
    try:
        age = (date.today() - date.fromisoformat(r["date"])).days
    except Exception:
        age = 30
    return s - 0.6 * max(age, 0)


def _href(r: dict) -> str:
    if r["kind"] == "official" and r.get("id"):
        return f"/news/item/u/{r['id']}"
    if r["kind"] == "company":
        return f"/news/item/c/{r['id']}"
    return r.get("url") or "#"


def _is_ext(r: dict) -> bool:
    return r["kind"] in ("press", "notice", "guideline")


def _seed(r: dict) -> str:
    return hashlib.sha1((r.get("id") or r.get("title") or "").encode()).hexdigest()[:10]


def _thumb(r: dict, cls: str = "") -> str:
    sec = r["section"] if r["section"] in SEC_COLOR else "industry"
    pub = re.sub(r"[^A-Za-z]", "", (r.get("publisher") or "V"))[:4].upper() or "V"
    return f'<img class="th {cls}" loading="lazy" alt="" src="/thumb/{sec}/{_seed(r)}.svg?p={_e(pub)}">'


def _kind_label(r: dict) -> str:
    if r["kind"] in ("official", "notice"):
        return "Official"
    if r["kind"] == "guideline":
        try:
            st = (json.loads(r.get("tags") or "[]") or [""])[0]
        except Exception:
            st = ""
        return "Guideline" + (f" · {st}" if st else "")
    return {"company": "Company release", "press": "Press"}.get(r["kind"], "")


def _src(r: dict) -> str:
    pub = _e(r.get("publisher"))
    if r["kind"] == "company" and r.get("ticker"):
        pub += f" ({_e(r['ticker'])})"
    return f'{FLAG.get(r.get("country"), "")} {pub}'.strip()


def _label(r: dict) -> str:
    sec = r["section"]
    return (f'<a class="lab" style="color:{SEC_COLOR.get(sec, "#0f766e")}" href="/news/{sec}">'
            f'{_e(nw.SECTIONS.get(sec, sec))}</a><span class="kd">{_e(_kind_label(r))}</span>')


def _meta(r: dict) -> str:
    return f'<div class="by">{_src(r)} · {_e(_fmt(r["date"]))}</div>'


def _a(r: dict, inner: str) -> str:
    tgt = ' target="_blank" rel="noopener nofollow"' if _is_ext(r) else ""
    ext = ' <span class="ext" title="Opens the source">↗</span>' if _is_ext(r) and not inner.startswith("<img") else ""
    return f'<a href="{_e(_href(r))}"{tgt}>{inner}{ext}</a>'


def lead_card(r: dict) -> str:
    summ = f"<p>{_e(r.get('summary'))}</p>" if _real_summary(r) else ""
    return (f'<article class="lead-card">{_a(r, _thumb(r, "wide"))}<div class="lc-body">{_label(r)}'
            f'<h2 class="h-lead">{_a(r, _e(r["title"]))}</h2>{summ}{_meta(r)}</div></article>')


def mini_card(r: dict) -> str:
    return f'<article class="mini">{_a(r, _thumb(r, "sq"))}<div>{_label(r)}<h3>{_a(r, _e(r["title"]))}</h3>{_meta(r)}</div></article>'


def river_item(r: dict) -> str:
    summ = f'<p>{_e(r.get("summary"))}</p>' if _real_summary(r) else ""
    return (f'<article class="river">{_a(r, _thumb(r, "rv"))}<div>{_label(r)}<h3>{_a(r, _e(r["title"]))}</h3>'
            f'{summ}{_meta(r)}</div></article>')


def grid_card(r: dict) -> str:
    return f'<article class="gcard">{_a(r, _thumb(r, "wide"))}{_label(r)}<h3>{_a(r, _e(r["title"]))}</h3>{_meta(r)}</article>'


def text_item(r: dict) -> str:
    return f'<li>{_a(r, _e(r["title"]))}<div class="by">{_src(r)} · {_e(_fmt(r["date"]))}</div></li>'


_NCSS = """
:root{--paper:#ffffff;--rule:#e5e7eb}
@media (prefers-color-scheme: dark){:root{--paper:#060c18;--rule:#1f2a3a}}
body{background:var(--paper)}
.ticker-strip{background:#050c17;padding:7px 0;border-bottom:1px solid #1e2d44}
.secbar{border-bottom:1px solid var(--rule);background:var(--paper);position:sticky;top:58px;z-index:4}
.secbar .wrap{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
.secbar a{padding:12px 12px;font-weight:800;font-size:12.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink);
  border-bottom:3px solid transparent;white-space:nowrap}
.secbar a:hover,.secbar a.on{border-bottom-color:var(--c,#0f172a);text-decoration:none}
.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;padding:20px 0 12px;border-bottom:3px solid var(--ink);flex-wrap:wrap}
.mast h1{font:900 40px/1 Georgia,'Times New Roman',serif;letter-spacing:-.02em}
.mast h1 b{color:#0d9488}.mast p{color:var(--mut);font-size:14px;margin-top:6px}
.mast .date{font-size:12.5px;color:var(--mut);font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.th{display:block;width:100%;background:#0f172a;border-radius:4px;object-fit:cover}
.th.wide{aspect-ratio:16/9;height:auto}.th.sq{width:96px;height:72px;flex:none}.th.rv{width:170px;height:106px;flex:none}
@media(max-width:640px){.th.rv{width:104px;height:72px}}
.lab{font-weight:800;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em}
.kd{font-size:11px;color:var(--mut);margin-left:8px;text-transform:uppercase;letter-spacing:.05em}
.by{font-size:12.5px;color:var(--mut);margin-top:4px}
.ext{font-size:.75em;color:var(--mut)}
article h2 a,article h3 a{color:var(--ink)}article h2 a:hover,article h3 a:hover{color:var(--accent);text-decoration:none}
.front{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:28px;margin-top:22px}
@media(max-width:900px){.front{grid-template-columns:1fr}}
.lead-card .lc-body{padding-top:12px}.h-lead{font:800 31px/1.15 Georgia,'Times New Roman',serif;letter-spacing:-.01em;margin:6px 0 8px}
.lead-card p{font-size:16.5px;color:var(--ink);opacity:.85}
.tops h2,.side h2,.row-h{font-weight:900;font-size:13px;letter-spacing:.08em;text-transform:uppercase;border-bottom:2px solid var(--ink);padding-bottom:6px;margin-bottom:6px}
.mini{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid var(--rule)}.mini:last-child{border-bottom:0}
.mini h3{font-size:15.5px;line-height:1.3;margin-top:3px}
.body3{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:30px;margin-top:30px}
@media(max-width:900px){.body3{grid-template-columns:1fr}}
.river{display:flex;gap:16px;padding:16px 0;border-bottom:1px solid var(--rule)}
.river h3{font:800 19px/1.25 Georgia,'Times New Roman',serif;margin-top:3px}.river p{color:var(--mut);font-size:14.5px;margin-top:5px;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.row{margin-top:34px}.row-h{display:flex;justify-content:space-between;align-items:baseline;border-color:var(--c,#0f172a)}
.row-h a{font-size:12px;letter-spacing:.04em}
.g4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;margin-top:10px}
@media(max-width:1000px){.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.g4{grid-template-columns:1fr}}
.gcard h3{font-size:16px;line-height:1.3;margin-top:4px}.gcard .lab{display:inline-block;margin-top:10px}
.side .box{border:1px solid var(--rule);border-radius:6px;padding:14px;margin-bottom:20px;background:var(--paper)}
.side ul{list-style:none}.side li{padding:9px 0;border-bottom:1px solid var(--rule);font-size:14px;font-weight:600;line-height:1.35}
.side li:last-child{border-bottom:0}.side li a{color:var(--ink)}
.side .nl-box{background:#0b1626;color:#e5e7eb;border-color:#0b1626}.side .nl-box h2{border-color:#e5e7eb;color:#fff}
.side .nl-box p{color:#cbd5e1;font-size:13.5px}
.side .sub input{min-width:0}
.cu{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--rule);font-size:13.5px}.cu:last-child{border-bottom:0}
.cu b{flex:none;width:52px;color:var(--accent);font-size:12.5px}.cu a{color:var(--ink);font-weight:600}
.pager{display:flex;gap:10px;margin:18px 0}
.hub{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 26px}@media(max-width:760px){.hub{grid-template-columns:1fr}}
"""


def _secbar(active: str | None = None) -> str:
    items = [("", "Home", "#0f172a"), ("regulatory", "Regulatory", SEC_COLOR["regulatory"]),
             ("pharmacovigilance", "Pharmacovigilance", SEC_COLOR["pharmacovigilance"]),
             ("quality", "Quality & compliance", SEC_COLOR["quality"]),
             ("market-access", "Market access", SEC_COLOR["market-access"]),
             ("industry", "Industry", SEC_COLOR["industry"])]
    links = []
    for k, label, c in items:
        href = f"/news/{k}" if k else "/"
        on = (active == k) if k else active == "home"
        links.append(f'<a class="{"on" if on else ""}" style="--c:{c}" href="{href}">{_e(label)}</a>')
    for href, label, c, key in (("/analysis", "Analysis & opinion", "#0f172a", "analysis"), ("/safety", "Safety hub", "#be123c", "safety"), ("/guidelines", "Guidelines", "#6d28d9", "guidelines"),
                                ("/pdufa", "PDUFA", "#0369a1", "pdufa"), ("/approvals", "Approvals", "#0369a1", "approvals"),
                                ("/calendar", "Calendar", "#0f766e", "calendar")):
        links.append(f'<a class="{"on" if active == key else ""}" style="--c:{c}" href="{href}">{label}</a>')
    return f'<nav class="secbar" aria-label="Sections"><div class="wrap">{"".join(links)}</div></nav>'


def _ticker() -> str:
    return ('<div class="ticker-strip"><div class="wrap"><script src="/embed/ticker.js" data-theme="dark" '
            'data-limit="24" data-title="Coming up" async></script></div></div>')


def _page(title, desc, path, body, ld=None):
    from . import articles as _am
    return pp._page(title, desc, path, f"<style>{_NCSS}{_am.AD_CSS}</style>{body}", ld)


def _coming_up(n: int = 8) -> str:
    items = pp.ticker_items(n)
    if not items:
        return ""
    rows = "".join(f'<div class="cu"><b>{_e(_fmt(i["date"])[4:10])}</b><div><a href="{_e(i["url"].replace(_base(), ""))}">'
                   f'{_e(i["title"])}</a><div class="by">{FLAG.get(i["region"], "")} {_e(i["label"])}</div></div></div>' for i in items)
    return (f'<section class="box"><h2>Coming up</h2>{rows}'
            f'<p class="by"><a href="/pdufa">PDUFA calendar</a> · <a href="/calendar">EU calendar</a></p></section>')


def _sidebar(section: str | None = None, press: list[dict] | None = None) -> str:
    nl = (f'<section class="box nl-box"><h2>VigiEye Daily</h2><p>Regulatory, pharmacovigilance, quality and market-access news '
          f'with approvals and PDUFA dates, every weekday morning. Free.</p>{pp._newsletter_box("sidebar:" + (section or "home"))}</section>')
    heads = ""
    if press:
        heads = (f'<section class="box"><h2>Industry headlines</h2><ul>{"".join(text_item(r) for r in press[:10])}</ul>'
                 f'<p class="by">Links open the publisher. VigiEye does not reproduce their articles.</p></section>')
    from . import articles as _am
    return f'<aside class="side">{_am.ad_html("sidebar-" + (section or "home"))}{nl}{_coming_up()}{heads}</aside>'


def _mast(sub: str) -> str:
    return (f'<div class="mast"><div><h1>Vigi<b>Eye</b></h1><p>{_e(sub)}</p></div>'
            f'<div class="date">{_e(date.today().strftime("%A %d %B %Y"))}</div></div>')


# --- home ----------------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home():
    rows = stream(limit=900)
    editorial = [r for r in rows if r["kind"] != "press"]
    ranked = sorted([r for r in editorial if _real_summary(r)], key=_score, reverse=True)
    lead = ranked[0] if ranked else (editorial[0] if editorial else None)
    used = {id(lead)} if lead else set()
    tops = []
    for r in ranked[1:]:
        if len(tops) >= 4:
            break
        if r["section"] != (lead or {}).get("section") or len(tops) >= 2:
            tops.append(r)
            used.add(id(r))
    river = [r for r in editorial if id(r) not in used and r["kind"] != "guideline"][:30]
    used |= {id(r) for r in river}
    press = [r for r in rows if r["kind"] == "press"]

    def row(k, extra=None):
        items = sorted([r for r in editorial if r["section"] == k and id(r) not in used], key=_score, reverse=True)[:8]
        if len(items) < 8:
            items += [r for r in press if r["section"] == k][: 8 - len(items)]
        used.update(id(r) for r in items)
        if not items:
            return ""
        return (f'<section class="row" style="--c:{SEC_COLOR[k]}"><div class="row-h"><span style="color:{SEC_COLOR[k]}">{_e(nw.SECTIONS[k])}</span>'
                f'<a href="{extra or "/news/" + k}">More {_e(nw.SECTIONS[k])} →</a></div>'
                f'<div class="g4">{"".join(grid_card(r) for r in items)}</div></section>')
    guides = [r for r in rows if r["kind"] == "guideline"][:4]
    rest = [r for r in rows if id(r) not in used and r["kind"] != "guideline"][:40]
    more_heads = ""
    if rest:
        more_heads = ('<section class="row"><div class="row-h"><span>More news</span><a href="/news">All news →</a></div>'
                      '<ul class="heads2" style="columns:2;column-gap:28px;list-style:none">'
                      + "".join(f'<li style="break-inside:avoid;padding:9px 0;border-bottom:1px solid var(--rule)">{_label(r)}'
                                f'<div style="font-weight:700;margin-top:2px">{_a(r, _e(r["title"]))}</div>{_meta(r)}</li>' for r in rest)
                      + "</ul></section>")
    guide_row = ""
    if guides:
        guide_row = (f'<section class="row" style="--c:#6d28d9"><div class="row-h"><span style="color:#6d28d9">New guidelines: ICH, FDA, EMA, MHRA</span>'
                     f'<a href="/guidelines">Guidelines tracker →</a></div><div class="g4">{"".join(grid_card(r) for r in guides)}</div></section>')
    front = ""
    if lead:
        front = (f'<div class="front"><div>{lead_card(lead)}</div><div class="tops"><h2>Top stories</h2>'
                 f'{"".join(mini_card(r) for r in tops)}</div></div>')
    body = f"""{_ticker()}{_secbar("home")}<main class="wrap">
{_mast("Regulatory · Pharmacovigilance · Quality · Market access: EU & US pharma, from the official sources")}
{front or '<p class="note" style="margin-top:20px">The news feeds are loading for the first time. Check back in a few minutes.</p>'}
<style>{_ACSS}</style>
{editorial_row(3)}
<div class="body3"><div><div class="row-h" style="margin-top:0"><span>Latest</span><a href="/news">All news →</a></div>
{"".join(river_item(r) for r in river[:8])}{art_mod.ad_html("home-river", "inline")}{"".join(river_item(r) for r in river[8:])}</div>{_sidebar(None, press)}</div>
{row("pharmacovigilance", "/safety")}{row("regulatory")}{art_mod.ad_html("home-banner", "banner")}{row("quality")}{row("market-access")}{guide_row}{row("industry")}
{more_heads}
<p class="note" style="margin-top:28px">{pp.DISCLAIMER}</p></main>"""
    ld = [{"@context": "https://schema.org", "@type": "NewsMediaOrganization", "name": "VigiEye", "url": _base() + "/"},
          {"@context": "https://schema.org", "@type": "WebSite", "name": "VigiEye", "url": _base() + "/",
           "potentialAction": {"@type": "SearchAction", "target": _base() + "/drugs?q={query}",
                               "query-input": "required name=query"}}]
    return _page("VigiEye: regulatory, pharmacovigilance, quality & market access news for pharma",
                 "EU and US pharma news for regulatory, pharmacovigilance, quality and market-access professionals: EMA, FDA, "
                 "MHRA, PRAC, HTA, GMP and enforcement, with official sources.", "/", body, ld)


# --- section fronts ------------------------------------------------------------------------

_SEO = {"pharmacovigilance": "Pharmacovigilance news: PRAC, signals, DHPCs, MHRA, FDA safety | VigiEye",
        "regulatory": "Pharma regulatory news: FDA, EMA, CHMP, MHRA approvals and guidance | VigiEye",
        "market-access": "Market access news: EU HTA, G-BA, HAS, NICE, ICER, pricing & reimbursement | VigiEye",
        "quality": "Pharma quality & compliance news: GMP, GDP, recalls, warning letters, enforcement | VigiEye",
        "industry": "Pharma & biotech industry news: company announcements and headlines | VigiEye"}


def _section_front(section: str | None, page: int):
    per = 30
    rows = stream(section=section, limit=per + 1, offset=(page - 1) * per)
    more = len(rows) > per
    rows = rows[:per]
    title = nw.SECTIONS.get(section, "All news") if section else "All news"
    blurb = nw.SECTION_BLURB.get(section, "Regulatory, pharmacovigilance, quality and market-access news from EU and US "
                                          "regulators, HTA bodies and industry.")
    lead_html = ""
    if page == 1 and rows:
        cand = sorted([r for r in rows[:20] if r["kind"] != "press" and _real_summary(r)], key=_score, reverse=True)
        if cand:
            lead = cand[0]
            rows = [r for r in rows if r is not lead]
            lead_html = f'<div style="margin:18px 0 6px">{lead_card(lead)}</div>'
    base = f"/news/{section}" if section else "/news"
    pager = '<div class="pager">' + (f'<a class="btn ghost" href="{base}?page={page - 1}">← Newer</a>' if page > 1 else "") + \
            (f'<a class="btn ghost" href="{base}?page={page + 1}">Older →</a>' if more else "") + "</div>"
    press = stream(kinds=("press",), limit=40)
    color = SEC_COLOR.get(section or "", "inherit")
    body = f"""{_ticker()}{_secbar(section or "")}<main class="wrap">
<div class="mast"><div><h1 style="font-size:34px;color:{color}">{_e(title)}</h1><p>{_e(blurb)}</p></div>
<div class="date">{_e(date.today().strftime("%d %B %Y"))}</div></div>
<div class="body3"><div>{lead_html}{"".join(river_item(r) for r in rows) or '<p class="note">No stories in this section yet.</p>'}{pager}</div>
{_sidebar(section, press)}</div><p class="note">{pp.DISCLAIMER}</p></main>"""
    return _page(_SEO.get(section, "Pharma regulatory, pharmacovigilance, quality & market access news | VigiEye"),
                 blurb[:155], base + (f"?page={page}" if page > 1 else ""), body)


@router.get("/news", response_class=HTMLResponse, include_in_schema=False)
def news_all(page: int = 1):
    return _section_front(None, max(1, page))


@router.get("/news/{section}", include_in_schema=False)
def news_section(section: str, page: int = 1):
    if section in nw.SECTION_ALIAS:
        return RedirectResponse(f"/news/{nw.SECTION_ALIAS[section]}", status_code=301)
    if section not in nw.SECTIONS:
        raise HTTPException(404)
    return _section_front(section, max(1, page))


# --- article pages --------------------------------------------------------------------------

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
        u = {**u, "title": _clean(u.get("title")), "summary": _clean(u.get("summary"))}
        r = {"kind": "official", "id": u["id"], "section": nw.section_for_update(u), "publisher": u.get("authority"),
             "country": u.get("country"), "title": u["title"], "summary": u["summary"], "url": u.get("source_url"),
             "date": u.get("date"), "impact": u.get("impact"), "company": None, "ticker": None, "tags": None}
    elif kind == "c":
        n = nw.get(nid)
        if not n or n["kind"] != "company":
            raise HTTPException(404)
        r = {**n, "section": nw.norm_section(n.get("section")), "impact": "high"}
    else:
        raise HTTPException(404)
    related = [x for x in stream(section=r["section"], limit=14) if x.get("id") != r.get("id")][:6]
    src_label = "Read the official notice" if r["kind"] == "official" else "Read the company's press release (SEC filing)"
    summary = r.get("summary") or ""
    note = ("Summary written by VigiEye from the official notice." if r["kind"] == "official" else
            "Brief written by VigiEye from the company's press release filed with the SEC. Claims are the company's own.")
    if not summary:
        note = "Headline as published. Open the source for details."
    press = stream(kinds=("press",), limit=20)
    body = f"""{_ticker()}{_secbar(r["section"])}<main class="wrap"><div class="body3"><article class="article" style="max-width:760px">
<div style="margin-top:8px">{_label(r)}</div>
<h1 style="font:800 36px/1.15 Georgia,'Times New Roman',serif;margin:8px 0 10px">{_e(r["title"])}</h1>
{_meta(r)}
<div style="margin:18px 0">{_thumb(r, "wide")}</div>
{f'<p class="detail" style="font-size:18px">{_e(summary)}</p>' if summary else ""}
<p style="margin-top:18px"><a class="btn" href="{_e(r.get("url"))}" target="_blank" rel="noopener">{_e(src_label)} ↗</a></p>
<p class="note">{_e(note)} AI-assisted; check the source before acting.</p>
<div class="row" style="--c:{SEC_COLOR.get(r["section"])}"><div class="row-h"><span>More {_e(nw.SECTIONS.get(r["section"], ""))}</span>
<a href="/news/{r["section"]}">All →</a></div><div class="g4" style="grid-template-columns:repeat(3,minmax(0,1fr))">{"".join(grid_card(x) for x in related)}</div></div>
</article>{_sidebar(r["section"], press)}</div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "NewsArticle", "headline": r["title"][:110],
           "datePublished": r.get("date"), "description": summary[:300],
           "image": [f"{_base()}/thumb/{r['section']}/{_seed(r)}.svg"],
           "publisher": {"@type": "Organization", "name": "VigiEye", "url": _base() + "/"},
           "isBasedOn": r.get("url"), "articleSection": nw.SECTIONS.get(r["section"], "")}]
    return _page(f"{r['title'][:90]} | VigiEye", (summary or r["title"])[:155], f"/news/item/{kind}/{nid}", body, ld)


# --- generated story artwork --------------------------------------------------------------------

_GLYPH = {
    "pharmacovigilance": '<path d="M-36 0 H-16 L-10 -14 L-2 14 L6 -6 L10 0 H36" fill="none" stroke="#fff" stroke-width="3.5" stroke-linejoin="round"/>'
                         '<circle r="30" fill="none" stroke="#fff" stroke-width="2.5" opacity=".7"/>',
    "regulatory": '<rect x="-24" y="-30" width="48" height="60" rx="4" fill="none" stroke="#fff" stroke-width="3"/>'
                  '<path d="M-14 -14 H14 M-14 -2 H14 M-14 10 H4" stroke="#fff" stroke-width="3" stroke-linecap="round"/>',
    "quality": '<circle r="26" fill="none" stroke="#fff" stroke-width="3"/><path d="M-12 0 L-3 10 L14 -10" fill="none" stroke="#fff" '
               'stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
    "market-access": '<path d="M-30 22 H30 M-22 22 V2 M-8 22 V-10 M6 22 V-2 M20 22 V-22" stroke="#fff" stroke-width="5" stroke-linecap="round"/>',
    "industry": '<path d="M-14 -30 C14 -14 -14 14 14 30 M14 -30 C-14 -14 14 14 -14 30" fill="none" stroke="#fff" stroke-width="3"/>'
                '<path d="M-9 -18 H9 M-10 0 H10 M-9 18 H9" stroke="#fff" stroke-width="2.5" opacity=".8"/>',
}


@router.get("/thumb/{section}/{seed}.svg", include_in_schema=False)
def thumb(section: str, seed: str, p: str = ""):
    sec = section if section in SEC_COLOR else "industry"
    h = hashlib.sha1(seed.encode()).digest()
    c = SEC_COLOR[sec]
    ang = h[0] % 180
    circles = "".join(
        f'<circle cx="{40 + (h[i] * 3) % 560}" cy="{20 + (h[i + 1] * 2) % 300}" r="{20 + h[i + 2] % 90}" '
        f'fill="#fff" opacity="{0.04 + (h[i + 3] % 8) / 100:.2f}"/>' for i in range(1, 17, 4))
    lines = "".join(f'<path d="M{(h[i] * 5) % 640} 0 L{(h[i + 1] * 5) % 640} 360" stroke="#fff" stroke-opacity=".06" '
                    f'stroke-width="{1 + h[i] % 3}"/>' for i in range(0, 12, 2))
    label = re.sub(r"[^A-Z]", "", (p or "").upper())
    label = label if label in ("ANALYSIS", "VIEW") else label[:4]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" preserveAspectRatio="xMidYMid slice">
<defs><linearGradient id="g" gradientTransform="rotate({ang} .5 .5)"><stop offset="0" stop-color="{c}"/><stop offset="1" stop-color="#0b1220"/></linearGradient></defs>
<rect width="640" height="360" fill="url(#g)"/>{lines}{circles}
<g transform="translate(320 170) scale(2.2)">{_GLYPH[sec]}</g>
<text x="24" y="336" font-family="system-ui,Segoe UI,sans-serif" font-size="22" font-weight="800" fill="#fff" opacity=".85" letter-spacing="3">{SEC_ABBR[sec]}{(" · " + label) if label else ""}</text>
</svg>"""
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800, immutable"})


# --- RSS of VigiEye's own briefs ------------------------------------------------------------------

@router.get("/feed.xml", include_in_schema=False)
def rss():
    rows = [r for r in stream(limit=100) if r["kind"] in ("official", "company")]
    items = "".join(
        f"<item><title>{_e(r['title'])}</title><link>{_e(_base() + _href(r))}</link>"
        f"<guid isPermaLink=\"true\">{_e(_base() + _href(r))}</guid><pubDate>{_e(r['date'])}</pubDate>"
        f"<category>{_e(nw.SECTIONS.get(r['section'], ''))}</category><description>{_e(r.get('summary') or '')}</description></item>"
        for r in rows)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>VigiEye</title>'
           f'<link>{_base()}/</link><description>EU &amp; US pharma regulatory, PV, quality and market access news</description>{items}</channel></rss>')
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def sitemap_urls() -> list[tuple[str, str, str]]:
    urls = [("/news", "hourly", "0.9"), ("/safety", "daily", "0.9")] + [(f"/news/{k}", "hourly", "0.9") for k in nw.SECTIONS]
    for r in stream(limit=800):
        if r["kind"] in ("official", "company") and r.get("id"):
            urls.append((_href(r), "monthly", "0.6"))
    return urls


# helpers used by the guidelines tracker and safety hub below
def _rail_panel(title: str, items: list[dict], more: str, show_days: bool = False) -> str:
    if not items:
        return ""
    return pp._panel(title, items, more, show_days=show_days)


def _card(r: dict, summary: bool = True, big: bool = False) -> str:
    return river_item(r)


def _secnav(active: str | None = None) -> str:
    return _secbar(active)


# --- guidelines tracker ---------------------------------------------------------------

@router.get("/guidelines", response_class=HTMLResponse, include_in_schema=False)
def guidelines_page(body: str = "", status: str = "", page: int = 1):
    page = max(1, page)
    rows = [r for r in _company_press(limit=1500) if r["kind"] == "guideline"]
    rows.sort(key=lambda r: r["date"], reverse=True)

    def tags(r):
        try:
            return json.loads(r.get("tags") or "[]")
        except Exception:
            return []
    if body == "ICH":
        rows = [r for r in rows if "ICH" in tags(r) or "ICH" in (r.get("publisher") or "")]
    elif body in ("FDA", "EMA", "MHRA"):
        rows = [r for r in rows if (r.get("publisher") or "").startswith(body)]
    if status:
        rows = [r for r in rows if status in tags(r)]
    per = 40
    shown = rows[(page - 1) * per: page * per]
    cons = [e for e in cal.load_events() if e["status"] != "past" and e.get("type") in ("consultation", "guideline")][:10]

    def tab(label, b, st=""):
        on = (b == body and st == status)
        q = "&".join(x for x in [f"body={b}" if b else "", f"status={st}" if st else ""] if x)
        return f'<a class="{"on" if on else ""}" href="/guidelines{"?" + q if q else ""}">{_e(label)}</a>'
    tabs = "".join([tab("All", ""), tab("ICH", "ICH"), tab("FDA", "FDA"), tab("EMA", "EMA"), tab("MHRA", "MHRA"),
                    tab("Drafts & consultations", body, "draft"), tab("Final", body, "final")])
    items = "".join(_card(r) for r in shown) or '<p class="note">No guidelines match this filter yet.</p>'
    pager = '<div class="pager">' + (f'<a class="btn ghost" href="/guidelines?body={_e(body)}&status={_e(status)}&page={page-1}">← Newer</a>' if page > 1 else "") + \
            (f'<a class="btn ghost" href="/guidelines?body={_e(body)}&status={_e(status)}&page={page+1}">Older →</a>' if len(rows) > page * per else "") + "</div>"
    cons_html = "".join(f'<div class="ms"><div class="top"><b>{_e(_fmt(e["date"]))}</b><span>{_e(e.get("body"))}</span>{cal._countdown(e)}</div>'
                        f'<h3><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a></h3></div>' for e in cons)
    body_html = f"""{_ticker()}{_secbar("guidelines")}<main class="wrap">
<div class="mast"><div><h1 style="font-size:34px;color:#6d28d9">Guidelines tracker: ICH, FDA, EMA, MHRA</h1></div><div class="date">{_e(date.today().strftime("%d %B %Y"))}</div></div>
<p class="note" style="font-size:15px">New, draft and final guidance as it is published: FDA notices of availability (including the ICH
guidelines FDA adopts) from the Federal Register, EMA scientific and procedural guidelines and public consultations (including
ICH texts adopted in the EU), and MHRA guidance. Each entry links to the official document.</p>
<div class="tabs">{tabs}</div>
<div class="body3" style="margin-top:8px"><div>{items}{pager}</div>
<aside class="side">{f'<section class="box"><h2>Consultations &amp; effective dates</h2>{cons_html}</section>' if cons_html else ""}
<section class="box nl-box"><h2>New guidelines, daily</h2>{pp._newsletter_box("guidelines")}</section></aside></div>
<p class="note">ICH's own website can't be monitored automatically, so ICH guidelines are tracked through FDA's Federal Register
notices and EMA's guideline feeds, the two routes by which ICH texts take effect in the US and EU.</p></main>"""
    return _page("Guidelines tracker: new ICH, FDA, EMA & MHRA guidance | VigiEye",
                 "Track new, draft and final regulatory guidance from ICH, FDA, EMA and MHRA, with public consultations and "
                 "effective dates, each linked to the official document.", "/guidelines", body_html)


# --- safety hub (replaces the data-only safety page) ------------------------------------

@router.get("/safety", response_class=HTMLResponse, include_in_schema=False)
def safety_hub():
    t = date.today()
    pv = stream(section="pharmacovigilance", kinds=("official", "notice", "company"), limit=400)
    qa_recalls = [r for r in stream(section="quality", kinds=("official", "notice"), limit=200)
                  if "recall" in r["title"].lower() or "alert" in (r.get("publisher") or "").lower()][:8]

    def pick(pred, n=8):
        return [r for r in pv if pred(r)][:n]
    pub = lambda r: (r.get("publisher") or "").lower()
    prac = pick(lambda r: bool(re.search(r"\bprac\b|\bsignals?\b", (r["title"] + " " + pub(r)).lower())), 10)
    mhra = pick(lambda r: "mhra" in pub(r))
    fda = pick(lambda r: r.get("country") == "US")
    national = pick(lambda r: r.get("country") in ("DE", "FR", "ES", "IT", "NL", "IE", "SE", "BE"))
    other = pick(lambda r: r.get("country") in ("CA", "INT"))
    dhpc = pp._safe(lambda: pd.milestones_between((t - timedelta(days=180)).isoformat(), t.isoformat(), ["dhpc"], limit=10, desc=True), [])
    refs = pp._safe(lambda: pd.milestones_between((t - timedelta(days=365)).isoformat(), t.isoformat(),
                                                  ["referral_start", "prac_recommendation", "referral_opinion", "referral_ec"], limit=10, desc=True), [])
    short = pp._safe(lambda: pd.milestones_between(None, None, ["shortage"], limit=10, desc=True), [])
    prac_dates = [e for e in cal.load_events() if e["status"] != "past" and e.get("body") == "PRAC"][:4]

    def blk(title, rows, more=None, color="#e11d48"):
        if not rows:
            return ""
        link = f'<a href="{more}">More →</a>' if more else ""
        return (f'<section class="row" style="--c:{color};margin-top:22px"><div class="row-h"><span style="color:{color}">{_e(title)}</span>{link}</div>'
                f'{"".join(river_item(r) for r in rows)}</section>')
    news_blocks = "".join([blk("Latest from PRAC & EU signals", prac, "/news/pharmacovigilance"),
                           blk("MHRA Drug Safety Update & alerts", mhra),
                           blk("FDA: MedWatch, safety communications & recalls", fda),
                           blk("National agencies (ANSM, AEMPS, BfArM, AIFA...)", national),
                           blk("Quality-defect recalls (FDA, MHRA)", qa_recalls, "/news/quality", "#b45309"),
                           blk("Health Canada & WHO", other, color="#64748b")])
    data_blocks = "".join(x for x in [
        _rail_panel("EU DHPCs (EMA data)", dhpc, "/news/pharmacovigilance"),
        _rail_panel("EU referrals (EMA data)", refs, "/news/pharmacovigilance"),
        _rail_panel("Ongoing EU shortages", short, "/news/pharmacovigilance")] if x)
    pd_html = "".join(f'<div class="ms"><div class="top"><b>{_e(_fmt(e["date"]))}</b>{cal._countdown(e)}</div>'
                      f'<h3><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a></h3></div>' for e in prac_dates)
    body = f"""{_ticker()}{_secbar("safety")}<main class="wrap">
<div class="mast"><div><h1 style="font-size:34px;color:#be123c">Pharmacovigilance &amp; safety hub</h1></div><div class="date">{_e(date.today().strftime("%d %B %Y"))}</div></div>
<p class="note" style="font-size:15px">PRAC outputs and EU signals, DHPCs, referrals and shortages, MHRA Drug Safety Update, FDA MedWatch
and recalls, and national agency safety news, updated every day from the official sources.</p>
<div class="body3" style="margin-top:6px"><div>{news_blocks or '<p class="note">Safety feeds are loading.</p>'}</div>
<aside class="side">{f'<section class="panel"><h2><span>Next PRAC meetings</span><a href="/calendar">Calendar →</a></h2>{pd_html}</section>' if pd_html else ""}
{data_blocks}<section class="box nl-box"><h2>VigiEye Daily</h2>{pp._newsletter_box("safety")}</section></aside></div>
<p class="note">{pp.DISCLAIMER}</p></main>"""
    return _page("Pharmacovigilance & drug safety hub: PRAC, DHPCs, MHRA, FDA MedWatch, recalls | VigiEye",
                 "Daily pharmacovigilance news: PRAC recommendations and signals, DHPCs, referrals, shortages, MHRA Drug Safety "
                 "Update, FDA MedWatch and recalls, with official sources.", "/safety", body)


# --- analysis & opinion (VigiEye editorial) ------------------------------------------------------

from . import articles as art_mod  # noqa: E402

_ACSS = """
.op-badge{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;padding:2px 8px;border-radius:3px}
.op-badge.analysis{background:#0f172a;color:#fff}.op-badge.opinion{background:#facc15;color:#1f2937}
.feat{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr);gap:22px;margin-top:10px}
@media(max-width:900px){.feat{grid-template-columns:1fr}}
.acard h3{font:800 20px/1.22 Georgia,'Times New Roman',serif;margin:6px 0}.acard.big h3{font-size:25px}
.acard p{color:var(--mut);font-size:14.5px}
.abody h2{font:800 22px/1.25 Georgia,'Times New Roman',serif;margin:26px 0 8px}.abody p{font-size:17.5px;line-height:1.7;margin:10px 0}
.take{border-left:4px solid #0d9488;background:var(--soft);padding:12px 16px;margin:22px 0}.take li{margin:6px 0 6px 18px}
.srcs li{font-size:14px;margin:6px 0 6px 18px}
""" + art_mod.AD_CSS


def _art_thumb(a: dict) -> str:
    sec = a.get("section") if a.get("section") in SEC_COLOR else "industry"
    seed = hashlib.sha1(a.get("slug", "").encode()).hexdigest()[:10]
    return f'<img class="th wide" loading="lazy" alt="" src="/thumb/{sec}/{seed}.svg?p={"VIEW" if a.get("type") == "opinion" else "ANALYSIS"}">'


def article_card(a: dict, big: bool = False) -> str:
    t = a.get("type", "analysis")
    return (f'<article class="acard{" big" if big else ""}"><a href="/analysis/{_e(a["slug"])}">{_art_thumb(a)}</a>'
            f'<div style="margin-top:10px"><span class="op-badge {t}">{"Opinion · VigiEye View" if t == "opinion" else "Analysis"}</span> '
            f'<a class="lab" style="color:{SEC_COLOR.get(a.get("section"), "#0f766e")};margin-left:6px" href="/news/{_e(a.get("section"))}">'
            f'{_e(nw.SECTIONS.get(a.get("section"), ""))}</a></div>'
            f'<h3><a href="/analysis/{_e(a["slug"])}">{_e(a["title"])}</a></h3><p>{_e(a.get("dek"))}</p>'
            f'<div class="by">VigiEye editorial · {_e(_fmt(a.get("date", "")))}</div></article>')


def editorial_row(n: int = 3) -> str:
    try:
        arts = art_mod.list_articles(12)
    except Exception:
        arts = []
    if not arts:
        return ""
    op = next((a for a in arts if a.get("type") == "opinion"), None)
    an = [a for a in arts if a.get("type") != "opinion"]
    pick = ([an[0]] if an else []) + ([op] if op else []) + an[1:]
    pick = pick[:n]
    return (f'<section class="row" style="--c:#0f172a"><div class="row-h"><span>Analysis &amp; opinion</span><a href="/analysis">All analysis →</a></div>'
            f'<div class="feat">{"".join(article_card(a, i == 0) for i, a in enumerate(pick))}</div></section>')


@router.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
def analysis_index(type: str = ""):
    arts = art_mod.list_articles(80, type if type in ("analysis", "opinion") else None)
    cards = "".join(f'<div style="padding:16px 0;border-bottom:1px solid var(--rule)">{article_card(a)}</div>' for a in arts) or \
        '<p class="note">The first analysis and opinion pieces are published with the next daily refresh.</p>'
    tabs = "".join(f'<a class="{"on" if (type or "") == k else ""}" href="/analysis{("?type=" + k) if k else ""}">{v}</a>'
                   for k, v in (("", "All"), ("analysis", "Analysis"), ("opinion", "Opinion")))
    body = f"""<style>{_ACSS}</style>{_ticker()}{_secbar("analysis")}<main class="wrap">
<div class="mast"><div><h1 style="font-size:34px">Analysis &amp; opinion</h1><p>In-depth analysis and the VigiEye View on regulatory,
pharmacovigilance, quality and market-access developments, built from the official records and linked to every source.</p></div>
<div class="date">{_e(date.today().strftime("%d %B %Y"))}</div></div><div class="tabs">{tabs}</div>
<div class="body3"><div>{cards}</div><aside class="side">{art_mod.ad_html("analysis-index")}{_coming_up()}</aside></div></main>"""
    return _page("Pharma regulatory analysis & opinion: PV, regulatory, quality, market access | VigiEye",
                 "In-depth analysis and opinion on EU and US pharma regulation, pharmacovigilance, quality and market access.",
                 "/analysis", body)


@router.get("/analysis/{slug}", response_class=HTMLResponse, include_in_schema=False)
def analysis_page(slug: str):
    a = art_mod.get_article(slug)
    if not a:
        raise HTTPException(404)
    t = a.get("type", "analysis")
    secs = "".join(f'<h2>{_e(s.get("heading"))}</h2>' + "".join(f"<p>{_e(p)}</p>" for p in (s.get("paragraphs") or []))
                   for s in a.get("sections", []))
    take = "".join(f"<li>{_e(x)}</li>" for x in a.get("takeaways", []))
    srcs = "".join(f'<li><a href="{_e(s.get("url"))}" target="_blank" rel="noopener">{_e(s.get("title"))}</a> '
                   f'<span class="by">{_e(s.get("publisher"))} · {_e(_fmt(s.get("date", "")))}</span></li>' for s in a.get("sources", []))
    more = [x for x in art_mod.list_articles(10) if x["slug"] != slug][:3]
    label = "Opinion · VigiEye View" if t == "opinion" else "Analysis"
    body = f"""<style>{_ACSS}</style>{_ticker()}{_secbar(a.get("section"))}<main class="wrap"><div class="body3"><article class="article" style="max-width:760px">
<div style="margin-top:14px"><span class="op-badge {t}">{label}</span> <a class="lab" style="color:{SEC_COLOR.get(a.get("section"))};margin-left:6px" href="/news/{_e(a.get("section"))}">{_e(nw.SECTIONS.get(a.get("section"), ""))}</a></div>
<h1 style="font:800 38px/1.12 Georgia,'Times New Roman',serif;margin:10px 0">{_e(a["title"])}</h1>
<p style="font-size:19px;color:var(--mut)">{_e(a.get("dek"))}</p>
<div class="by">VigiEye editorial · {_e(_fmt(a.get("date", "")))}</div>
<div style="margin:18px 0">{_art_thumb(a)}</div>
<div class="abody">{secs}</div>
{f'<div class="take"><b>What to do</b><ul>{take}</ul></div>' if take else ""}
{art_mod.ad_html("article-inline", "inline")}
<h2 style="font-size:18px;margin-top:22px">Sources this {("column" if t == "opinion" else "analysis")} is based on</h2><ul class="srcs">{srcs}</ul>
<p class="note">{("Opinion: this column argues a position." if t == "opinion" else "")} Written by VigiEye's editorial system with AI assistance
(model {_e(a.get("model"))}) strictly from the official records listed above. It can contain errors: check the sources before acting.
Not legal, medical or investment advice.</p>
{f'<div class="row"><div class="row-h"><span>More analysis &amp; opinion</span><a href="/analysis">All →</a></div><div class="feat">{"".join(article_card(x) for x in more)}</div></div>' if more else ""}
</article>{_sidebar(a.get("section"), stream(kinds=("press",), limit=20))}</div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "OpinionNewsArticle" if t == "opinion" else "AnalysisNewsArticle",
           "headline": a["title"][:110], "description": a.get("dek", ""), "datePublished": a.get("date"),
           "author": {"@type": "Organization", "name": "VigiEye editorial"},
           "publisher": {"@type": "Organization", "name": "VigiEye", "url": _base() + "/"},
           "isBasedOn": [s.get("url") for s in a.get("sources", []) if s.get("url")]}]
    return _page(f"{a['title'][:90]} | VigiEye {label}", (a.get("dek") or a["title"])[:155], f"/analysis/{slug}", body, ld)



# --- data feed for CompePharma (pharma business portal) --------------------------------------------

@router.get("/api/business-news", include_in_schema=False)
def api_business_news(beat: str = "", limit: int = 120, days: int = 30):
    """Pharma business stream for CompePharma: company releases, trade-press headlines, approvals,
    recalls/warning letters and enforcement, each tagged with a business beat."""
    since = (date.today() - timedelta(days=max(1, min(days, 120)))).isoformat()
    rows = [r for r in stream(limit=1500) if r["date"] >= since]
    # approvals from the regulatory data: novel FDA approvals and EU marketing authorisations
    appr = pp._safe(lambda: pd.milestones_between(since, date.today().isoformat(), ["fda_approval", "ec_decision"],
                                                  limit=300, desc=True), [])
    for m in appr:
        if m["kind"] == "fda_approval" and m.get("impact") != "high":
            continue
        href = pp._drug_href(m)
        rows.append({"kind": "approval", "id": m["id"], "section": "regulatory", "publisher": "FDA" if m["region"] == "US" else "European Commission",
                     "country": m["region"], "title": m["title"], "summary": m.get("detail") or "", "url": m.get("source_url"),
                     "date": m["date"], "impact": "high", "ticker": None, "_href": (_base() + href) if href else None})
    rows.sort(key=lambda r: r["date"], reverse=True)
    keep, recalls = [], 0
    for r in rows:
        pub = (r.get("publisher") or "").lower()
        if r["kind"] == "guideline":
            continue
        if "enforcement report" in pub:
            recalls += 1
            if recalls > 6:
                continue      # FDA's recall list is long; a few recent ones are enough for business readers
        if r["kind"] in ("official", "notice") and not (
                "justice" in pub or "enforcement report" in pub or "mhra alerts" in pub or "recall" in r["title"].lower()):
            continue          # regulatory/PV notices stay on VigiEye; only business-relevant official items pass
        b = "launches" if r["kind"] == "approval" else nw.biz_beat(r["title"], r.get("summary") or "")
        if beat and b != beat:
            continue
        keep.append({"id": f'{r["kind"][0]}-{r.get("id") or _seed(r)}', "kind": r["kind"], "beat": b,
                     "beat_label": nw.BIZ_BEATS.get(b, b), "title": r["title"],
                     "summary": (r.get("summary") if r["kind"] in ("company", "official", "notice") and _real_summary(r)
                                 else (r.get("summary") or "") if r["kind"] == "approval" else ""),
                     "publisher": r.get("publisher"), "country": r.get("country"), "ticker": r.get("ticker"),
                     "date": r["date"], "source_url": r.get("url"),
                     "vigieye_url": r.get("_href") or ((_base() + _href(r)) if r["kind"] in ("official", "company") else None),
                     "score": round(_score(r) if r["kind"] != "approval" else 6.0, 2)})
        if len(keep) >= max(1, min(limit, 400)):
            break
    return Response(json.dumps({"items": keep, "beats": nw.BIZ_BEATS, "generated": date.today().isoformat()},
                               ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@router.get("/api/editorial", include_in_schema=False)
def api_editorial(limit: int = 6):
    arts = art_mod.list_articles(max(1, min(limit, 30)))
    out = [{"slug": a["slug"], "type": a.get("type"), "section": a.get("section"), "title": a["title"],
            "dek": a.get("dek"), "date": a.get("date"), "url": f"{_base()}/analysis/{a['slug']}"} for a in arts]
    return Response(json.dumps({"items": out}, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=900", "Access-Control-Allow-Origin": "*"})
