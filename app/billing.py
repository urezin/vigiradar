"""
Stripe billing — via the Stripe REST API over httpx (no `stripe` package).

Calling the REST API directly keeps billing dependency-free, so it never forces
a requirements.txt change (those were breaking the Render build). Webhook
signatures are verified with the standard-library hmac module.

Everything is inert until STRIPE_SECRET_KEY is set, so this is safe to ship
before Stripe is configured.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from .config import settings

API = "https://api.stripe.com/v1"

# Inline plan pricing, matching the public pricing page (amounts overridable via
# env). If a pre-created Stripe Price ID is configured for a plan it takes
# precedence; otherwise we build the price inline via price_data, so checkout
# works with just STRIPE_SECRET_KEY — no Stripe Products to pre-create.
PLANS = {
    "pro_monthly": {
        "name": "VigiEye Pro",
        "amount": int(os.getenv("VIGI_PRO_MONTHLY_CENTS", "24900")),    # €249 / user / month
        "currency": os.getenv("VIGI_CURRENCY", "eur"),
        "interval": "month",
        "trial_days": int(os.getenv("VIGI_PRO_TRIAL_DAYS", "14")),      # 14-day trial (as advertised)
        "price_id": lambda: settings.stripe_price_pro_monthly,
    },
    "team_annual": {
        "name": "VigiEye Team (annual)",
        "amount": int(os.getenv("VIGI_TEAM_ANNUAL_CENTS", "238800")),   # €199/user/mo billed annually = €2,388/yr
        "currency": os.getenv("VIGI_CURRENCY", "eur"),
        "interval": "year",
        "trial_days": int(os.getenv("VIGI_TEAM_TRIAL_DAYS", "0")),
        "price_id": lambda: settings.stripe_price_team_annual,
    },
}


def _post(path: str, data: dict) -> dict:
    import httpx

    with httpx.Client(timeout=30) as c:
        r = c.post(f"{API}{path}", data=data,
                   auth=(settings.stripe_secret_key, ""))
    if r.status_code >= 400:
        raise RuntimeError(f"stripe {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def ensure_customer(email: str, name: str = "", existing: str = "") -> str:
    """Return a Stripe customer id, creating one if needed."""
    if existing:
        return existing
    data = {"email": email}
    if name:
        data["name"] = name
    return _post("/customers", data)["id"]


def create_checkout_session(sku: str, customer_id: str, base_url: str) -> str:
    """Create a subscription Checkout Session and return its URL.

    Uses a pre-created Stripe Price if one is configured for the plan; otherwise
    builds the price inline with price_data so it works with just the secret key.
    """
    plan = PLANS.get(sku)
    if not plan:
        raise ValueError("unknown plan")
    base = base_url.rstrip("/")
    data = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{base}/app?checkout=success",
        "cancel_url": f"{base}/app?checkout=cancel",
        "allow_promotion_codes": "true",
    }
    pid = plan["price_id"]()
    if pid:
        data["line_items[0][price]"] = pid
    else:
        data["line_items[0][price_data][currency]"] = plan["currency"]
        data["line_items[0][price_data][unit_amount]"] = str(plan["amount"])
        data["line_items[0][price_data][recurring][interval]"] = plan["interval"]
        data["line_items[0][price_data][product_data][name]"] = plan["name"]
    if plan["trial_days"]:
        data["subscription_data[trial_period_days]"] = str(plan["trial_days"])
    return _post("/checkout/sessions", data)["url"]


def create_portal_session(customer_id: str, base_url: str) -> str:
    """Create a Billing Portal session so a customer can manage/cancel."""
    data = {"customer": customer_id, "return_url": f"{base_url.rstrip('/')}/app"}
    return _post("/billing_portal/sessions", data)["url"]


def verify_webhook(payload: bytes, sig_header: str) -> bool:
    """Verify a Stripe webhook signature (scheme v1) with hmac-SHA256.

    Header form: 't=<ts>,v1=<sig>,v1=<sig2>...'. We recompute HMAC over
    '<t>.<payload>' and constant-time compare against any provided v1."""
    secret = settings.stripe_webhook_secret
    if not secret or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts = parts.get("t", "")
    if not ts:
        return False
    # tolerate clock skew but reject very old timestamps (replay guard)
    try:
        if abs(time.time() - int(ts)) > 60 * 10:
            return False
    except ValueError:
        return False
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    provided = [v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p) if k == "v1"]
    return any(hmac.compare_digest(expected, p) for p in provided)


def plan_name_for_price(price_id: str) -> str:
    if price_id and price_id == settings.stripe_price_pro_monthly:
        return "pro_monthly"
    if price_id and price_id == settings.stripe_price_team_annual:
        return "team_annual"
    return "subscription"
