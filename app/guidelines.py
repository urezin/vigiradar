"""
Guidelines tracker + extra safety notices, stored in portal_news.

kind="guideline"  new / draft / final guidance from FDA (Federal Register notices of
                  availability, incl. ICH guidelines FDA adopts), EMA (scientific and
                  regulatory-procedural guideline feeds, public consultations; these carry the
                  ICH texts adopted in the EU) and MHRA (GOV.UK guidance).
kind="notice"     official safety notices not covered by the radar: EMA "what's new" PRAC /
                  signal / DHPC documents, FDA drug recalls (openFDA enforcement reports),
                  Health Canada health-product alerts.

Texts shown are the agencies' own public abstracts (US federal works are public domain;
EMA and GOV.UK allow reuse with attribution), always linked to the source.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta

from . import news as nw

FEDREG = "https://www.federalregister.gov/api/v1/documents.json"
EMA_GUIDE_FEEDS = [
    ("ema-sci", "https://www.ema.europa.eu/en/scientific-guidelines.xml"),
    ("ema-reg", "https://www.ema.europa.eu/en/regulatory-and-procedural-guideline.xml"),
    ("ema-cons", "https://www.ema.europa.eu/en/public-consultations.xml"),
]
EMA_WHATSNEW = "https://www.ema.europa.eu/en/whats-new.xml"
MHRA_GUIDANCE = ("https://www.gov.uk/search/guidance-and-regulation.atom?"
                 "organisations%5B%5D=medicines-and-healthcare-products-regulatory-agency")
HC_ALERTS = "https://recalls-rappels.canada.ca/en/feed/health-products-alerts-recalls"
OPENFDA_RECALLS = "https://api.fda.gov/drug/enforcement.json"

_ICH_CODE = re.compile(r"\b(ICH|E\d{1,2}[A-Z]?\(R\d\)|M\d{1,2}|Q\d{1,2}[A-Z]?(\(R\d\))?|S\d{1,2}[A-Z]?)\b")
_HONEST_UA = {"User-Agent": "VigiEye regulatory monitor (+https://vigi-eye.com; info@vigi-eye.com)",
              "Accept": "application/json, application/rss+xml, application/atom+xml, application/xml, */*"}


def _get(url: str, timeout: float = 60):
    import httpx
    with httpx.Client(timeout=timeout, headers=_HONEST_UA, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        return r


# FDA also regulates food, cosmetics and tobacco; keep medicines, biologics, devices, animal drugs
_FDA_SKIP = ("seafood", "food", "dietary", "cosmetic", "tobacco", "nicotine", "infant formula", "juice",
             "allergen", "menu labeling", "produce safety", "egg", "milk", "beverage", "pet food", "feed")


def _status(title: str, text: str = "") -> str:
    t = f"{title} {text}".lower()
    if "concept paper" in t:
        return "concept paper"
    if "consultation" in t or "draft" in t or "comment" in t:
        return "draft"
    if "revision" in t or "revised" in t or "update" in t:
        return "revised"
    return "final"


def _is_ich(title: str, text: str = "") -> bool:
    t = f"{title} {text}"
    return "International Council for Harmonisation" in t or bool(re.search(r"\bICH\b", t))


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    import html
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _add(row: dict) -> bool:
    return nw._upsert({"country": None, "company": None, "ticker": None, "summary": None, **row})


def _tags(*t) -> str:
    import json
    return json.dumps([x for x in t if x])


# --- guidelines -------------------------------------------------------------------

def ingest_fda_guidance(days: int = 180) -> dict:
    from urllib.parse import urlencode
    nw.init()
    params = [("conditions[agencies][]", "food-and-drug-administration"), ("conditions[type][]", "NOTICE"),
              ("conditions[term]", "guidance availability"),
              ("conditions[publication_date][gte]", (date.today() - timedelta(days=days)).isoformat()),
              ("order", "newest"), ("per_page", "200")]
    for f in ("title", "publication_date", "html_url", "abstract", "document_number"):
        params.append(("fields[]", f))
    j = _get(f"{FEDREG}?{urlencode(params)}").json()
    new = 0
    for d in j.get("results", []) or []:
        title = _clean(d.get("title"))
        tl = title.lower()
        if "guidance" not in tl or any(x in tl for x in _FDA_SKIP):
            continue
        abstract = _clean(d.get("abstract"))
        ich = _is_ich(title, abstract)
        st = "draft" if "draft guidance" in title.lower() else ("revised" if "revision" in title.lower() else "final")
        clean_title = re.sub(r";\s*(Draft\s+)?Guidance for .*?;\s*Availability$|;\s*Availability$", "", title).strip()
        if _add({"id": nw._id("fdaguid", d.get("document_number")), "kind": "guideline",
                 "section": nw.classify(clean_title, abstract, "regulatory"),
                 "publisher": "FDA · ICH" if ich else "FDA", "country": "US", "title": clean_title[:300],
                 "summary": abstract[:1200] or None, "url": d.get("html_url"), "date": d.get("publication_date"),
                 "tags": _tags(st, "ICH" if ich else "", "Federal Register")}):
            new += 1
    return {"notices": len(j.get("results", []) or []), "new": new}


def _feed(url: str):
    import feedparser
    return feedparser.parse(_get(url).content).entries


def _edate(e) -> str:
    return nw._date_of(e)


def ingest_ema_guidelines() -> dict:
    nw.init()
    new, n = 0, 0
    for fid, url in EMA_GUIDE_FEEDS:
        for e in _feed(url):
            n += 1
            title = _clean(getattr(e, "title", ""))
            link = (getattr(e, "link", "") or "").strip()
            if not title or not link:
                continue
            desc = _clean(getattr(e, "summary", "") or getattr(e, "description", ""))[:900]
            ich = _is_ich(title, desc)
            st = "consultation" if fid == "ema-cons" else _status(title, desc)
            if _add({"id": nw._id("emaguid", link), "kind": "guideline",
                     "section": nw.classify(title, desc, "regulatory"), "publisher": "EMA · ICH" if ich else "EMA",
                     "country": "EU", "title": title[:300], "summary": desc or None, "url": link, "date": _edate(e),
                     "tags": _tags(st, "ICH" if ich else "", "public consultation" if fid == "ema-cons" else "")}):
                new += 1
    return {"items": n, "new": new}


def ingest_mhra_guidance() -> dict:
    nw.init()
    new, n = 0, 0
    for e in _feed(MHRA_GUIDANCE):
        n += 1
        title = _clean(getattr(e, "title", ""))
        link = (getattr(e, "link", "") or "").strip()
        desc = _clean(getattr(e, "summary", ""))[:900]
        if title and link and _add({"id": nw._id("mhraguid", link), "kind": "guideline",
                                    "section": nw.classify(title, desc, "regulatory"), "publisher": "MHRA",
                                    "country": "UK", "title": title[:300], "summary": desc or None, "url": link,
                                    "date": _edate(e), "tags": _tags(_status(title, desc), "GOV.UK")}):
            new += 1
    return {"items": n, "new": new}


# --- safety notices --------------------------------------------------------------------

_PRAC_KEEP = ("prac ", " prac", "signal", "dhpc", "direct healthcare professional communication", "psusa", "periodic safety",
              "referral", "pharmacovigilance", "safety", "risk minimisation", "product information")


def ingest_ema_prac() -> dict:
    """EMA 'what's new': keep PRAC / signal / DHPC / safety documents."""
    nw.init()
    new, n, kept = 0, 0, 0
    for e in _feed(EMA_WHATSNEW):
        n += 1
        title = _clean(getattr(e, "title", ""))
        link = (getattr(e, "link", "") or "").strip()
        tl = title.lower()
        if not title or not link or not any(k in tl for k in _PRAC_KEEP):
            continue
        if re.search(r"good (manufacturing|distribution|clinical) practice|\bgmp\b|\bgdp\b", tl):
            continue          # 'practice' contains 'prac'; GMP/GDP items are quality, not PRAC
        if any(x in tl for x in ("agenda", "minutes", "presentation", "webinar", "vacancy", "information session",
                                 "online,", "since september 2012", "executive steering group", "training", "workshop")):
            continue
        kept += 1
        desc = _clean(getattr(e, "summary", ""))[:700]
        if re.match(r"^PSUSA/\d+/\d+$", title):
            title = f"PSUSA outcome published: {title}"
        if _add({"id": nw._id("emaprac", link), "kind": "notice", "section": "pharmacovigilance",
                 "publisher": "EMA · PRAC" if re.search(r"\bprac\b|\bsignals?\b", tl) else "EMA", "country": "EU",
                 "title": title[:300], "summary": desc or None, "url": link, "date": _edate(e),
                 "tags": _tags("PRAC" if "prac" in tl else "", "signal" if "signal" in tl else "")}):
            new += 1
    return {"items": n, "kept": kept, "new": new}


