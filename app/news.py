"""
VigiEye news layer: the pharma regulatory news portal.

Three kinds of item, shown together and sorted into four sections
(Pharmacovigilance, Regulatory, Market access, Compliance) plus Industry:

  official  regulator / HTA / enforcement notices (EMA, FDA, MHRA, HMA, EC, WHO, G-BA,
            HAS, ICER, DOJ, national agencies). Stored in the existing `updates` table by
            the radar ingest and summarised in VigiEye's own words.
  company   company press releases filed with the SEC (8-K exhibits) about approvals,
            CHMP opinions, CRLs, designations, reimbursement, warning letters. Stored here
            with VigiEye's own AI summary and a link to the filing.
  press     trade-press headlines (BioPharma Dive, Fierce, Endpoints, STAT...). Headline,
            publisher, date and link ONLY. No summary or text is copied.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, String, Table, Text, delete, insert, or_, select

from . import store
from .config import settings

log = logging.getLogger("vigiradar.news")
_meta = MetaData()

news = Table(
    "portal_news", _meta,
    Column("id", String(24), primary_key=True),
    Column("kind", String(10), nullable=False),          # company | press
    Column("section", String(20), nullable=False),
    Column("publisher", String(120), nullable=False),
    Column("country", String(8)),
    Column("title", Text, nullable=False),
    Column("summary", Text),                             # company only (VigiEye's words)
    Column("url", Text, nullable=False),
    Column("date", String(10), nullable=False),
    Column("company", Text),
    Column("ticker", String(16)),
    Column("tags", Text),                                # json list
    Column("ingested_at", String(40), nullable=False),
)

SECTIONS = {
    "regulatory": "Regulatory",
    "pharmacovigilance": "Pharmacovigilance",
    "quality": "Quality & compliance",
    "market-access": "Market access",
    "industry": "Industry",
}
SECTION_BLURB = {
    "pharmacovigilance": "Safety signals, PRAC recommendations, DHPCs, Drug Safety Update, MedWatch and risk minimisation.",
    "regulatory": "Approvals, CHMP opinions, FDA decisions, guidelines, designations and regulatory policy.",
    "market-access": "HTA, pricing and reimbursement: EU HTA Regulation, G-BA, HAS, NICE, ICER, IRA and payers.",
    "quality": "GMP and GDP, inspections, quality-defect recalls, warning letters, data integrity and enforcement.",
    "industry": "Deals, pipeline readouts and company news from across biotech and pharma.",
}
SECTION_ALIAS = {"compliance": "quality"}


def norm_section(s: str | None) -> str:
    s = s or "industry"
    return SECTION_ALIAS.get(s, s)

_KW = {
    "pharmacovigilance": ("pharmacovigilance", "safety signal", "signal", "prac", "dhpc", "drug safety update",
                          "adverse", "side effect", "psur", "risk management plan", "rmp", "pass ", "boxed warning",
                          "safety communication", "medwatch", "risk minimi", "contraindicat", "vigilance",
                          "suspected", "#medsafetyweek", "black box", "safety review",
                          "pharmakovigilanz", "farmacovigilanza", "pharmacovigilance", "sécurité", "seguridad"),
    "market-access": ("reimburs", "pricing", "price", "hta", "health technology assessment", "joint clinical assessment",
                      "g-ba", "iqwig", "amnog", "nutzenbewertung", "has ", "haute autorité", "nice ", "icer",
                      "payer", "formulary", "medicare negotiation", "inflation reduction act", " ira ", "rebate",
                      "cost-effectiveness", "value assessment", "market access", "tender", "eunethta", "pbm",
                      "most favored nation", "mfn", "tariff", "remboursement", "prix"),
    "quality": ("gmp", "good manufacturing", "good distribution", "gdp ", "quality defect", "recall", "field safety",
                "contaminat", "sterility", "out of specification", "batch", "lot ", "nitrosamine", "impurit",
                "qualified person", "manufacturing site", "inspection", "warning letter", "form 483", "483",
                   "enforcement", "settle", "fraud", "kickback", "false claims", "sentenced", "indicted", "pleaded",
                   "guilty", "fine", "penalt", "doj", "oig", "data integrity", "non-compliance", "noncompliance",
                   "falsified", "counterfeit", "promotion", "off-label", "sunshine", "transparency", "anti-bribery",
                   "whistleblower", "consent decree", "import alert", "compliance"),
    "regulatory": ("approv", "fda", "ema", "chmp", "opinion", "authoris", "authoriz", "guideline", "guidance",
                   "designation", "breakthrough", "fast track", "orphan", "priority review", "pdufa", "complete response",
                   "crl", "label", "indication", "submission", "bla", "nda", "maa", "clinical trial", "regulation",
                   "mhra", "commission", "variation", "biosimilar", "accelerated", "conditional", "ctis", "ich"),
}
_AUTH_SECTION = {"mhra drug safety update": "pharmacovigilance", "mhra alerts": "quality",
                 "fda medwatch": "pharmacovigilance", "g-ba": "market-access", "has": "market-access",
                 "icer": "market-access", "us department of justice": "quality"}
_SUBJECT_SECTION = {"Signal management": "pharmacovigilance", "PSUR / PBRER": "pharmacovigilance",
                    "RMP & PASS": "pharmacovigilance", "Shortages & recalls": "quality",
                    "GVP modules": "pharmacovigilance", "Market access": "market-access",
                    "Compliance & enforcement": "quality", "Falsified medicines": "quality",
                    "Labelling & SmPC": "regulatory", "Clinical trials (CTR)": "regulatory",
                    "Fees & guidance": "regulatory"}


def classify(title: str, text: str = "", default: str = "industry") -> str:
    t = f" {title} {text} ".lower()
    scores = {k: sum(1 for w in words if w in t) for k, words in _KW.items()}
    # title hits count double
    tl = f" {title} ".lower()
    for k, words in _KW.items():
        scores[k] += sum(1 for w in words if w in tl)
    best = max(scores, key=lambda k: (scores[k], k != "regulatory"))
    return best if scores[best] > 0 else default


def section_for_update(r: dict) -> str:
    a = (r.get("authority") or "").lower()
    if a in _AUTH_SECTION:
        return _AUTH_SECTION[a]
    by_kw = classify(r.get("title", ""), r.get("summary", ""), "")
    subj = _SUBJECT_SECTION.get(r.get("subject") or "", "regulatory")
    # keyword evidence beats the coarse subject, except PV subjects which are reliable
    if subj == "pharmacovigilance" and r.get("subject") in ("Signal management", "PSUR / PBRER", "RMP & PASS"):
        return subj
    if by_kw:
        return by_kw
    # no topical evidence: a source's default subject is not proof of a safety story
    return "regulatory" if subj == "pharmacovigilance" else subj


_JUNK_TITLES = re.compile(r"^(participaci[oó]n p[uú]blica|campa[nñ]as institucionales|notas informativas|"
                          r"agenda|newsletter|suscr|rss|contact|home|inicio|accueil|actualit[eé]s)\b", re.I)


def is_junk(r: dict) -> bool:
    t = (r.get("title") or "").strip()
    return len(t.split()) < 4 or bool(_JUNK_TITLES.match(t))


def init() -> None:
    _meta.create_all(store._engine)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def _upsert(row: dict) -> bool:
    with store._engine.begin() as c:
        if c.execute(select(news.c.id).where(news.c.id == row["id"])).first():
            return False
        c.execute(insert(news).values(**{**row, "ingested_at": _now()}))
    return True


# --- trade press headlines ------------------------------------------------------

PRESS_FEEDS = [
    ("biopharmadive", "BioPharma Dive", "https://www.biopharmadive.com/feeds/news/"),
    ("fiercepharma", "Fierce Pharma", "https://www.fiercepharma.com/rss/xml"),
    ("fiercebiotech", "Fierce Biotech", "https://www.fiercebiotech.com/rss/xml"),
    ("endpoints", "Endpoints News", "https://endpts.com/feed/"),
    ("statnews", "STAT", "https://www.statnews.com/feed/"),
    ("pharmatimes", "PharmaTimes", "https://pharmatimes.com/feed/"),
    ("pharmaphorum", "pharmaphorum", "https://pharmaphorum.com/rss.xml"),
    ("pmlive", "PMLiVE", "https://pmlive.com/feed/"),
    ("valueinhealth", "Value in Health (ISPOR)", "https://www.valueinhealthjournal.com/current.rss"),
]
_FORCE_SECTION = {"valueinhealth": "market-access"}

_UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36 VigiEye/0.2 (+https://vigi-eye.com)"),
       "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}

# STAT covers broad health policy; keep its biopharma items only
_STAT_KEEP = ("fda", "drug", "biotech", "pharma", "trial", "vaccine", "approval", "cms", "medicare", "price",
              "ema", "gene", "cell therapy", "obesity", "glp-1", "cancer")


def _date_of(entry) -> str:
    for k in ("published_parsed", "updated_parsed"):
        v = getattr(entry, k, None) or (entry.get(k) if isinstance(entry, dict) else None)
        if v:
            try:
                return date(v.tm_year, v.tm_mon, v.tm_mday).isoformat()
            except Exception:
                pass
    return date.today().isoformat()


def ingest_press(max_per_feed: int = 40) -> dict:
    import feedparser
    import httpx
    init()
    stats = {"feeds": 0, "new": 0, "errors": []}
    with httpx.Client(timeout=30, headers=_UA, follow_redirects=True) as client:
        for fid, publisher, url in PRESS_FEEDS:
            try:
                parsed = feedparser.parse(client.get(url).content)
            except Exception as e:
                stats["errors"].append(f"{fid}: {e}")
                continue
            stats["feeds"] += 1
            for e in parsed.entries[:max_per_feed]:
                title = re.sub(r"\s+", " ", (getattr(e, "title", "") or "")).strip()
                link = (getattr(e, "link", "") or "").strip()
                if not title or not link.startswith("http"):
                    continue
                desc = re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or "")
                if fid == "statnews" and not any(k in f"{title} {desc}".lower() for k in _STAT_KEEP):
                    continue
                if _upsert({"id": _id("press", link), "kind": "press",
                            "section": _FORCE_SECTION.get(fid) or classify(title, desc[:400], "industry"),
                            "publisher": publisher,
                            "country": None, "title": title[:300], "summary": None, "url": link,
                            "date": _date_of(e), "company": None, "ticker": None, "tags": None}):
                    stats["new"] += 1
    return stats


# --- ISPOR (HEOR / market access) ------------------------------------------------------

ISPOR_PAGES = ["https://www.ispor.org/", "https://www.ispor.org/heor-resources/news-top"]
_ISPOR_LINK = re.compile(r'<a[^>]+href="((?:https://www\.ispor\.org)?/heor-resources/news-top/news/view/'
                         r'(\d{4})/(\d{2})/(\d{2})/[^"]+)"[^>]*>(.*?)</a>', re.S)


def ingest_ispor() -> dict:
    """ISPOR's own HEOR news items (headline + link; ISPOR has no RSS feed)."""
    import html as _h
    import httpx
    init()
    stats = {"found": 0, "new": 0, "errors": []}
    seen = set()
    with httpx.Client(timeout=30, headers={"User-Agent": "VigiEye regulatory monitor (+https://vigi-eye.com)"},
                      follow_redirects=True) as client:
        for page in ISPOR_PAGES:
            try:
                body = client.get(page).text
            except Exception as e:
                stats["errors"].append(f"{page}: {e}")
                continue
            for href, y, m, d, inner in _ISPOR_LINK.findall(body):
                url = href if href.startswith("http") else "https://www.ispor.org" + href
                title = re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
                if url in seen or len(title) < 15:
                    continue
                seen.add(url)
                stats["found"] += 1
                if _upsert({"id": _id("ispor", url), "kind": "press", "section": "market-access", "publisher": "ISPOR",
                            "country": "INT", "title": title[:300], "summary": None, "url": url,
                            "date": f"{y}-{m}-{d}", "company": None, "ticker": None, "tags": None}):
                    stats["new"] += 1
    return stats


