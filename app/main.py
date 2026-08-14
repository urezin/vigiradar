"""
VigiEye — EU pharmacovigilance & regulatory monitoring.

MVP surface: serves the marketing landing page and the monitoring workspace,
captures early-access leads, exposes the updates feed as JSON, and provides a
Stripe checkout stub that activates automatically once billing keys are set.

Run:  uvicorn app.main:app --reload   →   http://127.0.0.1:8000
"""
import logging
import os

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .data import UPDATES, COUNTRIES, SUBJECTS
from . import store, ingest as ingest_mod, digest as digest_mod, emailer, prices as prices_mod

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


@app.get("/billing/plans")
def billing_plans():
    return {"billing_enabled": settings.billing_enabled}


@app.post("/billing/checkout")
def billing_checkout(body: CheckoutIn):
    if not settings.billing_enabled:
        return JSONResponse({"error": "Billing is not enabled yet."}, status_code=200)
    price = {"pro_monthly": settings.stripe_price_pro_monthly,
             "team_annual": settings.stripe_price_team_annual}.get(body.sku, "")
    if not price:
        return JSONResponse({"error": "Unknown plan."}, status_code=400)
    try:
        import stripe
        stripe.api_key = settings.stripe_secret_key
        base = settings.app_base_url.rstrip("/")
        sess = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            success_url=f"{base}/app?checkout=success",
            cancel_url=f"{base}/#pricing",
            customer_email=body.email or None,
        )
        return {"url": sess.url}
    except Exception as e:  # keep the surface friendly if Stripe isn't wired yet
        return JSONResponse({"error": f"Checkout unavailable: {e}"}, status_code=200)