def ingest_fda_recalls(days: int = 45) -> dict:
    nw.init()
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    j = _get(f"{OPENFDA_RECALLS}?search=report_date:[{start}+TO+{end}]+AND+classification:(%22Class+I%22+%22Class+II%22)"
             f"&sort=report_date:desc&limit=100").json()
    new = 0
    for r in j.get("results", []) or []:
        cls = r.get("classification", "")
        prod = _clean(r.get("product_description"))[:140]
        firm = _clean(r.get("recalling_firm")).title()
        d = r.get("report_date", "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else date.today().isoformat()
        reason = _clean(r.get("reason_for_recall"))[:600]
        title = f"{cls} drug recall: {prod.split(',')[0][:90]} ({firm})"
        if _add({"id": nw._id("fdarecall", r.get("recall_number")), "kind": "notice", "section": "quality",
                 "publisher": "FDA enforcement report", "country": "US", "title": title[:300],
                 "summary": f"{reason} Recall {r.get('recall_number')}; status {r.get('status')}; "
                            f"distribution: {_clean(r.get('distribution_pattern'))[:160]}.",
                 "url": "https://www.accessdata.fda.gov/scripts/ires/index.cfm", "date": d,
                 "tags": _tags(cls, "recall")}):
            new += 1
    return {"results": len(j.get("results", []) or []), "new": new}


def ingest_hc_alerts() -> dict:
    nw.init()
    new, n = 0, 0
    for e in _feed(HC_ALERTS):
        n += 1
        title = _clean(getattr(e, "title", ""))
        link = (getattr(e, "link", "") or "").strip()
        if title and link and _add({"id": nw._id("hc", link), "kind": "notice",
                                    "section": nw.classify(title, getattr(e, "summary", "") or "", "quality"),
                                    "publisher": "Health Canada", "country": "CA", "title": title[:300],
                                    "summary": _clean(getattr(e, "summary", ""))[:600] or None, "url": link,
                                    "date": _edate(e), "tags": _tags("alert")}):
            new += 1
    return {"items": n, "new": new}


def run_all() -> dict:
    out = {}
    for name, fn in (("fda_guidance", ingest_fda_guidance), ("ema_guidelines", ingest_ema_guidelines),
                     ("mhra_guidance", ingest_mhra_guidance), ("ema_prac", ingest_ema_prac),
                     ("fda_recalls", ingest_fda_recalls), ("hc_alerts", ingest_hc_alerts)):
        t0 = time.time()
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"}
        out[name]["seconds"] = round(time.time() - t0, 1)
    return out
