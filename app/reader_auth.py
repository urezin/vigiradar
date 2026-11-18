"""
Reader accounts with a username and password, plus "Continue with Google", for VigiEye AND CompePharma.

Builds on gate.py (the paywall hub). Until Nov 2026 readers signed in only through an emailed link; now:

  * Create account: email + username + password. The reader is signed in at once; we still email a link to confirm
    the address (the free daily email starts only after that click, so nobody can sign someone else up for mail).
  * Sign in: email OR username + password. 8 wrong passwords in 15 minutes lock that account for 15 minutes.
  * Forgot password: emailed reset link (2 hours, single use: it stops working once the password changes).
    Readers from the old email-link era (no password yet) use it once to set one, or simply continue with Google.
  * Continue with Google: one OAuth client on vigi-eye.com (redirect /reader/google/callback). Google accounts with a
    verified email are linked to the reader account with that email, or create one.
  * Email links still work as a fallback (gate.py /reader/confirm), so nobody already signed up is locked out.

CompePharma has no accounts of its own: its /reader/* routes call the /api/readers/* endpoints below and keep the
returned access token in its cp_reader cookie. Google sign-in for CompePharma goes through vigi-eye.com and comes back
to compepharma.com/reader/sso with a 10-minute hand-off token.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from html import escape as _e
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import gate
from .config import settings

log = logging.getLogger("vigieye.reader_auth")
router = APIRouter()

RESET_MAX_AGE = 2 * 3600
HANDOFF_MAX_AGE = 600
STATE_MAX_AGE = 900
_USER = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")
_RESERVED = {"admin", "administrator", "root", "support", "info", "vigieye", "compepharma", "pqra", "owner", "billing"}
GOOGLE = {"authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
          "userinfo": "https://openidconnect.googleapis.com/v1/userinfo"}


def _table():
    return gate._accounts()


def _by(**kw) -> dict | None:
    from sqlalchemy import select
    t, eng = _table()
    (k, v), = kw.items()
    with eng.connect() as c:
        row = c.execute(select(t).where(getattr(t.c, k) == v)).mappings().first()
    return dict(row) if row else None


def _set(email: str, **vals) -> None:
    gate._acct_set(email, **vals)


# --- passwords -----------------------------------------------------------------------------------------------
_ITER = 260_000


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def check_pw(pw: str, stored: str | None) -> bool:
    try:
        algo, it, salt, h = (stored or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def _pw_problem(pw: str) -> str | None:
    if len(pw) < 8:
        return "Use a password of at least 8 characters."
    if len(pw) > 200:
        return "That password is too long."
    if pw.isdigit() or pw.lower() in ("password", "password1", "12345678", "qwertyui"):
        return "Choose a less obvious password."
    return None


# --- brute-force lock (per account, in memory) ---------------------------------------------------------------
_fails: dict[str, list[float]] = {}


def _locked(key: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = hits
    return len(hits) >= 8


def _fail(key: str) -> None:
    _fails.setdefault(key, []).append(time.time())
    if len(_fails) > 20000:
        _fails.clear()


# --- usernames -----------------------------------------------------------------------------------------------

def _username_problem(u: str) -> str | None:
    if not _USER.match(u):
        return "Usernames are 3 to 30 characters: letters, numbers, dot, dash or underscore, starting with a letter or number."
    if u in _RESERVED:
        return "That username is reserved, please choose another."
    if _by(username=u):
        return "That username is taken, please choose another."
    return None


def _free_username(seed: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", (seed or "reader").lower())[:24].strip("._-") or "reader"
    if len(base) < 3:
        base = (base + "reader")[:8]
    cand = base
    while cand in _RESERVED or _by(username=cand):
        cand = f"{base}{secrets.randbelow(9000) + 1000}"
    return cand


# --- core operations (used by the browser routes here and by CompePharma over the API) ---------------------

def _site(v) -> str:
    return "compepharma" if v == "compepharma" else gate.SITE


def _home(site: str) -> str:
    return gate.CP if site == "compepharma" else gate._base()


def _result(email: str, site: str, ret: str) -> dict:
    """What a signed-in client needs: tokens, and where to send the reader next."""
    ret = gate._safe_return(ret, _home(site) + "/")
    paid = gate.is_paid(email)
    link = gate.link_token(email)
    a = _by(email=email) or {}
    nxt = ret if paid else f"{gate._base()}/reader/subscribe?t={quote(link, safe='')}&return={quote(ret, safe='')}"
    return {"ok": True, "email": email, "username": a.get("username") or "", "paid": paid,
            "access": gate.access_token(email), "link": link, "next": nxt}


def _touch(email: str) -> None:
    a = _by(email=email) or {}
    _set(email, last_login_at=int(time.time()), login_count=int(a.get("login_count") or 0) + 1)


def register(body: dict) -> tuple[int, dict]:
    site = _site(body.get("site"))
    rec = gate._clean({**body, "site": site})
    if isinstance(rec, str):
        return 400, {"error": rec}
    email = rec["email"]
    username = str(body.get("username") or "").strip().lower()
    pw = str(body.get("password") or "")
    err = _username_problem(username) or _pw_problem(pw)
    if err:
        return 400, {"error": err}
    a = _by(email=email)
    if a and (a.get("password_hash") or a.get("google_sub") or a.get("confirmed_at") or a.get("sub_id")):
        return 409, {"error": "An account with this email already exists. Sign in, or use “Forgot password” to set a new password."}
    # a bare row (email entered once, never confirmed) is free to claim: nobody proved they own that address
    _set(email, username=username, password_hash=hash_pw(pw), name=rec.get("name") or None, signup_site=site,
         signup_method="password", last_login_at=int(time.time()), login_count=1)
    try:
        gate._store({**rec, "source": f"register:{rec.get('source') or 'article'}"})
    except Exception:
        pass
    _fails.pop(email, None)
    warn = gate._send_link(rec, str(body.get("return") or ""))   # confirm address (+ newsletter if ticked)
    out = _result(email, site, str(body.get("return") or ""))
    if warn and "just sent" not in warn:
        out["notice"] = "Account created. We could not send the confirmation email just now; you can still read and subscribe."
    return 200, out


def login(body: dict) -> tuple[int, dict]:
    site = _site(body.get("site"))
    who = str(body.get("login") or body.get("email") or body.get("username") or "").strip().lower()
    pw = str(body.get("password") or "")
    if not who or not pw:
        return 400, {"error": "Enter your email or username and your password."}
    a = _by(email=who) if "@" in who else _by(username=who)
    key = (a or {}).get("email") or who
    if _locked(key):
        return 429, {"error": "Too many attempts. Wait 15 minutes, or use “Forgot password”."}
    if not a or not check_pw(pw, a.get("password_hash")):
        _fail(key)
        if a and not a.get("password_hash"):
            return 401, {"error": "This account has no password yet. Use “Forgot password” to set one"
                                  + (", or continue with Google." if a.get("google_sub") else ".")}
        return 401, {"error": "Wrong email/username or password."}
    _fails.pop(key, None)
    _touch(a["email"])
    return 200, _result(a["email"], site, str(body.get("return") or ""))


def _reset_tok(email: str, pw_hash: str | None) -> str:
    # binds the token to the current password: once the password changes, the link is dead (single use)
    return gate._tok("r", email, hashlib.sha256((pw_hash or "-").encode()).hexdigest()[:16])


def _reset_email(tok: str) -> str | None:
    d = gate._untok(tok, "r", RESET_MAX_AGE)
    if not d or len(d) < 2:
        return None
    a = _by(email=d[0])
    if not a or hashlib.sha256((a.get("password_hash") or "-").encode()).hexdigest()[:16] != d[1]:
        return None
    return d[0]


_reset_sent: dict[str, float] = {}


def forgot(body: dict) -> tuple[int, dict]:
    from . import emailer
    site = _site(body.get("site"))
    who = str(body.get("email") or body.get("login") or "").strip().lower()
    a = _by(email=who) if "@" in who else _by(username=who)
    ok = {"ok": True, "message": "If an account exists for that address, we have emailed a link to set a new password."}
    if not a:
        return 200, ok
    if time.time() - _reset_sent.get(a["email"], 0) < 60:
        return 429, {"error": "We just sent you an email. Please check your inbox (and spam folder)."}
    _reset_sent[a["email"]] = time.time()
    cp = site == "compepharma"
    name, home, color = ("CompePharma", gate.CP, "#e4322b") if cp else (gate.SITE_NAME, gate._base(), "#0f766e")
    link = f"{home}/reader/reset?t={quote(_reset_tok(a['email'], a.get('password_hash')), safe='')}"
    uname = a.get("username") or ""
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#0f172a">
<h2 style="margin:0 0 12px">{name}: set a new password</h2>
<p>Someone (hopefully you) asked to set a new password for <b>{_e(a['email'])}</b>{f' (username <b>{_e(uname)}</b>)' if uname else ''}.</p>
<p style="margin:22px 0"><a href="{_e(link)}" style="background:{color};color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:bold">Set a new password</a></p>
<p style="font-size:12px;color:#64748b">The link works for 2 hours and only once. If you did not ask for it, ignore this email: your password stays as it is.</p></div>"""
    kw = {}
    if cp:
        from . import cp_email
        kw = {"sender": cp_email.CP_FROM, "reply_to": cp_email.CP_REPLY_TO}
    try:
        emailer.send(a["email"], f"Set a new password for {name}", html_body, **kw)
    except Exception as ex:
        log.warning("reset email to %s failed: %s", a["email"], ex)
        return 500, {"error": "We could not send the email right now, please try again."}
    return 200, ok


