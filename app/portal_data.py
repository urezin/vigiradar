"""
VigiEye portal: data layer and ingesters for the EU + US biotech portal.

Every row stores the official URL it came from, plus a source_type:
  "official"        regulator data (EMA, FDA, Federal Register)
  "company_filing"  a company's own SEC filing (how PDUFA dates are announced:
                    FDA does not publish PDUFA goal dates itself)
  "estimate"        a date derived by rule from an official date (e.g. expected
                    EC decision = CHMP opinion + 67 days); always labelled as such

Ingesters (all idempotent, safe to run daily):
  ingest_ema_medicines   EMA medicines data (EPAR JSON): EU drugs, CHMP opinions, EC decisions
  ingest_ema_referrals   EMA referrals JSON: PRAC/CHMP referrals
  ingest_ema_dhpc        EMA DHPC JSON: safety communications
  ingest_ema_shortages   EMA shortages JSON
  ingest_fda_approvals   openFDA Drugs@FDA: US NDA/BLA original approvals
  ingest_sec_pdufa       SEC EDGAR full-text search of 8-K exhibits for PDUFA dates
  ingest_fda_adcomm      Federal Register notices of FDA advisory committee meetings
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (Column, MetaData, String, Table, Text, delete, func, insert,
                        or_, select)

from . import store
from .config import settings

log = logging.getLogger("vigiradar.portal")

_meta = MetaData()

drugs = Table(
    "portal_drugs", _meta,
    Column("id", String(160), primary_key=True),       # "eu-hemlibra" / "us-nda761083"
    Column("region", String(4), nullable=False),         # EU | US
    Column("slug", String(160), nullable=False),
    Column("name", Text, nullable=False),
    Column("inn", Text),
    Column("company", Text),
    Column("company_slug", String(160)),
    Column("status", String(80)),
    Column("therapeutic_area", Text),
    Column("indication", Text),
    Column("flags", Text),                               # json list, e.g. ["Orphan","PRIME"]
    Column("opinion_date", String(10)),
    Column("decision_date", String(10)),                 # EC decision / FDA approval
    Column("first_auth_date", String(10)),
    Column("source_url", Text),
    Column("extra", Text),                               # json
    Column("updated_at", String(40), nullable=False),
)

milestones = Table(
    "portal_milestones", _meta,
    Column("id", String(40), primary_key=True),
    Column("date", String(10), nullable=False),
    Column("region", String(4), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("title", Text, nullable=False),
    Column("detail", Text),
    Column("drug_name", Text),
    Column("drug_id", String(160)),
    Column("inn", Text),
    Column("company", Text),
    Column("company_slug", String(160)),
    Column("impact", String(8)),
    Column("source_url", Text),
    Column("source_type", String(20)),
    Column("source_label", Text),
    Column("ingested_at", String(40), nullable=False),
)

# processed SEC filings (so each exhibit is downloaded/parsed once)
sec_seen = Table(
    "portal_sec_seen", _meta,
    Column("id", String(120), primary_key=True),
    Column("result", Text),                              # json: extracted items for this filing
    Column("seen_at", String(40), nullable=False),
)

analyses = Table(
    "portal_analyses", _meta,
    Column("id", String(200), primary_key=True),         # e.g. "drug:eu-hemlibra", "week:2026-W44"
    Column("facts_hash", String(40)),
    Column("body", Text, nullable=False),                # sanitised plain text with simple markup
    Column("model", String(80)),
    Column("created_at", String(40), nullable=False),
)

KIND_LABEL = {
    "pdufa": "PDUFA date",
    "adcomm": "FDA advisory committee",
    "fda_approval": "FDA approval",
    "chmp_opinion_pos": "CHMP positive opinion",
    "chmp_opinion_neg": "CHMP negative opinion",
    "ec_decision": "EU approval (EC decision)",
    "ec_expected": "Expected EC decision",
    "eu_refusal": "EU refusal",
    "eu_withdrawal": "Application withdrawn (EU)",
    "referral_start": "Referral started",
    "prac_recommendation": "PRAC recommendation",
    "referral_opinion": "Referral opinion",
    "referral_ec": "Referral EC decision",
    "dhpc": "DHPC safety letter",
    "shortage": "EU shortage",
}

UA = {"User-Agent": "VigiEye regulatory monitor info@vigi-eye.com",
      "Accept": "application/json, text/html, */*"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init() -> None:
    _meta.create_all(store._engine)


def slugify(s: str, n: int = 90) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-") or "x"


_CO_SUFFIX = re.compile(
    r"\b(inc|inc\.|incorporated|corp|corporation|co|company|ltd|limited|llc|plc|gmbh|ag|sa|s\.a\.|"
    r"s\.r\.l|srl|bv|b\.v\.|nv|n\.v\.|ab|as|a/s|oy|spa|s\.p\.a|kft|sas|pharms|pharma|pharmaceuticals?|"
    r"therapeutics|biotherapeutics|registration|europe|holdings?|international|ireland|deutschland|"
    r"the)\b\.?", re.I)


def company_slug(name: str) -> str:
    """Loose normaliser so 'Roche Registration GmbH' and 'Roche' share a page."""
    s = re.sub(r"\(.*?\)", " ", name or "")
    s = _CO_SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return slugify(s or name or "unknown", 60)


def _mid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def _dmy(s: str) -> str | None:
    """'27/03/2025' -> '2025-03-27'."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _http_get(url: str, timeout: float = 90.0, headers: dict | None = None):
    import httpx
    with httpx.Client(timeout=timeout, headers={**UA, **(headers or {})},
                      follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r


# --- writes -----------------------------------------------------------------

def _upsert_drug(row: dict) -> None:
    row = {**row, "updated_at": _now()}
    with store._engine.begin() as c:
        c.execute(delete(drugs).where(drugs.c.id == row["id"]))
        c.execute(insert(drugs).values(**row))


def _upsert_milestone(row: dict) -> None:
    row = {**row, "ingested_at": _now()}
    with store._engine.begin() as c:
        c.execute(delete(milestones).where(milestones.c.id == row["id"]))
        c.execute(insert(milestones).values(**row))


def _replace_kinds(kinds: list[str], rows: list[dict]) -> int:
    """Full refresh of a set of milestone kinds (for snapshot datasets)."""
    now = _now()
    seen, payload = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        payload.append({**r, "ingested_at": now})
    with store._engine.begin() as c:
        c.execute(delete(milestones).where(milestones.c.kind.in_(kinds)))
        for i in range(0, len(payload), 500):
            if payload[i:i + 500]:
                c.execute(insert(milestones), payload[i:i + 500])
    return len(payload)


# --- EMA --------------------------------------------------------------------

EMA_MEDICINES = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
EMA_REFERRALS = "https://www.ema.europa.eu/en/documents/report/referrals-output-json-report_en.json"
EMA_DHPC = "https://www.ema.europa.eu/en/documents/report/dhpc-output-json-report_en.json"
EMA_SHORTAGES = "https://www.ema.europa.eu/en/documents/report/shortages-output-json-report_en.json"

_FLAG_FIELDS = [("orphan_medicine", "Orphan"), ("prime_priority_medicine", "PRIME"),
                ("advanced_therapy", "ATMP"), ("biosimilar", "Biosimilar"), ("generic", "Generic"),
                ("conditional_approval", "Conditional"), ("exceptional_circumstances", "Exceptional circumstances"),
                ("accelerated_assessment", "Accelerated assessment"), ("additional_monitoring", "Additional monitoring")]

EC_DECISION_LAG_DAYS = 67   # standard EC decision-making period after a CHMP opinion


def _ema_json(url: str) -> list[dict]:
    r = _http_get(url, timeout=180)
    return (r.json() or {}).get("data", []) or []


def _clean(s: str) -> str:
    s = (s or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


def ingest_ema_medicines(rows: list[dict] | None = None, horizon_days: int = 400) -> dict:
    """EU drugs (human) + CHMP opinion / EC decision milestones."""
    init()
    rows = rows if rows is not None else _ema_json(EMA_MEDICINES)
    today = date.today()
    since = (today - timedelta(days=horizon_days)).isoformat()
    n_drugs, ms, statuses = 0, [], {}
    for r in rows:
        if (r.get("category") or "").lower() != "human":
            continue
        name = _clean(r.get("name_of_medicine"))
        if not name:
            continue
        status = _clean(r.get("medicine_status"))
        statuses[status] = statuses.get(status, 0) + 1
        inn = _clean(r.get("international_non_proprietary_name_common_name") or r.get("active_substance"))
        company = _clean(r.get("marketing_authorisation_developer_applicant_holder"))
        cslug = company_slug(company) if company else None
        flags = [lab for f, lab in _FLAG_FIELDS if (r.get(f) or "").strip().lower() == "yes"]
        opinion = _dmy(r.get("opinion_adopted_date"))
        ma = _dmy(r.get("marketing_authorisation_date"))
        refusal = _dmy(r.get("refusal_of_marketing_authorisation_date"))
        withdrawal = _dmy(r.get("withdrawal_of_application_date"))
        url = (r.get("medicine_url") or "").strip()
        did = "eu-" + slugify(name + "-" + (r.get("ema_product_number") or "").split("/")[-1], 120)
        _upsert_drug({
            "id": did, "region": "EU", "slug": did[3:], "name": name, "inn": inn,
            "company": company, "company_slug": cslug, "status": status,
            "therapeutic_area": _clean(r.get("therapeutic_area_mesh")).replace(";", "; "),
            "indication": _clean(r.get("therapeutic_indication"))[:4000],
            "flags": json.dumps(flags), "opinion_date": opinion, "decision_date": ma,
            "first_auth_date": ma, "source_url": url,
            "extra": json.dumps({"ema_product_number": r.get("ema_product_number"),
                                 "opinion_status": r.get("opinion_status"),
                                 "atc": r.get("atc_code_human"),
                                 "pharmacotherapeutic_group": r.get("pharmacotherapeutic_group_human")}),
        })
        n_drugs += 1
        base = {"region": "EU", "drug_name": name, "drug_id": did, "inn": inn, "company": company,
                "company_slug": cslug, "source_url": url, "source_type": "official",
                "source_label": "EMA medicines data"}
        neg = "negative" in (r.get("opinion_status") or "").lower() or bool(refusal)
        tags = (", ".join(flags[:3])) if flags else ""
        if opinion and opinion >= since:
            kind = "chmp_opinion_neg" if neg else "chmp_opinion_pos"
            ms.append({**base, "id": _mid("op", did, opinion), "date": opinion, "kind": kind,
                       "title": f"{name} ({inn}): CHMP {'negative' if neg else 'positive'} opinion",
                       "detail": "; ".join(x for x in [company, tags] if x), "impact": "high"})
            if not neg and not ma:
                exp = (date.fromisoformat(opinion) + timedelta(days=EC_DECISION_LAG_DAYS)).isoformat()
                ms.append({**base, "id": _mid("exp", did, opinion), "date": exp, "kind": "ec_expected",
                           "title": f"{name} ({inn}): expected EC decision",
                           "detail": f"Estimated as CHMP opinion ({opinion}) + {EC_DECISION_LAG_DAYS} days, "
                                     "the standard Commission decision period. Not an official date.",
                           "impact": "high", "source_type": "estimate",
                           "source_label": "Estimate from EMA opinion date"})
        if ma and ma >= since:
            ms.append({**base, "id": _mid("ma", did, ma), "date": ma, "kind": "ec_decision",
                       "title": f"{name} ({inn}) approved in the EU",
                       "detail": "; ".join(x for x in [f"Marketing authorisation to {company}", tags] if x),
                       "impact": "high"})
        if refusal and refusal >= since:
            ms.append({**base, "id": _mid("ref", did, refusal), "date": refusal, "kind": "eu_refusal",
                       "title": f"{name} ({inn}): EU marketing authorisation refused",
                       "detail": company, "impact": "high"})
        if withdrawal and withdrawal >= since:
            ms.append({**base, "id": _mid("wd", did, withdrawal), "date": withdrawal, "kind": "eu_withdrawal",
                       "title": f"{name} ({inn}): EU application withdrawn",
                       "detail": company, "impact": "med"})
    n_ms = _replace_kinds(["chmp_opinion_pos", "chmp_opinion_neg", "ec_expected", "ec_decision",
                           "eu_refusal", "eu_withdrawal"], ms)
    return {"drugs": n_drugs, "milestones": n_ms, "statuses": statuses}


def ingest_ema_referrals(rows: list[dict] | None = None, horizon_days: int = 400) -> dict:
    init()
    rows = rows if rows is not None else _ema_json(EMA_REFERRALS)
    since = (date.today() - timedelta(days=horizon_days)).isoformat()
    ms = []
    for r in rows:
        if (r.get("category") or "").lower() != "human":
            continue
        name = _clean(r.get("referral_name"))
        inn = _clean(r.get("international_non_proprietary_name_inn_common_name"))
        url = (r.get("referral_url") or "").strip()
        safety = (r.get("safety_referral") or "").lower() == "yes"
        rtype = _clean(r.get("referral_type"))
        rec = _clean(r.get("prac_recommendation"))
        base = {"region": "EU", "drug_name": name, "inn": inn, "source_url": url,
                "source_type": "official", "source_label": "EMA referrals data",
                "company": None, "company_slug": None, "drug_id": None}
        detail = "; ".join(x for x in [rtype, "Safety referral" if safety else "",
                                        _clean(r.get("current_status"))] if x)
        for field, kind, verb, imp in [
            ("procedure_start_date", "referral_start", "referral started", "high" if safety else "med"),
            ("prac_recommendation_date", "prac_recommendation", "PRAC recommendation", "high"),
            ("chmp_cvmp_opinion_date", "referral_opinion", "CHMP opinion", "high"),
            ("cmdh_position_date", "referral_opinion", "CMDh position", "high"),
            ("european_commission_decision_date", "referral_ec", "EC decision", "med"),
        ]:
            d = _dmy(r.get(field))
            if d and d >= since:
                ms.append({**base, "id": _mid(kind, url or name, field, d), "date": d, "kind": kind,
                           "title": f"{name}: {verb}",
                           "detail": (detail + (f". PRAC: {rec}" if rec and kind == "prac_recommendation" else "")),
                           "impact": imp})
    n = _replace_kinds(["referral_start", "prac_recommendation", "referral_opinion", "referral_ec"], ms)
    return {"milestones": n}


def ingest_ema_dhpc(rows: list[dict] | None = None, horizon_days: int = 400) -> dict:
    init()
    rows = rows if rows is not None else _ema_json(EMA_DHPC)
    since = (date.today() - timedelta(days=horizon_days)).isoformat()
    ms = []
    for r in rows:
        if (r.get("category") or "").lower() != "human":
            continue
        d = _dmy(r.get("dissemination_date")) or _dmy(r.get("first_published_date"))
        if not d or d < since:
            continue
        name = _clean(r.get("name_of_medicine"))
        inn = _clean(r.get("active_substances")).replace(";", ", ")
        ms.append({"id": _mid("dhpc", r.get("dhpc_url"), d), "date": d, "region": "EU", "kind": "dhpc",
                   "title": f"{name} ({inn}): DHPC sent to healthcare professionals",
                   "detail": "; ".join(x for x in [_clean(r.get("dhpc_type")).replace(";", ", "),
                                                   _clean(r.get("therapeutic_area_mesh")).replace(";", ", ")] if x),
                   "drug_name": name, "inn": inn, "drug_id": None, "company": None, "company_slug": None,
                   "impact": "high", "source_url": r.get("dhpc_url"), "source_type": "official",
                   "source_label": "EMA DHPC data"})
    return {"milestones": _replace_kinds(["dhpc"], ms)}


def ingest_ema_shortages(rows: list[dict] | None = None) -> dict:
    init()
    rows = rows if rows is not None else _ema_json(EMA_SHORTAGES)
    ms = []
    for r in rows:
        if (r.get("category") or "").lower() != "human":
            continue
        status = _clean(r.get("supply_shortage_status"))
        if "resolved" in status.lower():
            continue
        d = _dmy(r.get("last_updated_date")) or _dmy(r.get("first_published_date"))
        if not d:
            continue
        name = _clean(r.get("medicine_affected"))
        inn = _clean(r.get("international_non_proprietary_name_inn_or_common_name"))
        res = _clean(r.get("expected_resolution")) or (_dmy(r.get("expected_resolution_date")) or "")
        alt = _clean(r.get("availability_of_alternatives"))
        ms.append({"id": _mid("short", r.get("shortage_url")), "date": d, "region": "EU", "kind": "shortage",
                   "title": f"{name}: {status.lower()}",
                   "detail": "; ".join(x for x in [
                       _clean(r.get("pharmaceutical_forms_affected")).replace(";", ", "),
                       f"Expected resolution: {res}" if res else "",
                       f"Alternatives available: {alt}" if alt else ""] if x),
                   "drug_name": name, "inn": inn, "drug_id": None, "company": None, "company_slug": None,
                   "impact": "high" if alt.lower() == "no" else "med", "source_url": r.get("shortage_url"),
                   "source_type": "official", "source_label": "EMA shortages data"})
    return {"milestones": _replace_kinds(["shortage"], ms)}


# --- FDA approvals (openFDA) ------------------------------------------------

OPENFDA = "https://api.fda.gov/drug/drugsfda.json"


def _ymd(s: str) -> str | None:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def ingest_fda_approvals(results: list[dict] | None = None, days: int = 400) -> dict:
    """US original NDA/BLA approvals in the last `days` (generics excluded)."""
    init()
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    if results is None:
        results, skip = [], 0
        q = (f"submissions.submission_type:ORIG+AND+submissions.submission_status:AP+AND+"
             f"submissions.submission_status_date:[{start}+TO+{end}]")
        while True:
            r = _http_get(f"{OPENFDA}?search={q}&limit=1000&skip={skip}", timeout=120)
            j = r.json()
            batch = j.get("results", []) or []
            results += batch
            total = j.get("meta", {}).get("results", {}).get("total", 0)
            skip += len(batch)
            if not batch or skip >= total or skip >= 25000:
                break
            time.sleep(0.3)
    ms, n_drugs = [], 0
    for a in results:
        appno = a.get("application_number") or ""
        if not (appno.startswith("NDA") or appno.startswith("BLA")):
            continue
        orig = [s for s in a.get("submissions", []) or []
                if s.get("submission_type") == "ORIG" and s.get("submission_status") == "AP"]
        if not orig:
            continue
        d = _ymd(orig[0].get("submission_status_date"))
        if not d or d < f"{start[:4]}-{start[4:6]}-{start[6:]}":
            continue
        prods = a.get("products", []) or []
        brand = _clean((prods[0].get("brand_name") if prods else "") or
                       ((a.get("openfda") or {}).get("brand_name") or [""])[0]).title()
        ingr = sorted({_clean(i.get("name", "")).lower() for p in prods for i in (p.get("active_ingredients") or [])})
        inn = ", ".join(i for i in ingr if i)
        forms = sorted({_clean(p.get("dosage_form", "")).lower() for p in prods if p.get("dosage_form")})
        sponsor = _clean(a.get("sponsor_name")).title()
        cls = orig[0].get("submission_class_code") or ""
        prio = orig[0].get("review_priority") or ""
        novel = appno.startswith("BLA") or cls in ("TYPE 1", "TYPE 1/4")
        flags = (["Novel"] if novel else []) + (["Priority review"] if prio.upper() == "PRIORITY" else [])
        did = "us-" + slugify(appno, 40)
        appnum = re.sub(r"\D", "", appno)
        url = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={appnum}"
        _upsert_drug({"id": did, "region": "US", "slug": did[3:], "name": brand or appno, "inn": inn,
                      "company": sponsor, "company_slug": company_slug(sponsor), "status": "Approved",
                      "therapeutic_area": "", "indication": "", "flags": json.dumps(flags),
                      "opinion_date": None, "decision_date": d, "first_auth_date": d, "source_url": url,
                      "extra": json.dumps({"application_number": appno, "class": cls, "priority": prio,
                                           "forms": forms})})
        n_drugs += 1
        ms.append({"id": _mid("fda", appno, d), "date": d, "region": "US", "kind": "fda_approval",
                   "title": f"{brand or appno} ({inn}) approved by FDA" if inn else f"{brand or appno} approved by FDA",
                   "detail": "; ".join(x for x in [sponsor, appno, ", ".join(flags), ", ".join(forms)] if x),
                   "drug_name": brand, "drug_id": did, "inn": inn, "company": sponsor,
                   "company_slug": company_slug(sponsor), "impact": "high" if novel else "med",
                   "source_url": url, "source_type": "official", "source_label": "FDA Drugs@FDA (openFDA)"})
    return {"drugs": n_drugs, "milestones": _replace_kinds(["fda_approval"], ms)}


# --- PDUFA dates from SEC filings -------------------------------------------

SEC_FTS = "https://efts.sec.gov/LATEST/search-index"
_PDUFA_PHRASES = ['"PDUFA target action date"', '"PDUFA date"', '"PDUFA goal date"',
                  '"Prescription Drug User Fee Act (PDUFA)"']
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan\\.?|Feb\\.?|Mar\\.?|Apr\\.?|Jun\\.?|Jul\\.?|Aug\\.?|Sep\\.?|Sept\\.?|Oct\\.?|Nov\\.?|Dec\\.?")
_DATE_RE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),\s+(20\d\d)\b")
_MONTH_NUM = {m[:3].lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}


def _html_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    t = re.sub(r"<[^>]+>", " ", h)
    import html as _h
    t = _h.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _to_iso(mon: str, day: str, year: str) -> str | None:
    try:
        return date(int(year), _MONTH_NUM[mon[:3].lower()], int(day)).isoformat()
    except Exception:
        return None


def extract_pdufa_regex(text: str) -> list[dict]:
    """Dates that sit within ~220 chars after a PDUFA mention. Returns [{date, context}]."""
    out = []
    for m in re.finditer(r"PDUFA", text):
        window = text[m.start(): m.start() + 260]
        dm = _DATE_RE.search(window)
        if dm:
            iso = _to_iso(*dm.groups())
            if iso:
                ctx = text[max(0, m.start() - 320): m.start() + 260]
                out.append({"date": iso, "date_text": dm.group(0), "context": ctx})
    return out


_EXTRACT_PROMPT = """Below is a press release a company filed with the SEC. Extract every FDA PDUFA target
action date it states for a product. Return ONLY JSON: {{"items":[{{"drug":"brand or code name",
"inn":"generic name or empty","indication":"short indication or empty","application":"NDA|BLA|sNDA|sBLA|empty",
"pdufa_date_text":"the date exactly as written in the text","status":"set|extended|met|other"}}]}}.
Use only facts written in the text. If there is no PDUFA date, return {{"items":[]}}.

TEXT:
{text}"""


def _ai_extract(text: str) -> list[dict] | None:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(model=settings.anthropic_model, max_tokens=700,
                                     messages=[{"role": "user", "content": _EXTRACT_PROMPT.format(text=text[:14000])}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        j = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return j.get("items", [])
    except Exception as e:
        log.warning("[pdufa] AI extract failed: %s", e)
        return None


def _sec_search(phrase: str, start: str, end: str, max_hits: int = 200) -> list[dict]:
    from urllib.parse import urlencode
    hits, frm = [], 0
    while frm < max_hits:
        q = urlencode({"q": phrase, "forms": "8-K", "dateRange": "custom", "startdt": start,
                       "enddt": end, "from": frm})
        j = _http_get(f"{SEC_FTS}?{q}", timeout=60).json()
        batch = (j.get("hits") or {}).get("hits") or []
        hits += batch
        if len(batch) < 100:
            break
        frm += 100
        time.sleep(0.25)
    return hits


_DATELINE = re.compile(r"\b[A-Z][A-Z.'\-]+(?:\s[A-Z][A-Z.'\-]+){0,3}\s?(?:,\s*[A-Z][A-Za-z.]{1,15}){0,2},?\s*"
                       r"(?:\([^)]*\)\s*)?[,\-–—]?\s*(?:" + _MONTHS + r")\s+\d{1,2},\s+20\d\d")
_EX_MARK = re.compile(r"\b(?:EX-99\.\d+|Exhibit\s+99\.\d+)\b", re.I)
_LEAD_LABELS = re.compile(r"^(?:\s*(?:Document|FOR IMMEDIATE RELEASE|Press Release|NEWS RELEASE|"
                          r"\d+\s+\S+\.htm)\b[:\s]*)+", re.I)


def clean_exhibit(text: str) -> str:
    """Drop the EDGAR exhibit wrapper ('EX-99.1 2 x.htm EX-99.1 Exhibit 99.1 FOR IMMEDIATE RELEASE ...')."""
    t = (text or "").strip()
    head = t[:400]
    marks = list(_EX_MARK.finditer(head))
    if marks:
        t = t[marks[-1].end():]
    return _LEAD_LABELS.sub("", t).strip()


def extract_headline(text: str) -> str:
    """Press-release headline: text between 'Exhibit 99.x' and the city/date dateline."""
    t = clean_exhibit(text)
    d = _DATELINE.search(t[:900])
    head = t[: d.start()] if d and d.start() > 25 else t[:220]
    head = re.split(r"\s[–—-]\s(?=[A-Z])", head.strip())[0]
    return re.sub(r"\s+", " ", head).strip(" -–—")[:220]


def pdufa_sentence(text: str, date_text: str) -> str:
    """The sentence of the filing that states the PDUFA date (verbatim, trimmed)."""
    i = text.find(date_text)
    if i < 0:
        return ""
    j = text.rfind("PDUFA", 0, i + 1)
    start = max(text.rfind(". ", 0, j if j >= 0 else i), text.rfind(" • ", 0, j if j >= 0 else i),
                text.rfind("•", 0, j if j >= 0 else i))
    start = start + 2 if start >= 0 else max(0, i - 300)
    if i - start > 420:
        start = i - 420
    end = text.find(". ", i)
    end = end + 1 if 0 <= end < i + 300 else i + len(date_text) + 120
    out = text[start:end].strip(" •-–—")
    k = out.find(date_text)
    dl = _DATELINE.search(out, k + len(date_text)) if k >= 0 else None
    if dl:
        out = out[:dl.start()].strip(" •-–—,")
    out = re.sub(r"\s[–—-]\s*[A-Z][A-Z .']+\s*,\s*[A-Z][a-z]{2,4}\.?$", "", out)   # trailing "– SAN DIEGO , Aug."
    return ("…" if start > 0 and text[start - 2:start] != ". " else "") + out[:520]


def _drug_key(drug: str) -> str:
    """'ivonescimab (SMT112)' and 'Ivonescimab' -> 'ivonescimab'."""
    d = re.sub(r"\(.*?\)|[®™]", " ", drug or "").strip().lower()
    return slugify(d.split()[0] if d.split() else drug, 40)


def _resolve_pdufa(cached: list[dict]) -> list[dict]:
    """Turn cached per-filing extractions into one current row per company+drug (or per company
    when the drug name isn't known). The newest filing wins, so extended dates replace old ones."""
    cached = sorted(cached, key=lambda c: c.get("filed", ""), reverse=True)
    best: dict[str, dict] = {}
    latest_unnamed_filing: dict[str, str] = {}
    named_dates: dict[str, set] = {}
    for c in cached:
        cs = company_slug(c["company"])
        for it in c.get("items", []):
            if it.get("drug"):
                key = f"{cs}|{_drug_key(it['drug'])}"
                if key not in best:
                    best[key] = {**it, **{k: c[k] for k in ("company", "ticker", "filed", "url", "headline")}}
                    named_dates.setdefault(cs, set()).add(it["date"])
    for c in cached:
        cs = company_slug(c["company"])
        unnamed = [it for it in c.get("items", []) if not it.get("drug")]
        if not unnamed:
            continue
        if cs in latest_unnamed_filing and latest_unnamed_filing[cs] != c["filed"]:
            continue          # only the newest unnamed filing per company counts
        latest_unnamed_filing[cs] = c["filed"]
        for it in unnamed:
            if it["date"] in named_dates.get(cs, set()):
                continue
            key = f"{cs}|date-{it['date']}"
            best.setdefault(key, {**it, **{k: c[k] for k in ("company", "ticker", "filed", "url", "headline")}})
    rows, seen_cd = [], set()
    for key, it in sorted(best.items(), key=lambda kv: kv[1].get("filed", ""), reverse=True):
        cd = (company_slug(it["company"]), it["date"], _drug_key(it.get("drug") or ""))
        if cd in seen_cd:
            continue
        seen_cd.add(cd)
        if it.get("sentence") and len(it["sentence"]) > 320:
            it = {**it, "sentence": it["sentence"][:317].rsplit(" ", 1)[0] + "…"}
        company, drug = it["company"], it.get("drug") or ""
        if drug:
            title = f"{drug}: PDUFA target action date" + (f" ({it['indication']})" if it.get("indication") else "")
        else:
            title = f"{company}: PDUFA target action date"
        detail = "; ".join(x for x in [
            f"{company}" + (f" ({it['ticker']})" if it.get("ticker") else ""), it.get("inn", ""),
            it.get("application", ""), f"status: {it['status']}" if it.get("status") else "",
            f'"{it["sentence"]}"' if it.get("sentence") else (f'"{it["headline"]}"' if it.get("headline") else ""),
            f"filed {it['filed']}"] if x)
        rows.append({"id": _mid("pdufa", key), "date": it["date"], "region": "US", "kind": "pdufa",
                     "title": title, "detail": detail, "drug_name": drug or None, "drug_id": None,
                     "inn": it.get("inn") or None, "company": company, "company_slug": company_slug(company),
                     "impact": "high", "source_url": it["url"], "source_type": "company_filing",
                     "source_label": f"{company} 8-K filed {it['filed']} (SEC EDGAR)"})
    return rows


def ingest_sec_pdufa(lookback_days: int = 540, max_docs: int = 250) -> dict:
    """PDUFA dates companies announced in 8-K exhibits (press releases).
    Each filing is downloaded and parsed once (cached in portal_sec_seen); the PDUFA table is
    rebuilt from the cache every run so the newest statement per product always wins."""
    init()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    hits, errors = {}, []
    for ph in _PDUFA_PHRASES:
        try:
            for h in _sec_search(ph, start, end):
                hits[h["_id"]] = h
        except Exception as e:
            errors.append(f"search {ph}: {e}")
        time.sleep(0.25)
    ordered = sorted(hits.values(), key=lambda h: h["_source"].get("file_date", ""), reverse=True)
    stats = {"hits": len(ordered), "processed": 0, "ai_mode": bool(settings.anthropic_api_key),
             "skipped_seen": 0, "errors": errors}
    ai_on = bool(settings.anthropic_api_key)
    with store._engine.connect() as c:
        seen = set()
        for rid, res in c.execute(select(sec_seen.c.id, sec_seen.c.result)):
            # once an API key is configured, filings read without AI are read again with AI
            if ai_on and '"mode": "regex"' in (res or "") and '"items": []' not in (res or ""):
                continue
            seen.add(rid)
    for h in ordered:
        if stats["processed"] >= max_docs:
            break
        hid = h["_id"]
        if hid in seen:
            stats["skipped_seen"] += 1
            continue
        src = h["_source"]
        try:
            adsh, fn = hid.split(":", 1)
            cik = (src.get("ciks") or ["0"])[0].lstrip("0")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fn}"
            text = clean_exhibit(_html_text(_http_get(url, timeout=60).text))
        except Exception as e:
            stats["errors"].append(f"{hid}: {e}")
            continue
        stats["processed"] += 1
        time.sleep(0.15)   # SEC fair access: well under 10 requests/second
        disp = (src.get("display_names") or [""])[0]
        cm = re.match(r"^(.*?)\s*\(([A-Z0-9.\-, ]+)\)\s*\(CIK", disp)
        company = (cm.group(1) if cm else disp.split("(CIK")[0]).strip()
        company = company.title() if company.isupper() else company
        ticker = cm.group(2).split(",")[0].strip() if cm else ""
        found = extract_pdufa_regex(text)
        items = []
        ai = _ai_extract(text) if found else None
        if ai:
            flat = text.replace(".", "")
            for it in ai:
                dm = _DATE_RE.search(it.get("pdufa_date_text") or "")
                iso = _to_iso(*dm.groups()) if dm else None
                # anti-hallucination: the date must literally appear in the filing
                if iso and dm.group(0).replace(".", "") in flat:
                    items.append({"date": iso, "drug": (it.get("drug") or "").strip()[:80],
                                  "inn": (it.get("inn") or "").strip()[:80],
                                  "indication": (it.get("indication") or "").strip()[:120],
                                  "application": (it.get("application") or "").strip()[:10],
                                  "status": (it.get("status") or "").strip()[:20],
                                  "sentence": pdufa_sentence(text, dm.group(0))})
        if not items:
            for d_ in sorted({f["date"] for f in found}):
                dt = next(f["date_text"] for f in found if f["date"] == d_)
                items.append({"date": d_, "drug": "", "inn": "", "indication": "", "application": "",
                              "status": "", "sentence": pdufa_sentence(text, dt)})
        rec = {"company": company, "ticker": ticker, "filed": src.get("file_date", ""), "url": url,
               "headline": extract_headline(text), "items": items, "mode": "ai" if ai else "regex"}
        with store._engine.begin() as c:
            c.execute(delete(sec_seen).where(sec_seen.c.id == hid))
            c.execute(insert(sec_seen).values(id=hid, result=json.dumps(rec), seen_at=_now()))
    with store._engine.connect() as c:
        cached = []
        for (res,) in c.execute(select(sec_seen.c.result)):
            try:
                r = json.loads(res or "{}")
                if r.get("items"):
                    cached.append(r)
            except Exception:
                pass
    rows = _resolve_pdufa(cached)
    stats["filings_with_dates"] = len(cached)
    stats["milestones"] = _replace_kinds(["pdufa"], rows)
    return stats


# --- FDA advisory committees (Federal Register) ------------------------------

FEDREG = "https://www.federalregister.gov/api/v1/documents.json"


def ingest_fda_adcomm(days_back: int = 120) -> dict:
    """FDA advisory committee meeting notices from the Federal Register API."""
    init()
    from urllib.parse import urlencode
    params = [("conditions[agencies][]", "food-and-drug-administration"),
              ("conditions[type][]", "NOTICE"),
              ("conditions[term]", '"advisory committee" "notice of meeting"'),
              ("conditions[publication_date][gte]", (date.today() - timedelta(days=days_back)).isoformat()),
              ("order", "newest"), ("per_page", "100")]
    for f in ("title", "publication_date", "html_url", "abstract", "dates", "document_number"):
        params.append(("fields[]", f))
    j = _http_get(f"{FEDREG}?{urlencode(params)}", timeout=60).json()
    ms = []
    for d in j.get("results", []) or []:
        title = _clean(d.get("title"))
        if "advisory committee" not in title.lower():
            continue
        text = _clean(d.get("dates") or "") or _clean(d.get("abstract") or "")
        dm = _DATE_RE.search(text)
        if not dm:
            continue
        iso = _to_iso(*dm.groups())
        if not iso:
            continue
        committee = re.sub(r";?\s*Notice of Meeting.*$", "", title, flags=re.I).strip()
        ms.append({"id": _mid("adcomm", d.get("document_number"), iso), "date": iso, "region": "US",
                   "kind": "adcomm", "title": committee or title,
                   "detail": (_clean(d.get("abstract")) or text)[:600], "drug_name": None, "drug_id": None,
                   "inn": None, "company": None, "company_slug": None, "impact": "high",
                   "source_url": d.get("html_url"), "source_type": "official",
                   "source_label": f"Federal Register notice {d.get('document_number')} ({d.get('publication_date')})"})
    return {"notices": len(j.get("results", []) or []), "milestones": _replace_kinds(["adcomm"], ms)}


# --- runner -----------------------------------------------------------------

INGESTERS = [
    ("ema_medicines", ingest_ema_medicines),
    ("ema_referrals", ingest_ema_referrals),
    ("ema_dhpc", ingest_ema_dhpc),
    ("ema_shortages", ingest_ema_shortages),
    ("fda_approvals", ingest_fda_approvals),
    ("fda_adcomm", ingest_fda_adcomm),
    ("sec_pdufa", ingest_sec_pdufa),
]


def run_all(only: list[str] | None = None) -> dict:
    store.init()
    init()
    out = {}
    for name, fn in INGESTERS:
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            out[name] = fn()
        except Exception as e:
            log.exception("portal ingest %s failed", name)
            out[name] = {"error": f"{type(e).__name__}: {e}"}
        out[name]["seconds"] = round(time.time() - t0, 1) if isinstance(out[name], dict) else None
    return out


# --- queries ----------------------------------------------------------------

def _rows(stmt) -> list[dict]:
    with store._engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(stmt)]


def milestones_between(start: str | None = None, end: str | None = None, kinds: list[str] | None = None,
                       region: str | None = None, limit: int = 500, desc: bool = False) -> list[dict]:
    s = select(milestones)
    if start:
        s = s.where(milestones.c.date >= start)
    if end:
        s = s.where(milestones.c.date <= end)
    if kinds:
        s = s.where(milestones.c.kind.in_(kinds))
    if region:
        s = s.where(milestones.c.region == region)
    s = s.order_by(milestones.c.date.desc() if desc else milestones.c.date.asc()).limit(limit)
    return _rows(s)


def drug_get(region: str, slug: str) -> dict | None:
    r = _rows(select(drugs).where(drugs.c.id == f"{region.lower()}-{slug}"))
    return r[0] if r else None


def drugs_by_inn(inn: str, exclude_id: str = "") -> list[dict]:
    if not inn:
        return []
    first = inn.split(",")[0].strip().lower()
    if len(first) < 4:
        return []
    return _rows(select(drugs).where(func.lower(drugs.c.inn).like(f"%{first}%"))
                 .where(drugs.c.id != exclude_id).limit(12))


def milestones_for_drug(d: dict) -> list[dict]:
    conds = [milestones.c.drug_id == d["id"]]
    if d.get("name"):
        conds.append(func.lower(milestones.c.drug_name) == d["name"].lower())
    return _rows(select(milestones).where(or_(*conds)).order_by(milestones.c.date.desc()).limit(60))


def company_drugs(cslug: str) -> list[dict]:
    return _rows(select(drugs).where(drugs.c.company_slug == cslug)
                 .order_by(drugs.c.decision_date.desc()).limit(400))


def company_milestones(cslug: str) -> list[dict]:
    return _rows(select(milestones).where(milestones.c.company_slug == cslug)
                 .order_by(milestones.c.date.desc()).limit(200))


def companies_with_activity(limit: int = 5000) -> list[str]:
    with store._engine.connect() as c:
        return [r[0] for r in c.execute(select(drugs.c.company_slug).distinct()
                                        .where(drugs.c.company_slug.is_not(None)).limit(limit))]


def all_drug_ids(limit: int = 20000) -> list[tuple[str, str, str]]:
    with store._engine.connect() as c:
        return [(r[0], r[1], r[2] or "") for r in c.execute(
            select(drugs.c.region, drugs.c.slug, drugs.c.updated_at).limit(limit))]


def counts() -> dict:
    with store._engine.connect() as c:
        d = dict(c.execute(select(drugs.c.region, func.count()).group_by(drugs.c.region)).all())
        m = dict(c.execute(select(milestones.c.kind, func.count()).group_by(milestones.c.kind)).all())
    return {"drugs": d, "milestones": m}


def analysis_get(aid: str) -> dict | None:
    r = _rows(select(analyses).where(analyses.c.id == aid))
    return r[0] if r else None


def analysis_put(aid: str, facts_hash: str, body: str, model: str) -> None:
    with store._engine.begin() as c:
        c.execute(delete(analyses).where(analyses.c.id == aid))
        c.execute(insert(analyses).values(id=aid, facts_hash=facts_hash, body=body, model=model,
                                          created_at=_now()))
