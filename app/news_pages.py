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

from sqlalchemy import select

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

FLAG = {"MT": "🇲🇹", "LU": "🇱🇺", "HR": "🇭🇷", "NO": "🇳🇴", "IS": "🇮🇸", "AT": "🇦🇹", "BE": "🇧🇪", "BG": "🇧🇬", "CZ": "🇨🇿", "DK": "🇩🇰", "EE": "🇪🇪", "FI": "🇫🇮", "GR": "🇬🇷", "HU": "🇭🇺", "PT": "🇵🇹", "SK": "🇸🇰", "SI": "🇸🇮", "PL": "🇵🇱", "RO": "🇷🇴", "CA": "🇨🇦", "INT": "🌐", "SE": "🇸🇪", "IE": "🇮🇪", "NL": "🇳🇱", "UK": "🇬🇧", "US": "🇺🇸", "EU": "🇪🇺", "US": "🇺🇸", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱",
        "IE": "🇮🇪", "SE": "🇸🇪", "BE": "🇧🇪", "INT": "🌐", "CA": "🇨🇦"}
SEC_COLOR = {"regulatory": "#0369a1", "clinical-trials": "#0e7490", "pharmacovigilance": "#be123c", "quality": "#b45309",
             "market-access": "#6d28d9", "industry": "#0f766e"}
SEC_ABBR = {"regulatory": "REG", "clinical-trials": "CT", "pharmacovigilance": "PV", "quality": "QA", "market-access": "MA", "industry": "BIO"}
_PRIORITY_PUB = ("ema", "fda", "mhra", "prac", "european commission", "hma", "cmdh", "who")
ALL_KINDS = ("official", "notice", "guideline", "company")


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
                    "summary": _tidy(r["summary"]), "url": r.get("source_url") or "", "date": r.get("date") or "",
                    "impact": r.get("impact") or "med", "company": None, "ticker": None, "tags": None})
    return out


def _is_fin(r: dict) -> bool:
    return "financing" in (r.get("tags") or "")


def _company_press(limit: int = 1200, financing: bool = False) -> list[dict]:
    try:
        rows = nw.latest(limit=limit)
    except Exception:
        rows = []
    out = []
    for r in rows:
        if not financing and _is_fin(r):
            continue                     # financing news is CompePharma's beat, not VigiEye's
        sec = nw.norm_section(r.get("section"))
        if sec in ("regulatory", "industry") and r["kind"] in ("press", "guideline", "notice", "company") and \
                nw.classify(r.get("title") or "", r.get("summary") or "", "") == "clinical-trials":
            sec = "clinical-trials"      # items stored before the Clinical trials section existed
        out.append({**r, "section": sec, "title": _clean(r.get("title")),
                    "summary": _tidy(_clean(r.get("summary"))),
                    "impact": "high" if r["kind"] in ("company", "notice") else "med"})
    return out


def stream(section: str | None = None, kinds: tuple = ALL_KINDS, limit: int = 60, offset: int = 0,
           financing: bool = False) -> list[dict]:
    rows = []
    if "official" in kinds:
        rows += _official()
    if any(k in kinds for k in ("company", "notice", "guideline")):
        rows += [r for r in _company_press(financing=financing) if r["kind"] in kinds and r["kind"] != "press"]
    hm = _story_map()
    if hm:
        keep = []
        for r in rows:
            st = hm.get(_story_key(r))
            if st and st["skip"]:
                continue
            if st and st["has_body"]:
                r = {**r, "orig_title": r.get("title"), "title": st["headline"], "summary": st["dek"] or r.get("summary"), "story": True}
            keep.append(r)
        rows = keep
    if section:
        rows = [r for r in rows if r["section"] == section]
    rows.sort(key=lambda r: (r["date"], bool(r.get("story")), r["impact"] == "high"), reverse=True)
    # the same notice often arrives through two feeds (e.g. MHRA news + MHRA alerts): keep one
    seen, out = set(), []
    for r in rows:
        k = re.sub(r"[^a-z0-9]+", "", (r.get("orig_title") or r.get("title") or "").lower())[:90]
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out[offset: offset + limit]


def _story_key(r: dict) -> str:
    if r["kind"] == "official":
        return f"u:{r.get('id')}"
    return f"{'c' if r['kind'] == 'company' else 'n'}:{r.get('id')}"


_SM_CACHE: dict = {"t": 0.0, "m": {}}


def _story_map() -> dict:
    import time as _t
    if _t.time() - _SM_CACHE["t"] > 120:
        try:
            from . import stories as _st
            _SM_CACHE["m"] = _st.headline_map()
        except Exception:
            _SM_CACHE["m"] = {}
        _SM_CACHE["t"] = _t.time()
    return _SM_CACHE["m"]