def reset(body: dict) -> tuple[int, dict]:
    email = _reset_email(str(body.get("t") or ""))
    if not email:
        return 400, {"error": "This link has expired or was already used. Ask for a new one."}
    pw = str(body.get("password") or "")
    err = _pw_problem(pw)
    if err:
        return 400, {"error": err}
    a = _by(email=email) or {}
    vals = {"password_hash": hash_pw(pw)}
    if not a.get("confirmed_at"):
        vals["confirmed_at"] = datetime.now(timezone.utc).isoformat()   # the email link proves the address
    if not a.get("username"):
        vals["username"] = _free_username(email.split("@")[0])
    if not a.get("signup_method"):
        vals["signup_method"] = "email-link"
    _set(email, **vals)
    _fails.pop(email, None)
    _touch(email)
    return 200, _result(email, _site(body.get("site")), str(body.get("return") or ""))


def set_password(email: str, body: dict) -> tuple[int, dict]:
    """Signed-in reader sets or changes the password (current one required if there is one)."""
    a = _by(email=email)
    if not a:
        return 401, {"error": "Please sign in again."}
    if a.get("password_hash") and not check_pw(str(body.get("current") or ""), a.get("password_hash")):
        return 401, {"error": "Your current password is not right."}
    pw = str(body.get("password") or "")
    err = _pw_problem(pw)
    if err:
        return 400, {"error": err}
    vals = {"password_hash": hash_pw(pw)}
    u = str(body.get("username") or "").strip().lower()
    if u and u != (a.get("username") or ""):
        err = _username_problem(u)
        if err:
            return 400, {"error": err}
        vals["username"] = u
    elif not a.get("username"):
        vals["username"] = _free_username(email.split("@")[0])
    _set(email, **vals)
    return 200, {"ok": True, "username": vals.get("username") or a.get("username")}


