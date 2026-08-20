"""
VigiEye — EU pharmacovigilance & regulatory monitoring.

MVP surface: serves the marketing landing page and the monitoring workspace,
captures early-access leads, exposes the updates feed as JSON, and provides a
Stripe checkout stub that activates automatically once billing keys are set.

Run:  uvicorn app.main:app --reload   →   http://127.0.0.1:8000
"""
import logging
import os

from fastapi import FastAPI, Request, Header, HTTPException, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .data import UPDATES, COUNTRIES, SUBJECTS
from . import (store, ingest as ingest_mod, digest as digest_mod, emailer,
               prices as prices_mod, auth as auth_mod, billing as billing_mod)

log = logging.getLogger("vigiradar")

app = FastAPI(title="VigiEye", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_BASE = os.path.dirname(__file__)
_STATIC = os.path.join(_BASE, "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

# in-memory lead store for the MVP (swap for a DB when persistence is needed)
_LEADS: list[dict] = []


class LeadIn(BaseModel):
    email: str
    source: str = "landing"


class CheckoutIn(BaseModel):
    sku: str
    email: str | None = None


@app.get("/", include_in_schema=False)
def landing():
    return FileResponse(os.path.join(_STATIC, "landing.html"))


@app.get("/app", include_in_schema=False)
def workspace():
    return FileResponse(os.path.join(_STATIC, "app.html"))


@app.get("/health")
def health():
    return {"status": "ok", "billing_enabled": settings.billing_enabled,
            "ai_enabled": bool(settings.anthropic_api_key)}


@app.get("/api/meta")
def meta():
    return {"countries": COUNTRIES, "subjects": SUBJECTS}


def _current_updates(country=None, subject=None, impact=None) -> tuple[list[dict], str]:
    """Live rows from the store once ingested; curated sample otherwise."""
    try:
        store.init()
        if store.count() > 0:
            return store.query(country=country, subject=subject, impact=impact), "live"
    except Exception:
        pass
    rows = UPDATES
    if country:
        rows = [r for r in rows if r["country"] == country]
    if subject:
        rows = [r for r in rows if r["subject"] == subject]
    if impact:
        rows = [r for r in rows if r["impact"] == impact]
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    return rows, "sample"


@app.get("/api/updates")
def updates(country: str | None = None, subject: str | None = None, impact: str | None = None):
    rows, src = _current_updates(country, subject, impact)
    return {"count": len(rows), "updates": rows, "source": src}


@app.post("/admin/ingest")
def admin_ingest(per_source: int = 15, x_admin_token: str = Header(default="")):
    """Trigger an ingestion pass. Protected by ADMIN_TOKEN (set on Render)."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    return ingest_mod.run_ingest(per_source=per_source)


@app.post("/admin/prices/ingest")
def admin_prices_ingest(x_admin_token: str = Header(default="")):
    """Full-refresh the national price/reimbursement datasets. Token-protected."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    store.init()
    stats = {"sources": 0, "by_country": {}, "errors": [], "total": 0}
    for src in prices_mod.PRICE_SOURCES:
        stats["sources"] += 1
        try:
            rows = src.fetch()
            n = store.prices_replace_country(src.country, rows)
            stats["by_country"][src.country] = n
            stats["total"] += n
        except Exception as e:
            stats["errors"].append({"source": src.id, "error": str(e)})
    stats["total_in_store"] = store.prices_count()
    return stats


@app.get("/api/prices")
def api_prices(country: str | None = None, search: str | None = None, limit: int = 200):
    """Filterable price/reimbursement rows. Returns [] until an ingest has run."""
    try:
        store.init()
        rows = store.prices_query(country=country, search=search, limit=min(limit, 500))
        return {"count": len(rows), "total": store.prices_count(country),
                "countries": store.prices_countries(), "prices": rows}
    except Exception as e:
        return {"count": 0, "total": 0, "countries": [], "prices": [], "error": str(e)}


@app.get("/admin/probe")
def admin_probe(url: str, x_admin_token: str = Header(default="")):
    """Dev aid: fetch a candidate price-source URL from Render (which has open
    egress) and return its head, so the parser can be written against the real
    file structure. Token-protected; https-only; temporary."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    if not url.startswith("https://"):
        return {"error": "https only"}
    import httpx
    ua = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1")}
    try:
        with httpx.Client(timeout=60, headers=ua, follow_redirects=True) as c:
            r = c.get(url)
        body = r.content[:4000]
        try:
            head = body.decode("utf-8")
        except UnicodeDecodeError:
            head = body.decode("latin-1", "replace")
        return {"status": r.status_code, "content_type": r.headers.get("content-type", ""),
                "final_url": str(r.url), "length": len(r.content), "head": head}
    except Exception as e:
        return {"error": str(e)}


@app.get("/admin/probe_table")
def admin_probe_table(url: str, kind: str = "", sheet: int = 0, rows: int = 15,
                      x_admin_token: str = Header(default="")):
    """Dev aid: download a candidate xlsx/xls/pdf/html price file from Render and
    return its structure (sheet names + first rows, or PDF text/tables) so a
    parser can be written against the real layout. Token-protected; temporary."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    import io
    import httpx
    ua = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1")}
    try:
        with httpx.Client(timeout=120, headers=ua, follow_redirects=True) as c:
            r = c.get(url)
        content = r.content
        ct = r.headers.get("content-type", "")
        k = (kind or "").lower()
        if not k:
            low = str(r.url).lower()
            if low.endswith(".xlsx") or "openxml" in ct:
                k = "xlsx"
            elif low.endswith(".xls") or "ms-excel" in ct:
                k = "xls"
            elif low.endswith(".pdf") or "pdf" in ct:
                k = "pdf"
            elif content[:2] == b"PK":
                k = "xlsx"
            elif content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                k = "xls"
            elif content[:4] == b"%PDF":
                k = "pdf"
            else:
                k = "html"
        # real-BIFF .xls (magic D0CF) can't be read without xlrd; many gov ".xls"
        # are actually HTML tables, so treat those as html.
        if k == "xls" and content[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            k = "html"
        out = {"status": r.status_code, "content_type": ct, "final_url": str(r.url),
               "length": len(content), "kind": k}
        if k == "xlsx":
            grid = prices_mod.read_xlsx(content, sheet, rows)
            out["preview"] = [[c[:45] for c in row] for row in grid]
        elif k == "xls":
            out["note"] = "binary BIFF .xls — needs conversion; no reader installed"
            out["head"] = content[:64].hex()
        elif k == "pdf":
            out["note"] = "pdf — table extraction disabled (no pdf lib installed)"
            out["pages_hint"] = content.count(b"/Type/Page")
        else:  # html or unknown text — surface tables via BeautifulSoup
            from bs4 import BeautifulSoup
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1", "replace")
            soup = BeautifulSoup(text, "html.parser")
            tables = soup.find_all("table")
            out["tables"] = len(tables)
            if tables:
                trs = tables[0].find_all("tr")[:rows]
                out["preview"] = [[" ".join(td.get_text(" ", strip=True).split())[:45]
                                   for td in tr.find_all(["td", "th"])] for tr in trs]
            else:
                out["head"] = text[:3500]
        return out
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}


@app.get("/digest/preview", response_class=HTMLResponse)
def digest_preview(country: str | None = None, subject: str | None = None):
    """Render the weekly digest email from the current feed (live or sample)."""
    rows, _ = _current_updates(country, subject, None)
    scope = f"{country or 'EU'} · {subject or 'all subjects'}"
    return HTMLResponse(digest_mod.render_html(rows, scope_label=scope,
                                               app_url=settings.app_base_url))


@app.post("/admin/digest/send")
def admin_digest_send(to: str, country: str | None = None, subject: str | None = None,
                      x_admin_token: str = Header(default="")):
    """Send the digest to one address. Protected by ADMIN_TOKEN."""
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    rows, src = _current_updates(country, subject, None)
    html = digest_mod.render_html(rows, scope_label=f"{country or 'EU'} · {subject or 'all subjects'}",
                                  app_url=settings.app_base_url)
    result = emailer.send(to, "Your VigiEye digest — this week", html)
    return {"sent_to": to, "items": len(rows), "high_impact": len(digest_mod.high_impact(rows)),
            "data_source": src, "email": result}


@app.post("/leads")
def capture_lead(body: LeadIn):
    if not any(l["email"] == body.email for l in _LEADS):
        _LEADS.append({"email": body.email, "source": body.source})
        log.info("[lead] %s (%s)", body.email, body.source)
    return {"status": "ok", "email": body.email}


# --- accounts / SSO ---------------------------------------------------------

def _current_user(session: str | None) -> dict | None:
    data = auth_mod.read_session(session)
    if not data:
        return None
    try:
        store.init()
        user = store.user_get(data["uid"])
    except Exception:
        user = None
    # fall back to the cookie claims if the DB is briefly unavailable
    return user or {"id": data["uid"], "email": data.get("email", ""),
                    "name": data.get("name", ""), "plan": "", "plan_status": ""}


@app.get("/auth/providers")
def auth_providers():
    return {"google": settings.google_enabled, "microsoft": settings.microsoft_enabled,
            "auth_enabled": settings.auth_enabled}


@app.get("/api/me")
def api_me(session: str | None = Cookie(default=None, alias=auth_mod.COOKIE_NAME)):
    user = _current_user(session)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": {
        "email": user.get("email", ""), "name": user.get("name", ""),
        "plan": user.get("plan", ""), "plan_status": user.get("plan_status", "")}}


