"""
National-agency scrapers.

National medicines agencies don't publish RSS, so we scrape their news/updates
listing pages. Each Scraper points at a listing URL and a regex that matches the
agency's news-article link paths; we pull every matching <a>, use its text as the
title, and best-effort a date. Output is normalised to the SAME shape as the RSS
connectors (app/sources.py) so ingestion treats both identically.

Reality check: HTML scraping is more fragile than RSS. Only ANSM is verified
against the live page; the others are best-effort patterns that need one live
tuning pass (watch the ingest logs, adjust `list_url` / `link_re`). Errors are
per-scraper isolated so one broken site never breaks a run.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass(frozen=True)
class Scraper:
    id: str
    country: str
    authority: str
    list_url: str
    link_re: str          # regex matched against each <a href>
    default_subject: str
    verified: bool = False  # True = confirmed against the live page


SCRAPERS: list[Scraper] = [
    # Verified against the live listing page.
    Scraper("ansm", "FR", "ANSM",
            "https://ansm.sante.fr/actualites",
            r"/actualites/[a-z0-9][a-z0-9\-]{8,}$", "Signal management", verified=True),
    # AEMPS (ES) — verified live: "informa" bulletins + news articles.
    Scraper("aemps", "ES", "AEMPS",
            "https://www.aemps.gob.es/acciones-informativas/ultima-informacion/",
            r"/informa/|/acciones-informativas/[a-z0-9\-]{8,}", "Signal management", verified=True),
    # NOTE: AIFA (IT) and BfArM (DE) moved to RSS connectors in app/sources.py —
    # their news lists are JavaScript-rendered, so HTML scraping returned nothing.
    # They now consume the agencies' underlying RSS feeds instead.
]

_DATE_RE = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})|(\d{4})-(\d{2})-(\d{2})")
# English "DD Mon YYYY" (e.g. AIFA: "15 Jun 2026")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_MONTH_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})")


def item_id(scraper_id: str, url: str, title: str) -> str:
    basis = f"{scraper_id}|{url}|{title}".encode("utf-8", "ignore")
    return hashlib.sha1(basis).hexdigest()[:16]


def _guess_date(text_near: str) -> str:
    text_near = text_near or ""
    m = _DATE_RE.search(text_near)
    if m:
        if m.group(3):   # dd/mm/yyyy or dd.mm.yyyy
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"   # yyyy-mm-dd
    m = _DATE_MONTH_RE.search(text_near)   # "15 Jun 2026"
    if m and m.group(2).lower() in _MONTHS:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def fetch_raw_items(scraper: Scraper, timeout: float = 20.0, cap: int = 40) -> list[dict]:
    """Fetch a listing page, extract news links, normalise to raw items."""
    import httpx
    from bs4 import BeautifulSoup

    headers = {"User-Agent": "VigiEye/0.1 (+https://vigi-eye.com)",
               "Accept-Language": "en,fr;q=0.8,de;q=0.6,es;q=0.6,it;q=0.6"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        resp = client.get(scraper.list_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

    pat = re.compile(scraper.link_re, re.IGNORECASE)
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not pat.search(href):
            continue
        url = urljoin(scraper.list_url, href)
        if url in seen:
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < 12:   # skip nav/icon links with no real headline
            continue
        seen.add(url)
        # best-effort date from surrounding text (list rows often carry a date)
        near = a.find_parent(["article", "li", "div"])
        date = _guess_date(near.get_text(" ", strip=True) if near else "")
        items.append({
            "id": item_id(scraper.id, url, title),
            "source_id": scraper.id,
            "country": scraper.country,
            "authority": scraper.authority,
            "title": title,
            "link": url,
            "description": title,   # listing pages rarely give a blurb; AI/heuristic summarises the title
            "published": date,
        })
        if len(items) >= cap:
            break
    return items