# --- Google ----------------------------------------------------------------------------------------------------

def google_on() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _redirect_uri() -> str:
    return f"{gate._base()}/reader/google/callback"


def google_start_url(site: str, ret: str) -> str:
    return f"{gate._base()}/reader/google/start?site={_site(site)}&return={quote(ret, safe='')}"


def _google_account(info: dict, site: str) -> str:
    email = str(info.get("email") or "").strip().lower()
    sub = str(info.get("sub") or "")
    a = _by(google_sub=sub) or _by(email=email)
    now = datetime.now(timezone.utc).isoformat()
    if a:
        vals = {"google_sub": sub}
        if not a.get("confirmed_at"):
            vals["confirmed_at"] = now
        if not a.get("username"):
            vals["username"] = _free_username(email.split("@")[0])
        if not a.get("name") and info.get("name"):
            vals["name"] = str(info["name"])[:200]
        if not a.get("signup_method"):
            vals["signup_method"] = "google" if not a.get("confirmed_at") else "email-link"
        _set(a["email"], **vals)
        email = a["email"]
    else:
        _set(email, google_sub=sub, confirmed_at=now, username=_free_username(email.split("@")[0]),
             name=str(info.get("name") or "")[:200] or None, signup_site=site, signup_method="google")
        try:
            gate._store({"email": email, "site": site, "name": str(info.get("name") or "")[:200], "company": "", "role": "",
                         "source": "google", "newsletter": False})
        except Exception:
            pass
    _touch(email)
    return email


