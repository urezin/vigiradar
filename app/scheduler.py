"""
Self-scheduling: the server runs its own refreshes instead of relying on GitHub cron.

GitHub's scheduled workflows are best-effort: on 28-29 Oct 2026 the 2-hourly news refresh ran
2 times out of 5 overnight and the 04:40 UTC daily run never started. So the schedule now lives
here, in a background thread that wakes every 5 minutes and asks "what is due?":

  news   every 2 hours                     (steps: news)
  daily  once a day from 04:30 UTC          (steps: ingest, news, analyse, articles [+ week on Monday]),
         then the newsletters: owner briefing every day, public lists on weekdays

State (last start / finish per job) is kept in the database, so a restart or redeploy never
loses track and two triggers never start the same job twice. GitHub's hourly workflow just
calls /admin/portal/tick: it wakes the server if it was asleep and runs whatever is due.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("vigiradar.scheduler")
NEWS_EVERY = timedelta(hours=2)
DAILY_AT_UTC = (4, 30)
STALE = timedelta(minutes=75)    # a job "started" this long ago without finishing (e.g. killed by a deploy) is re-run
_LOCK = threading.Lock()
_STARTED = {"loop": False}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get(key: str) -> dict:
    from . import portal_data as pd
    pd.init()
    a = pd.analysis_get(f"sched:{key}")
    try:
        return json.loads(a["body"]) if a else {}
    except Exception:
        return {}


def _put(key: str, val: dict) -> None:
    from . import portal_data as pd
    pd.analysis_put(f"sched:{key}", key, json.dumps(val), "scheduler")


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


def due(now: datetime | None = None) -> str | None:
    """'daily', 'news' or None."""
    now = now or _now()
    today = now.date().isoformat()
    d = _get("daily")
    started = _ts(d.get("started"))
    daily_time = now.replace(hour=DAILY_AT_UTC[0], minute=DAILY_AT_UTC[1], second=0, microsecond=0)
    if now >= daily_time and d.get("done_for") != today:
        if not started or started < daily_time or (now - started > STALE and d.get("started_for") == today):
            return "daily"
    n = _get("news")
    last = _ts(n.get("started"))
    if not last or now - last >= NEWS_EVERY:
        return "news"
    return None


def _send_newsletters(today: str) -> dict:
    from . import emailer, store, cp_email
    from .portal_pages import render_daily
    out = {}
    owner = os.getenv("OWNER_EMAIL", "").strip() or _get("owner").get("email", "")
    if owner:
        if True:
            try:
                s1, h1, _ = render_daily(owner)
                emailer.send(owner, s1, h1)
                s2, h2, _ = cp_email.render_cp_daily(owner)
                emailer.send(owner, s2, h2)
                try:
                    s3, h3 = cp_email.render_social_kit()
                    if h3:
                        emailer.send(owner, s3, h3)
                except Exception:
                    pass
                out["owner"] = "sent" if emailer.settings.email_enabled else "logged (no RESEND_API_KEY)"
            except Exception as e:
                out["owner"] = f"error: {str(e)[:120]}"
    if date.fromisoformat(today).weekday() < 5:
        sent = 0
        try:
            store.init()
            for em in store.subscribers_active("daily"):
                s, h, _ = render_daily(em)
                emailer.send(em, s, h)
                sent += 1
                time.sleep(0.6)
            for em in cp_email.cp_active():
                s, h, _ = cp_email.render_cp_daily(em)
                emailer.send(em, s, h)
                sent += 1
                time.sleep(0.6)
        except Exception as e:
            out["public_error"] = str(e)[:120]
        out["public_sent"] = sent
    return out


def _run(kind: str) -> None:
    from . import portal_pages as pp
    now = _now()
    today = now.date().isoformat()
    if kind == "daily":
        steps = ["ingest", "news", "analyse", "articles"] + (["week"] if now.weekday() == 0 else [])
        _put("daily", {**_get("daily"), "started": now.isoformat(), "started_for": today})
    else:
        steps = ["news"]
    _put("news", {"started": now.isoformat()})          # a daily run includes the news step
    try:
        pp._run_job(steps)
    finally:
        res = {"finished": _now().isoformat(), "steps": steps}
        if kind == "daily":
            res["newsletters"] = _send_newsletters(today)
            _put("daily", {**_get("daily"), **res, "done_for": today})
        _put("last_result", {"kind": kind, **res, "result": pp._JOB.get("result")})


def tick() -> dict:
    """Start whatever is due (non-blocking). Safe to call from anywhere, any number of times."""
    from . import portal_pages as pp
    with _LOCK:
        if pp._JOB.get("running"):
            return {"status": "busy", "step": pp._JOB.get("step")}
        kind = due()
        if not kind:
            return {"status": "nothing due", "news": _get("news"), "daily": _get("daily")}
        # mark immediately so a second tick a moment later does not start it again
        if kind == "daily":
            _put("daily", {**_get("daily"), "started": _now().isoformat(), "started_for": _now().date().isoformat()})
        _put("news", {"started": _now().isoformat()})
        threading.Thread(target=_run, args=(kind,), daemon=True).start()
        return {"status": "started", "kind": kind}


def _loop() -> None:
    time.sleep(90)                     # let the app finish starting (and a deploy settle)
    while True:
        try:
            r = tick()
            if r.get("status") == "started":
                log.info("scheduler started %s", r.get("kind"))
        except Exception as e:
            log.warning("scheduler tick failed: %s", e)
        time.sleep(300)


def start() -> None:
    if _STARTED["loop"] or os.getenv("DISABLE_SCHEDULER"):
        return
    _STARTED["loop"] = True
    threading.Thread(target=_loop, daemon=True, name="vigieye-scheduler").start()
