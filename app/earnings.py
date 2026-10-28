"""
Quarterly results of drugmakers, from the results press releases they file with the SEC
(8-K, Item 2.02, exhibit 99). These are official filings; CompePharma's earnings analyses are
written from them. (Earnings-call transcripts are licensed content and are not used.)

Table portal_earnings: one row per results release, with the cleaned release text.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Column, Float, MetaData, String, Table, Text, insert, select

from . import store

log = logging.getLogger("vigiradar.earnings")
_meta = MetaData()

earnings = Table(
    "portal_earnings", _meta,
    Column("id", String(40), primary_key=True),
    Column("company", Text, nullable=False),
    Column("ticker", String(16)),
    Column("cik", String(12)),
    Column("date", String(10), nullable=False),
    Column("period", String(40)),
    Column("headline", Text),
    Column("revenue_musd", Float),          # largest revenue figure found, USD millions (rough, for ranking)
    Column("url", Text, nullable=False),
    Column("text", Text),
    Column("ingested_at", String(40), nullable=False),
)

SEC_FTS = "https://efts.sec.gov/LATEST/search-index"
_QUERIES = ['"financial results" "net product revenue"', '"financial results" "product sales" FDA',
            '"quarter" "financial results" "total revenues" pharmaceuticals', '"reports third quarter" pharmaceutical',
            '"third quarter 2026" revenue FDA', '"fourth quarter" "full year" revenue FDA patients',
            '"first quarter" "financial results" FDA patients', '"second quarter" "financial results" FDA patients',
            '"worldwide revenues" pharmaceutical', '"reported revenue" biopharmaceutical quarter']
# large drugmakers' CIKs (results releases are queried for these directly)
_BIG = ["78003", "310158", "200406", "59478", "14272", "1551152", "318154", "882095", "872589", "875320", "875045",
        "1682852", "1555280", "1792044", "1821825", "818686", "1178670", "879169", "1048477", "1232524", "1800",
        "10456", "10795", "1739104", "1585364"]
# SIC codes kept: drugs, biologics, diagnostics substances, R&D labs, medical devices, drug wholesale
_SICS = {"2833", "2834", "2835", "2836", "8731", "3841", "3842", "3845", "5122", "2844"}
_PHARMA = re.compile(r"\b(fda|ema|patients|therap|clinical|pharma|biopharm|drug|medicine|vaccine|oncology|indication)", re.I)
_RESULTS = re.compile(r"(quarter|full[- ]year).{0,80}(results|revenue)", re.I | re.S)
_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)", re.I)
_PERIOD = re.compile(r"\b(first|second|third|fourth)[- ]quarter(?: (?:of )?(?:fiscal )?(20\d\d))?|\b(Q[1-4])\s?(20\d\d)\b|full[- ]year (20\d\d)", re.I)


def init() -> None:
    _meta.create_all(store._engine)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revenue(text: str) -> float | None:
    """Largest $ figure near the word revenue/sales, in USD millions. Rough; used for ranking only."""
    best = None
    for m in _MONEY.finditer(text[:20000]):
        ctx = text[max(0, m.start() - 120): m.end() + 60].lower()
        if not re.search(r"revenue|sales", ctx):
            continue
        v = float(m.group(1).replace(",", "")) * (1000 if m.group(2).lower() == "billion" else 1)
        if v < 200000:
            best = v if best is None else max(best, v)
    return best


def _period(text: str) -> str:
    m = _PERIOD.search(text[:3000])
    if not m:
        return ""
    if m.group(1):
        return f"{m.group(1).title()} quarter {m.group(2) or ''}".strip()
    if m.group(3):
        return f"{m.group(3).upper()} {m.group(4)}"
    return f"Full year {m.group(5)}"


def ingest(days: int = 10, max_docs: int = 60) -> dict:
    from urllib.parse import urlencode
    from . import portal_data as pd
    init()
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    hits, errors = {}, []
    reqs = [{"q": q} for q in _QUERIES] + [{"q": '"quarter" results', "ciks": ",".join(c.zfill(10) for c in _BIG[i:i + 8])}
                                           for i in range(0, len(_BIG), 8)]
    for rq in reqs:
        q = rq["q"]
        try:
            qs = urlencode({**rq, "forms": "8-K", "dateRange": "custom", "startdt": start, "enddt": end})
            j = pd._http_get(f"{SEC_FTS}?{qs}", timeout=60).json()
            for h in (j.get("hits") or {}).get("hits") or []:
                s = h["_source"]
                d = f'{s.get("file_description") or ""} {h["_id"]}'.lower()
                items = " ".join(s.get("items") or [])
                sics = set(s.get("sics") or [])
                if (s.get("file_type") or "").upper().startswith("EX-99") and "presentation" not in d and \
                        (not items or "2.02" in items) and (not sics or sics & _SICS):
                    hits[h["_id"]] = h
        except Exception as e:
            errors.append(f"{q}: {e}")
        time.sleep(0.25)
    with store._engine.connect() as c:
        have = {r[0] for r in c.execute(select(earnings.c.id))}
    stats = {"hits": len(hits), "new": 0, "not_pharma": 0, "errors": errors}
    done = 0
    for hid, h in sorted(hits.items(), key=lambda kv: kv[1]["_source"].get("file_date", ""), reverse=True):
        eid = re.sub(r"[^A-Za-z0-9]", "", hid)[:40]
        if eid in have or done >= max_docs:
            continue
        s = h["_source"]
        try:
            adsh, fn = hid.split(":", 1)
            cik = (s.get("ciks") or ["0"])[0].lstrip("0")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fn}"
            text = pd.clean_exhibit(pd._html_text(pd._http_get(url, timeout=60).text))
        except Exception as e:
            errors.append(f"{hid}: {e}")
            continue
        done += 1
        time.sleep(0.15)
        head = text[:2500]
        if not _RESULTS.search(head) or len(_PHARMA.findall(text[:12000])) < 4:
            stats["not_pharma"] += 1
            with store._engine.begin() as c:     # remember, so it isn't fetched again
                c.execute(insert(earnings).values(id=eid, company="-", date=s.get("file_date", end), url=url,
                                                  text=None, ingested_at=_now()))
            continue
        disp = (s.get("display_names") or [""])[0]
        cm = re.match(r"^(.*?)\s*\(([A-Z0-9.\-, ]+)\)\s*\(CIK", disp)
        company = (cm.group(1) if cm else disp.split("(CIK")[0]).strip()
        company = company.title() if company.isupper() else company
        ticker = cm.group(2).split(",")[0].strip() if cm else ""
        with store._engine.begin() as c:
            c.execute(insert(earnings).values(
                id=eid, company=company, ticker=ticker, cik=cik, date=s.get("file_date", end), period=_period(text),
                headline=pd.extract_headline(text)[:300], revenue_musd=_revenue(text), url=url, text=text[:40000],
                ingested_at=_now()))
        stats["new"] += 1
    return stats


def latest(days: int = 14, limit: int = 40) -> list[dict]:
    init()
    since = (date.today() - timedelta(days=days)).isoformat()
    with store._engine.connect() as c:
        rows = [dict(r._mapping) for r in c.execute(
            select(earnings).where(earnings.c.date >= since).where(earnings.c.company != "-")
            .order_by(earnings.c.date.desc()).limit(400))]
    rows.sort(key=lambda r: (r.get("revenue_musd") or 0), reverse=True)
    return rows[:limit]
