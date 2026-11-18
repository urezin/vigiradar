"""
Reader paywall for article pages: one $4.99/month subscription unlocks every article on VigiEye AND CompePharma.

Anyone can read headlines, the standfirst and the first paragraph. The rest of every article is for subscribers.

Flow (no passwords):
  1. The reader enters an email under an article.
  2. We email a confirmation link (double opt-in). Nothing is unlocked and no newsletter subscription is made until
     the link is clicked, so nobody can sign someone else up.
  3. The link signs the reader in (signed cookie, one year). A paying reader is sent back to the article, unlocked;
     anyone else goes straight to Stripe Checkout ($4.99/month, cancel any time) and back to the article after paying.
  4. On a new device the same email + link signs a subscriber back in.

This VigiEye app is the hub: it holds the reader accounts, sends the confirmation emails and owns the Stripe
subscription. CompePharma's gate.py is a thin client of the /api/readers/* endpoints below.

Search and ad crawlers (Googlebot, the AdSense crawler) get the full text, and article pages declare the locked
part with Google's paywalled-content markup (isAccessibleForFree=false + cssSelector .gated), which is how Google
expects paywalls to be marked so that it is not treated as cloaking.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from html import escape as _e
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings

log = logging.getLogger("vigieye.gate")

SITE = "vigieye"
SITE_NAME = "VigiEye"
COOKIE = "vg_reader"
MAX_AGE = 365 * 24 * 3600
CONFIRM_MAX_AGE = 48 * 3600
LINK_MAX_AGE = 6 * 3600
PRICE_CENTS = int(os.getenv("READER_PRICE_CENTS", "499"))
CURRENCY = os.getenv("READER_CURRENCY", "usd")
PRICE_LABEL = os.getenv("READER_PRICE_LABEL", "$4.99")
PLAN_NAME = "VigiEye + CompePharma: all articles"
CP = os.getenv("COMPEPHARMA_URL", "https://compepharma.com").rstrip("/")
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


def _base() -> str:
    return (settings.app_base_url or "https://vigi-eye.com").rstrip("/")


# --- signed tokens -----------------------------------------------------------------------------------
# kind "a": access (cookie, 1 year) · "c": confirmation link (48 h) · "l": link to checkout / billing portal (6 h)

def _tok(kind: str, *fields: str) -> str:
    raw = base64.urlsafe_b64encode(json.dumps([kind, int(time.time()), *fields]).encode()).decode().rstrip("=")
    return f"{raw}.{hmac.new(_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]}"


def _untok(tok: str, kind: str, max_age: int) -> list | None:
    raw, _, sig = (tok or "").partition(".")
    if not raw or not sig or not hmac.compare_digest(sig, hmac.new(_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 3 or data[0] != kind or time.time() - int(data[1]) > max_age:
        return None
    return data[2:]


def access_token(email: str) -> str:
    return _tok("a", email)


def link_token(email: str) -> str:
    return _tok("l", email)


def email_from_access(tok: str) -> str | None:
    d = _untok(tok, "a", MAX_AGE)
    return d[0] if d else None


def reader(request: Request) -> str | None:
    """The signed-in (email-confirmed) reader's email, or None."""
    return email_from_access(request.cookies.get(COOKIE) or "")


def is_crawler(request: Request) -> bool:
    return bool(_BOTS.search(request.headers.get("user-agent", "")))


