"""
IndexNow: tell Bing, Yandex, Seznam, Naver and others about new pages immediately
(https://www.indexnow.org). The key is public by design: it is served at /<key>.txt on each
host to prove ownership. CompePharma serves the same key file.
"""
from __future__ import annotations

import logging
import os
from datetime import date

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

log = logging.getLogger("vigiradar.indexnow")
router = APIRouter()
KEY = os.getenv("INDEXNOW_KEY", "9177237c1d4269414fc9178f1355df96")
CP = os.getenv("COMPEPHARMA_URL", "https://compepharma.com").rstrip("/")


@router.get(f"/{KEY}.txt", response_class=PlainTextResponse, include_in_schema=False)
def key_file():
    return KEY


def _submit(base: str, urls: list[str]) -> dict:
    import httpx
    from urllib.parse import urlparse
    urls = list(dict.fromkeys(u for u in urls if u.startswith(base)))[:10000]
    if not urls:
        return {"host": base, "submitted": 0}
    host = urlparse(base).hostname
    try:
        r = httpx.post("https://api.indexnow.org/indexnow", timeout=30,
                       json={"host": host, "key": KEY, "keyLocation": f"{base}/{KEY}.txt", "urlList": urls})
        return {"host": host, "submitted": len(urls), "status": r.status_code}
    except Exception as e:
        return {"host": host, "submitted": 0, "error": str(e)[:120]}


def ping_fresh(hours: int = 3) -> dict:
    from . import news_pages as np_, articles as art
    from .config import settings
    base = (settings.app_base_url or "https://vigi-eye.com").rstrip("/")
    if "localhost" in base or "127.0.0.1" in base:
        return {"skipped": "local"}
    out = {"vigieye": _submit(base, np_.fresh_urls(hours))}
    today = date.today().isoformat()
    cp_urls = [f"{CP}/insights/{a['slug']}" for a in art.cp_list(40) if a.get("date") == today]
    out["compepharma"] = _submit(CP, cp_urls + [f"{CP}/", f"{CP}/insights"])
    return out
