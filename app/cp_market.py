"""
CompePharma enterprise & market-analysis back end.

CompePharma's front end (compepharma.com) has no persistent store of its own, so its
enterprise and market-analysis enquiries, and the product catalogue behind the 5-year
market-analysis service, are served from here:

  GET  /api/cp-products         product catalogue (EU-authorised medicines + FDA approvals in
                                VigiEye's drug database): search, region, type, paging
  POST /cp/enquiry              an enterprise or market-analysis enquiry/order from CompePharma;
                                stored, and e-mailed to the owner (CP_ENQUIRY_TO / OWNER_EMAIL)
  GET  /admin/cp/enquiries      list enquiries (X-Admin-Token)
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, func, insert, or_, select

from . import portal_data as pd
from . import store
from .config import settings

router = APIRouter()
_meta = MetaData()
enquiries = Table(
    "cp_enquiries", _meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(40), nullable=False),       # enterprise | report-standard | report-custom | report-pack
    Column("name", String(200)), Column("email", String(320), nullable=False),
    Column("company", String(200)), Column("role", String(200)),
    Column("product", Text), Column("markets", Text), Column("message", Text),
    Column("source", String(80)), Column("created_at", String(40), nullable=False),
)

ENQUIRY_TO = (os.getenv("CP_ENQUIRY_TO") or os.getenv("OWNER_EMAIL") or "dgkikas@pqra.gr").strip()
KIND_LABEL = {"enterprise": "Enterprise enquiry", "report-standard": "Ready-to-go 5-year market analysis order",
              "report-custom": "Custom market analysis request", "report-pack": "Portfolio pack enquiry"}

def init() -> None:
    _meta.create_all(store._engine)
    pd.init()


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _flags(raw) -> list[str]:
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


# --- product catalogue -------------------------------------------------------------------

@router.get("/api/cp-products", include_in_schema=False)
def api_cp_products(q: str = "", region: str = "", type: str = "", limit: int = 50, offset: int = 0):
    """Products a 5-year market analysis can be ordered for: every EU-authorised medicine and
    FDA approval in VigiEye's drug database."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    init()
    d = pd.drugs
    conds = []
    if q.strip():
        like = f"%{q.strip().lower()[:80]}%"
        conds.append(or_(func.lower(d.c.name).like(like), func.lower(d.c.inn).like(like),
                         func.lower(d.c.company).like(like), func.lower(d.c.therapeutic_area).like(like),
                         func.lower(d.c.indication).like(like)))
    if region.upper() in ("EU", "US"):
        conds.append(d.c.region == region.upper())
    if type == "biosimilar":
        conds.append(func.lower(d.c.flags).like("%biosimilar%"))
    elif type == "orphan":
        conds.append(func.lower(d.c.flags).like("%orphan%"))
    elif type == "new":
        conds.append(or_(func.lower(d.c.flags).like("%new molecular%"), func.lower(d.c.flags).like("%new biologic%"),
                         func.lower(d.c.flags).like("%atmp%"), func.lower(d.c.flags).like("%prime%")))
    with store._engine.connect() as c:
        total = c.execute(select(func.count()).select_from(d).where(*conds)).scalar() or 0
        by_region = dict(c.execute(select(d.c.region, func.count()).group_by(d.c.region)).all())
        rows = [dict(r._mapping) for r in c.execute(
            select(d.c.id, d.c.region, d.c.slug, d.c.name, d.c.inn, d.c.company, d.c.status,
                   d.c.therapeutic_area, d.c.flags, d.c.decision_date, d.c.opinion_date)
            .where(*conds).order_by(d.c.decision_date.desc().nullslast(), d.c.name).limit(limit).offset(offset))]
    items = [{"id": r["id"], "region": r["region"], "slug": r["slug"], "name": r["name"], "inn": r.get("inn") or "",
              "company": r.get("company") or "", "status": r.get("status") or "",
              "therapeutic_area": (r.get("therapeutic_area") or "")[:160], "flags": _flags(r.get("flags")),
              "date": r.get("decision_date") or r.get("opinion_date") or ""} for r in rows]
    return {"total": total, "catalogue": {"EU": by_region.get("EU", 0), "US": by_region.get("US", 0),
                                          "all": sum(by_region.values())},
            "offset": offset, "limit": limit, "items": items}


# --- enquiries ---------------------------------------------------------------------------

class EnquiryIn(BaseModel):
    kind: str
    email: str
    name: str = ""
    company: str = ""
    role: str = ""
    product: str = ""
    markets: str = ""
    message: str = ""
    source: str = "compepharma"
    website: str = ""          # honeypot: real visitors leave it empty


@router.post("/cp/enquiry", include_in_schema=False)
def cp_enquiry(body: EnquiryIn):
    if body.website:
        return {"ok": True}
    email = (body.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 320:
        return JSONResponse({"error": "Enter a valid work email address."}, status_code=400)
    kind = body.kind if body.kind in KIND_LABEL else "enterprise"
    row = {"kind": kind, "email": email, "name": body.name.strip()[:200], "company": body.company.strip()[:200],
           "role": body.role.strip()[:200], "product": body.product.strip()[:2000], "markets": body.markets.strip()[:1000],
           "message": body.message.strip()[:6000], "source": (body.source or "compepharma")[:80],
           "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    init()
    with store._engine.begin() as c:
        rid = c.execute(insert(enquiries).values(**row)).inserted_primary_key[0]
    sent = "skipped"
    try:
        from . import emailer
        from .cp_email import CP_FROM
        fields = [("Type", KIND_LABEL[kind]), ("Name", row["name"]), ("Email", email), ("Company", row["company"]),
                  ("Role", row["role"]), ("Product(s)", row["product"]), ("Markets", row["markets"]),
                  ("Message", row["message"]), ("Page", row["source"]), ("Received (UTC)", row["created_at"])]
        trs = "".join(f'<tr><td style="padding:6px 12px 6px 0;color:#64748b;vertical-align:top;white-space:nowrap">{_e(k)}</td>'
                      f'<td style="padding:6px 0;white-space:pre-wrap">{_e(v)}</td></tr>' for k, v in fields if v)
        html_body = (f'<div style="font:14px/1.5 system-ui;max-width:640px"><h2 style="margin:0 0 12px">CompePharma: '
                     f'{_e(KIND_LABEL[kind])} #{rid}</h2><table style="border-collapse:collapse">{trs}</table>'
                     f'<p style="color:#64748b;margin-top:16px">Reply to this e-mail to answer {_e(email)} directly.</p></div>')
        subj = f"CompePharma {KIND_LABEL[kind]}: {row['company'] or email}"[:140]
        sent = emailer.send(ENQUIRY_TO, subj, html_body, sender=CP_FROM, reply_to=email).get("status")
    except Exception:
        sent = "error"
    return {"ok": True, "id": rid, "notified": sent}


@router.get("/admin/cp/enquiries", include_in_schema=False)
def admin_cp_enquiries(limit: int = 100, x_admin_token: str = Header(default="")):
    if not settings.admin_token or not hmac.compare_digest(x_admin_token or "", settings.admin_token):
        raise HTTPException(status_code=401, detail="Admin token required.")
    init()
    with store._engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(select(enquiries).order_by(enquiries.c.id.desc()).limit(min(limit, 500)))]
    return {"enquiries": rows}