def _tidy(sm: str | None) -> str:
    """Summaries stored with a length cap can end mid-word: cut back to the last full sentence."""
    sm = (sm or "").strip()
    if not sm or sm[-1] in ".!?)\"”":
        return sm
    i = max(sm.rfind(". "), sm.rfind("; "))
    return sm[:i + 1] if i > 60 else sm.rsplit(" ", 1)[0] + "…"


def _real_summary(r: dict) -> bool:
    sm, ti = (r.get("summary") or "").strip().lower(), (r.get("title") or "").strip().lower()
    return len(sm) > 70 and sm != ti and not sm.startswith(ti[:40])


def _section_extra(section: str | None) -> str:
    """Clinical trials front: legislation & guidance block + newly registered Phase 3 trials."""
    if section != "clinical-trials":
        return ""
    allr = _company_press(limit=1500)
    guides = [r for r in allr if r["kind"] == "guideline" and r["section"] == "clinical-trials"][:8]
    trials = [r for r in allr if r["kind"] == "trial"][:10]
    cal_ct = [e for e in cal.load_events() if e["status"] != "past" and (e.get("subject") or "").startswith("Clinical trials")][:6]
    out = ""
    if guides or cal_ct:
        cal_html = "".join(f'<li><a href="/calendar/{_e(e["slug"])}">{_e(e.get("title"))}</a><div class="by">{_e(_fmt(e["date"]))} · {_e(e.get("body"))}</div></li>' for e in cal_ct)
        out += (f'<section class="row" style="--c:#0e7490;margin-top:10px"><div class="row-h"><span style="color:#0e7490">Legislation &amp; guidance</span>'
                f'<a href="/guidelines">Guidelines tracker →</a></div>{"".join(river_item(r) for r in guides)}'
                + (f'<div class="box" style="margin-top:12px"><h2 style="font-size:13px;font-weight:900;letter-spacing:.08em;text-transform:uppercase">Key dates</h2><ul style="list-style:none">{cal_html}</ul></div>' if cal_html else "")
                + '</section>')
    if trials:
        li = "".join(f'<li style="padding:9px 0;border-bottom:1px solid var(--rule)"><a href="/news/item/n/{_e(r["id"])}" style="font-weight:700;color:var(--ink)">'
                     f'{_e(r["title"].replace("New Phase 3 trial: ", ""))}</a><div class="by">{_e(r.get("summary"))} · first posted {_e(_fmt(r["date"]))}</div></li>' for r in trials)
        out += (f'<section class="row" style="--c:#0e7490"><div class="row-h"><span style="color:#0e7490">Newly registered industry Phase 3 trials</span>'
                f'<a href="https://clinicaltrials.gov" target="_blank" rel="noopener">ClinicalTrials.gov ↗</a></div><ul style="list-style:none">{li}</ul></section>')
    return out


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
    s += 1.5 if r.get("story") else 0          # VigiEye has written the article
    return s - 1.6 * max(age, 0)               # yesterday's lead should not stay the lead


def _href(r: dict) -> str:
    if r["kind"] == "official" and r.get("id"):
        return f"/news/item/u/{r['id']}"
    if r["kind"] == "company":
        return f"/news/item/c/{r['id']}"
    if r.get("id"):
        return f"/news/item/n/{r['id']}"
    return "#"


