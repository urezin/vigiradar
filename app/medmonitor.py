"""
VigiEye PhV Medical Monitoring — a sellable add-on module.

A medical review workbench for pharmacovigilance case safety: log individual
case safety reports (ICSRs / adverse events), triage seriousness and expedited
reporting clocks, record causality and medical assessment, and raise & track
medical queries — with a clear view of what's overdue against the regulatory
reporting timeline (15 days for serious, 90 for non-serious).

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


cases = Table(
    "mm_cases", _meta,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False, index=True),
    Column("case_ref", String(60)),
    Column("product", String(200)),
    Column("event", Text),                 # reported adverse event / reaction
    Column("seriousness", String(20)),     # Serious | Non-serious
    Column("causality", String(30)),       # Not assessed | Unrelated | Unlikely | Possibly related | Probably related | Related
    Column("listedness", String(20)),      # Listed | Unlisted | Unknown
    Column("outcome", String(30)),
    Column("status", String(24)),          # Awaiting review | Under review | Query raised | Reviewed
    Column("monitor", String(200)),        # assigned medical monitor
    Column("received", String(10)),        # YYYY-MM-DD
    Column("due", String(10)),             # reporting-clock due date
    Column("notes", Text),
    Column("created_at", String(40), nullable=False),
)

queries = Table(
    "mm_queries", _meta,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), nullable=False, index=True),
    Column("case_id", String(32), nullable=False, index=True),
    Column("question", Text, nullable=False),
    Column("status", String(16)),          # Open | Answered
    Column("answer", Text),
    Column("created_at", String(40), nullable=False),
)

grants = Table(
    "mm_grants", _meta,
    Column("user_id", String(32), primary_key=True),
    Column("source", String(20)),
    Column("created_at", String(40), nullable=False),
)

_inited = {"v": False}
SERIOUSNESS = ["Serious", "Non-serious"]
CAUSALITY = ["Not assessed", "Unrelated", "Unlikely", "Possibly related", "Probably related", "Related"]
STATUSES = ["Awaiting review", "Under review", "Query raised", "Reviewed"]
OUTCOMES = ["Unknown", "Recovered", "Recovering", "Not recovered", "Recovered with sequelae", "Fatal"]
LISTEDNESS = ["Unknown", "Listed", "Unlisted"]


def _init():
    if not _inited["v"]:
        _meta.create_all(_engine)
        _inited["v"] = True


def _clock_days(seriousness: str) -> int:
    return 15 if seriousness == "Serious" else 90


# ---------------------------------------------------------------------------
# Entitlement (mirrors the compliance module)
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
# Seed — a small, realistic starter caseload
# ---------------------------------------------------------------------------
def _seed_for(uid: str):
    _init()
    with _engine.connect() as c:
        if c.execute(select(cases.c.id).where(cases.c.user_id == uid)).first():
            return
    today = date.today()
    def d(days): return (today + timedelta(days=days)).isoformat()
    seed = [
        ("PV-2026-0142", "Atorvastatin 20mg", "Rhabdomyolysis with acute kidney injury", "Serious",
         "Possibly related", "Unlisted", "Not recovered", "Awaiting review", "", d(-11)),
        ("PV-2026-0143", "Metformin 500mg", "Nausea and mild diarrhoea", "Non-serious",
         "Probably related", "Listed", "Recovering", "Under review", "Dr Medical Monitor", d(-4)),
        ("PV-2026-0144", "Adalimumab", "Injection-site reaction", "Non-serious",
         "Related", "Listed", "Recovered", "Reviewed", "Dr Medical Monitor", d(-30)),
        ("PV-2026-0145", "Warfarin", "Gastrointestinal haemorrhage", "Serious",
         "Not assessed", "Unknown", "Not recovered", "Awaiting review", "", d(-2)),
        ("PV-2026-0146", "Amoxicillin", "Maculopapular rash", "Non-serious",
         "Possibly related", "Listed", "Recovered", "Query raised", "Dr Medical Monitor", d(-8)),
    ]
    rows = []
    for (ref, prod, ev, ser, cau, lst, out, st, mon, rec) in seed:
        due = (date.fromisoformat(rec) + timedelta(days=_clock_days(ser))).isoformat()
        rows.append(dict(id=secrets.token_hex(12), user_id=uid, case_ref=ref, product=prod, event=ev,
                         seriousness=ser, causality=cau, listedness=lst, outcome=out, status=st,
                         monitor=mon, received=rec, due=due, notes="", created_at=_now()))
    with _engine.begin() as c:
        c.execute(insert(cases), rows)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["medical-monitoring"])
COOKIE = auth_mod.COOKIE_NAME


def _case_dict(r) -> dict:
    d = dict(r._mapping)
    today = date.today().isoformat()
    d["overdue"] = bool(d.get("due") and d["due"] < today and d.get("status") != "Reviewed")
    return d


@router.get("/api/mm/status")
def status(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return {"authenticated": False, "active": False}
    return {"authenticated": True, "active": _entitled(user),
            "plan_active": _plan_active(user),
            "self_grant": os.getenv("ALLOW_SELF_GRANT", "1") == "1",
            "name": user.get("name") or user.get("email", "")}


@router.post("/api/mm/activate")
def activate(session: str | None = Cookie(default=None, alias=COOKIE)):
    user = _current(session)
    if not user:
        return JSONResponse({"error": "Please sign in."}, status_code=401)
    if _entitled(user):
        _seed_for(user["id"])
        return {"ok": True, "active": True}
    if os.getenv("ALLOW_SELF_GRANT", "1") != "1":
        return JSONResponse({"error": "The medical monitoring module isn’t part of your plan yet."}, status_code=402)
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
        return None, JSONResponse({"error": "Medical monitoring module not active.", "locked": True}, status_code=403)
    return user, None


@router.get("/api/mm/summary")
def summary(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    today = date.today().isoformat()
    with _engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            select(cases).where(cases.c.user_id == user["id"])).fetchall()]
    total = len(rows)
    awaiting = sum(1 for r in rows if r["status"] in ("Awaiting review", "Under review", "Query raised"))
    serious = sum(1 for r in rows if r["seriousness"] == "Serious")
    reviewed = sum(1 for r in rows if r["status"] == "Reviewed")
    overdue = sum(1 for r in rows if r["due"] and r["due"] < today and r["status"] != "Reviewed")
    return {"total": total, "awaiting": awaiting, "serious": serious,
            "reviewed": reviewed, "overdue": overdue,
            "seriousness": SERIOUSNESS, "causality": CAUSALITY, "statuses": STATUSES,
            "outcomes": OUTCOMES, "listedness": LISTEDNESS}


@router.get("/api/mm/cases")
def list_cases(session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        rows = c.execute(select(cases).where(cases.c.user_id == user["id"])
                         .order_by(cases.c.due)).fetchall()
    return {"items": [_case_dict(r) for r in rows]}


class CaseIn(BaseModel):
    case_ref: str = ""
    product: str = ""
    event: str
    seriousness: str = "Non-serious"
    causality: str = "Not assessed"
    listedness: str = "Unknown"
    outcome: str = "Unknown"
    monitor: str = ""
    received: str = ""


@router.post("/api/mm/cases")
def add_case(body: CaseIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    if not body.event.strip():
        return JSONResponse({"error": "The adverse event is required."}, status_code=400)
    ser = body.seriousness if body.seriousness in SERIOUSNESS else "Non-serious"
    rec = body.received.strip() or date.today().isoformat()
    try:
        due = (date.fromisoformat(rec) + timedelta(days=_clock_days(ser))).isoformat()
    except Exception:
        rec = date.today().isoformat()
        due = (date.today() + timedelta(days=_clock_days(ser))).isoformat()
    cid = secrets.token_hex(12)
    ref = body.case_ref.strip() or ("PV-" + cid[:8].upper())
    _init()
    with _engine.begin() as c:
        c.execute(insert(cases).values(
            id=cid, user_id=user["id"], case_ref=ref, product=body.product.strip(),
            event=body.event.strip(), seriousness=ser,
            causality=body.causality if body.causality in CAUSALITY else "Not assessed",
            listedness=body.listedness if body.listedness in LISTEDNESS else "Unknown",
            outcome=body.outcome if body.outcome in OUTCOMES else "Unknown",
            status="Awaiting review", monitor=body.monitor.strip(),
            received=rec, due=due, notes="", created_at=_now()))
    return {"ok": True, "id": cid, "case_ref": ref, "due": due}


class FieldIn(BaseModel):
    field: str    # status | causality | seriousness | outcome | monitor
    value: str


@router.post("/api/mm/cases/{cid}/set")
def set_field(cid: str, body: FieldIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    allowed = {
        "status": STATUSES, "causality": CAUSALITY, "seriousness": SERIOUSNESS,
        "outcome": OUTCOMES, "listedness": LISTEDNESS, "monitor": None,
    }
    if body.field not in allowed:
        return JSONResponse({"error": "Unknown field."}, status_code=400)
    if allowed[body.field] is not None and body.value not in allowed[body.field]:
        return JSONResponse({"error": "Invalid value."}, status_code=400)
    _init()
    values = {body.field: body.value}
    # Changing seriousness re-computes the reporting clock from the received date.
    if body.field == "seriousness":
        with _engine.connect() as c:
            row = c.execute(select(cases).where(cases.c.id == cid, cases.c.user_id == user["id"])).first()
        if row:
            rec = row._mapping.get("received") or date.today().isoformat()
            try:
                values["due"] = (date.fromisoformat(rec) + timedelta(days=_clock_days(body.value))).isoformat()
            except Exception:
                pass
    with _engine.begin() as c:
        res = c.execute(sql_update(cases).where(
            cases.c.id == cid, cases.c.user_id == user["id"]).values(**values))
    if not res.rowcount:
        return JSONResponse({"error": "Not found."}, status_code=404)
    return {"ok": True}


class QueryIn(BaseModel):
    case_id: str
    question: str


@router.post("/api/mm/queries")
def add_query(body: QueryIn, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    if not body.question.strip():
        return JSONResponse({"error": "Question is required."}, status_code=400)
    _init()
    with _engine.connect() as c:
        owns = c.execute(select(cases.c.id).where(
            cases.c.id == body.case_id, cases.c.user_id == user["id"])).first()
    if not owns:
        return JSONResponse({"error": "Case not found."}, status_code=404)
    with _engine.begin() as c:
        c.execute(insert(queries).values(
            id=secrets.token_hex(12), user_id=user["id"], case_id=body.case_id,
            question=body.question.strip(), status="Open", answer="", created_at=_now()))
        c.execute(sql_update(cases).where(cases.c.id == body.case_id).values(status="Query raised"))
    return {"ok": True}


@router.get("/api/mm/cases/{cid}/queries")
def case_queries(cid: str, session: str | None = Cookie(default=None, alias=COOKIE)):
    user, err = _guard(session)
    if err:
        return err
    _init()
    with _engine.connect() as c:
        rows = c.execute(select(queries).where(
            queries.c.case_id == cid, queries.c.user_id == user["id"])
            .order_by(queries.c.created_at.desc())).fetchall()
    return {"items": [dict(r._mapping) for r in rows]}