# --- company announcements from SEC filings -----------------------------------------

SEC_FTS = "https://efts.sec.gov/LATEST/search-index"
_SEC_QUERIES = ['"FDA approval"', '"approved by the U.S. Food and Drug Administration"', '"Complete Response Letter"',
                '"CHMP"', '"European Commission" approval', '"marketing authorisation"', '"Breakthrough Therapy designation"',
                '"Fast Track designation"', '"Orphan Drug Designation"', '"Priority Review"', '"accelerated approval"',
                '"clinical hold"', '"warning letter"', '"Form 483"', '"reimbursement" FDA', '"label expansion"',
                '"Refuse to File"', '"Advisory Committee"']

_CO_PROMPT = """Below is a press release a pharma/biotech company filed with the SEC. Write a short news brief
for regulatory, pharmacovigilance, market-access and compliance professionals.

Return ONLY JSON: {{"headline":"factual news headline, max 110 chars, in your own words",
"summary":"2-3 sentences in your own words: what happened, product and indication, regulator/body, date",
"section":"one of pharmacovigilance|regulatory|market-access|quality|industry",
"newsworthy":true or false (false for routine financing, earnings with nothing regulatory, executive hires)}}.
Use only facts in the text. Do not copy sentences from it.

TEXT:
{text}"""


