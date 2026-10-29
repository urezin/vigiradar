"""
CompePharma Daily (email) and the owner's morning briefing.

CompePharma's data and editorial live in VigiEye's database, so its newsletter is rendered and
sent from here; links point to CompePharma (COMPEPHARMA_URL). Subscribers are kept in their own
table so a CompePharma sign-up never adds anyone to VigiEye's list.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import Column, MetaData, String, Table, insert, select, update

from . import store
from .config import settings

router = APIRouter()
_meta = MetaData()
cp_subs = Table("cp_subscribers", _meta,
                Column("email", String(320), primary_key=True), Column("source", String(60)),
                Column("created_at", String(40)), Column("unsubscribed", String(4)))

CP = os.getenv("COMPEPHARMA_URL", "https://compepharma.com").rstrip("/")
# CompePharma's newsletter is sent as CompePharma (set CP_EMAIL_FROM once compepharma.com is verified in Resend)
CP_FROM = os.getenv("CP_EMAIL_FROM", "CompePharma <info@compepharma.com>")
CP_REPLY_TO = os.getenv("CP_EMAIL_REPLY_TO", "info@compepharma.com")


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _base() -> str:
    return (settings.app_base_url or "https://vigi-eye.com").rstrip("/")


def init() -> None:
    _meta.create_all(store._engine)


def _token(email: str) -> str:
    key = (settings.admin_token or "vigieye").encode()
    return hmac.new(key, ("cp:" + email.lower()).encode(), hashlib.sha256).hexdigest()[:24]


class CpSub(BaseModel):
    email: str
    source: str = "compepharma"


@router.post("/cp/subscribe")
def cp_subscribe(body: CpSub):
    email = (body.email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(email) > 320:
        return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
    init()
    with store._engine.begin() as c:
        if c.execute(select(cp_subs.c.email).where(cp_subs.c.email == email)).first():
            c.execute(update(cp_subs).where(cp_subs.c.email == email).values(unsubscribed=""))
        else:
            c.execute(insert(cp_subs).values(email=email, source=(body.source or "compepharma")[:60],
                                             created_at=datetime.now(timezone.utc).isoformat(), unsubscribed=""))
    return {"ok": True, "message": "You're in. CompePharma Daily arrives every weekday morning."}


@router.get("/cp/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def cp_unsubscribe(email: str = "", t: str = ""):
    if not email or not hmac.compare_digest(t, _token(email)):
        return HTMLResponse("<p>Link not valid. Reply to any CompePharma email and we will remove you.</p>", status_code=400)
    init()
    with store._engine.begin() as c:
        c.execute(update(cp_subs).where(cp_subs.c.email == email.strip().lower()).values(unsubscribed="1"))
    return HTMLResponse(f'<div style="font:16px system-ui;max-width:560px;margin:60px auto"><h1>You are unsubscribed</h1>'
                        f'<p>You will not receive CompePharma Daily any more. <a href="{CP}/">Back to CompePharma</a></p></div>')


def cp_active() -> list[str]:
    init()
    with store._engine.connect() as c:
        return [r[0] for r in c.execute(select(cp_subs.c.email).where(cp_subs.c.unsubscribed == ""))]


# --- rendering ------------------------------------------------------------------------

_A = "color:#0e7c6f;font-weight:700;text-decoration:none"


def _art_rows(arts: list[dict], url) -> str:
    out = ""
    for a in arts:
        kind = {"earnings": "Earnings analysis", "opinion": "Opinion", "analysis": "Analysis"}.get(a.get("type"), "Analysis")
        figs = ""
        if a.get("figures"):
            figs = ('<div style="margin-top:6px">' + "".join(
                f'<span style="display:inline-block;background:#f1f5f9;border-radius:4px;padding:3px 7px;margin:2px 4px 2px 0;font-size:12px">'
                f'<b>{_e(f.get("value"))}</b> {_e(f.get("label"))}</span>' for f in a["figures"][:3]) + "</div>")
        out += (f'<tr><td style="padding:12px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
                f'<span style="font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#d7263d">{_e(kind)}</span><br>'
                f'<a href="{_e(url(a))}" style="{_A};font-size:16px">{_e(a["title"])}</a><br>'
                f'<span style="color:#475569">{_e(a.get("dek"))}</span>{figs}</td></tr>')
    return out


def render_cp_daily(email: str = "") -> tuple[str, str, int]:
    from . import articles as art
    from . import news_pages as np_
    today = date.today().isoformat()
    arts = [a for a in art.cp_list(40) if a.get("date", "") >= today] or art.cp_list(10)
    earn = [a for a in arts if a.get("type") == "earnings"]
    rest = [a for a in arts if a.get("type") != "earnings"]
    biz = json.loads(np_.api_business_news(limit=60, days=3).body).get("items", [])[:12]
    curl = lambda a: f"{CP}/insights/{a['slug']}?utm_source=cp_daily&utm_medium=email"
    sec = ""
    if earn:
        sec += f'<h3 style="font:800 17px system-ui;margin:24px 0 4px">Earnings analysis</h3><table style="width:100%;border-collapse:collapse">{_art_rows(earn, curl)}</table>'
    if rest:
        sec += f'<h3 style="font:800 17px system-ui;margin:24px 0 4px">Analysis &amp; opinion</h3><table style="width:100%;border-collapse:collapse">{_art_rows(rest, curl)}</table>'
    if biz:
        sec += ('<h3 style="font:800 17px system-ui;margin:24px 0 4px">Top business news</h3><table style="width:100%;border-collapse:collapse">'
                + "".join(f'<tr><td style="padding:9px 0;border-bottom:1px solid #e6ebf2;font:14px system-ui">'
                          f'<span style="color:#64748b;font-size:12px">{_e(b.get("beat_label"))} · {_e(b.get("publisher"))}</span><br>'
                          f'<a href="{CP}/story/{_e(b["id"])}?utm_source=cp_daily&utm_medium=email" style="{_A}">{_e(b["title"])}</a></td></tr>'
                          for b in biz) + "</table>")
    unsub = f"{_base()}/cp/unsubscribe?email={_e(email)}&t={_token(email)}" if email else "#"
    html_ = f"""<div style="max-width:660px;margin:auto;font:15px system-ui;color:#0d1321">