@app.get("/auth/login/{provider}")
def auth_login(provider: str):
    if not auth_mod.provider_ok(provider):
        raise HTTPException(status_code=404, detail="Unknown or unconfigured provider.")
    state = auth_mod.make_state(provider)
    return RedirectResponse(auth_mod.authorize_url(provider, state), status_code=302)


@app.get("/auth/callback/{provider}")
def auth_callback(provider: str, code: str = "", state: str = "", error: str = ""):
    base = settings.app_base_url.rstrip("/")
    if error or not code or not auth_mod.check_state(provider, state):
        return RedirectResponse(f"{base}/app?login=failed", status_code=302)
    try:
        tokens = auth_mod.exchange_code(provider, code)
        info = auth_mod.fetch_userinfo(provider, tokens.get("access_token", ""))
        if not info.get("email") and tokens.get("id_token"):
            info = auth_mod.decode_id_token_claims(tokens["id_token"])
        if not info.get("email"):
            raise ValueError("no email from provider")
        store.init()
        user = store.user_upsert_oauth(provider, info["sub"], info["email"], info["name"])
    except Exception as e:
        log.warning("[auth] %s callback failed: %s", provider, e)
        return RedirectResponse(f"{base}/app?login=failed", status_code=302)
    resp = RedirectResponse(f"{base}/app?login=ok", status_code=302)
    resp.set_cookie(auth_mod.COOKIE_NAME, auth_mod.make_session(user),
                    max_age=settings.session_ttl_days * 86400, httponly=True,
                    secure=base.startswith("https"), samesite="lax")
    return resp


