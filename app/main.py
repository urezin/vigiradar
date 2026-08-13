"""
VigiRadar — EU pharmacovigilance & regulatory monitoring.

MVP surface: serves the marketing landing page and the monitoring workspace,
captures early-access leads, exposes the updates feed as JSON, and provides a
Stripe checkout stub that activates automatically once billing keys are set.

Run:  uvicorn app.main:app --reload   →   http://127.0.0.1:8000
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import settings
from .data import UPDATES, COUNTRIES, SUBJECTS

log = logging.getLogger("vigiradar")

app = FastAPI(title="VigiRadar", version="0.1.0")
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


@app.get("/api/updates")
def updates(country: str | None = None, subject: str | None = None, impact: str | None = None):
    rows = UPDATES
    if country:
        rows = [r for r in rows if r["country"] == country]
    if subject:
        rows = [r for r in rows if r["subject"] == subject]
    if impact:
        rows = [r for r in rows if r["impact"] == impact]
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    return {"count": len(rows), "updates": rows}


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