<div style="background:#0a1730;color:#fff;padding:18px 22px;border-radius:6px 6px 0 0"><span style="font:900 26px system-ui;letter-spacing:-.03em">Compe<span style="color:#2dd4bf">Pharma</span> Daily</span>
<div style="color:#aab6cc;font-size:13px;margin-top:4px">{_e(datetime.now(timezone.utc).strftime("%A %d %B %Y"))} · the business of pharma</div></div>
<div style="padding:4px 22px 18px;border:1px solid #e3e6eb;border-top:0">{sec or '<p>A quiet day on the desk.</p>'}
<p style="margin-top:22px"><a href="{CP}/?utm_source=cp_daily" style="{_A}">Open CompePharma</a> · <a href="{CP}/insights" style="{_A}">All insights</a> ·
<a href="{CP}/news/earnings" style="{_A}">Earnings</a></p>
<p style="font-size:12px;color:#94a3b8;margin-top:18px">Written by CompePharma's editorial system from official filings (SEC, FDA, EMA, European Commission).
Not investment advice. <a href="{unsub}" style="color:#94a3b8">Unsubscribe</a></p></div></div>"""
    lead = (earn or rest or [{"title": "the business of pharma"}])[0]["title"]
    return f"CompePharma Daily: {lead[:80]}", html_, len(arts) + len(biz)


# --- sending ---------------------------------------------------------------------------

def _auth(tok: str) -> None:
    if not settings.admin_token or not hmac.compare_digest(tok or "", settings.admin_token):
        raise HTTPException(401)


@router.post("/admin/portal/send-cp-daily")
def send_cp_daily(dry_run: bool = True, x_admin_token: str = Header(default="")):
    from . import emailer
    import time
    _auth(x_admin_token)
    emails = cp_active()
    if dry_run:
        s, _, n = render_cp_daily("")
        return {"dry_run": True, "would_send_to": len(emails), "subject": s, "items": n}
    sent, errors = 0, []
    for em in emails:
        s, h, _ = render_cp_daily(em)
        try:
            emailer.send(em, s, h, sender=CP_FROM, reply_to=CP_REPLY_TO)
            sent += 1
        except Exception as ex:
            errors.append(str(ex)[:120])
        time.sleep(0.6)
    return {"sent": sent, "errors": errors[:10]}


@router.post("/admin/portal/send-owner")
def send_owner(email: str = "", x_admin_token: str = Header(default="")):
    """The owner's morning: VigiEye Daily and CompePharma Daily, every day including weekends.
    The address comes from the OWNER_EMAIL GitHub secret (passed as ?email=) or the OWNER_EMAIL env var."""
    from . import emailer
    from .portal_pages import render_daily
    _auth(x_admin_token)
    email = (email or os.getenv("OWNER_EMAIL", "")).strip()
    if not email:
        return {"skipped": "no OWNER_EMAIL configured"}
    out = {"email_enabled": settings.email_enabled}
    s1, h1, n1 = render_daily(email)
    out["vigieye"] = emailer.send(email, s1, h1).get("status")
    s2, h2, n2 = render_cp_daily(email)
    out["compepharma"] = emailer.send(email, s2, h2, sender=CP_FROM, reply_to=CP_REPLY_TO).get("status")
    out["items"] = {"vigieye": n1, "compepharma": n2}
    try:
        s3, h3 = render_social_kit()
        if h3:
            out["social"] = emailer.send(email, s3, h3).get("status")
    except Exception as e:
        out["social"] = f"error: {str(e)[:100]}"
    return out


_SOCIAL_PROMPT = """You write LinkedIn posts for the founder of two pharma publications: VigiEye (EU/US regulatory,
pharmacovigilance, quality, market access) and CompePharma (the business of pharma, earnings analysis). Write 3 posts he can
paste today, each promoting one of the articles below. Each post: a strong first line, 3-5 short lines of genuine insight
taken only from the article title and standfirst (no invented facts or numbers), a question to invite comments, the link on
its own line, and 3 relevant hashtags. Professional, no emojis except at most one, no hype words.

