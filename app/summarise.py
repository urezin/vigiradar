"""
AI summariser.

Turns a raw feed item into { summary, subject, impact }:
  - summary: 1–2 sentence plain-English "what changed & why it matters" for a
    regulatory-affairs / PV reader (not a copy of the feed blurb).
  - subject: one value from taxonomy.SUBJECTS.
  - impact: "high" | "med" | "low".

Uses Anthropic when ANTHROPIC_API_KEY is set; otherwise falls back to the
deterministic keyword heuristic so the pipeline runs end-to-end without a key.
"""
from __future__ import annotations

import json
import logging

from .config import settings
from . import taxonomy

log = logging.getLogger("vigiradar.summarise")

_SYSTEM = (
    "You are a regulatory intelligence analyst for EU pharmacovigilance and "
    "medicines regulation. You write for regulatory-affairs and PV professionals. "
    "Be precise, factual, and never invent specifics not present in the input."
)

_PROMPT = """Summarise this regulatory/pharmacovigilance update for a QA/PV professional.

Source authority: {authority}
Title: {title}
Description: {description}

Return ONLY a JSON object with exactly these keys:
- "summary": 1-2 sentences in plain English stating what changed and why it matters. No preamble.
- "subject": choose the single best fit from this list: {subjects}
- "impact": one of "high", "med", "low" (high = urgent safety/PRAC/referral/restriction; low = fees/agendas/administrative).

JSON only, no markdown fences."""


def _fallback(item: dict, default_subject: str) -> dict:
    h = taxonomy.heuristic(item.get("title", ""), item.get("description", ""), default_subject)
    h["mode"] = "heuristic"
    return h


def summarise_item(item: dict, default_subject: str = "") -> dict:
    """Return {summary, subject, impact, mode}. Never raises."""
    if not settings.anthropic_api_key:
        return _fallback(item, default_subject)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{"role": "user", "content": _PROMPT.format(
                authority=item.get("authority", ""),
                title=item.get("title", ""),
                description=(item.get("description", "") or "")[:2000],
                subjects=", ".join(taxonomy.SUBJECTS),
            )}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object in model output")
        data = json.loads(text[start:end + 1])
        subject = data.get("subject", "")
        if subject not in taxonomy.SUBJECTS:
            subject = taxonomy.classify_subject(item.get("title", "") + " " + item.get("description", "")) \
                or default_subject or "GVP modules"
        impact = data.get("impact", "med")
        if impact not in ("high", "med", "low"):
            impact = "med"
        summary = (data.get("summary") or "").strip() or item.get("title", "")
        return {"summary": summary, "subject": subject, "impact": impact, "mode": "llm"}
    except Exception as e:  # any API/parse failure -> deterministic fallback
        log.warning("summarise fell back to heuristic: %s", e)
        return _fallback(item, default_subject)
