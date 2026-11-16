"""
Free reader sign-up ("registration wall") for article pages.

Anyone can read headlines, the standfirst and the first paragraph. The rest of an article needs a free account:
an email address (plus optional name, company and role). Signing up sets a signed cookie for a year; there is no
password. Search and ad crawlers (Googlebot, the AdSense crawler) get the full text, and article pages declare the
gated part with Google's paywalled-content markup (isAccessibleForFree=false + cssSelector .gated), which is how
Google expects registration walls to be marked so that it is not treated as cloaking.

The same module exists in the CompePharma repo; only `_store` and `_newsletter` differ.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from html import escape as _e

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import settings

SITE = "vigieye"
SITE_NAME = "VigiEye"
COOKIE = "vg_reader"
MAX_AGE = 365 * 24 * 3600
_SECRET = (settings.session_secret + "|reader-gate").encode()
_BOTS = re.compile(r"Googlebot|Mediapartners-Google|AdsBot-Google|Google-InspectionTool|GoogleOther|Storebot-Google|"
                   r"bingbot|BingPreview|DuckDuckBot|Applebot|LinkedInBot|Twitterbot|facebookexternalhit", re.I)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

# Paywalled-content markup to merge into an article's NewsArticle JSON-LD
LD_GATED = {"isAccessibleForFree": False,
            "hasPart": {"@type": "WebPageElement", "isAccessibleForFree": False, "cssSelector": ".gated"}}

ROLES = ["Regulatory affairs", "Pharmacovigilance / drug safety", "Quality / GMP", "Medical affairs", "Market access / HTA",
         "Clinical", "Commercial / business development", "Finance / investor", "Healthcare professional", "Consultant",
         "Other"]


# --- signed cookie -----------------------------------------------------------------------------------

def _sign(email: str) -> str:
    raw = base64.urlsafe_b64encode(f"{int(time.time())}|{email}".encode()).decode().rstrip("=")
    sig = hmac.new(_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def reader(request: Request) -> str | None:
    """The signed-in reader's email, or None."""
    tok = request.cookies.get(COOKIE) or ""
    raw, _, sig = tok.partition(".")
    if not raw or not sig:
        return None
    if not hmac.compare_digest(sig, hmac.new(_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        ts, _, email = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode().partition("|")
        if time.time() - int(ts) > MAX_AGE:
            return None
        return email or None
    except Exception:
        return None


def is_crawler(request: Request) -> bool:
    return bool(_BOTS.search(request.headers.get("user-agent", "")))


def can_read(request: Request) -> bool:
    return bool(reader(request)) or is_crawler(request)


# --- page fragments ----------------------------------------------------------------------------------

_CSS = """.rg{position:relative;margin:6px 0 26px}.rg .fade{height:90px;margin-top:-90px;position:relative;
background:linear-gradient(rgba(255,255,255,0),var(--bg,#fff))}
.rg .card{border:1px solid rgba(100,116,139,.35);border-radius:14px;padding:22px;background:var(--card,#f8fafc)}
.rg h3{margin:0 0 6px;font-size:21px}.rg p{margin:6px 0 12px;font-size:15px}.rg form{display:grid;gap:10px;grid-template-columns:1fr 1fr}
.rg input,.rg select{width:100%;padding:11px 12px;border:1px solid rgba(100,116,139,.45);border-radius:9px;font-size:15px;background:#fff;color:#0f172a}
.rg .full{grid-column:1/-1}.rg label.ck{display:flex;gap:8px;align-items:flex-start;font-size:13.5px;grid-column:1/-1}
.rg label.ck input{width:auto;margin-top:3px}.rg button{grid-column:1/-1;padding:12px;border:0;border-radius:10px;font-weight:800;
font-size:16px;cursor:pointer;background:#0f766e;color:#fff}.rg .msg{grid-column:1/-1;font-size:14px;min-height:1em}
.rg .fine{font-size:12.5px;opacity:.8;grid-column:1/-1}@media(max-width:600px){.rg form{grid-template-columns:1fr}}"""


def signup_box(source: str = "article") -> str:
    roles = "".join(f"<option>{_e(r)}</option>" for r in ROLES)
    return f"""<style>{_CSS}</style><div class="rg"><div class="fade"></div><div class="card">
<h3>Keep reading with a free {SITE_NAME} account</h3>
<p>Free for pharma and healthcare professionals. No password, no payment: just your email.</p>
<form onsubmit="return rgSignup(this)" data-src="{_e(source)}">
<input class="full" type="email" name="email" placeholder="Work email" required autocomplete="email">
<input type="text" name="name" placeholder="Name (optional)" autocomplete="name">
<input type="text" name="company" placeholder="Company (optional)" autocomplete="organization">
<select class="full" name="role"><option value="">Your role (optional)</option>{roles}</select>
<label class="ck"><input type="checkbox" name="newsletter" value="1"> Also send me the free daily {SITE_NAME} email (unsubscribe any time)</label>
<button type="submit">Create free account and keep reading</button><div class="msg" aria-live="polite"></div>
<div class="fine">By signing up you accept our <a href="/terms">terms</a> and <a href="/disclaimer">disclaimer</a>; we use your details as
described in our <a href="/privacy">privacy policy</a>. Already signed up on this device? Your access is remembered for a year.</div>
</form></div></div>
<script>async function rgSignup(f){{const m=f.querySelector('.msg');m.textContent='…';
const d=Object.fromEntries(new FormData(f).entries());d.newsletter=!!d.newsletter;d.source=f.dataset.src||'article';
try{{const r=await fetch('/reader/signup',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d),credentials:'same-origin'}});
const j=await r.json();if(r.ok){{m.textContent='Done, loading the full article…';location.reload();}}else m.textContent=j.error||'That did not work.';}}
catch(e){{m.textContent='Could not sign you up right now, please try again.';}}return false;}}</script>"""


def gated(request: Request, lead_html: str, rest_html: str, source: str = "article") -> str:
    """Lead is always shown; the rest only to signed-in readers and crawlers (inside .gated for the markup)."""
    if not rest_html.strip():
        return lead_html
    if can_read(request):
        return f'{lead_html}<div class="gated">{rest_html}</div>'
    return lead_html + signup_box(source)


def ld(obj: dict) -> dict:
    """Add the paywalled-content markup to one JSON-LD article object."""
    return {**obj, **LD_GATED}


# --- storage (VigiEye: own database) -----------------------------------------------------------------

def _table():
    from sqlalchemy import Boolean, Column, MetaData, String, Table
    from . import store
    meta = MetaData()
    t = Table("portal_readers", meta,
              Column("email", String(320), primary_key=True), Column("site", String(20), primary_key=True),
              Column("name", String(200)), Column("company", String(200)), Column("role", String(80)),
              Column("source", String(120)), Column("newsletter", Boolean), Column("created_at", String(40)),
              Column("last_seen", String(40)))
    meta.create_all(store._engine)
    return t, store._engine


def _store(rec: dict) -> None:
    from sqlalchemy import select
    t, eng = _table()
    now = datetime.now(timezone.utc).isoformat()
    with eng.begin() as c:
        hit = c.execute(select(t.c.email).where(t.c.email == rec["email"], t.c.site == rec["site"])).first()
        vals = {k: rec.get(k) for k in ("name", "company", "role", "source", "newsletter")}
        if hit:
            c.execute(t.update().where(t.c.email == rec["email"], t.c.site == rec["site"]).values(last_seen=now, **vals))
        else:
            c.execute(t.insert().values(email=rec["email"], site=rec["site"], created_at=now, last_seen=now, **vals))


def _newsletter(email: str) -> None:
    from . import store
    try:
        store.subscriber_add(email, "reader-signup", "daily")
    except Exception:
        pass


def _clean(rec: dict) -> dict | str:
    email = str(rec.get("email") or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 320:
        return "Enter a valid email address."
    role = str(rec.get("role") or "")[:80]
    return {"email": email, "name": str(rec.get("name") or "").strip()[:200], "company": str(rec.get("company") or "").strip()[:200],
            "role": role if role in ROLES else "", "source": str(rec.get("source") or "article")[:120],
            "newsletter": bool(rec.get("newsletter")), "site": str(rec.get("site") or SITE)[:20]}


router = APIRouter()


@router.post("/reader/signup", include_in_schema=False)
async def reader_signup(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    rec = _clean({**body, "site": SITE})
    if isinstance(rec, str):
        return JSONResponse({"error": rec}, status_code=400)
    try:
        _store(rec)
    except Exception:
        return JSONResponse({"error": "Could not sign you up right now, please try again."}, status_code=500)
    if rec["newsletter"]:
        _newsletter(rec["email"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, _sign(rec["email"]), max_age=MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


@router.post("/api/readers/signup", include_in_schema=False)
async def api_readers_signup(request: Request):
    """Server-to-server: CompePharma records its sign-ups here (site=compepharma)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    rec = _clean(body)
    if isinstance(rec, str):
        return JSONResponse({"error": rec}, status_code=400)
    try:
        _store(rec)
    except Exception:
        return JSONResponse({"error": "store failed"}, status_code=500)
    return {"ok": True}


@router.get("/reader/signout", include_in_schema=False)
def reader_signout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/api/readers/count", include_in_schema=False)
def readers_count():
    from sqlalchemy import func, select
    try:
        t, eng = _table()
        with eng.connect() as c:
            rows = c.execute(select(t.c.site, func.count()).group_by(t.c.site)).all()
        return {"readers": {s: n for s, n in rows}}
    except Exception:
        return {"readers": {}}
