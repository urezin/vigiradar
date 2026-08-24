"""
VigiEye Medical Literature Monitoring — a sellable add-on module.

A literature surveillance workbench for pharmacovigilance (GVP Module VI): log
references surfaced from scientific/medical literature (PubMed, Embase, journals),
screen each for adverse events and relevance to your products, record the review
decision, and escalate any valid individual case safety report — with a weekly
screening cadence and overdue flags, and an audit trail of who screened what.

Self-contained and additive: its own tables on the same database, its own router,
and its own entitlement gate — so it can be sold as a separate module. Activation
is granted when the account has an active paid plan, or (for evaluation) via a
self-serve activate call when ALLOW_SELF_GRANT is on.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone, date, timedelta

from fastapi import APIRouter, Cookie
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import (create_engine, MetaData, Table, Column, String, Text,
                        select, insert, update as sql_update)

from . import auth as auth_mod
from . import store as store_mod


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    path = os.getenv("VIGIRADAR_DB", os.path.join(os.path.dirname(__file__), "vigieye.db"))
    return f"sqlite:///{path}"


_engine = create_engine(_database_url(), future=True, pool_pre_ping=True)
_meta = MetaData()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


refs = Table(
    "lit_references", _meta,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False, index=True),
    Column("source", String(30)),         # PubMed | Embase | Journal | Conference | Manual
    Column("citation", String(120)),      # PMID / DOI / reference id
    Column("title", Text),
    Column("product", String(200)),
    Column("contains_ae", String(12)),    # Unknown | Yes | No
    Column("relevance", String(20)),      # Unassessed | Not relevant | Relevant
    Column("status", String(24)),         # Awaiting screening | Screened | Escalated to case
    Column("reviewer", String(200)),
    Column("received", String(10)),
    Column("due", String(10)),
    Column("notes", Text),
    Column("created_at", String(40), nullable=False),
)

grants = Table(
    "lit_grants", _meta,
    Column("user_id", String(32), primary_key=True),
    Column("source", String(20)),
    Column("created_at", String(40), nullable=False),
)

_inited = {"v": False}
SOURCES = ["PubMed", "Embase", "Journal", "Conference", "Manual"]
CONTAINS_AE = ["Unknown", "Yes", "No"]
RELEVANCE = ["Unassessed", "Not relevant", "Relevant"]
STATUSES = ["Awaiting screening", "Screened", "Escalated to case"]
SCREEN_DAYS = int(os.getenv("LIT_SCREEN_DAYS", "7"))   # weekly screening cadence


def _init():
    if not _inited["v"]:
        _meta.create_all(_engine)
        _inited["v"] = True


# ---------------------------------------------------------------------------
# Entitlement (mirrors the other add-on modules)
# ---------------------------------------------------------------------------
def _plan_active(user: dict) -> bool:
    return (user.get("plan") or "") not in ("", "free") and \
           (user.get("plan_status") or "") in ("active", "trialing")


def _has_grant(uid: str) -> bool:
    _init()
    with _engine.connect() as c:
        return c.execute(select(grants.c.user_id).where(grants.c.user_id == uid)).first() is not None


def _entitled(user: dict) -> bool:
    return bool(user) and (_plan_active(user) or _has_grant(user["id"]))


def _current(session):
    data = auth_mod.read_session(session)
    if not data:
        return None
    try:
        store_mod.init()
        u = store_mod.user_get(data["uid"])
    except Exception:
        u = None
    return u or {"id": data["uid"], "email": data.get("email", ""),
                 "name": data.get("name", ""), "plan": "", "plan_status": ""}


# ---------------------------------------------------------------------------
# Seed — a realistic starter screening worklist
# ---------------------------------------------------------------------------
def _seed_for(uid: str):
    _init()
    with _engine.connect() as c:
        if c.execute(select(refs.c.id).where(refs.c.user_id == uid)).first():
            return
    today = date.today()
    def d(days): return (today + timedelta(days=days)).isoformat()
    seed = [
        ("PubMed", "PMID 39218847", "A case of atorvastatin-associated rhabdomyolysis in an elderly patient",
         "Atorvastatin", "Yes", "Relevant", "Awaiting screening", "", d(-2)),
        ("Embase", "EMB-2026-11902", "Real-world safety of adalimumab: injection-site reactions in a cohort study",
         "Adalimumab", "Yes", "Unassessed", "Awaiting screening", "", d(-1)),
        ("PubMed", "PMID 39201233", "Metformin and gastrointestinal tolerability: a systematic review",
         "Metformin", "No", "Not relevant", "Screened", "Dr Reviewer", d(-6)),
        ("Journal", "Drug Saf 2026;49(8)", "Warfarin-related major bleeding: incidence and risk factors",
         "Warfarin", "Yes", "Relevant", "Escalated to case", "Dr Reviewer", d(-9)),
        ("PubMed", "PMID 39230114", "Amoxicillin-induced cutaneous reactions: a pharmacovigilance analysis",
         "Amoxicillin", "Unknown", "Unassessed", "Awaiting screening", "", d(0)),
    ]
    rows = []
    for (src, cit, title, prod, ae, rel, st, rev, rec) in seed:
        due = (date.fromisoformat(rec) + timedelta(days=SCREEN_DAYS)).isoformat()
        rows.append(dict(id=secrets.token_hex(12), user_id=uid, source=src, citation=cit, title=title,
                         product=prod, contains_ae=ae, relevance=rel, status=st, reviewer=rev,
                         received=rec, due=due, notes="", created_at=_now()))
    with _engine.begin() as c:
        c.execute(insert(refs), rows)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["literature-monitoring"])
COOKIE = auth_mod.COOKIE_NAME


def _ref_dict(r) -> dict:
    d = dict(r._mapping)
    today = date.today().isoformat()
    d["overdue"] = bool(d.get("due") and d["due"] < today and d.get("status") == "Awaiting screening")
    return d


@router.get("/api/lit/status")
def status(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return {"authenticated": False, "active": False}
    return {"authenticated": True, "active": _entitled(user),
            "plan_active": _plan_active(user),
            "self_grant": os.getenv("ALLOW_SELF_GRANT", "1") == "1",
            "name": user.get("name") or user.get("email", "")}


@router.post("/api/lit/activate")
def activate(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return JSONResponse({"error": "Please sign in."}, status_code=401)
    if _entitled(user):
        _seed_for(user["id"])
        return {"ok": True, "active": True}
    if os.getenv("ALLOW_SELF_GRANT", "1") != "1":
        return JSONResponse({"error": "The literature monitoring module isn’t part of your plan yet."}, status_code=402)
    _init()
    with _engine.begin() as c:
        c.execute(insert(grants).values(user_id=user["id"], source="self", created_at=_now()))
    _seed_for(user["id"])
    return {"ok": True, "active": True}


def _guard(session):
    user = _current(session)
    if not user:
        return None, JSONResponse({"error": "Please sign in."}, status_code=401)
    if not _entitled(user):
        return None, JSONResponse({"error": "Literature monitoring module not active.", "locked": True}, status_code=403)
    return user, None


@router.get("/api/lit/summary")
def summary(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    today = date.today().isoformat()
    with _engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            select(refs).where(refs.c.user_id == user["id"])).fetchall()]
    total = len(rows)
    awaiting = sum(1 for r in rows if r["status"] == "Awaiting screening")
    with_ae = sum(1 for r in rows if r["contains_ae"] == "Yes")
    escalated = sum(1 for r in rows if r["status"] == "Escalated to case")
    overdue = sum(1 for r in rows if r["due"] and r["due"] < today and r["status"] == "Awaiting screening")
    return {"total": total, "awaiting": awaiting, "with_ae": with_ae,
            "escalated": escalated, "overdue": overdue,
            "sources": SOURCES, "contains_ae": CONTAINS_AE, "relevance": RELEVANCE, "statuses": STATUSES}


@router.get("/api/lit/references")
def list_refs(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        rows = c.execute(select(refs).where(refs.c.user_id == user["id"])
                         .order_by(refs.c.due)).fetchall()
    return {"items": [_ref_dict(r) for r in rows]}


class RefIn(BaseModel):
    title: str
    source: str = "PubMed"
    citation: str = ""
    product: str = ""
    reviewer: str = ""
    received: str = ""


@router.post("/api/lit/references")
def add_ref(body: RefIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    if not body.title.strip():
        return JSONResponse({"error": "The article title is required."}, status_code=400)
    rec = body.received.strip() or date.today().isoformat()
    try:
        due = (date.fromisoformat(rec) + timedelta(days=SCREEN_DAYS)).isoformat()
    except Exception:
        rec = date.today().isoformat()
        due = (date.today() + timedelta(days=SCREEN_DAYS)).isoformat()
    rid = secrets.token_hex(12)
    _init()
    with _engine.begin() as c:
        c.execute(insert(refs).values(
            id=rid, user_id=user["id"],
            source=body.source if body.source in SOURCES else "PubMed",
            citation=body.citation.strip(), title=body.title.strip(), product=body.product.strip(),
            contains_ae="Unknown", relevance="Unassessed", status="Awaiting screening",
            reviewer=body.reviewer.strip(), received=rec, due=due, notes="", created_at=_now()))
    return {"ok": True, "id": rid, "due": due}


class FieldIn(BaseModel):
    field: str    # contains_ae | relevance | status | reviewer | source
    value: str


@router.post("/api/lit/references/{rid}/set")
def set_field(rid: str, body: FieldIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    allowed = {"contains_ae": CONTAINS_AE, "relevance": RELEVANCE, "status": STATUSES,
               "source": SOURCES, "reviewer": None}
    if body.field not in allowed:
        return JSONResponse({"error": "Unknown field."}, status_code=400)
    if allowed[body.field] is not None and body.value not in allowed[body.field]:
        return JSONResponse({"error": "Invalid value."}, status_code=400)
    _init()
    with _engine.begin() as c:
        res = c.execute(sql_update(refs).where(
            refs.c.id == rid, refs.c.user_id == user["id"]).values(**{body.field: body.value}))
    if not res.rowcount:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return {"ok": True}