@router.get("/reader/google/start", include_in_schema=False)
def google_start(request: Request, site: str = "", ret: str = ""):
    site = _site(site)
    ret = gate._safe_return(request.query_params.get("return") or ret, _home(site) + "/")
    if not google_on():
        return HTMLResponse(gate._page("Google sign-in unavailable", "<p>Google sign-in is not switched on yet. Please create an "
                                       "account with a username and password instead.</p>"), status_code=503)
    nonce = secrets.token_urlsafe(16)
    state = gate._tok("g", site, ret, nonce)
    q = urlencode({"client_id": settings.google_client_id, "redirect_uri": _redirect_uri(), "response_type": "code",
                   "scope": "openid email profile", "state": state, "prompt": "select_account"})
    resp = RedirectResponse(f"{GOOGLE['authorize']}?{q}", status_code=303)
    resp.set_cookie("vg_gstate", nonce, max_age=STATE_MAX_AGE, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return resp


@router.get("/reader/google/callback", include_in_schema=False)
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    d = gate._untok(state, "g", STATE_MAX_AGE)
    if error or not code or not d or len(d) < 3 or not hmac.compare_digest(str(d[2]), request.cookies.get("vg_gstate") or ""):
        return HTMLResponse(gate._page("Google sign-in cancelled", "<p>Google sign-in did not complete. Go back and try again, "
                                       "or sign in with your username and password.</p>"), status_code=400)
    site, ret = _site(d[0]), str(d[1])
    try:
        import httpx
        with httpx.Client(timeout=20) as c:
            tok = c.post(GOOGLE["token"], data={"client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                                "code": code, "grant_type": "authorization_code", "redirect_uri": _redirect_uri()},
                         headers={"Accept": "application/json"})
            tok.raise_for_status()
            info = c.get(GOOGLE["userinfo"], headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
            info.raise_for_status()
            info = info.json()
    except Exception as ex:
        log.warning("google sign-in failed: %s", ex)
        return HTMLResponse(gate._page("Google sign-in failed", "<p>We could not reach Google just now. Please try again.</p>"), status_code=502)
    if not info.get("email") or not info.get("email_verified"):
        return HTMLResponse(gate._page("Email not verified", "<p>Your Google account's email address is not verified, so we cannot "
                                       "use it. Create an account with a username and password instead.</p>"), status_code=400)
    email = _google_account(info, site)
    r = _result(email, site, ret)
    if site == "compepharma":
        hand = gate._tok("h", email)
        resp = RedirectResponse(f"{gate.CP}/reader/sso?t={quote(hand, safe='')}&return={quote(ret, safe='')}", status_code=303)
    else:
        resp = gate._set_cookie(RedirectResponse(r["next"], status_code=303), request, email)
    resp.delete_cookie("vg_gstate")
    return resp


# --- JSON API (CompePharma calls these server-to-server; same bodies as the browser routes) --------------

async def _body(request: Request) -> dict:
    return await gate._json(request)


def _j(res: tuple[int, dict]) -> JSONResponse:
    return JSONResponse(res[1], status_code=res[0])


@router.get("/api/readers/config", include_in_schema=False)
def api_config():
    return {"google": google_on(), "google_start": f"{gate._base()}/reader/google/start"}


@router.post("/api/readers/register", include_in_schema=False)
async def api_register(request: Request):
    return _j(register(await _body(request)))


@router.post("/api/readers/login", include_in_schema=False)
async def api_login(request: Request):
    return _j(login(await _body(request)))


@router.post("/api/readers/forgot", include_in_schema=False)
async def api_forgot(request: Request):
    return _j(forgot(await _body(request)))


@router.post("/api/readers/reset", include_in_schema=False)
async def api_reset(request: Request):
    return _j(reset(await _body(request)))


@router.post("/api/readers/handoff", include_in_schema=False)
async def api_handoff(request: Request):
    b = await _body(request)
    d = gate._untok(str(b.get("t") or ""), "h", HANDOFF_MAX_AGE)
    if not d:
        return JSONResponse({"error": "invalid or expired"}, status_code=400)
    return _result(d[0], "compepharma", str(b.get("return") or ""))


@router.post("/api/readers/password", include_in_schema=False)
async def api_password(request: Request):
    b = await _body(request)
    email = gate.email_from_access(str(b.get("access") or ""))
    if not email:
        return JSONResponse({"error": "Please sign in again."}, status_code=401)
    return _j(set_password(email, b))


# --- browser routes on vigi-eye.com ----------------------------------------------------------------------------

def _browser(request: Request, res: tuple[int, dict]) -> JSONResponse:
    code, data = res
    out = {k: v for k, v in data.items() if k not in ("access", "link")}
    resp = JSONResponse(out, status_code=code)
    if code == 200 and data.get("access"):
        gate._set_cookie(resp, request, data["email"])
    return resp


@router.post("/reader/register", include_in_schema=False)
async def reader_register(request: Request):
    return _browser(request, register({**(await _body(request)), "site": gate.SITE}))


@router.post("/reader/password-login", include_in_schema=False)
async def reader_login_post(request: Request):
    return _browser(request, login({**(await _body(request)), "site": gate.SITE}))


@router.post("/reader/forgot", include_in_schema=False)
async def reader_forgot(request: Request):
    return _j(forgot({**(await _body(request)), "site": gate.SITE}))


@router.post("/reader/reset", include_in_schema=False)
async def reader_reset_post(request: Request):
    return _browser(request, reset({**(await _body(request)), "site": gate.SITE}))


@router.post("/reader/password", include_in_schema=False)
async def reader_password(request: Request):
    email = gate.reader(request)
    if not email:
        return JSONResponse({"error": "Please sign in again."}, status_code=401)
    return _j(set_password(email, await _body(request)))


@router.get("/reader/reset", include_in_schema=False)
def reader_reset_page(request: Request, t: str = ""):
    from . import portal_pages as _pp
    ok = bool(_reset_email(t))
    html = f'<main style="max-width:520px;margin:30px auto;padding:0 16px"><h1 style="font-size:30px">Set a new password</h1>' + (
        reset_form(t) if ok else '<p>This link has expired or was already used. <a href="/reader/login">Ask for a new one</a>.</p>') + '</main>'
    return _pp._page("Set a new password | VigiEye", "Set a new password for your VigiEye account.", "/reader/reset", html)


# --- page fragments (shared look; CompePharma has a copy in its gate.py) --------------------------------------

AUTH_CSS = """.rg form[hidden]{display:none!important}.rg .tabs{display:flex;gap:0;margin:4px 0 14px;border-bottom:1px solid rgba(100,116,139,.35)}
.rg .tabs button{flex:1;background:none!important;color:#475569!important;-webkit-text-fill-color:#475569;border:0!important;
border-bottom:3px solid transparent!important;border-radius:0!important;padding:10px 6px!important;font-size:15px!important}
.rg .tabs button.on{color:#0f172a!important;-webkit-text-fill-color:#0f172a;border-bottom-color:var(--rg-accent,#0f766e)!important}
.rg a.rg-google{display:flex!important;align-items:center;justify-content:center;gap:10px;background:#fff!important;color:#1f2937!important;
-webkit-text-fill-color:#1f2937;border:1px solid #cbd5e1!important;border-radius:10px;padding:11px 16px;font-weight:700;text-decoration:none!important;font-size:15.5px}
.rg .or{display:flex;align-items:center;gap:10px;color:#64748b;font-size:13px;margin:12px 0}.rg .or:before,.rg .or:after{content:"";flex:1;height:1px;background:rgba(100,116,139,.35)}
.rg .lnk{background:none!important;border:0!important;padding:0!important;color:var(--rg-accent,#0f766e)!important;-webkit-text-fill-color:var(--rg-accent,#0f766e);
font-weight:600!important;font-size:13.5px!important;text-decoration:underline!important;display:inline!important;cursor:pointer}
.rg .hint{font-size:12.5px;color:#64748b!important;margin:-6px 0 0}"""

G_ICON = ('<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.4 30.2 0 24 0 '
          '14.6 0 6.6 5.4 2.6 13.3l7.8 6.1C12.3 13.6 17.7 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9h12.4c-.5 2.9-2.2 '
          '5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17z"/><path fill="#FBBC05" d="M10.4 28.6c-.5-1.4-.8-3-.8-4.6s.3-3.2.8-4.6l-7.8-6.1C1 16.6 0 20.2 0 24s1 '
          '7.4 2.6 10.7l7.8-6.1z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.3-8.5 2.3-6.3 0-11.7-4.1-13.6-9.9l-7.8 '
          '6.1C6.6 42.6 14.6 48 24 48z"/></svg>')


def auth_box(return_url: str, source: str = "article", heading: str = "Subscribe to keep reading", fade: bool = True,
             site_name: str = gate.SITE_NAME, accent: str = "#0f766e", google_url: str | None = None, prefix: str = "/reader",
             login_path: str = "/reader/password-login", start: str = "register", show_price: bool = True) -> str:
    """Create account / Sign in / Forgot password card, with Continue with Google when google_url is given."""
    g = (f'<a class="rg-google" href="{_e(google_url)}">{G_ICON}<span>Continue with Google</span></a><div class="or">or</div>'
         if google_url else "")
    price = (f'<p><span class="price">{_e(gate.PRICE_LABEL)}</span> / month</p>{gate._BENEFITS}' if show_price else "")
    nl = f"Also send me the free daily {site_name} email (unsubscribe any time)"
    return f"""<style>{gate._CSS}{AUTH_CSS}</style><div class="rg" style="--rg-accent:{accent}">{'<div class="fade"></div>' if fade else ''}<div class="card">
<h3>{_e(heading)}</h3>{price}
<div class="tabs" role="tablist"><button type="button" data-t="register">Create account</button><button type="button" data-t="login">Sign in</button></div>
{g}
<form data-mode="register" onsubmit="rgAuth(this);return false;" data-src="{_e(source)}" data-ret="{_e(return_url)}">
<input type="email" name="email" placeholder="Email" required autocomplete="email">
<input type="text" name="username" placeholder="Username" required autocomplete="username" minlength="3" maxlength="30" pattern="[A-Za-z0-9][A-Za-z0-9._-]{{2,29}}">
<input type="password" name="password" placeholder="Password (at least 8 characters)" required autocomplete="new-password" minlength="8">
<label class="ck"><input type="checkbox" name="newsletter" value="1"> {_e(nl)}</label>
<button type="submit">Create account</button><div class="msg" aria-live="polite"></div>
<div class="fine">You'll be signed in straight away; we also email you a link to confirm your address. Reading every article takes a
{_e(gate.PRICE_LABEL)}/month subscription (secure checkout by Stripe), billed monthly until you cancel; access starts at once, so the
14-day withdrawal right ends when it does. See our <a href="/terms">terms</a> and <a href="/privacy">privacy policy</a>.</div>
</form>
<form data-mode="login" onsubmit="rgAuth(this);return false;" data-src="{_e(source)}" data-ret="{_e(return_url)}" hidden>
<input type="text" name="login" placeholder="Email or username" required autocomplete="username">
<input type="password" name="password" placeholder="Password" required autocomplete="current-password">
<button type="submit">Sign in</button><div class="msg" aria-live="polite"></div>
<p class="fine" style="margin:0"><button type="button" class="lnk" onclick="rgForgot(this)">Forgot password?</button> · Signed up before with
just your email? Use “Forgot password?” once to set a password.</p>
</form>
<form data-mode="forgot" onsubmit="rgAuth(this);return false;" hidden>
<p class="fine" style="margin:0">Enter your email or username and we'll email you a link to set a new password.</p>
<input type="text" name="email" placeholder="Email or username" required autocomplete="username">
<button type="submit">Email me a link</button><div class="msg" aria-live="polite"></div>
<p class="fine" style="margin:0"><button type="button" class="lnk" onclick="rgTab(this,'login')">Back to sign in</button></p>
</form></div></div>
<script>(function(){{const s=document.currentScript.previousElementSibling;
s.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>rgTab(b,b.dataset.t));rgTab(s.querySelector('.tabs button'),'{start}');}})();
function rgTab(el,t){{const c=el.closest('.card');c.querySelectorAll('form').forEach(f=>f.hidden=f.dataset.mode!==t);
c.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t||(t==='forgot'&&b.dataset.t==='login')));}}
function rgForgot(el){{rgTab(el,'forgot');}}
async function rgAuth(f){{const m=f.querySelector('.msg');m.textContent='…';const mode=f.dataset.mode;
const d=Object.fromEntries(new FormData(f).entries());d.newsletter=!!d.newsletter;d.source=f.dataset.src||'article';d.return=f.dataset.ret||location.href;
const url={{register:'{prefix}/register',login:'{login_path}',forgot:'{prefix}/forgot'}}[mode];
try{{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d),credentials:'same-origin'}});
const j=await r.json();if(!r.ok){{m.textContent=j.error||'That did not work.';return false;}}
if(mode==='forgot'){{m.textContent=j.message||'Check your inbox.';return false;}}
if(j.notice)alert(j.notice);location.href=j.next||d.return;}}catch(e){{m.textContent='Something went wrong, please try again.';}}return false;}}</script>"""


def reset_form(t: str, prefix: str = "/reader", accent: str = "#0f766e") -> str:
    return f"""<style>{gate._CSS}</style><div class="rg" style="--rg-accent:{accent}"><div class="card">
<form onsubmit="rgReset(this);return false;" data-t="{_e(t)}">
<input type="password" name="password" placeholder="New password (at least 8 characters)" required minlength="8" autocomplete="new-password">
<input type="password" name="password2" placeholder="New password again" required minlength="8" autocomplete="new-password">
<button type="submit">Save password and sign in</button><div class="msg" aria-live="polite"></div></form></div></div>
<script>async function rgReset(f){{const m=f.querySelector('.msg');const d=Object.fromEntries(new FormData(f).entries());
if(d.password!==d.password2){{m.textContent='The two passwords are not the same.';return false;}}m.textContent='…';
try{{const r=await fetch('{prefix}/reset',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{t:f.dataset.t,password:d.password}}),credentials:'same-origin'}});
const j=await r.json();if(!r.ok){{m.textContent=j.error||'That did not work.';return false;}}location.href=j.next||'/';}}
catch(e){{m.textContent='Something went wrong, please try again.';}}return false;}}</script>"""


def password_form(has_password: bool, username: str, prefix: str = "/reader") -> str:
    cur = ('<input type="password" name="current" placeholder="Current password" required autocomplete="current-password">'
           if has_password else "")
    return f"""<form onsubmit="rgPw(this);return false;" style="margin-top:14px">
<h3 style="font-size:17px">{'Change password' if has_password else 'Set a password'}</h3>
<p class="fine" style="margin:0">Username: <b>{_e(username or '—')}</b>. You can sign in with it or with your email.</p>
<input type="text" name="username" placeholder="Username (optional change)" value="{_e(username)}" autocomplete="username">
{cur}<input type="password" name="password" placeholder="New password (at least 8 characters)" required minlength="8" autocomplete="new-password">
<button type="submit">Save</button><div class="msg" aria-live="polite"></div></form>
<script>async function rgPw(f){{const m=f.querySelector('.msg');m.textContent='…';const d=Object.fromEntries(new FormData(f).entries());
try{{const r=await fetch('{prefix}/password',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d),credentials:'same-origin'}});
const j=await r.json();m.textContent=r.ok?'Saved.':(j.error||'That did not work.');if(r.ok)f.reset();}}catch(e){{m.textContent='Something went wrong.';}}return false;}}</script>"""


def account_extra(email: str) -> str:
    a = _by(email=email) or {}
    return password_form(bool(a.get("password_hash")), a.get("username") or "")


# --- subscriber statistics -------------------------------------------------------------------------------------

def stats() -> dict:
    """Counts for the daily statistics email (no personal data)."""
    from sqlalchemy import func, select
    t, eng = _table()
    now = int(time.time())
    day = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
    week = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).isoformat()
    out: dict = {}
    with eng.connect() as c:
        rows = c.execute(select(t.c.email, t.c.created_at, t.c.confirmed_at, t.c.password_hash, t.c.google_sub,
                                t.c.signup_site, t.c.signup_method, t.c.sub_status, t.c.paid_until, t.c.sub_id,
                                t.c.last_login_at)).mappings().all()
    acct = [r for r in rows if r["password_hash"] or r["google_sub"] or r["confirmed_at"]]
    paid = [r for r in rows if r["sub_status"] in ("active", "trialing") and (r["paid_until"] or 0) > now]
    out["accounts"] = len(acct)
    out["accounts_new_24h"] = sum(1 for r in acct if (r["created_at"] or "") >= day)
    out["accounts_new_7d"] = sum(1 for r in acct if (r["created_at"] or "") >= week)
    out["with_password"] = sum(1 for r in acct if r["password_hash"])
    out["with_google"] = sum(1 for r in acct if r["google_sub"])
    out["email_link_only"] = sum(1 for r in acct if not r["password_hash"] and not r["google_sub"])
    out["unconfirmed_emails"] = sum(1 for r in rows if not (r["password_hash"] or r["google_sub"] or r["confirmed_at"]))
    out["by_site"] = {s: sum(1 for r in acct if (r["signup_site"] or "vigieye") == s) for s in ("vigieye", "compepharma")}
    out["signed_in_24h"] = sum(1 for r in rows if (r["last_login_at"] or 0) >= now - 86400)
    out["paying"] = len(paid)
    out["paying_by_site"] = {s: sum(1 for r in paid if (r["signup_site"] or "vigieye") == s) for s in ("vigieye", "compepharma")}
    out["mrr_usd"] = round(len(paid) * gate.PRICE_CENTS / 100, 2)
    out["cancelled_or_past_due"] = sum(1 for r in rows if r["sub_id"] and r not in paid)
    try:
        from . import cp_email, store
        store.init()
        out["newsletter_vigieye_daily"] = len(store.subscribers_active("daily"))
        out["newsletter_vigieye_all"] = len(store.subscribers_active())
        out["newsletter_compepharma"] = len(cp_email.cp_active())
    except Exception as ex:
        out["newsletter_error"] = str(ex)[:100]
    return out