def _is_ext(r: dict) -> bool:
    return False          # every story opens on VigiEye; only the article page links the official source


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
/* full-width layout */
.wrap{max-width:1480px;padding:0 28px}@media(max-width:640px){.wrap{padding:0 16px}}
:root{--band:#f4f6f9}@media (prefers-color-scheme: dark){:root{--band:#0a1220}}
.band{padding:34px 0}.band.alt{background:var(--band)}.band .row{margin-top:0}
.band.dark{background:radial-gradient(1400px 700px at 80% -20%,#0f3b57 0%,#08182b 45%,#050b15 100%);color:#e5e7eb}
.band.dark a,.band.dark article h2 a,.band.dark article h3 a{color:#f8fafc}.band.dark .by,.band.dark .kd{color:#94a3b8}
.mast.dark{border-color:#e5e7eb}.mast.dark h1{color:#fff}.mast.dark p,.mast.dark .date{color:#a9b4c9}
.hero{padding-top:18px}.hero-grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:34px;margin-top:24px;align-items:start}
@media(max-width:980px){.hero-grid{grid-template-columns:1fr}}
.hero-img{border-radius:10px;box-shadow:0 30px 60px -20px rgba(0,0,0,.6)}
.hero .h-lead{font-size:38px;line-height:1.1;margin:8px 0 12px}@media(max-width:640px){.hero .h-lead{font-size:28px}}
.hero-sum{font-size:17.5px;color:#cbd5e1}.hero-side{margin-top:18px;border-top:1px solid #1e2d44}
.hero-side .mini{border-color:#1e2d44}.hero-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:22px;margin-top:30px;padding-top:24px;border-top:1px solid #1e2d44}
@media(max-width:980px){.hero-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.hero-strip{grid-template-columns:1fr}}
.hcard h3{font-size:16.5px;line-height:1.3;margin-top:4px}.hcard .lab{display:inline-block;margin-top:10px}
.row-h.big{font-size:15px;align-items:center}.ico{width:20px;height:20px;vertical-align:-4px;margin-right:4px}
.pulse{padding:22px 0;background:linear-gradient(180deg,var(--band),var(--paper))}
.kpis2{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:14px;margin-top:10px}
@media(max-width:1200px){.kpis2{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:640px){.kpis2{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi2{display:flex;flex-direction:column;gap:2px;padding:14px;border-radius:10px;background:var(--paper);border:1px solid var(--rule);border-top:4px solid var(--k);color:var(--ink)}
.kpi2:hover{text-decoration:none;box-shadow:0 10px 24px -14px rgba(15,23,42,.35)}
.kpi2 .kl{font-size:11.5px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.kpi2 b{font:800 30px/1.1 Georgia,'Times New Roman',serif;color:var(--k)}.kpi2 .ks{font-size:12px;color:var(--mut)}
.spark{width:100%;height:34px;margin-top:6px}
.g4{gap:24px}.gcard{transition:transform .15s}.gcard:hover{transform:translateY(-2px)}
.th{border-radius:8px}.gcard .th,.hcard .th{box-shadow:0 14px 30px -18px rgba(15,23,42,.5)}
.heads2{columns:3;column-gap:30px;list-style:none}@media(max-width:1000px){.heads2{columns:2}}@media(max-width:600px){.heads2{columns:1}}
.heads2 li{break-inside:avoid;padding:10px 0;border-bottom:1px solid var(--rule)}.heads2 .mh{font-weight:700;margin-top:2px}
.hub{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 26px}@media(max-width:760px){.hub{grid-template-columns:1fr}}
"""


def _secbar(active: str | None = None) -> str:
    items = [("", "Home", "#0f172a"), ("regulatory", "Regulatory", SEC_COLOR["regulatory"]),
             ("clinical-trials", "Clinical trials", SEC_COLOR["clinical-trials"]),
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
    return pp._page(title, desc, path, f"<style>{_NCSS}{_ACSS}</style>{body}", ld)


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
    nat = [r for r in stream(kinds=("official",), limit=300) if r.get("country") not in ("EU", "US", "UK", "INT", "CA")][:8]
    heads = (f'<section class="box"><h2>Across Europe</h2><ul>{"".join(text_item(r) for r in nat)}</ul>'
             f'<p class="by">From national competent authorities and inspectorates, reported in English by VigiEye.</p></section>') if nat else ""
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
        if len(tops) >= 7:
            break
        if r["section"] != (lead or {}).get("section") or len(tops) >= 3:
            tops.append(r)
            used.add(id(r))
    river = [r for r in editorial if id(r) not in used and r["kind"] != "guideline"][:30]
    used |= {id(r) for r in river}
    press = [r for r in rows if r["kind"] == "press"]

    def row(k, extra=None, band="alt"):
        items = sorted([r for r in editorial if r["section"] == k and id(r) not in used], key=_score, reverse=True)[:8]
        if len(items) < 8:
            items += [r for r in press if r["section"] == k and id(r) not in used][: 8 - len(items)]
        used.update(id(r) for r in items)
        if not items:
            return ""
        return (f'<section class="band {band}"><div class="wrap"><div class="row-h big" style="--c:{SEC_COLOR[k]}">'
                f'<span style="color:{SEC_COLOR[k]}">{_svg_icon(k)} {_e(nw.SECTIONS[k])}</span>'
                f'<a href="{extra or "/news/" + k}">More {_e(nw.SECTIONS[k])} →</a></div>'
                f'<div class="g4">{"".join(grid_card(r) for r in items)}</div></div></section>')
    guides = [r for r in rows if r["kind"] == "guideline"][:8]
    rest = [r for r in rows if id(r) not in used and r["kind"] != "guideline"][:40]
    hero = ""
    if lead:
        strip = "".join(f'<article class="hcard">{_a(r, _thumb(r, "wide"))}{_label(r)}<h3>{_a(r, _e(r["title"]))}</h3>{_meta(r)}</article>'
                        for r in tops[3:7])
        side = "".join(mini_card(r) for r in tops[:3])
        hero = f"""<section class="band dark hero"><div class="wrap">
<div class="mast dark"><div><h1>Vigi<b>Eye</b></h1><p>Regulatory · Clinical trials · Pharmacovigilance · Quality · Market access, for EU &amp; US pharma, from the official sources</p></div>
<div class="date">{_e(date.today().strftime("%A %d %B %Y"))}</div></div>
<div class="hero-grid"><div class="hero-lead">{_a(lead, _thumb(lead, "wide hero-img"))}</div>
<div class="hero-text">{_label(lead)}<h2 class="h-lead">{_a(lead, _e(lead["title"]))}</h2>
{f'<p class="hero-sum">{_e(lead.get("summary"))}</p>' if _real_summary(lead) else ""}{_meta(lead)}
<div class="hero-side">{side}</div></div></div>
{f'<div class="hero-strip">{strip}</div>' if strip else ""}</div></section>"""
    guide_band = ""
    if guides:
        guide_band = (f'<section class="band"><div class="wrap"><div class="row-h big" style="--c:#6d28d9"><span style="color:#6d28d9">{_svg_icon("guidelines")} '
                      f'New guidelines: ICH, FDA, EMA, MHRA</span><a href="/guidelines">Guidelines tracker →</a></div>'
                      f'<div class="g4">{"".join(grid_card(r) for r in guides)}</div></div></section>')
    more_heads = ""
    if rest:
        more_heads = ('<section class="band alt"><div class="wrap"><div class="row-h big"><span>More news</span><a href="/news">All news →</a></div>'
                      '<ul class="heads2">' + "".join(f'<li>{_label(r)}<div class="mh">{_a(r, _e(r["title"]))}</div>{_meta(r)}</li>' for r in rest)
                      + "</ul></div></section>")
    ed = editorial_row(3)
    body = f"""{_ticker()}{_secbar("home")}
{hero or '<div class="wrap"><p class="note" style="margin:24px 0">The news feeds are loading for the first time. Check back in a few minutes.</p></div>'}
{_pulse(rows)}
{f'<section class="band"><div class="wrap">{ed}</div></section>' if ed else ""}
<section class="band"><div class="wrap"><div class="body3" style="margin-top:0"><div><div class="row-h big"><span>Latest</span><a href="/news">All news →</a></div>
{"".join(river_item(r) for r in river)}</div>{_sidebar(None, press)}</div></div></section>
{row("pharmacovigilance", "/safety")}{row("regulatory", band="")}{row("clinical-trials")}
<section class="band"><div class="wrap">{art_mod.ad_html("home-banner", "banner")}</div></section>
{row("quality")}{row("market-access", band="")}{guide_band}{row("industry")}
{more_heads}
<div class="wrap"><p class="note" style="margin:24px 0">{pp.DISCLAIMER}</p></div>"""
    ld = [{"@context": "https://schema.org", "@type": "NewsMediaOrganization", "name": "VigiEye", "url": _base() + "/"},
          {"@context": "https://schema.org", "@type": "WebSite", "name": "VigiEye", "url": _base() + "/",
           "potentialAction": {"@type": "SearchAction", "target": _base() + "/drugs?q={query}",
                               "query-input": "required name=query"}}]
    return _page("VigiEye: pharma regulatory, clinical trials, pharmacovigilance, quality & market access news",
                 "EU and US pharma news for regulatory, clinical trials, pharmacovigilance, quality and market-access professionals: "
                 "EMA, FDA, MHRA, PRAC, HTA, GMP and enforcement, with official sources.", "/", body, ld)


_ICON = {
    "regulatory": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    "clinical-trials": '<path d="M9 3h6M10 3v6L5 20h14L14 9V3"/><path d="M7.5 15h9"/>',
    "pharmacovigilance": '<path d="M3 12h4l2-5 3 10 2-5h7"/>',
    "quality": '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    "market-access": '<path d="M4 20h16M7 20v-6M11 20V9M15 20v-4M19 20V5"/>',
    "industry": '<path d="M9 3c6 4-6 10 0 18M15 3c-6 4 6 10 0 18"/>',
    "guidelines": '<path d="M5 4h10l4 4v12H5z"/><path d="M15 4v4h4M8 13h8M8 17h6"/>',
}


def _svg_icon(k: str) -> str:
    return (f'<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICON.get(k, _ICON["industry"])}</svg>')


def _spark(vals: list[int], color: str) -> str:
    if not vals:
        return ""
    w, h, mx = 120, 34, max(max(vals), 1)
    pts = [(round(i * w / max(len(vals) - 1, 1), 1), round(h - 3 - (v / mx) * (h - 6), 1)) for i, v in enumerate(vals)]
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"0,{h} " + line + f" {w},{h}"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polygon points="{area}" fill="{color}" opacity=".15"/><polyline points="{line}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round"/></svg>')


def _pulse(rows: list[dict]) -> str:
    """Live statistics strip: what moved in the last two weeks, with sparklines."""
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]

    def series(pred):
        c = {d: 0 for d in days}
        for r in rows:
            if r["date"] in c and pred(r):
                c[r["date"]] += 1
        return [c[d] for d in days]
    t = today.isoformat()
    week = (today - timedelta(days=7)).isoformat()
    pdufa = pp._safe(lambda: pd.milestones_between(t, (today + timedelta(days=30)).isoformat(), ["pdufa"], limit=500), [])
    ecexp = pp._safe(lambda: pd.milestones_between(t, (today + timedelta(days=60)).isoformat(), ["ec_expected"], limit=500), [])
    appr = pp._safe(lambda: pd.milestones_between((today - timedelta(days=30)).isoformat(), t, ["ec_decision", "fda_approval"], limit=999), [])
    cards = [
        ("Stories today", sum(1 for r in rows if r["date"] == t), "all sources", series(lambda r: True), "#0d9488", "/news"),
        ("Safety & PV, 7 days", sum(1 for r in rows if r["date"] >= week and r["section"] == "pharmacovigilance"), "PRAC, DHPCs, MHRA, FDA",
         series(lambda r: r["section"] == "pharmacovigilance"), SEC_COLOR["pharmacovigilance"], "/safety"),
        ("Quality & recalls, 7 days", sum(1 for r in rows if r["date"] >= week and r["section"] == "quality"), "GMP, recalls, enforcement",
         series(lambda r: r["section"] == "quality"), SEC_COLOR["quality"], "/news/quality"),
        ("New guidelines, 7 days", sum(1 for r in rows if r["date"] >= week and r["kind"] == "guideline"), "ICH, FDA, EMA, MHRA",
         series(lambda r: r["kind"] == "guideline"), "#6d28d9", "/guidelines"),
        ("PDUFA dates, next 30 days", len(pdufa), "company-announced", None, SEC_COLOR["regulatory"], "/pdufa"),
        ("EU decisions due, 60 days", len(ecexp), "estimated from CHMP opinions", None, SEC_COLOR["regulatory"], "/approvals?tab=expected"),
        ("Approvals, last 30 days", len(appr), "EU + FDA (NDA/BLA)", None, SEC_COLOR["clinical-trials"], "/approvals"),
    ]
    html_cards = "".join(
        f'<a class="kpi2" href="{href}" style="--k:{col}"><span class="kl">{_e(label)}</span><b>{val}</b>'
        f'<span class="ks">{_e(sub)}</span>{_spark(ser, col) if ser else ""}</a>' for label, val, sub, ser, col, href in cards)
    return f'<section class="band pulse"><div class="wrap"><div class="row-h"><span>Pulse: the last two weeks</span><a href="/calendar">EU calendar →</a></div><div class="kpis2">{html_cards}</div></div></section>'


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
    press = []
    color = SEC_COLOR.get(section or "", "inherit")
    body = f"""{_ticker()}{_secbar(section or "")}<main class="wrap">
<div class="mast"><div><h1 style="font-size:34px;color:{color}">{_e(title)}</h1><p>{_e(blurb)}</p></div>
<div class="date">{_e(date.today().strftime("%d %B %Y"))}</div></div>
<div class="body3"><div>{lead_html}{_section_extra(section) if page == 1 else ""}{"".join(river_item(r) for r in rows) or '<p class="note">No stories in this section yet.</p>'}{pager}</div>
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

def _story_html(st: dict) -> str:
    dek = f'<p class="detail" style="font-size:20px;color:var(--mut);font-style:italic">{_e(st.get("dek"))}</p>' if st.get("dek") else ""
    body = "".join(f'<p style="font-size:18px;line-height:1.65;margin:0 0 16px">{_e(p)}</p>' for p in st["body"])
    tk = st.get("takeaways") or []
    tk_html = (f'<div class="box" style="margin:22px 0;border-left:4px solid var(--c,#0369a1)"><h2 style="font-size:13px;'
               f'font-weight:900;letter-spacing:.08em;text-transform:uppercase">What it means for your team</h2><ul style="margin:8px 0 0 18px">'
               + "".join(f'<li style="margin:6px 0">{_e(t)}</li>' for t in tk) + '</ul></div>') if tk else ""
    return dek + body + tk_html


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
    elif kind in ("c", "n"):
        n = nw.get(nid)
        if not n or n["kind"] in ("press", "skip") or (kind == "c") != (n["kind"] == "company"):
            raise HTTPException(404)
        r = {**n, "section": nw.norm_section(n.get("section")), "impact": "high",
             "title": _clean(n.get("title")), "summary": _clean(n.get("summary"))}
    else:
        raise HTTPException(404)
    from . import stories as _st
    from .official import safe_url
    story = _st.get(_story_key(r)) if r.get("id") else None
    if story and story.get("lang") == "skip":
        story = None
    if story and story.get("body"):
        r = {**r, "title": story["headline"]}
    related = [x for x in stream(section=r["section"], limit=14) if x.get("id") != r.get("id")][:6]
    src_label = {"official": "Official source", "notice": "Official source", "guideline": "Official guideline text",
                 "trial": "ClinicalTrials.gov record"}.get(r["kind"], "Company's press release (SEC filing)")
    src_url = safe_url(r.get("url"))
    summary = r.get("summary") or ""
    note = ("Summary written by VigiEye from the official notice." if r["kind"] == "official" else
            "Brief written by VigiEye from the company's press release filed with the SEC. Claims are the company's own.")
    if story and story.get("body"):
        note = (f"Article written by VigiEye from the official source ({r.get('publisher')}). "
                + ("Claims are the company's own. " if r["kind"] == "company" else ""))
    elif not summary:
        note = "Headline as published by the authority."
    press = None
    body = f"""{_ticker()}{_secbar(r["section"])}<main class="wrap"><div class="body3"><article class="article" style="max-width:760px">
<div style="margin-top:8px">{_label(r)}</div>
<h1 style="font:800 36px/1.15 Georgia,'Times New Roman',serif;margin:8px 0 10px">{_e(r["title"])}</h1>
{_meta(r)}
<div style="margin:18px 0">{_thumb(r, "wide")}</div>
{_story_html(story) if story and story.get("body") else (f'<p class="detail" style="font-size:18px">{_e(summary)}</p>' if summary else "")}
{f'<p style="margin-top:18px"><a class="btn" href="{_e(src_url)}" target="_blank" rel="noopener">{_e(src_label)}: {_e(r.get("publisher"))} ↗</a></p>' if src_url else ""}
<p class="note">{_e(note)} AI-assisted; check the source before acting.</p>
<div class="row" style="--c:{SEC_COLOR.get(r["section"])}"><div class="row-h"><span>More {_e(nw.SECTIONS.get(r["section"], ""))}</span>
<a href="/news/{r["section"]}">All →</a></div><div class="g4" style="grid-template-columns:repeat(3,minmax(0,1fr))">{"".join(grid_card(x) for x in related)}</div></div>
</article>{_sidebar(r["section"], press)}</div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "NewsArticle", "headline": r["title"][:110],
           "datePublished": r.get("date"), "description": summary[:300],
           "image": [f"{_base()}/thumb/{r['section']}/{_seed(r)}.svg"],
           "publisher": {"@type": "Organization", "name": "VigiEye", "url": _base() + "/"},
           "isBasedOn": src_url, "articleSection": nw.SECTIONS.get(r["section"], "")}]
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
    "clinical-trials": '<path d="M-10 -30 H10 M-6 -30 V-6 L-24 24 H24 L6 -6 V-30" fill="none" stroke="#fff" stroke-width="3.5" stroke-linejoin="round"/>'
                       '<path d="M-14 10 H14" stroke="#fff" stroke-width="3"/>',
    "industry": '<path d="M-14 -30 C14 -14 -14 14 14 30 M14 -30 C-14 -14 14 14 -14 30" fill="none" stroke="#fff" stroke-width="3"/>'
                '<path d="M-9 -18 H9 M-10 0 H10 M-9 18 H9" stroke="#fff" stroke-width="2.5" opacity=".8"/>',
}


_SEC2 = {"regulatory": "#22d3ee", "clinical-trials": "#5eead4", "pharmacovigilance": "#fb7185", "quality": "#fbbf24",
         "market-access": "#c4b5fd", "industry": "#6ee7b7"}


@router.get("/thumb/{section}/{seed}.svg", include_in_schema=False)
def thumb(section: str, seed: str, p: str = ""):
    """Generated story artwork: layered gradient, molecular network, glowing section glyph, label pill."""
    sec = section if section in SEC_COLOR else "industry"
    h = hashlib.sha1(seed.encode()).digest() + hashlib.sha1(seed[::-1].encode()).digest()
    c, c2 = SEC_COLOR[sec], _SEC2.get(sec, "#99f6e4")
    ang = h[0] % 180
    # molecular network: nodes + links between near neighbours
    nodes = [(30 + (h[i] * 2.4) % 580, 24 + (h[i + 1] * 1.25) % 312, 3 + h[i + 2] % 6) for i in range(2, 38, 3)]
    links = []
    for i, (x1, y1, _) in enumerate(nodes):
        for x2, y2, _ in nodes[i + 1:]:
            if (x1 - x2) ** 2 + (y1 - y2) ** 2 < 150 ** 2:
                links.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}"/>')
    dots = "".join(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}"/>' for x, y, r in nodes)
    blobs = "".join(f'<circle cx="{(h[i] * 3) % 640}" cy="{(h[i + 1] * 2) % 360}" r="{60 + h[i + 2] % 140}" fill="url(#rg)" opacity=".55"/>'
                    for i in (30, 33, 36))
    label = re.sub(r"[^A-Z]", "", (p or "").upper())
    label = label if label in ("ANALYSIS", "VIEW") else label[:4]
    tag = f'{SEC_ABBR[sec]}{(" · " + label) if label else ""}'
    tw = 22 + len(tag) * 13
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" preserveAspectRatio="xMidYMid slice">
<defs>
<linearGradient id="g" gradientTransform="rotate({ang} .5 .5)"><stop offset="0" stop-color="{c}"/><stop offset=".62" stop-color="#0b1a2c"/><stop offset="1" stop-color="#050b15"/></linearGradient>
<radialGradient id="rg"><stop offset="0" stop-color="{c2}" stop-opacity=".55"/><stop offset="1" stop-color="{c2}" stop-opacity="0"/></radialGradient>
<pattern id="hx" width="28" height="48.5" patternUnits="userSpaceOnUse" patternTransform="scale(1.1)"><path d="M14 0 L28 8 V24 L14 32 L0 24 V8 Z M14 32 V48.5" fill="none" stroke="#fff" stroke-opacity=".05" stroke-width="1"/></pattern>
<filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="640" height="360" fill="url(#g)"/>{blobs}<rect width="640" height="360" fill="url(#hx)"/>
<g stroke="{c2}" stroke-opacity=".28" stroke-width="1.4">{"".join(links)}</g><g fill="{c2}" fill-opacity=".7">{dots}</g>
<circle cx="320" cy="172" r="92" fill="#fff" fill-opacity=".06" stroke="#fff" stroke-opacity=".18" stroke-width="2"/>
<g transform="translate(320 172) scale(2.25)" filter="url(#glow)">{_GLYPH[sec]}</g>
<rect x="20" y="310" rx="14" width="{tw}" height="30" fill="#000" fill-opacity=".35"/>
<text x="{20 + 12}" y="331" font-family="system-ui,Segoe UI,sans-serif" font-size="17" font-weight="800" fill="#fff" letter-spacing="2.5">{_e(tag)}</text>
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
    for r in stream(limit=1500):
        if r.get("id") and (r["kind"] in ("official", "company") or r.get("story")):
            urls.append((_href(r), "monthly", "0.6"))
    urls += [("/analysis", "daily", "0.9")] + [(f"/analysis/{a['slug']}", "monthly", "0.8") for a in art_mod.list_articles(300)]
    return urls


@router.get("/news-sitemap.xml", include_in_schema=False)
def news_sitemap():
    """Google News sitemap: VigiEye's own articles from the last 2 days."""
    since = (date.today() - timedelta(days=2)).isoformat()
    items = [(f"{_base()}/analysis/{a['slug']}", a["title"], a.get("date")) for a in art_mod.list_articles(60) if (a.get("date") or "") >= since]
    items += [(_base() + _href(r), r["title"], r["date"]) for r in stream(limit=600) if r.get("story") and r["date"] >= since]
    body = "".join(f"<url><loc>{_e(u)}</loc><news:news><news:publication><news:name>VigiEye</news:name><news:language>en</news:language>"
                   f"</news:publication><news:publication_date>{_e(d)}</news:publication_date><news:title>{_e(t)}</news:title></news:news></url>"
                   for u, t, d in items[:1000])
    return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    f'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{body}</urlset>', media_type="application/xml")


def fresh_urls(hours: int = 3) -> list[str]:
    """VigiEye pages created in the last few hours (for IndexNow)."""
    from datetime import datetime, timezone
    from . import stories as _st
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out = []
    try:
        _st.init()
        with store._engine.connect() as c:
            keys = [k for (k,) in c.execute(select(_st.stories.c.key).where(_st.stories.c.created_at >= cut)
                                            .where(_st.stories.c.lang == "en"))]
        out += [f"{_base()}/news/item/{k.split(':')[0]}/{k.split(':', 1)[1]}" for k in keys]
    except Exception:
        pass
    today = date.today().isoformat()
    out += [f"{_base()}/analysis/{a['slug']}" for a in art_mod.list_articles(40) if a.get("date") == today]
    return out + [f"{_base()}/", f"{_base()}/news", f"{_base()}/analysis"]


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
    from .official import is_official as _iso
    srcs = "".join((f'<li><a href="{_e(s.get("url"))}" target="_blank" rel="noopener">{_e(s.get("title"))}</a> ' if _iso(s.get("url"))
                    else f'<li>{_e(s.get("title"))} ')
                   + f'<span class="by">{_e(s.get("publisher"))} · {_e(_fmt(s.get("date", "")))}</span></li>' for s in a.get("sources", []))
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
</article>{_sidebar(a.get("section"), None)}</div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "OpinionNewsArticle" if t == "opinion" else "AnalysisNewsArticle",
           "headline": a["title"][:110], "description": a.get("dek", ""), "datePublished": a.get("date"),
           "author": {"@type": "Organization", "name": "VigiEye editorial"},
           "publisher": {"@type": "Organization", "name": "VigiEye", "url": _base() + "/"},
           "isBasedOn": [s.get("url") for s in a.get("sources", []) if s.get("url")]}]
    return _page(f"{a['title'][:90]} | VigiEye {label}", (a.get("dek") or a["title"])[:155], f"/analysis/{slug}", body, ld)



# --- data feed for CompePharma (pharma business portal) --------------------------------------------

@router.get("/api/story/{kind}/{nid}", include_in_schema=False)
def api_story(kind: str, nid: str):
    """The VigiEye-written article for one item (used by CompePharma story pages)."""
    from . import stories as _st
    from .official import safe_url
    k = {"u": "u", "o": "u", "c": "c", "n": "n"}.get(kind)
    st = _st.get(f"{k}:{nid}") if k else None
    if k == "u":
        try:
            store.init()
            raw = store.get_update(nid)
        except Exception:
            raw = None
        src, pub = (raw or {}).get("source_url"), (raw or {}).get("authority")
    else:
        raw = nw.get(nid) if k else None
        src, pub = (raw or {}).get("url"), (raw or {}).get("publisher")
    if not raw:
        raise HTTPException(404)
    ok = bool(st and st.get("body") and st.get("lang") != "skip")
    out = {"headline": st["headline"] if ok else _clean(raw.get("title")), "dek": st.get("dek") if ok else "",
           "body": st["body"] if ok else [], "takeaways": st.get("takeaways") if ok else [],
           "summary": _clean(raw.get("summary")), "publisher": pub, "source_url": safe_url(src),
           "vigieye_url": f"{_base()}/news/item/{k}/{nid}"}
    return Response(json.dumps(out, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=900", "Access-Control-Allow-Origin": "*"})


@router.get("/api/business-news", include_in_schema=False)
def api_business_news(beat: str = "", limit: int = 120, days: int = 30):
    from .official import safe_url
    """Pharma business stream for CompePharma: company releases (SEC filings), approvals,
    recalls/warning letters and enforcement, each tagged with a business beat."""
    since = (date.today() - timedelta(days=max(1, min(days, 120)))).isoformat()
    rows = [r for r in stream(limit=1500, financing=True) if r["date"] >= since]
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
    keep, recalls, seen_co = [], 0, set()
    for r in rows:
        pub = (r.get("publisher") or "").lower()
        if r["kind"] == "guideline":
            continue
        if re.search(r"\bcosmetic|teeth whitening|food supplement|tattoo", (r["title"] or "").lower()):
            continue          # not pharma business
        if "enforcement report" in pub:
            recalls += 1
            if recalls > 6:
                continue      # FDA's recall list is long; a few recent ones are enough for business readers
        if r["kind"] in ("official", "notice") and not (
                "justice" in pub or "enforcement report" in pub or "mhra alerts" in pub or "recall" in r["title"].lower()):
            continue          # regulatory/PV notices stay on VigiEye; only business-relevant official items pass
        b = "launches" if r["kind"] == "approval" else ("financing" if _is_fin(r) else nw.biz_beat(r["title"], r.get("summary") or ""))
        if r["kind"] == "company":
            ck = ((r.get("company") or r.get("publisher") or "").lower(), r["date"], b)
            if ck in seen_co:
                continue      # one company often files the same news as two exhibits
            seen_co.add(ck)
        if beat and b != beat:
            continue
        keep.append({"id": f'{r["kind"][0]}-{r.get("id") or _seed(r)}', "kind": r["kind"], "beat": b,
                     "beat_label": nw.BIZ_BEATS.get(b, b), "title": r["title"],
                     "summary": (r.get("summary") if r["kind"] in ("company", "official", "notice") and _real_summary(r)
                                 else (r.get("summary") or "") if r["kind"] == "approval" else ""),
                     "publisher": r.get("publisher"), "country": r.get("country"), "ticker": r.get("ticker"),
                     "date": r["date"], "source_url": safe_url(r.get("url")),
                     "vigieye_url": r.get("_href") or (_base() + _href(r)),
                     "score": round(_score(r) if r["kind"] != "approval" else 6.0, 2)})
        if len(keep) >= max(1, min(limit, 400)):
            break
    return Response(json.dumps({"items": keep, "beats": nw.BIZ_BEATS, "generated": date.today().isoformat()},
                               ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@router.get("/api/cp-articles", include_in_schema=False)
def api_cp_articles(limit: int = 30, kind: str = ""):
    """CompePharma's editorial (earnings analysis, business analysis, opinion): list without bodies."""
    arts = art_mod.cp_list(max(1, min(limit, 100)), kind=kind or None)
    out = [{k: a.get(k) for k in ("slug", "type", "beat", "title", "dek", "date", "company", "ticker", "period", "figures")}
           for a in arts]
    return Response(json.dumps({"items": out}, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"})


@router.get("/api/cp-article/{slug}", include_in_schema=False)
def api_cp_article(slug: str):
    a = art_mod.cp_get(slug)
    if not a:
        raise HTTPException(404)
    return Response(json.dumps(a, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=900", "Access-Control-Allow-Origin": "*"})


@router.get("/api/editorial", include_in_schema=False)
def api_editorial(limit: int = 6):
    arts = art_mod.list_articles(max(1, min(limit, 30)))
    out = [{"slug": a["slug"], "type": a.get("type"), "section": a.get("section"), "title": a["title"],
            "dek": a.get("dek"), "date": a.get("date"), "url": f"{_base()}/analysis/{a['slug']}"} for a in arts]
    return Response(json.dumps({"items": out}, ensure_ascii=False), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=900", "Access-Control-Allow-Origin": "*"})