@app.get("/auth/logout")
def auth_logout():
    resp = RedirectResponse(f"{settings.app_base_url.rstrip('/')}/app", status_code=302)
    resp.delete_cookie(auth_mod.COOKIE_NAME)
    return resp


# --- billing (Stripe) -------------------------------------------------------

@app.get("/billing/plans")
def billing_plans():
    return {"billing_enabled": settings.billing_enabled,
            "pro_monthly": bool(settings.stripe_price_pro_monthly),
            "team_annual": bool(settings.stripe_price_team_annual)}


@app.post("/billing/checkout")
def billing_checkout(body: CheckoutIn,
                     session: str | None = Cookie(default=None, alias=auth_mod.COOKIE_NAME)):
    if not settings.billing_enabled:
        return JSONResponse({"error": "Billing is not enabled yet."}, status_code=200)
    user = _current_user(session)
    email = (user or {}).get("email") or body.email
    if not email:
        return JSONResponse({"error": "Please sign in first."}, status_code=401)
    try:
        store.init()
        customer_id = billing_mod.ensure_customer(
            email, (user or {}).get("name", ""), (user or {}).get("stripe_customer_id", ""))
        if user and user.get("id") and not user.get("stripe_customer_id"):
            store.user_set_stripe_customer(user["id"], customer_id)
        url = billing_mod.create_checkout_session(body.sku, customer_id, settings.app_base_url)
        return {"url": url}
    except ValueError:
        return JSONResponse({"error": "Unknown plan."}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Checkout unavailable: {e}"}, status_code=200)


@app.post("/billing/portal")
def billing_portal(session: str | None = Cookie(default=None, alias=auth_mod.COOKIE_NAME)):
    user = _current_user(session)
    if not user or not user.get("stripe_customer_id"):
        return JSONResponse({"error": "No billing account yet."}, status_code=400)
    try:
        return {"url": billing_mod.create_portal_session(
            user["stripe_customer_id"], settings.app_base_url)}
    except Exception as e:
        return JSONResponse({"error": f"Portal unavailable: {e}"}, status_code=200)


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not billing_mod.verify_webhook(payload, sig):
        raise HTTPException(status_code=400, detail="Bad signature.")
    import json as _json
    try:
        event = _json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad payload.")
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    try:
        store.init()
        customer = obj.get("customer", "")
        if etype.startswith("customer.subscription."):
            status = obj.get("status", "")
            if etype.endswith("deleted"):
                status = "canceled"
            items = (obj.get("items", {}) or {}).get("data", [])
            price_id = items[0].get("price", {}).get("id", "") if items else ""
            store.user_set_plan(customer, billing_mod.plan_name_for_price(price_id), status)
        elif etype == "checkout.session.completed" and customer:
            store.user_set_plan(customer, "subscription", "active")
    except Exception as e:
        log.warning("[billing] webhook %s handling failed: %s", etype, e)
    return {"received": True}