ARTICLES:
{arts}

Return ONLY JSON: [{{"site":"VigiEye or CompePharma","post":"full post text with line breaks as \\n"}}]"""


def render_social_kit() -> tuple[str, str]:
    """Owner-only: three ready-to-paste LinkedIn posts promoting today's articles (organic traffic)."""
    from . import articles as art
    if not settings.anthropic_api_key:
        return "", ""
    today = date.today().isoformat()
    ve = [a for a in art.list_articles(20) if a.get("date") == today][:4]
    cp = [a for a in art.cp_list(20) if a.get("date") == today][:4]
    lines = [f"- VigiEye | {a['title']} | {a.get('dek', '')} | {_base()}/analysis/{a['slug']}?utm_source=linkedin" for a in ve]
    lines += [f"- CompePharma | {a['title']} | {a.get('dek', '')} | {CP}/insights/{a['slug']}?utm_source=linkedin" for a in cp]
    if not lines:
        return "", ""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(model=settings.anthropic_model, max_tokens=2000,
                                 messages=[{"role": "user", "content": _SOCIAL_PROMPT.format(arts="\n".join(lines))}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    posts = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    blocks = "".join(f'<div style="border:1px solid #e3e6eb;border-radius:6px;padding:14px 16px;margin:14px 0">'
                     f'<div style="font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#64748b">{_e(p.get("site"))}</div>'
                     f'<div style="white-space:pre-wrap;font:14px/1.5 system-ui;margin-top:6px">{_e(p.get("post"))}</div></div>'
                     for p in posts if isinstance(p, dict))
    html_ = (f'<div style="max-width:660px;margin:auto;font:15px system-ui;color:#0d1321"><p style="font:800 20px system-ui">'
             f'Today\'s LinkedIn posts</p><p style="color:#64748b">Three ready-to-paste posts promoting today\'s VigiEye and '
             f'CompePharma articles. Edit freely; links carry utm_source=linkedin so you can see the traffic.</p>{blocks}</div>')
    return f"Your LinkedIn posts for {date.today().strftime('%d %b')}", html_
