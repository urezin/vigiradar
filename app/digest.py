"""
Digest + alerts.

Turns a list of update rows (same shape as /api/updates) into:
  - a branded HTML digest email (grouped High -> Medium -> Low), and
  - a high-impact "alert" subset for real-time notification.

Pure rendering: no I/O here, so it's trivially testable and reusable by both the
preview endpoint and the scheduled sender.
"""
from __future__ import annotations

_FLAG = {"EU": "🇪🇺", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹",
         "NL": "🇳🇱", "IE": "🇮🇪", "SE": "🇸🇪", "BE": "🇧🇪"}
_IMPACT_ORDER = {"high": 0, "med": 1, "low": 2}
_IMPACT_LABEL = {"high": "High impact", "med": "Medium impact", "low": "Low impact"}
_IMPACT_COLOR = {"high": "#e11d48", "med": "#b45309", "low": "#0f766e"}
_IMPACT_BG = {"high": "#fee2e8", "med": "#fef3e2", "low": "#e7fbf5"}


def high_impact(updates: list[dict]) -> list[dict]:
    """Items that warrant a real-time alert."""
    return [u for u in updates if u.get("impact") == "high"]


def summarise_counts(updates: list[dict]) -> dict:
    c = {"total": len(updates), "high": 0, "med": 0, "low": 0, "countries": set()}
    for u in updates:
        c[u.get("impact", "med")] = c.get(u.get("impact", "med"), 0) + 1
        c["countries"].add(u.get("country", ""))
    c["countries"] = len([x for x in c["countries"] if x])
    return c


def _item_html(u: dict) -> str:
    imp = u.get("impact", "med")
    flag = _FLAG.get(u.get("country", ""), "•")
    src = u.get("source_url") or "#"
    return f"""
      <tr><td style="padding:14px 0;border-bottom:1px solid #e6ebf2">
        <div style="font-size:12px;color:#64748b;margin-bottom:4px">
          <span style="font-weight:700;color:#0b2942">{flag} {u.get('authority','')}</span>
          &nbsp;·&nbsp; <span style="color:#14b8a6;font-weight:600">{u.get('subject','')}</span>
          &nbsp;·&nbsp; <span style="color:{_IMPACT_COLOR[imp]};font-weight:700">{_IMPACT_LABEL[imp]}</span>
          &nbsp;·&nbsp; {u.get('date','')}
        </div>
        <div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:3px">{u.get('title','')}</div>
        <div style="font-size:14px;color:#475569;line-height:1.5">{u.get('summary','')}</div>
        <a href="{src}" style="font-size:13px;color:#0ea5e9;font-weight:600;text-decoration:none">View official document ↗</a>
      </td></tr>"""


def render_html(updates: list[dict], period_label: str = "this week",
                scope_label: str = "EU · all subjects", app_url: str = "https://vigi-eye.com") -> str:
    rows = sorted(updates, key=lambda u: (_IMPACT_ORDER.get(u.get("impact", "med"), 1),
                                          u.get("date", "")), reverse=False)
    rows = sorted(rows, key=lambda u: _IMPACT_ORDER.get(u.get("impact", "med"), 1))
    c = summarise_counts(updates)
    items = "".join(_item_html(u) for u in rows) or \
        '<tr><td style="padding:20px 0;color:#64748b">No changes in your scope this period.</td></tr>'
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f6f9fc;font-family:system-ui,Segoe UI,Roboto,sans-serif">
  <div style="max-width:640px;margin:0 auto;padding:24px">
    <div style="display:flex;align-items:center;gap:9px;padding:6px 2px 18px">
      <span style="display:inline-block;width:11px;height:11px;border-radius:50%;background:#34d399"></span>
      <span style="font-size:19px;font-weight:800;color:#0b2942">Vigi<span style="color:#0ea5e9">Eye</span></span>
    </div>
    <div style="background:linear-gradient(120deg,#08192b,#0f3a55);border-radius:14px;padding:22px 24px;color:#fff">
      <div style="font-size:13px;color:#7dd3fc;font-weight:700;text-transform:uppercase;letter-spacing:.05em">Your VigiEye digest — {period_label}</div>
      <div style="font-size:22px;font-weight:800;margin:6px 0 2px">{c['total']} regulatory changes across your scope</div>
      <div style="font-size:14px;color:#a9b4c9">{scope_label}</div>
      <div style="margin-top:14px;font-size:13px">
        <span style="background:#fee2e8;color:#e11d48;border-radius:999px;padding:4px 10px;font-weight:700">{c['high']} high</span>
        <span style="background:#fef3e2;color:#b45309;border-radius:999px;padding:4px 10px;font-weight:700">{c['med']} medium</span>
        <span style="background:#e7fbf5;color:#0f766e;border-radius:999px;padding:4px 10px;font-weight:700">{c['low']} low</span>
        <span style="color:#a9b4c9;margin-left:6px">· {c['countries']} countries</span>
      </div>
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">{items}</table>
    <div style="text-align:center;margin:22px 0">
      <a href="{app_url}/app" style="display:inline-block;background:linear-gradient(92deg,#0ea5e9,#14b8a6);color:#fff;
         text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:700;font-size:14px">Open the feed</a>
    </div>
    <div style="border-top:1px solid #e6ebf2;padding-top:16px;font-size:12px;color:#94a3b8;text-align:center">
      VigiEye — EU pharmacovigilance &amp; regulatory monitoring.<br>
      You're receiving this because you set up a watch at {app_url}.
    </div>
  </div>
</body></html>"""


def render_alert_html(u: dict, app_url: str = "https://vigi-eye.com") -> str:
    """A single high-impact real-time alert email."""
    flag = _FLAG.get(u.get("country", ""), "•")
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f6f9fc;font-family:system-ui,Segoe UI,Roboto,sans-serif">
  <div style="max-width:560px;margin:0 auto;padding:24px">
    <div style="font-size:19px;font-weight:800;color:#0b2942;padding-bottom:12px">Vigi<span style="color:#0ea5e9">Eye</span></div>
    <div style="background:#fee2e8;color:#e11d48;border-radius:8px;padding:6px 12px;display:inline-block;font-weight:800;font-size:12px">HIGH-IMPACT ALERT</div>
    <div style="font-size:13px;color:#64748b;margin:12px 0 4px">{flag} {u.get('authority','')} · {u.get('subject','')} · {u.get('date','')}</div>
    <div style="font-size:18px;font-weight:800;color:#0f172a;margin-bottom:6px">{u.get('title','')}</div>
    <div style="font-size:15px;color:#475569;line-height:1.55">{u.get('summary','')}</div>
    <p style="margin-top:16px"><a href="{u.get('source_url') or '#'}" style="color:#0ea5e9;font-weight:700;text-decoration:none">View official document ↗</a></p>
  </div>
</body></html>"""
