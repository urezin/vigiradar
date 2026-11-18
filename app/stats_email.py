"""
Daily subscriber statistics for all four sites, in one email to the owner every morning (with the owner briefing).

  VigiEye + CompePharma   read straight from this database (reader accounts, paying subscribers, newsletter lists)
  Competrain              GET {COMPETRAIN_STATS_URL}  with header X-Stats-Token: {STATS_TOKEN}
  VigiRadar               GET {VIGIRADAR_STATS_URL}   with header X-Stats-Token: {STATS_TOKEN}

Each day's numbers are kept (scheduler key "stats:<date>") so the email can show the change since yesterday.
Only counts travel or are stored: no names or addresses.

Admin (X-Admin-Token): GET /admin/stats (JSON), POST /admin/stats/send (send the email now).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape as _e

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from .config import settings

log = logging.getLogger("vigiradar.stats")
router = APIRouter()

REMOTE = {
    "competrain": os.getenv("COMPETRAIN_STATS_URL", "https://competrain.com/api/stats/subscribers"),
    "vigiradar": os.getenv("VIGIRADAR_STATS_URL", "https://vigiradar.com/api/stats/subscribers"),
}


def _remote(name: str) -> dict:
    url, tok = REMOTE.get(name, ""), os.getenv("STATS_TOKEN", "")
    if not url or not tok:
        return {"error": "STATS_TOKEN not set"}
    try:
        import httpx
        with httpx.Client(timeout=60, follow_redirects=True) as c:   # free Render services may need to wake up
            r = c.get(url, headers={"X-Stats-Token": tok, "User-Agent": "VigiEye stats"})
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as ex:
        return {"error": str(ex)[:120]}


def collect() -> dict:
    from . import reader_auth
    try:
        news = reader_auth.stats()
    except Exception as ex:
        news = {"error": str(ex)[:120]}
    return {"date": datetime.now(timezone.utc).date().isoformat(), "news": news,
            "competrain": _remote("competrain"), "vigiradar": _remote("vigiradar")}


def _snap_get(day: str) -> dict:
    from . import scheduler
    return scheduler._get(f"stats:{day}")


def _snap_put(day: str, data: dict) -> None:
    from . import scheduler
    scheduler._put(f"stats:{day}", data)


# --- rendering ------------------------------------------------------------------------------------------------

def _num(v) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return f"{v:,}" if isinstance(v, int) else _e(str(v))


def _delta(now, before) -> str:
    if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
        return ""
    d = now - before
    if not d:
        return '<span style="color:#94a3b8">±0</span>'
    col = "#15803d" if d > 0 else "#b91c1c"
    return f'<span style="color:{col};font-weight:700">{"+" if d > 0 else "−"}{_num(abs(d))}</span>'


def _table(title: str, color: str, rows: list[tuple[str, object, object]], err: str | None = None) -> str:
    head = (f'<h2 style="font-size:17px;margin:22px 0 6px;color:{color}">{_e(title)}</h2>')
    if err:
        return head + f'<p style="color:#b91c1c;font-size:14px">Could not read the numbers: {_e(err)}</p>'
    trs = "".join(f'<tr><td style="padding:5px 8px;border-bottom:1px solid #e2e8f0">{_e(k)}</td>'
                  f'<td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700">{_num(v)}</td>'
                  f'<td style="padding:5px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-size:13px">{_delta(v, b)}</td></tr>'
                  for k, v, b in rows if v is not None)
    return head + (f'<table style="border-collapse:collapse;width:100%;font-size:14.5px">'
                   f'<tr style="color:#64748b;font-size:12px"><td style="padding:2px 8px"></td><td style="padding:2px 8px;text-align:right">Today</td>'
                   f'<td style="padding:2px 8px;text-align:right">vs yesterday</td></tr>{trs}</table>')


def _rows(cur: dict, prev: dict, spec: list[tuple[str, str]]) -> list:
    def get(d, path):
        for p in path.split("."):
            d = d.get(p) if isinstance(d, dict) else None
        return d
    return [(label, get(cur, key), get(prev, key)) for label, key in spec]


NEWS_SPEC = [("Reader accounts (total)", "accounts"), ("  new in the last 24 hours", "accounts_new_24h"),
             ("  new in the last 7 days", "accounts_new_7d"), ("  signed up on VigiEye", "by_site.vigieye"),
             ("  signed up on CompePharma", "by_site.compepharma"), ("  with username + password", "with_password"),
             ("  with Google", "with_google"), ("  email link only (old sign-ups)", "email_link_only"),
             ("Signed in in the last 24 hours", "signed_in_24h"),
             ("Paying subscribers ($4.99/month)", "paying"), ("  who joined on VigiEye", "paying_by_site.vigieye"),
             ("  who joined on CompePharma", "paying_by_site.compepharma"), ("Monthly recurring revenue (USD, gross)", "mrr_usd"),
             ("Cancelled / past-due subscriptions", "cancelled_or_past_due"),
             ("Free daily VigiEye email", "newsletter_vigieye_daily"), ("All VigiEye email lists", "newsletter_vigieye_all"),
             ("Free daily CompePharma email", "newsletter_compepharma"), ("Emails entered, never confirmed", "unconfirmed_emails")]
CT_SPEC = [("Learner accounts (total)", "accounts"), ("  new in the last 24 hours", "accounts_new_24h"),
           ("  new in the last 7 days", "accounts_new_7d"), ("  with Google", "with_google"),
           ("Customers who bought at least one course", "buyers"), ("Course purchases (total)", "purchases"),
           ("  in the last 24 hours", "purchases_24h"), ("Organisations on a paid plan", "paid_orgs"),
           ("Courses on sale", "courses_on_sale"), ("Email leads", "leads"), ("  new in the last 24 hours", "leads_24h")]
VR_SPEC = [("Workspaces (total)", "workspaces"), ("  new in the last 24 hours", "workspaces_new_24h"),
           ("  new in the last 7 days", "workspaces_new_7d"), ("  on free trial", "trialing"),
           ("  paying (Starter / Professional / Enterprise)", "paying"), ("  trial ended, not paying", "expired"),
           ("Users (total)", "users"), ("  with Google", "with_google"), ("  new in the last 24 hours", "users_new_24h"),
           ("Systems under validation", "systems")]


def render(cur: dict, prev: dict) -> tuple[str, str]:
    n, c, v = cur.get("news") or {}, cur.get("competrain") or {}, cur.get("vigiradar") or {}
    pn, pc, pv = prev.get("news") or {}, prev.get("competrain") or {}, prev.get("vigiradar") or {}
    headline = (f"{n.get('paying', '?')} paying news readers · {c.get('accounts', '?')} Competrain learners · "
                f"{v.get('workspaces', '?')} VigiRadar workspaces")
    body = (f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#0f172a">'
            f'<h1 style="font-size:22px;margin:0 0 4px">Subscriber statistics</h1>'
            f'<p style="margin:0 0 6px;color:#475569">{_e(cur.get("date", ""))} · counted at '
            f'{datetime.now(timezone.utc).strftime("%H:%M")} UTC</p><p style="margin:0 0 8px"><b>{_e(headline)}</b></p>'
            + _table("VigiEye + CompePharma (one shared reader subscription)", "#0f766e", _rows(n, pn, NEWS_SPEC), n.get("error"))
            + _table("Competrain", "#1d4ed8", _rows(c, pc, CT_SPEC), c.get("error"))
            + _table("VigiRadar", "#7c3aed", _rows(v, pv, VR_SPEC), v.get("error"))
            + '<p style="font-size:12px;color:#64748b;margin-top:20px">Counts only, no personal data. Sent every morning with the owner '
              'briefing. “vs yesterday” compares with the previous day\'s email.</p></div>')
    return f"Subscriber statistics {cur.get('date', '')}: {headline}", body


def send_daily(to: str | None = None) -> dict:
    from . import emailer, scheduler
    to = to or os.getenv("STATS_EMAIL", "").strip() or os.getenv("OWNER_EMAIL", "").strip() or scheduler._get("owner").get("email", "")
    if not to:
        return {"status": "no owner email"}
    cur = collect()
    prev = _snap_get((datetime.fromisoformat(cur["date"]) - timedelta(days=1)).date().isoformat())
    try:
        _snap_put(cur["date"], cur)
    except Exception as ex:
        log.warning("stats snapshot failed: %s", ex)
    subj, html = render(cur, prev)
    emailer.send([x.strip() for x in to.split(",") if x.strip()], subj, html)
    return {"status": "sent" if emailer.settings.email_enabled else "logged", "to": to}


def _admin(tok: str) -> bool:
    return bool(settings.admin_token) and tok == settings.admin_token


@router.get("/admin/stats", include_in_schema=False)
def admin_stats(x_admin_token: str = Header(default="")):
    if not _admin(x_admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return collect()


@router.post("/admin/stats/send", include_in_schema=False)
def admin_stats_send(to: str = "", x_admin_token: str = Header(default="")):
    if not _admin(x_admin_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return send_daily(to or None)


def to_json(d: dict) -> str:
    return json.dumps(d, indent=2, default=str)