def _ai_brief(text: str) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(model=settings.anthropic_model, max_tokens=500,
                                     messages=[{"role": "user", "content": _CO_PROMPT.format(text=text[:9000])}])
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:
        log.warning("[news] AI brief failed: %s", e)
        return None


def ingest_company(days: int = 4, max_docs: int = 60) -> dict:
    """Company press releases (8-K EX-99) mentioning regulatory events, last `days` days."""
    from urllib.parse import urlencode
    from . import portal_data as pd
    init()
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    hits = {}
    errors = []
    for q in _SEC_QUERIES:
        try:
            qs = urlencode({"q": q, "forms": "8-K", "dateRange": "custom", "startdt": start, "enddt": end})
            j = pd._http_get(f"{SEC_FTS}?{qs}", timeout=60).json()
            for h in (j.get("hits") or {}).get("hits") or []:
                src_ = h["_source"]
                desc_ = f'{src_.get("file_description") or ""} {h["_id"]}'.lower()
                if (src_.get("file_type") or "").upper().startswith("EX-99") and \
                        not any(x in desc_ for x in ("presentation", "deck", "slides")):
                    hits[h["_id"]] = h
        except Exception as e:
            errors.append(f"{q}: {e}")
        time.sleep(0.25)
    stats = {"hits": len(hits), "new": 0, "skipped": 0, "not_newsworthy": 0, "ai": bool(settings.anthropic_api_key),
             "errors": errors}
    with store._engine.connect() as c:
        have = {r[0] for r in c.execute(select(news.c.id).where(news.c.kind == "company"))}
    done = 0
    for hid, h in sorted(hits.items(), key=lambda kv: kv[1]["_source"].get("file_date", ""), reverse=True):
        nid = _id("co", hid)
        if nid in have:
            stats["skipped"] += 1
            continue
        if done >= max_docs:
            break
        src = h["_source"]
        try:
            adsh, fn = hid.split(":", 1)
            cik = (src.get("ciks") or ["0"])[0].lstrip("0")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fn}"
            text = pd.clean_exhibit(pd._html_text(pd._http_get(url, timeout=60).text))
        except Exception as e:
            stats["errors"].append(f"{hid}: {e}")
            continue
        done += 1
        time.sleep(0.15)
        disp = (src.get("display_names") or [""])[0]
        cm = re.match(r"^(.*?)\s*\(([A-Z0-9.\-, ]+)\)\s*\(CIK", disp)
        company = (cm.group(1) if cm else disp.split("(CIK")[0]).strip()
        company = company.title() if company.isupper() else company
        ticker = cm.group(2).split(",")[0].strip() if cm else ""
        headline = pd.extract_headline(text)
        brief = _ai_brief(text)
        if brief is not None and not brief.get("newsworthy", True):
            stats["not_newsworthy"] += 1
            with store._engine.begin() as c:   # remember it so it isn't fetched again
                c.execute(insert(news).values(id=nid, kind="skip", section="industry", publisher=company,
                                              title=headline[:300] or "-", url=url, date=src.get("file_date", end),
                                              ingested_at=_now()))
            continue
        if brief:
            title = (brief.get("headline") or headline)[:300]
            summary = (brief.get("summary") or "")[:900]
            section = norm_section(brief.get("section"))
            section = section if section in SECTIONS else classify(headline, text[:1500])
        else:
            # no AI: only clean headlines that clearly announce a regulatory event
            title, summary, section = headline[:300], None, classify(headline, text[:1500])
            hl = headline.lower()
            if (len(headline) < 30 or "contact" in hl or "financial results" in hl or "quarter" in hl
                    or not re.search(r"fda|approv|chmp|pdufa|designation|ema\b|european commission|"
                                     r"marketing authori[sz]ation|complete response|clinical hold|warning letter|"
                                     r"reimburse|priority review|breakthrough|fast track|orphan", hl)):
                with store._engine.begin() as c:
                    c.execute(insert(news).values(id=nid, kind="skip", section="industry", publisher=company,
                                                  title=headline[:300] or "-", url=url,
                                                  date=src.get("file_date", end), ingested_at=_now()))
                stats["not_newsworthy"] += 1
                continue
        if not title:
            continue
        _upsert({"id": nid, "kind": "company", "section": section, "publisher": company, "country": "US",
                 "title": title, "summary": summary, "url": url, "date": src.get("file_date", end),
                 "company": company, "ticker": ticker, "tags": None})
        stats["new"] += 1
    return stats


# --- queries --------------------------------------------------------------------

def latest(section: str | None = None, kinds: list[str] | None = None, limit: int = 30, since: str | None = None,
           offset: int = 0) -> list[dict]:
    init()
    s = select(news).where(news.c.kind != "skip")
    if section:
        s = s.where(news.c.section == section)
    if kinds:
        s = s.where(news.c.kind.in_(kinds))
    if since:
        s = s.where(news.c.date >= since)
    s = s.order_by(news.c.date.desc(), news.c.ingested_at.desc()).offset(offset).limit(limit)
    with store._engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(s)]


def get(nid: str) -> dict | None:
    init()
    with store._engine.connect() as c:
        r = c.execute(select(news).where(news.c.id == nid)).first()
        return dict(r._mapping) if r else None


def run_all() -> dict:
    out = {}
    for name, fn in (("press", ingest_press), ("ispor", ingest_ispor), ("company", ingest_company)):
        t0 = time.time()
        try:
            out[name] = fn()
        except Exception as e:
            log.exception("news ingest %s failed", name)
            out[name] = {"error": f"{type(e).__name__}: {e}"}
        out[name]["seconds"] = round(time.time() - t0, 1)
    return out