def _set_cookie(resp, request: Request, email: str):
    resp.set_cookie(COOKIE, access_token(email), max_age=MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


def _safe_return(url: str | None, default: str) -> str:
    """Only send readers back to our own sites."""
    try:
        u = urlparse(url or "")
    except Exception:
        return default
    hosts = {"vigi-eye.com", "www.vigi-eye.com", "compepharma.com", "www.compepharma.com",
             urlparse(_base()).hostname or "", urlparse(CP).hostname or ""}
    if u.scheme in ("https", "http") and u.hostname in hosts:
        return url
    return default


# --- accounts & subscription status --------------------------------------------------------------------

# Columns added in Nov 2026 for username/password and Google accounts (reader_auth.py). create_all() never alters an
# existing table, so _accounts() adds any that are missing with ALTER TABLE (works on SQLite and Postgres).
ACCOUNT_EXTRA = {"username": 40, "password_hash": 200, "google_sub": 64, "name": 200, "signup_site": 20,
                 "signup_method": 20, "last_login_at": None, "login_count": None}
_ACCOUNTS: dict = {}


def _accounts():
    if "t" in _ACCOUNTS:
        return _ACCOUNTS["t"], _ACCOUNTS["eng"]
    from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, text
    from . import store
    meta = MetaData()
    t = Table("reader_accounts", meta,
              Column("email", String(320), primary_key=True), Column("confirmed_at", String(40)),
              Column("customer_id", String(80)), Column("sub_id", String(80)), Column("sub_status", String(30)),
              Column("paid_until", Integer), Column("checked_at", Integer), Column("link_sent_at", Integer),
              Column("created_at", String(40)),
              *[Column(k, Integer if n is None else String(n)) for k, n in ACCOUNT_EXTRA.items()])
    meta.create_all(store._engine)
    have = {c["name"] for c in inspect(store._engine).get_columns("reader_accounts")}
    with store._engine.begin() as c:
        for k, n in ACCOUNT_EXTRA.items():
            if k not in have:
                c.execute(text(f"ALTER TABLE reader_accounts ADD COLUMN {k} {'INTEGER' if n is None else f'VARCHAR({n})'}"))
    _ACCOUNTS.update(t=t, eng=store._engine)
    return t, store._engine


def _acct(email: str) -> dict | None:
    from sqlalchemy import select
    t, eng = _accounts()
    with eng.connect() as c:
        row = c.execute(select(t).where(t.c.email == email)).mappings().first()
    return dict(row) if row else None


def _acct_set(email: str, **vals) -> None:
    from sqlalchemy import select
    t, eng = _accounts()
    with eng.begin() as c:
        if c.execute(select(t.c.email).where(t.c.email == email)).first():
            if vals:
                c.execute(t.update().where(t.c.email == email).values(**vals))
        else:
            c.execute(t.insert().values(email=email, created_at=datetime.now(timezone.utc).isoformat(), **vals))
    _paid_cache.pop(email, None)


# Stripe Managed Payments: Stripe (via Link) is the merchant of record, so it works out, collects and remits the VAT /
# sales tax of the reader's country and issues the receipts. Needs an eligible product tax code and API 2025-03-31+.
MANAGED_PAYMENTS = os.getenv("READER_MANAGED_PAYMENTS", "1").strip().lower() in ("1", "true", "yes", "on")
TAX_CODE = os.getenv("READER_TAX_CODE", "txcd_10304102")   # Digital Newspapers - viewable only - subscription
STRIPE_VERSION = os.getenv("READER_STRIPE_API_VERSION", "2025-03-31.basil")


def _stripe(method: str, path: str, data: dict | None = None) -> dict:
    import httpx
    with httpx.Client(timeout=30) as c:
        r = c.request(method, f"https://api.stripe.com/v1{path}", data=data, auth=(settings.stripe_secret_key, ""),
                      headers={"Stripe-Version": STRIPE_VERSION})
    if r.status_code >= 400:
        raise RuntimeError(f"stripe {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def _period_end(sub: dict) -> int:
    end = sub.get("current_period_end")
    if not end:   # newer Stripe API versions keep the period on the subscription item
        items = (sub.get("items") or {}).get("data") or []
        end = items[0].get("current_period_end") if items else 0
    return int(end or 0)


def _apply_subscription(sub: dict, email: str | None = None) -> None:
    email = (email or (sub.get("metadata") or {}).get("reader_email") or "").strip().lower()
    if not email:
        return
    _acct_set(email, customer_id=str(sub.get("customer") or ""), sub_id=str(sub.get("id") or ""),
              sub_status=str(sub.get("status") or ""), paid_until=_period_end(sub), checked_at=int(time.time()))


def on_subscription_event(obj: dict) -> None:
    """Called from the Stripe webhook for customer.subscription.* events (reader subscriptions carry reader_email)."""
    if (obj.get("metadata") or {}).get("reader_email"):
        _apply_subscription(obj)


_paid_cache: dict[str, tuple[float, bool]] = {}


def _complimentary() -> set[str]:
    """Readers with full access without paying: the site owner (OWNER_EMAIL, the address that gets the daily owner
    briefing) and anyone listed in READER_FREE_EMAILS (comma-separated), e.g. colleagues or reviewers."""
    emails = {e.strip().lower() for e in os.getenv("READER_FREE_EMAILS", "").split(",") if e.strip()}
    owner = os.getenv("OWNER_EMAIL", "").strip().lower()
    if not owner:
        try:
            from . import scheduler
            owner = str(scheduler._get("owner").get("email") or "").strip().lower()
        except Exception:
            owner = ""
    return emails | ({owner} if owner else set())


def is_paid(email: str | None) -> bool:
    if not email:
        return False
    if email.lower() in _complimentary():
        return True
    hit = _paid_cache.get(email)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    paid = False
    try:
        a = _acct(email)
        now = int(time.time())
        if a and a.get("sub_id"):
            ok = a.get("sub_status") in ("active", "trialing")
            if ok and (a.get("paid_until") or 0) > now:
                paid = True
            elif settings.stripe_secret_key and now - (a.get("checked_at") or 0) > 600:
                # period over (renewal?) or not active: ask Stripe, at most every 10 minutes
                _apply_subscription(_stripe("GET", f"/subscriptions/{a['sub_id']}"), email)
                a = _acct(email) or {}
                paid = a.get("sub_status") in ("active", "trialing") and (a.get("paid_until") or 0) > now
    except Exception as ex:
        log.warning("paid check for %s failed: %s", email, ex)
    _paid_cache[email] = (time.time(), paid)
    return paid


def can_read(request: Request) -> bool:
    return is_crawler(request) or is_paid(reader(request))


# --- page fragments ----------------------------------------------------------------------------------

_CSS = """.rg{position:relative;margin:6px 0 26px}.rg .fade{height:90px;margin-top:-90px;position:relative;
background:linear-gradient(rgba(255,255,255,0),var(--bg,#fff))}
.rg .card{border:1px solid rgba(100,116,139,.35);border-radius:14px;padding:22px;background:#f8fafc!important;color:#0f172a!important}.rg .card h3,.rg .card p,.rg .card li,.rg .card label,.rg .card .price,.rg .card .fine,.rg .card .msg{color:#0f172a!important}.rg .card .fine{color:#475569!important}.rg .card a:not(.rg-btn){color:#0f766e!important}
.rg h3{margin:0 0 6px;font-size:21px}.rg p{margin:6px 0 12px;font-size:15px}.rg form{display:grid;gap:10px;grid-template-columns:1fr}
.rg input{width:100%;padding:11px 12px;border:1px solid rgba(100,116,139,.45);border-radius:9px;font-size:15px;background:#fff;color:#0f172a}
.rg label.ck{display:flex;gap:8px;align-items:flex-start;font-size:13.5px}.rg label.ck input{width:auto;margin-top:3px}
.rg button,.rg a.rg-btn{display:block!important;text-align:center;padding:12px 16px!important;border:0!important;border-radius:10px!important;
font-weight:800!important;font-size:16px!important;cursor:pointer;background:#0f766e!important;color:#fff!important;text-decoration:none!important;
opacity:1!important;-webkit-text-fill-color:#fff}.rg a.rg-btn:hover,.rg button:hover{filter:brightness(1.1)}.rg .msg{font-size:14px;min-height:1em}.rg ul{margin:4px 0 12px;padding-left:20px;font-size:14.5px}
.rg .fine{font-size:12.5px;opacity:.8}.rg .price{font-size:26px;font-weight:800}"""

_BENEFITS = ("<ul><li>Every article, analysis and opinion piece on VigiEye and CompePharma</li>"
             "<li>One subscription for both sites, cancel any time</li></ul>")


def auth_box(return_url: str, source: str = "article", heading: str = "Subscribe to keep reading", fade: bool = True,
             start: str = "register") -> str:
    """Create account / sign in (username + password, or Google) card."""
    from . import reader_auth as ra
    return ra.auth_box(return_url, source, heading=heading, fade=fade, start=start,
                       google_url=ra.google_start_url(SITE, return_url) if ra.google_on() else None)


def signup_box(return_url: str, source: str = "article", site_name: str = SITE_NAME, action: str = "/reader/signup",
               newsletter_label: str | None = None, heading: str = "Subscribe to keep reading", fade: bool = True) -> str:
    nl = newsletter_label or f"Also send me the free daily {site_name} email (unsubscribe any time)"
    return f"""<style>{_CSS}</style><div class="rg">{'<div class="fade"></div>' if fade else ''}<div class="card">
<h3>{_e(heading)}</h3><p><span class="price">{_e(PRICE_LABEL)}</span> / month</p>{_BENEFITS}
<p class="fine" style="margin:-4px 0 10px">Already a subscriber? Enter your email below: we'll send you a sign-in link. No password needed.</p>
<form onsubmit="rgSignup(this);return false;" data-src="{_e(source)}" data-ret="{_e(return_url)}">
<input type="email" name="email" placeholder="Your email" required autocomplete="email">
<label class="ck"><input type="checkbox" name="newsletter" value="1"> {_e(nl)}</label>
<button type="submit">Continue</button><div class="msg" aria-live="polite"></div>
<div class="fine">We'll email you a link to confirm your address, then take you to secure checkout (Stripe). Billed monthly
until you cancel; access starts at once, so the 14-day withdrawal right ends when it does.
Already a subscriber? Enter your email and the link will sign you in.
See our <a href="/terms">terms</a> and <a href="/privacy">privacy policy</a>.</div>
</form></div></div>
<script>async function rgSignup(f){{const m=f.querySelector('.msg');m.textContent='…';
const d=Object.fromEntries(new FormData(f).entries());d.newsletter=!!d.newsletter;d.source=f.dataset.src||'article';d.return=f.dataset.ret||location.href;
try{{const r=await fetch('{action}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d),credentials:'same-origin'}});
const j=await r.json();if(r.ok){{f.innerHTML='<p><b>Check your inbox.</b> We sent a link to '+d.email.replace(/[<>&]/g,'')+'. Open it to continue (check spam if it is not there in a minute).</p>';}}else m.textContent=j.error||'That did not work.';}}
catch(e){{m.textContent='Something went wrong, please try again.';}}return false;}}</script>"""


def subscribe_box(email: str, subscribe_url: str, signout_url: str = "/reader/signout") -> str:
    return f"""<style>{_CSS}</style><div class="rg"><div class="fade"></div><div class="card">
<h3>Subscribe to keep reading</h3><p><span class="price">{_e(PRICE_LABEL)}</span> / month</p>{_BENEFITS}
<a class="rg-btn" href="{_e(subscribe_url)}">Subscribe with Stripe</a>
<p class="fine" style="margin-top:8px">Billed monthly until you cancel. Access starts at once, so the 14-day withdrawal right ends when it does.
<a href="/terms">Terms</a></p>
<p class="fine" style="margin-top:10px">Signed in as {_e(email)} · <a href="{_e(signout_url)}">not you?</a></p></div></div>"""


def _here(request: Request) -> str:
    q = f"?{request.url.query}" if request.url.query else ""
    return f"{_base()}{request.url.path}{q}"


def gated(request: Request, lead_html: str, rest_html: str, source: str = "article") -> str:
    """Lead is always shown; the rest only to paying subscribers and crawlers (inside .gated for the markup)."""
    if not rest_html.strip():
        return lead_html
    if can_read(request):
        return f'{lead_html}<div class="gated">{rest_html}</div>'
    here = _here(request)
    who = reader(request)
    if who:
        return lead_html + subscribe_box(who, f"/reader/subscribe?return={quote(here, safe='')}")
    return lead_html + auth_box(here, source)


def ld(obj: dict) -> dict:
    """Add the paywalled-content markup to one JSON-LD article object."""
    return {**obj, **LD_GATED}


# --- reader list (who signed up where) -----------------------------------------------------------------

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


def _newsletter(email: str, site: str) -> None:
    """Only called after the email is confirmed."""
    try:
        if site == "compepharma":
            from . import cp_email
            cp_email.cp_subscribe(cp_email.CpSub(email=email, source="cp:reader-signup"))
        else:
            from . import store
            store.subscriber_add(email, "reader-signup", "daily")
    except Exception as ex:
        log.warning("newsletter add for %s failed: %s", email, ex)


def _clean(rec: dict) -> dict | str:
    email = str(rec.get("email") or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 320:
        return "Enter a valid email address."
    role = str(rec.get("role") or "")[:80]
    return {"email": email, "name": str(rec.get("name") or "").strip()[:200], "company": str(rec.get("company") or "").strip()[:200],
            "role": role if role in ROLES else "", "source": str(rec.get("source") or "article")[:120],
            "newsletter": bool(rec.get("newsletter")), "site": "compepharma" if rec.get("site") == "compepharma" else SITE}


# --- confirmation email -------------------------------------------------------------------------------

def _send_link(rec: dict, return_url: str) -> str | None:
    """Email the confirmation / sign-in link. Returns an error message, or None."""
    from . import emailer
    email, site = rec["email"], rec["site"]
    try:
        a = _acct(email)
        if a and time.time() - (a.get("link_sent_at") or 0) < 60:
            return "We just sent you a link. Please check your inbox (and spam folder)."
        _acct_set(email, link_sent_at=int(time.time()))
    except Exception as ex:
        log.warning("account write for %s failed: %s", email, ex)
        return "Something went wrong, please try again."
    cp = site == "compepharma"
    name = "CompePharma" if cp else SITE_NAME
    home = CP if cp else _base()
    tok = _tok("c", email, site, "1" if rec.get("newsletter") else "", _safe_return(return_url, home + "/"))
    link = f"{home}/reader/confirm?t={quote(tok, safe='')}"
    color = "#7c3aed" if cp else "#0f766e"
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#0f172a">
<h2 style="margin:0 0 12px">{name}: confirm your email</h2>
<p>Click the button to confirm <b>{_e(email)}</b> and continue reading.</p>
<p style="margin:22px 0"><a href="{_e(link)}" style="background:{color};color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:bold">Confirm and continue</a></p>
<p style="font-size:14px">A subscription costs {_e(PRICE_LABEL)} per month and unlocks every article on VigiEye and CompePharma. If you already subscribe, this link signs you in.</p>
<p style="font-size:12px;color:#64748b">The link works for 48 hours. If you did not ask for it, ignore this email: nothing happens without your click.</p></div>"""
    try:
        kw = {}
        if cp:
            from . import cp_email
            kw = {"sender": cp_email.CP_FROM, "reply_to": cp_email.CP_REPLY_TO}
        emailer.send(email, f"Confirm your email for {name}", html_body, **kw)
    except Exception as ex:
        log.warning("confirmation email to %s failed: %s", email, ex)
        return "We could not send the email right now, please try again."
    return None


def _confirm(tok: str) -> dict | None:
    d = _untok(tok, "c", CONFIRM_MAX_AGE)
    if not d or len(d) < 4:
        return None
    email, site, newsletter, ret = d[0], d[1], d[2], d[3]
    a = _acct(email) or {}
    if not a.get("confirmed_at"):
        _acct_set(email, confirmed_at=datetime.now(timezone.utc).isoformat())
    if newsletter:
        _newsletter(email, site)
    return {"email": email, "site": site, "return": ret, "paid": is_paid(email)}


# --- routes --------------------------------------------------------------------------------------------

router = APIRouter()


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@router.post("/reader/signup", include_in_schema=False)
async def reader_signup(request: Request):
    body = await _json(request)
    rec = _clean({**body, "site": SITE})
    if isinstance(rec, str):
        return JSONResponse({"error": rec}, status_code=400)
    try:
        _store(rec)
    except Exception:
        pass
    err = _send_link(rec, str(body.get("return") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=429 if "just sent" in err else 500)
    return {"ok": True}


@router.post("/api/readers/start", include_in_schema=False)
async def api_readers_start(request: Request):
    """Server-to-server from CompePharma: record the sign-up and email the confirmation link."""
    body = await _json(request)
    rec = _clean(body)
    if isinstance(rec, str):
        return JSONResponse({"error": rec}, status_code=400)
    try:
        _store(rec)
    except Exception:
        pass
    err = _send_link(rec, str(body.get("return") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=429 if "just sent" in err else 500)
    return {"ok": True}


@router.post("/api/readers/signup", include_in_schema=False)
async def api_readers_signup(request: Request):
    """Legacy (older CompePharma builds): only records the sign-up; grants nothing."""
    rec = _clean(await _json(request))
    if isinstance(rec, str):
        return JSONResponse({"error": rec}, status_code=400)
    try:
        _store(rec)
    except Exception:
        return JSONResponse({"error": "store failed"}, status_code=500)
    return {"ok": True}


@router.get("/reader/confirm", include_in_schema=False)
def reader_confirm(request: Request, t: str = ""):
    try:
        r = _confirm(t)
    except Exception as ex:
        log.warning("confirm failed: %s", ex)
        r = None
    if not r:
        return HTMLResponse(_page("Link expired", "<p>This link has expired or is not valid. Go back to the article and enter "
                                  "your email again to get a new one.</p>"), status_code=400)
    ret = _safe_return(r["return"], _base() + "/")
    dest = ret if r["paid"] else f"/reader/subscribe?return={quote(ret, safe='')}"
    return _set_cookie(RedirectResponse(dest, status_code=303), request, r["email"])


@router.post("/api/readers/confirm", include_in_schema=False)
async def api_readers_confirm(request: Request):
    """Server-to-server from CompePharma: verify a confirmation token and hand back an access token."""
    body = await _json(request)
    r = _confirm(str(body.get("t") or ""))
    if not r:
        return JSONResponse({"error": "invalid or expired link"}, status_code=400)
    return {"email": r["email"], "paid": r["paid"], "return": r["return"],
            "access": access_token(r["email"]), "link": link_token(r["email"])}


@router.get("/api/readers/access", include_in_schema=False)
def api_readers_access(t: str = ""):
    """Server-to-server from CompePharma: who holds this access token, and have they paid?"""
    email = email_from_access(t)
    if not email:
        return JSONResponse({"error": "invalid token"}, status_code=401)
    a = _acct(email) or {}
    return {"email": email, "paid": is_paid(email), "link": link_token(email),
            "complimentary": email.lower() in _complimentary(), "has_subscription": bool(a.get("sub_id")),
            "username": a.get("username") or "", "has_password": bool(a.get("password_hash")), "google": bool(a.get("google_sub"))}


def _email_for_links(request: Request, t: str) -> str | None:
    if t:
        d = _untok(t, "l", LINK_MAX_AGE)
        if d:
            return d[0]
    return reader(request)


@router.get("/reader/subscribe", include_in_schema=False)
def reader_subscribe(request: Request, t: str = ""):
    ret = _safe_return(request.query_params.get("return"), _base() + "/")
    email = _email_for_links(request, t)
    if not email:
        return RedirectResponse(ret, status_code=303)
    if is_paid(email):
        return RedirectResponse(ret, status_code=303)
    if not settings.stripe_secret_key:
        return HTMLResponse(_page("Subscriptions open soon", "<p>Payments are not switched on yet. Please try again later.</p>"))
    try:
        a = _acct(email) or {}
        cust = a.get("customer_id") or _stripe("POST", "/customers", {"email": email, "metadata[reader_email]": email})["id"]
        if cust != a.get("customer_id"):
            _acct_set(email, customer_id=cust)
        s = _stripe("POST", "/checkout/sessions", {
            "mode": "subscription", "customer": cust, "client_reference_id": email,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": CURRENCY,
            "line_items[0][price_data][unit_amount]": str(PRICE_CENTS),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][product_data][name]": PLAN_NAME,
            "line_items[0][price_data][product_data][tax_code]": TAX_CODE,
            "line_items[0][price_data][tax_behavior]": "inclusive",   # $4.99 is the price the reader pays, tax included
            "subscription_data[metadata][reader_email]": email,
            "metadata[reader_email]": email, "allow_promotion_codes": "true",
            "success_url": f"{_base()}/reader/paid?session_id={{CHECKOUT_SESSION_ID}}&return={quote(ret, safe='')}",
            "cancel_url": ret,
            **({"managed_payments[enabled]": "true"} if MANAGED_PAYMENTS else {})})
        resp = RedirectResponse(s["url"], status_code=303)
        return _set_cookie(resp, request, email) if not reader(request) else resp
    except Exception as ex:
        log.warning("reader checkout for %s failed: %s", email, ex)
        return HTMLResponse(_page("Checkout unavailable", "<p>We could not open checkout right now. Please try again in a few minutes.</p>"),
                            status_code=502)


@router.get("/reader/paid", include_in_schema=False)
def reader_paid(request: Request, session_id: str = ""):
    ret = _safe_return(request.query_params.get("return"), _base() + "/")
    try:
        s = _stripe("GET", f"/checkout/sessions/{session_id}?expand[]=subscription")
        email = ((s.get("metadata") or {}).get("reader_email") or s.get("client_reference_id") or "").strip().lower()
        sub = s.get("subscription")
        if email and isinstance(sub, dict) and s.get("status") == "complete":
            _apply_subscription(sub, email)
            sep = "&" if "?" in ret else "?"
            return _set_cookie(RedirectResponse(f"{ret}{sep}subscribed=1", status_code=303), request, email)
    except Exception as ex:
        log.warning("reader paid check failed: %s", ex)
    return HTMLResponse(_page("Payment received?", f"<p>We could not confirm the payment yet. If you paid, it will be active within a "
                              f"few minutes: <a href=\"{_e(ret)}\">go back to the article</a> and refresh.</p>"))


@router.get("/reader/manage", include_in_schema=False)
def reader_manage(request: Request, t: str = ""):
    email = _email_for_links(request, t)
    a = _acct(email) if email else None
    if not a or not a.get("customer_id"):
        return HTMLResponse(_page("No subscription", "<p>We could not find a subscription for this browser. Open any article, "
                                  "enter your email and use the link we send you.</p>"))
    try:
        s = _stripe("POST", "/billing_portal/sessions", {"customer": a["customer_id"], "return_url": _base() + "/"})
        return RedirectResponse(s["url"], status_code=303)
    except Exception as ex:
        log.warning("billing portal for %s failed: %s", email, ex)
        return HTMLResponse(_page("Unavailable", "<p>Please try again in a few minutes, or email info@vigi-eye.com.</p>"), status_code=502)


def _account_html(email: str) -> str:
    a = _acct(email) or {}
    comp = email.lower() in _complimentary()
    paid = is_paid(email)
    if comp:
        status = "Full access (site administrator / complimentary)"
    elif paid:
        until = datetime.fromtimestamp(a.get("paid_until") or 0, timezone.utc).strftime("%d %b %Y") if a.get("paid_until") else ""
        status = f"Subscriber: full access{f', renews or ends {until}' if until else ''}"
    else:
        status = "Not subscribed: you can read headlines and the opening of each story"
    btns = []
    if not paid:
        btns.append(f'<a class="rg-btn" href="/reader/subscribe?return={quote(_base() + "/", safe="")}">Subscribe for {_e(PRICE_LABEL)} / month</a>')
    if a.get("sub_id") and not comp:
        btns.append('<a class="rg-btn" style="background:#334155!important" href="/reader/manage">Manage or cancel subscription</a>')
    btns.append('<a class="rg-btn" style="background:#64748b!important" href="/reader/signout">Sign out</a>')
    return (f'<style>{_CSS}</style><div class="rg"><div class="card"><h3>Your account</h3>'
            f'<p>Signed in as <b>{_e(email)}</b></p><p><b>{_e(status)}</b></p><div style="display:grid;gap:10px">{"".join(btns)}</div>'
            f'{_extra(email)}'
            f'<p class="fine" style="margin-top:12px">One subscription covers VigiEye and CompePharma: sign in on CompePharma with the same username or email and password.</p></div></div>')


def _extra(email: str) -> str:
    try:
        from . import reader_auth
        return reader_auth.account_extra(email)
    except Exception as ex:
        log.warning("account extras failed: %s", ex)
        return ""


HOW_IT_WORKS = ('<ol style="font-size:15px;line-height:1.6;padding-left:20px"><li><b>Create an account</b> with your email, a username and a '
                'password, or press <b>Continue with Google</b>.</li>'
                '<li>Next time, <b>sign in</b> with your username (or email) and password, on any phone or computer. The same account works on '
                'VigiEye and CompePharma.</li>'
                '<li>Subscribers go straight back to reading. Everyone else is taken to secure checkout (Stripe, '
                + PRICE_LABEL + ' a month, cancel any time).</li><li>Forgot your password, or signed up before with just your email? Use '
                '<b>Forgot password?</b> under Sign in and we email you a link to set one.</li></ol>')


@router.get("/reader/login", include_in_schema=False)
def reader_login(request: Request):
    from . import portal_pages as _pp
    who = reader(request)
    ret = _safe_return(request.query_params.get("return"), _base() + "/")
    if who:
        body = _account_html(who)
    else:
        body = (f'<p style="font-size:16px">Headlines and the first paragraph of every story are free. Full articles on VigiEye and '
                f'CompePharma are for subscribers.</p><h2 style="font-size:19px;margin:18px 0 4px">How signing in works</h2>{HOW_IT_WORKS}'
                + auth_box(ret, "login", heading="Sign in or create an account", fade=False, start="login"))
    html = f'<main style="max-width:640px;margin:30px auto;padding:0 16px"><h1 style="font-size:30px">{"Your account" if who else "Sign in"}</h1>{body}</main>'
    return _pp._page("Sign in | VigiEye", "Sign in to VigiEye or subscribe to read every article.", "/reader/login", html)


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


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{_e(title)} | {SITE_NAME}</title></head>'
            f'<body style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:60px auto;padding:0 16px;color:#0f172a">'
            f'<h1 style="font-size:26px">{_e(title)}</h1>{body}<p><a href="/">VigiEye home</a></p></body></html>')
