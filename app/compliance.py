"""
VigiEye Compliance — a sellable add-on module.

Turns the regulatory monitoring feed into action: a register of compliance
obligations (PSMF reviews, PSUR submissions, signal management, etc.), their
deadlines and owners, plus e-signature style attestations for sign-off and audit.

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
                        select, insert, update as sql_update, text)

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


obligations = Table(
    "compliance_obligations", _meta,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False, index=True),
    Column("title", Text, nullable=False),
    Column("framework", String(60)),      # GVP, GMP, ISO, etc.
    Column("authority", String(120)),
    Column("description", Text),
    Column("frequency", String(20)),       # once|monthly|quarterly|annual
    Column("due_date", String(10)),        # YYYY-MM-DD
    Column("owner", String(200)),
    Column("status", String(20)),          # Open|In progress|Complete
    Column("created_at", String(40), nullable=False),
)

attestations = Table(
    "compliance_attestations", _meta,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False, index=True),
    Column("obligation_id", String(32), nullable=False, index=True),
    Column("who", String(200), nullable=False),
    Column("statement", Text),
    Column("created_at", String(40), nullable=False),
)

grants = Table(
    "compliance_grants", _meta,
    Column("user_id", String(32), primary_key=True),
    Column("source", String(20)),
    Column("created_at", String(40), nullable=False),
)

_inited = {"v": False}


def _init():
    if not _inited["v"]:
        _meta.create_all(_engine)
        _inited["v"] = True


# ---------------------------------------------------------------------------
# Entitlement
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
# Seed — give a new user a useful pharmacovigilance starter register.
# ---------------------------------------------------------------------------
def _seed_for(uid: str):
    _init()
    with _engine.connect() as c:
        has = c.execute(select(obligations.c.id).where(obligations.c.user_id == uid)).first()
    if has:
        return
    today = date.today()
    def d(days): return (today + timedelta(days=days)).isoformat()
    starter = [
        ("Pharmacovigilance System Master File (PSMF) review", "GVP Module II", "EMA",
         "Review and update the PSMF to reflect the current pharmacovigilance system.", "annual", d(45),
         "QPPV"),
        ("PSUR / PBRER submission", "GVP Module VII", "EMA",
         "Prepare and submit the Periodic Safety Update Report per the EURD list.", "annual", d(20),
         "PV Manager"),
        ("Signal management review", "GVP Module IX", "EMA",
         "Review the safety signal database and document signal validation decisions.", "monthly", d(7),
         "Safety Physician"),
        ("Literature screening", "GVP Module VI", "EMA",
         "Weekly screening of medical literature for adverse events and safety information.", "monthly", d(3),
         "PV Associate"),
        ("QPPV contact details up to date", "GVP Module I", "National authority",
         "Confirm the QPPV and deputy contact details are current with the authorities.", "quarterly", d(60),
         "QPPV"),
        ("CAPA follow-up from last inspection", "GMP / GVP", "Inspectorate",
         "Track corrective and preventive actions to closure with evidence.", "once", d(30),
         "Quality Lead"),
    ]
    rows = [dict(id=secrets.token_hex(12), user_id=uid, title=t, framework=fw, authority=au,
                 description=desc, frequency=fr, due_date=due, owner=ow, status="Open",
                 created_at=_now())
            for (t, fw, au, desc, fr, due, ow) in starter]
    with _engine.begin() as c:
        c.execute(insert(obligations), rows)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["compliance"])
COOKIE = auth_mod.COOKIE_NAME


def _obl_dict(r) -> dict:
    d = dict(r._mapping)
    today = date.today().isoformat()
    overdue = bool(d.get("due_date") and d["due_date"] < today and d.get("status") != "Complete")
    d["overdue"] = overdue
    return d


@router.get("/api/compliance/status")
def status(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return {"authenticated": False, "active": False}
    return {"authenticated": True, "active": _entitled(user),
            "plan_active": _plan_active(user),
            "self_grant": os.getenv("ALLOW_SELF_GRANT", "0") == "1",
            "name": user.get("name") or user.get("email", "")}


@router.post("/api/compliance/activate")
def activate(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return JSONResponse({"error": "Please sign in."}, status_code=401)
    if _entitled(user):
        _seed_for(user["id"])
        return {"ok": True, "active": True}
    if os.getenv("ALLOW_SELF_GRANT", "0") != "1":
        return JSONResponse({"error": "The compliance module isn’t part of your plan yet."}, status_code=402)
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
        return None, JSONResponse({"error": "Compliance module not active.", "locked": True}, status_code=403)
    return user, None


@router.get("/api/compliance/summary")
def summary(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=14)).isoformat()
    with _engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            select(obligations).where(obligations.c.user_id == user["id"])).fetchall()]
    total = len(rows)
    complete = sum(1 for r in rows if r["status"] == "Complete")
    overdue = sum(1 for r in rows if r["due_date"] and r["due_date"] < today and r["status"] != "Complete")
    due_soon = sum(1 for r in rows if r["due_date"] and today <= r["due_date"] <= soon and r["status"] != "Complete")
    return {"total": total, "complete": complete, "overdue": overdue, "due_soon": due_soon,
            "compliance_rate": round(100 * complete / total) if total else 0}


@router.get("/api/compliance/obligations")
def list_obligations(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        rows = c.execute(select(obligations).where(obligations.c.user_id == user["id"])
                         .order_by(obligations.c.due_date)).fetchall()
    return {"items": [_obl_dict(r) for r in rows]}


class ObligationIn(BaseModel):
    title: str
    framework: str = ""
    authority: str = ""
    description: str = ""
    frequency: str = "annual"
    due_date: str = ""
    owner: str = ""


@router.post("/api/compliance/obligations")
def add_obligation(body: ObligationIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    if not body.title.strip():
        return JSONResponse({"error": "Title is required."}, status_code=400)
    _init()
    oid = secrets.token_hex(12)
    with _engine.begin() as c:
        c.execute(insert(obligations).values(
            id=oid, user_id=user["id"], title=body.title.strip(), framework=body.framework.strip(),
            authority=body.authority.strip(), description=body.description.strip(),
            frequency=body.frequency or "annual", due_date=body.due_date.strip(),
            owner=body.owner.strip(), status="Open", created_at=_now()))
    return {"ok": True, "id": oid}


class StatusIn(BaseModel):
    status: str


@router.post("/api/compliance/obligations/{oid}/status")
def set_status(oid: str, body: StatusIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    if body.status not in ("Open", "In progress", "Complete"):
        return JSONResponse({"error": "Invalid status."}, status_code=400)
    _init()
    with _engine.begin() as c:
        res = c.execute(sql_update(obligations).where(
            obligations.c.id == oid, obligations.c.user_id == user["id"]).values(status=body.status))
    if not res.rowcount:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return {"ok": True}


class AttestIn(BaseModel):
    obligation_id: str
    statement: str = "I confirm this obligation has been completed and the records are accurate."


@router.post("/api/compliance/attest")
def attest(body: AttestIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        obl = c.execute(select(obligations).where(
            obligations.c.id == body.obligation_id, obligations.c.user_id == user["id"])).first()
    if not obl:
        return JSONResponse({"error": "Obligation not found."}, status_code=404)
    who = user.get("name") or user.get("email") or "User"
    with _engine.begin() as c:
        c.execute(insert(attestations).values(
            id=secrets.token_hex(12), user_id=user["id"], obligation_id=body.obligation_id,
            who=who, statement=body.statement, created_at=_now()))
        c.execute(sql_update(obligations).where(obligations.c.id == body.obligation_id)
                  .values(status="Complete"))
    return {"ok": True, "who": who, "at": _now()}


@router.get("/api/compliance/obligations/{oid}/attestations")
def obligation_attestations(oid: str, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        rows = c.execute(select(attestations).where(
            attestations.c.obligation_id == oid, attestations.c.user_id == user["id"])
            .order_by(attestations.c.created_at.desc())).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}
