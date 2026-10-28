"""
EU Regulatory Catalyst Calendar — VigiEye's public, BPIQ-style layer.

BPIQ built its audience on a free, indexable catalyst calendar (company + drug
pages, countdowns, filters) with deeper analysis behind the paywall. This module
does the same for EU regulation/pharmacovigilance:

  /calendar              public calendar (server-rendered for search engines)
  /calendar/{slug}       one indexable page per event, with JSON-LD Event markup
  /calendar.ics          subscribable iCal feed (Outlook / Google Calendar)
  /radar                 public digest of the latest monitored regulatory changes
  /api/catalysts         JSON feed with filters
  /calendar/subscribe    weekly "EU regulatory week ahead" email sign-up
  /calendar/unsubscribe  one-click unsubscribe (signed link)
  /robots.txt, /sitemap.xml

Every event comes from catalysts_verified.json, where each row carries the
official source URL it was checked against. Nothing on these pages is invented:
if a date can't be traced to an official page, it isn't listed.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from .config import settings

router = APIRouter()

_DATA = os.path.join(os.path.dirname(__file__), "catalysts_verified.json")

TYPE_LABEL = {
    "committee": "Committee meeting",
    "legislation": "Legislation milestone",
    "guideline": "Guideline effective",
    "consultation": "Consultation deadline",
    "deadline": "Compliance deadline",
    "conference": "Conference",
}
IMPACT_LABEL = {"high": "High impact", "med": "Medium impact", "low": "Low impact"}


# --- data -------------------------------------------------------------------

def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:80].strip("-")


def load_events() -> list[dict]:
    """All events, enriched with slug + countdown, sorted by date."""
    try:
        with open(_DATA, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = []
    today = date.today()
    out, seen = [], set()
    from .official import is_official
    for r in raw:
        if r.get("source_url") and not is_official(r["source_url"]):
            r = {**r, "source_url": ""}       # only official sources are linked (e.g. ISPOR dates shown unlinked)
        try:
            d = date.fromisoformat(r["date"])
        except Exception:
            continue
        end = None
        if r.get("end_date"):
            try:
                end = date.fromisoformat(r["end_date"])
            except Exception:
                end = None
        slug = _slug(f"{r['date']}-{r.get('body', '')}-{r.get('title', '')}")
        if slug in seen:
            continue
        seen.add(slug)
        last = end or d
        status = "past" if last < today else ("now" if d <= today <= last else "upcoming")
        out.append({**r, "slug": slug, "end_date": end.isoformat() if end else None,
                    "days": (d - today).days, "status": status,
                    "type_label": TYPE_LABEL.get(r.get("type", ""), "Event"),
                    "impact": r.get("impact") if r.get("impact") in IMPACT_LABEL else "med"})
    out.sort(key=lambda e: (e["date"], e.get("body", "")))
    return out


def _filter(events, type_=None, body=None, subject=None, impact=None, q=None,
            include_past=False):
    rows = events if include_past else [e for e in events if e["status"] != "past"]
    if type_:
        rows = [e for e in rows if e.get("type") == type_]
    if body:
        rows = [e for e in rows if e.get("body") == body]
    if subject:
        rows = [e for e in rows if e.get("subject") == subject]
    if impact:
        rows = [e for e in rows if e.get("impact") == impact]
    if q:
        ql = q.lower()
        rows = [e for e in rows if ql in (e.get("title", "") + " " + e.get("detail", "")).lower()]
    return rows


# --- rendering helpers ------------------------------------------------------

def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _base() -> str:
    return settings.app_base_url.rstrip("/")


def _fmt(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")
    except Exception:
        return d


def _countdown(e: dict) -> str:
    if e["status"] == "now":
        return '<span class="cd now">Happening now</span>'
    if e["status"] == "past":
        return '<span class="cd past">Past</span>'
    n = e["days"]
    if n == 0:
        return '<span class="cd hot">Today</span>'
    if n == 1:
        return '<span class="cd hot">Tomorrow</span>'
    cls = "hot" if n <= 7 else ("soon" if n <= 30 else "")
    return f'<span class="cd {cls}">in {n} days</span>'


_CSS = """
:root{--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--accent:#0284c7;--soft:#f8fafc;--bg:#fff;
      --hi:#e11d48;--hibg:#fee2e8;--md:#b45309;--mdbg:#fef3e2;--lo:#0f766e;--lobg:#e7fbf5}
@media (prefers-color-scheme: dark){:root{--ink:#e5e7eb;--mut:#94a3b8;--line:#1f2a3a;--soft:#0b1422;--bg:#060c18;
      --accent:#38bdf8;--hibg:#3a1020;--mdbg:#33230b;--lobg:#0b2b26;--hi:#fb7185;--md:#fbbf24;--lo:#2dd4bf}}
*{box-sizing:border-box;margin:0;padding:0}
body{font:16px/1.6 system-ui,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
nav{position:sticky;top:0;background:rgba(6,12,24,.9);backdrop-filter:blur(10px);border-bottom:1px solid rgba(255,255,255,.08);z-index:5}
nav .wrap{display:flex;align-items:center;justify-content:space-between;height:58px;gap:12px}
.logo{font-weight:800;font-size:19px;color:#fff}.logo b{color:#2dd4bf}
.nl a{color:#cbd5e1;margin-left:14px;font-size:14px;font-weight:500}.nl a.cta{background:linear-gradient(92deg,#0ea5e9,#14b8a6);color:#fff;padding:8px 14px;border-radius:9px}
@media(max-width:1100px){.nl a.hide{display:none}}
@media(max-width:640px){.nl a.hide2{display:none}}
@media(max-width:640px){.nl a.hide{display:none}.nl a{margin-left:10px;font-size:13px}.nl a.cta{padding:7px 10px}.logo{font-size:17px}}
.nl{white-space:nowrap}
header.hero{background:radial-gradient(900px 500px at 75% -20%,#0b2942,#050c17);color:#fff;padding:44px 0 34px}
header.hero h1{font-size:36px;line-height:1.12;letter-spacing:-.02em;max-width:780px}
header.hero p{color:#a9b4c9;max-width:680px;margin-top:12px;font-size:17px}
@media(max-width:640px){header.hero h1{font-size:27px}}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.stat{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:999px;padding:6px 13px;font-size:13px;color:#cbd5e1}
.stat b{color:#7dd3fc}
.sub{display:flex;gap:8px;margin-top:20px;max-width:520px;flex-wrap:wrap}
.sub input{flex:1;min-width:200px;padding:11px 13px;border:1px solid rgba(255,255,255,.18);border-radius:10px;font:inherit;background:rgba(255,255,255,.06);color:#fff}
.btn{display:inline-block;background:linear-gradient(92deg,#0ea5e9,#14b8a6);color:#fff;padding:11px 18px;border-radius:10px;font-weight:600;border:0;cursor:pointer;font-size:15px}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--ink)}
.msg{font-size:13px;color:#7dd3fc;margin-top:8px;min-height:18px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin:22px 0 14px}
.filters select,.filters input{padding:9px 11px;border:1px solid var(--line);border-radius:9px;font:inherit;font-size:14px;background:var(--bg);color:var(--ink)}
.filters input{flex:1;min-width:180px}
.month{margin:26px 0 8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}
.ev{display:grid;grid-template-columns:118px 1fr auto;gap:14px;align-items:start;padding:14px;border:1px solid var(--line);border-radius:12px;margin-bottom:8px;background:var(--bg)}
.ev:hover{border-color:var(--accent)}
.ev .dt{font-weight:700;font-size:14px}.ev .dt small{display:block;color:var(--mut);font-weight:500}
.ev h3{font-size:16px;line-height:1.35}.ev h3 a{color:var(--ink)}
.ev p{color:var(--mut);font-size:14px;margin-top:4px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
.chip{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.chip.high{background:var(--hibg);color:var(--hi);border-color:transparent}
.chip.med{background:var(--mdbg);color:var(--md);border-color:transparent}
.chip.low{background:var(--lobg);color:var(--lo);border-color:transparent}
.cd{font-size:12.5px;font-weight:700;white-space:nowrap;padding:4px 10px;border-radius:999px;background:var(--soft);color:var(--mut)}
.cd.soon{color:var(--md)}.cd.hot,.cd.now{color:#fff;background:#e11d48}.cd.past{opacity:.7}
@media(max-width:640px){.ev{grid-template-columns:1fr}.ev .cdw{order:-1}}
.lock{border:1px dashed var(--accent);border-radius:12px;padding:18px;margin:26px 0;background:var(--soft)}
.lock .sub input{background:var(--bg);color:var(--ink);border-color:var(--line)}.lock .msg{color:var(--accent)}
.lock h3{font-size:17px}.lock p{color:var(--mut);font-size:14.5px;margin:6px 0 12px}
.article{max-width:760px;padding:30px 0}
.article h2{font-size:20px;margin:24px 0 8px}
.article .detail{font-size:17px}
.meta{display:grid;grid-template-columns:150px 1fr;gap:6px 14px;border:1px solid var(--line);border-radius:12px;padding:16px;margin:18px 0;font-size:15px}
.meta dt{color:var(--mut)}
.related a{display:block;padding:10px 0;border-bottom:1px solid var(--line);color:var(--ink)}
.related small{color:var(--mut)}
footer{border-top:1px solid var(--line);margin-top:44px;padding:24px 0;color:var(--mut);font-size:13.5px}
.empty{padding:30px;text-align:center;color:var(--mut);border:1px dashed var(--line);border-radius:12px}
.upd{padding:14px 0;border-bottom:1px solid var(--line)}.upd h3{font-size:16px}.upd p{color:var(--mut);font-size:14.5px;margin-top:4px}
"""


def _page(title: str, description: str, canonical: str, body: str,
          jsonld: list[dict] | None = None) -> HTMLResponse:
    ld = "".join(f'<script type="application/ld+json">{json.dumps(j, ensure_ascii=False)}</script>'
                 for j in (jsonld or []))
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(canonical)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="VigiEye">
<meta property="og:title" content="{_e(title)}"><meta property="og:description" content="{_e(description)}">
<meta property="og:url" content="{_e(canonical)}"><meta name="twitter:card" content="summary">
<link rel="alternate" type="text/calendar" title="VigiEye EU regulatory calendar" href="{_base()}/calendar.ics">
<style>{_CSS}</style>{ld}</head><body>
<nav><div class="wrap"><a class="logo" href="/">👁️ Vigi<b>Eye</b></a>
<div class="nl"><a href="/news">News</a><a class="hide2" href="/safety">Safety</a><a class="hide2" href="/pdufa">PDUFA</a><a class="hide" href="/drugs">Drugs</a><a class="hide" href="/welcome">For teams</a><a class="cta" href="/newsletter">Daily email</a></div></div></nav>
{body}
<footer><div class="wrap">VigiEye · EU pharmacovigilance &amp; regulatory monitoring. Every date links to its official source; always confirm against the source before acting.
 · <a href="/">Home</a> · <a href="/news/pharmacovigilance">Pharmacovigilance</a> · <a href="/news/regulatory">Regulatory</a> · <a href="/news/market-access">Market access</a> · <a href="/news/compliance">Compliance</a> · <a href="/safety">Safety hub</a> · <a href="/guidelines">Guidelines</a> · <a href="/feed.xml">RSS</a> · <a href="/pdufa">PDUFA calendar</a> · <a href="/approvals">Approvals</a> · <a href="/safety">Safety</a> · <a href="/drugs">Drugs</a> · <a href="/calendar">EU calendar</a> · <a href="/calendar.ics">iCal</a> · <a href="/radar">News</a> · <a href="/insights">Insights</a> · <a href="/newsletter">Newsletter</a> · <a href="/embed">Embed ticker</a> · <a href="/welcome">About</a></div></footer>
<script>
async function vgSub(form){{const i=form.querySelector('input[type=email]'),m=form.parentNode.querySelector('.msg');
 m.textContent='…';try{{const r=await fetch('/calendar/subscribe',{{method:'POST',headers:{{'Content-Type':'application/json'}},
 body:JSON.stringify({{email:i.value,source:form.dataset.src||'calendar'}})}});const j=await r.json();
 m.textContent=j.message||j.error||'Done.';if(r.ok)i.value='';}}catch(e){{m.textContent='Could not subscribe right now.';}}return false;}}
</script></body></html>"""
    return HTMLResponse(doc)


def _subscribe_box(src: str, dark: bool = True) -> str:
    return f"""<form class="sub" data-src="{_e(src)}" onsubmit="return vgSub(this)">
<input type="email" required placeholder="Work email" aria-label="Work email">
<button class="btn" type="submit">Get the week ahead</button></form>
<div class="msg" aria-live="polite"></div>"""


def _event_row(e: dict) -> str:
    rng = _fmt(e["date"])
    if e.get("end_date") and e["end_date"] != e["date"]:
        rng += f"<small>to {_e(_fmt(e['end_date']))}</small>"
    return f"""<article class="ev" data-type="{_e(e.get('type'))}" data-body="{_e(e.get('body'))}"
 data-subject="{_e(e.get('subject'))}" data-impact="{_e(e['impact'])}" data-text="{_e((e.get('title','')+' '+e.get('detail','')).lower())}">
<div class="dt">{rng}</div>
<div><h3><a href="/calendar/{_e(e['slug'])}">{_e(e.get('title'))}</a></h3>
<p>{_e(e.get('detail'))}</p>
<div class="chips"><span class="chip">{_e(e.get('body'))}</span><span class="chip">{_e(e['type_label'])}</span>
<span class="chip">{_e(e.get('subject'))}</span><span class="chip {_e(e['impact'])}">{_e(IMPACT_LABEL[e['impact']])}</span></div></div>
<div class="cdw">{_countdown(e)}</div></article>"""


def _options(values, label):
    opts = "".join(f'<option value="{_e(v)}">{_e(v)}</option>' for v in values)
    return f'<option value="">{_e(label)}</option>{opts}'


# --- pages ------------------------------------------------------------------

@router.get("/calendar", response_class=HTMLResponse, include_in_schema=False)
def calendar_page(past: int = 0):
    events = load_events()
    rows = _filter(events, include_past=bool(past))
    upcoming = [e for e in events if e["status"] != "past"]
    next30 = [e for e in upcoming if e["days"] <= 30]
    high = [e for e in upcoming if e["impact"] == "high"]
    groups, cur = [], None
    for e in rows:
        m = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%B %Y")
        if m != cur:
            groups.append(f'<h2 class="month" data-month>{_e(m)}</h2>')
            cur = m
        groups.append(_event_row(e))
    listing = "".join(groups) or '<div class="empty">No upcoming events listed yet.</div>'
    types = sorted({e["type_label"] for e in rows})
    type_opts = "".join(f'<option value="{_e(k)}">{_e(v)}</option>'
                        for k, v in TYPE_LABEL.items() if v in types)
    bodies = sorted({e.get("body", "") for e in rows if e.get("body")})
    subjects = sorted({e.get("subject", "") for e in rows if e.get("subject")})
    nxt = upcoming[0] if upcoming else None
    nxt_html = (f'<span class="stat">Next: <b>{_e(nxt.get("body"))}</b> {_e(_fmt(nxt["date"]))}</span>'
                if nxt else "")
    body = f"""<header class="hero"><div class="wrap">
<h1>EU regulatory &amp; pharmacovigilance catalyst calendar</h1>
<p>Upcoming EMA committee meetings (CHMP, PRAC, CAT, PDCO, COMP), EU legislation milestones, MDR/IVDR and EUDAMED deadlines, guideline effective dates and consultation deadlines, each linked to its official source.</p>
<div class="stats"><span class="stat"><b>{len(upcoming)}</b> upcoming events</span>
<span class="stat"><b>{len(next30)}</b> in the next 30 days</span><span class="stat"><b>{len(high)}</b> high impact</span>{nxt_html}</div>
{_subscribe_box("calendar-hero")}
</div></header>
<main class="wrap">
<div class="filters" role="search">
<input id="fq" type="search" placeholder="Search events (e.g. PRAC, HTA, EUDAMED)" aria-label="Search events">
<select id="ft" aria-label="Event type"><option value="">All event types</option>{type_opts}</select>
<select id="fb" aria-label="Body">{_options(bodies, "All bodies")}</select>
<select id="fs" aria-label="Subject">{_options(subjects, "All subjects")}</select>
<select id="fi" aria-label="Impact"><option value="">Any impact</option><option value="high">High</option><option value="med">Medium</option><option value="low">Low</option></select>
<a class="btn ghost" href="/calendar.ics" title="Subscribe in Outlook or Google Calendar">Add to calendar</a>
</div>
<div id="list">{listing}</div>
<p style="margin-top:14px"><a href="/calendar?past={0 if past else 1}">{"Hide past events" if past else "Show past events"}</a></p>
<div class="lock"><h3>Know what each date means for <em>your</em> products</h3>
<p>VigiEye subscribers get AI-summarised outcomes the day each meeting publishes, alerts filtered to their markets and subjects, and an inspection-ready audit trail of every change.</p>
<a class="btn" href="/welcome#pricing">See plans</a> <a class="btn ghost" href="/radar">Browse the latest changes</a></div>
</main>
<script>
(function(){{const q=document.getElementById('fq'),t=document.getElementById('ft'),b=document.getElementById('fb'),
s=document.getElementById('fs'),i=document.getElementById('fi');
function run(){{const qq=q.value.trim().toLowerCase();let last=null,shown=0;
document.querySelectorAll('#list > *').forEach(el=>{{if(el.hasAttribute('data-month')){{last=el;el.style.display='none';el.dataset.n=0;return;}}
const ok=(!qq||el.dataset.text.includes(qq))&&(!t.value||el.dataset.type===t.value)&&(!b.value||el.dataset.body===b.value)
&&(!s.value||el.dataset.subject===s.value)&&(!i.value||el.dataset.impact===i.value);
el.style.display=ok?'':'none';if(ok&&last){{last.style.display='';shown++;}}}});}}
[q,t,b,s,i].forEach(x=>x.addEventListener('input',run));}})();
</script>"""
    ld = [{"@context": "https://schema.org", "@type": "ItemList",
           "name": "EU regulatory and pharmacovigilance catalyst calendar",
           "itemListElement": [{"@type": "ListItem", "position": n + 1,
                                "url": f"{_base()}/calendar/{e['slug']}", "name": e.get("title", "")}
                               for n, e in enumerate(upcoming[:50])]}]
    return _page("EU Regulatory Calendar 2026–2027: CHMP & PRAC dates, MDR deadlines",
                 "Free calendar of upcoming EU pharma regulatory events: CHMP, PRAC, CAT, PDCO and COMP meeting dates, "
                 "MDR/IVDR, HTA and EUDAMED deadlines, guideline effective dates. Every date sourced. iCal feed.",
                 f"{_base()}/calendar", body, ld)


def _outcome_line(ev: dict) -> str:
    if ev.get("type") == "committee":
        return (f"When {ev.get('body')} publishes its highlights and minutes, VigiEye subscribers get an AI "
                "summary of what changed and which of their products and markets it touches.")
    if ev.get("type") == "consultation":
        return ("When EMA publishes the final text, VigiEye subscribers get an AI summary of what changed "
                "versus the draft and who it affects.")
    return ("VigiEye subscribers get related guidance and regulatory changes summarised by AI, "
            "filtered to their products and markets. The free weekly email lists this date when it is two weeks out.")


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1) if m else (url or "")


@router.get("/api/catalysts")
def api_catalysts(type: str | None = None, body: str | None = None, subject: str | None = None,
                  impact: str | None = None, q: str | None = None, past: bool = False):
    rows = _filter(load_events(), type, body, subject, impact, q, include_past=past)
    return {"count": len(rows), "events": rows}


# --- iCal -------------------------------------------------------------------

def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:   # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


@router.get("/calendar.ics", include_in_schema=False)
def calendar_ics():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//VigiEye//EU Regulatory Calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:VigiEye — EU regulatory calendar",
             "X-WR-CALDESC:EU pharma regulatory and pharmacovigilance dates (vigi-eye.com/calendar)"]
    for e in load_events():
        start = e["date"].replace("-", "")
        end = (date.fromisoformat(e.get("end_date") or e["date"]) + timedelta(days=1)).isoformat().replace("-", "")
        lines += ["BEGIN:VEVENT", f"UID:{e['slug']}@vigi-eye.com", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{start}", f"DTEND;VALUE=DATE:{end}",
                  _fold("SUMMARY:" + _ics_escape(f"[{e.get('body','')}] {e.get('title','')}")),
                  _fold("DESCRIPTION:" + _ics_escape(f"{e.get('detail','')}\nSource: {e.get('source_url','')}\n"
                                                     f"{_base()}/calendar/{e['slug']}")),
                  _fold("URL:" + f"{_base()}/calendar/{e['slug']}"), "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="vigieye-eu-regulatory.ics"'})


# --- subscribe --------------------------------------------------------------

class SubIn(BaseModel):
    email: str
    source: str = "calendar"


def _unsub_token(email: str) -> str:
    return hmac.new(settings.session_secret.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:24]


@router.post("/calendar/subscribe")
def subscribe(body: SubIn):
    from . import store
    email = (body.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 320:
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    try:
        store.init()
        store.subscriber_add(email, body.source or "calendar")
    except Exception:
        return JSONResponse({"error": "Could not subscribe right now — please try again."}, status_code=500)
    return {"ok": True, "message": "You're in. The EU regulatory week ahead arrives every Monday."}


@router.get("/calendar/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(email: str = "", t: str = ""):
    from . import store
    if not email or not hmac.compare_digest(t, _unsub_token(email)):
        return _page("Unsubscribe | VigiEye", "Unsubscribe", f"{_base()}/calendar",
                     '<main class="wrap article"><h1>Link not valid</h1><p>Reply to any digest email and we will remove you.</p></main>')
    store.init()
    store.subscriber_remove(email)
    return _page("Unsubscribed | VigiEye", "Unsubscribed", f"{_base()}/calendar",
                 '<main class="wrap article"><h1>You are unsubscribed</h1><p>You will not receive the weekly email any more.</p></main>')


def render_week_ahead(email: str) -> tuple[str, str]:
    """Weekly email: the next 14 days of dates + link back. Returns (subject, html)."""
    events = [e for e in load_events() if e["status"] != "past" and e["days"] <= 14]
    items = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
        f'<b>{_e(_fmt(e["date"]))}</b> · {_e(e.get("body"))}<br>'
        f'<a href="{_base()}/calendar/{_e(e["slug"])}" style="color:#0284c7;font-weight:600">{_e(e.get("title"))}</a><br>'
        f'<span style="color:#64748b">{_e(e.get("detail"))}</span></td></tr>' for e in events) \
        or '<tr><td style="font:14px system-ui;color:#64748b">No listed EU dates in the next two weeks.</td></tr>'
    unsub = f"{_base()}/calendar/unsubscribe?email={_q(email)}&t={_unsub_token(email)}"
    html_doc = f"""<div style="max-width:620px;margin:auto;font:15px system-ui;color:#0f172a">
<h2 style="font-size:20px">EU regulatory week ahead</h2>
<p style="color:#64748b">The next 14 days of EU pharma regulatory and pharmacovigilance dates, from VigiEye.</p>
<table style="width:100%;border-collapse:collapse">{items}</table>
<p style="margin-top:18px"><a href="{_base()}/calendar" style="color:#0284c7">Full calendar</a> ·
<a href="{_base()}/radar" style="color:#0284c7">Latest changes</a> · <a href="{_base()}/welcome#pricing" style="color:#0284c7">Monitor your markets</a></p>
<p style="font-size:12px;color:#94a3b8;margin-top:24px">You subscribed at vigi-eye.com. <a href="{unsub}" style="color:#94a3b8">Unsubscribe</a></p></div>"""
    return f"EU regulatory week ahead — {len(events)} dates", html_doc


@router.post("/admin/calendar/send-weekly")
def admin_send_weekly(dry_run: bool = True, x_admin_token: str = Header(default="")):
    """Send the week-ahead email to all active subscribers. dry_run=true by default."""
    from . import store, emailer
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    store.init()
    emails = store.subscribers_active()
    if dry_run:
        return {"dry_run": True, "would_send_to": len(emails)}
    sent, errors = 0, []
    for em in emails:
        subj, doc = render_week_ahead(em)
        try:
            emailer.send(em, subj, doc)
            sent += 1
        except Exception as ex:
            errors.append({"email": em, "error": str(ex)})
    return {"sent": sent, "errors": errors[:20]}


# --- public radar (indexable digest of the monitored feed) -----------------

@router.get("/radar", response_class=HTMLResponse, include_in_schema=False)
def radar_page():
    from . import main as _main   # late import: reuse the live/sample switch
    rows, src = _main._current_updates()
    from .portal_pages import _relevant
    rows = [r for r in rows if _relevant(r)][:40]
    items = "".join(
        f'<div class="upd"><div class="chips"><span class="chip">{_e(r.get("country"))}</span>'
        f'<span class="chip">{_e(r.get("authority") or r.get("auth"))}</span><span class="chip">{_e(r.get("subject"))}</span>'
        f'<span class="chip {_e(r.get("impact"))}">{_e(IMPACT_LABEL.get(r.get("impact"), ""))}</span>'
        f'<span class="chip">{_e(_fmt(r.get("date","")))}</span></div>'
        f'<h3 style="margin-top:6px">{_e(r.get("title"))}</h3><p>{_e(r.get("summary"))}</p>'
        + (f'<p><a href="{_e(r.get("source_url"))}" rel="noopener" target="_blank">Official source →</a></p>' if r.get("source_url") else "")
        + '</div>' for r in rows) or '<div class="empty">No updates yet.</div>'
    note = "" if src == "live" else '<p style="color:var(--mut);font-size:13px">Showing a curated sample while the live feed warms up.</p>'
    body = f"""<header class="hero"><div class="wrap"><h1>EU regulatory radar: the latest pharmacovigilance &amp; regulatory changes</h1>
<p>What EMA, HMA/CMDh, EUR-Lex and national agencies published recently, summarised in plain language with a link to each official source.</p>
{_subscribe_box("radar")}</div></header><main class="wrap" style="padding-top:18px">{note}{items}
<div class="lock"><h3>Filter this to your countries, subjects and products</h3><p>Subscribers get this feed filtered to their markets, with impact flags, alerts and an audit trail.</p>
<a class="btn" href="/welcome#pricing">See plans</a></div></main>"""
    return _page("EU Pharmacovigilance & Regulatory Updates — latest changes | VigiEye",
                 "Latest EU pharmacovigilance and regulatory changes from EMA, HMA/CMDh, EUR-Lex and national agencies, "
                 "summarised in plain language with official sources.", f"{_base()}/radar", body)


# --- crawl files ------------------------------------------------------------

@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots():
    return ("User-agent: *\nAllow: /\nDisallow: /app\nDisallow: /api/\nDisallow: /admin/\n"
            "Disallow: /auth/\nDisallow: /billing/\nDisallow: /newsletter/preview\nDisallow: /embed/\n"
            f"Sitemap: {_base()}/sitemap.xml\nSitemap: {_base()}/news-sitemap.xml\n")


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    today = date.today().isoformat()
    urls = [("/welcome", "weekly", "0.8"), ("/calendar", "daily", "1.0"), ("/radar", "daily", "0.9")]
    try:
        from . import portal_pages, news_pages
        urls += news_pages.sitemap_urls() + portal_pages.sitemap_urls()
    except Exception:
        pass
    urls += [(f"/calendar/{e['slug']}", "weekly", "0.7") for e in load_events() if e["status"] != "past"]
    body = "".join(f"<url><loc>{_e(_base() + u)}</loc><lastmod>{today}</lastmod>"
                   f"<changefreq>{c}</changefreq><priority>{p}</priority></url>" for u, c, p in urls)
    return Response('<?xml version="1.0" encoding="UTF-8"?>'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
                    media_type="application/xml")


# Registered last so fixed paths (/calendar/unsubscribe …) win over the slug route.
@router.get("/calendar/{slug}", response_class=HTMLResponse, include_in_schema=False)
def event_page(slug: str):
    events = load_events()
    ev = next((e for e in events if e["slug"] == slug), None)
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    related = [e for e in events if e is not ev and e["status"] != "past"
               and (e.get("body") == ev.get("body") or e.get("subject") == ev.get("subject"))][:6]
    rel = "".join(f'<a href="/calendar/{_e(r["slug"])}">{_e(r.get("title"))}<br><small>{_e(_fmt(r["date"]))} · {_e(r.get("body"))}</small></a>'
                  for r in related) or "<p>No related events listed.</p>"
    when = _fmt(ev["date"]) + (f" – {_fmt(ev['end_date'])}" if ev.get("end_date") and ev["end_date"] != ev["date"] else "")
    gcal = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={_q(ev.get('title',''))}&dates={ev['date'].replace('-','')}/"
            f"{(date.fromisoformat(ev.get('end_date') or ev['date']) + timedelta(days=1)).isoformat().replace('-','')}"
            f"&details={_q((ev.get('detail','') + ' Source: ' + ev.get('source_url','')))}")
    body = f"""<main class="wrap"><div class="article">
<p><a href="/calendar">← EU regulatory calendar</a></p>
<h1 style="font-size:30px;line-height:1.2;margin-top:10px">{_e(ev.get('title'))}</h1>
<div class="chips" style="margin-top:10px"><span class="chip">{_e(ev.get('body'))}</span><span class="chip">{_e(ev['type_label'])}</span>
<span class="chip {_e(ev['impact'])}">{_e(IMPACT_LABEL[ev['impact']])}</span> {_countdown(ev)}</div>
<dl class="meta"><dt>Date</dt><dd>{_e(when)}</dd><dt>Body</dt><dd>{_e(ev.get('body'))}</dd>
<dt>Scope</dt><dd>{_e(ev.get('country') or 'EU')}</dd><dt>Subject</dt><dd>{_e(ev.get('subject'))}</dd>
<dt>Official source</dt><dd>{f'<a href="{_e(ev.get("source_url"))}" rel="noopener" target="_blank">{_e(_host(ev.get("source_url","")))}</a>' if ev.get("source_url") else _e(ev.get("body") or "Organiser")}</dd></dl>
<p class="detail">{_e(ev.get('detail'))}</p>
<p style="margin-top:16px"><a class="btn ghost" href="{_e(gcal)}" target="_blank" rel="noopener">Add to Google Calendar</a>
 <a class="btn ghost" href="/calendar.ics">Subscribe to all dates (iCal)</a></p>
<div class="lock"><h3>Get the outcome, not just the date</h3>
<p>{_e(_outcome_line(ev))}</p>
{_subscribe_box("event:" + ev['slug'])}<p style="font-size:13px;color:var(--mut)">Free weekly "EU regulatory week ahead" email. Or <a href="/welcome#pricing">start monitoring</a>.</p></div>
<h2>Related upcoming dates</h2><div class="related">{rel}</div>
<p style="font-size:13px;color:var(--mut);margin-top:22px">Dates are taken from the official source above. Agencies occasionally reschedule; confirm on the source before acting.</p>
</div></main>"""
    ld = [{"@context": "https://schema.org", "@type": "Event", "name": ev.get("title", ""),
           "startDate": ev["date"], "endDate": ev.get("end_date") or ev["date"],
           "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
           "eventStatus": "https://schema.org/EventScheduled",
           "location": {"@type": "VirtualLocation", "url": ev.get("source_url", "")},
           "organizer": {"@type": "Organization", "name": ev.get("body", "")},
           "description": ev.get("detail", ""), "url": f"{_base()}/calendar/{ev['slug']}"},
          {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "Calendar", "item": f"{_base()}/calendar"},
              {"@type": "ListItem", "position": 2, "name": ev.get("title", ""),
               "item": f"{_base()}/calendar/{ev['slug']}"}]}]
    return _page(f"{ev.get('title')} — {_fmt(ev['date'])} | VigiEye",
                 (ev.get("detail") or ev.get("title", ""))[:155],
                 f"{_base()}/calendar/{ev['slug']}", body, ld)
