"""
Subject taxonomy + heuristic classifier / impact rater.

Used two ways:
  1. As the deterministic fallback when the AI summariser is unavailable
     (no ANTHROPIC_API_KEY) or errors.
  2. To constrain the AI: the model is asked to pick a subject from SUBJECTS.
"""
from __future__ import annotations

SUBJECTS = [
    "Signal management", "PSUR / PBRER", "Labelling & SmPC", "GVP modules",
    "Clinical trials (CTR)", "Falsified medicines", "RMP & PASS",
    "Shortages & recalls", "Fees & guidance",
]

# keyword -> subject (first match wins, order matters: specific before generic)
_SUBJECT_KEYWORDS: list[tuple[str, str]] = [
    ("signal", "Signal management"),
    ("prac", "Signal management"),
    ("psur", "PSUR / PBRER"),
    ("pbrer", "PSUR / PBRER"),
    ("periodic safety", "PSUR / PBRER"),
    ("smpc", "Labelling & SmPC"),
    ("product information", "Labelling & SmPC"),
    ("package leaflet", "Labelling & SmPC"),
    ("labelling", "Labelling & SmPC"),
    ("labeling", "Labelling & SmPC"),
    ("good pharmacovigilance", "GVP modules"),
    ("gvp", "GVP modules"),
    ("pharmacovigilance", "GVP modules"),
    ("clinical trial", "Clinical trials (CTR)"),
    ("ctis", "Clinical trials (CTR)"),
    ("ctr", "Clinical trials (CTR)"),
    ("falsified", "Falsified medicines"),
    ("safety feature", "Falsified medicines"),
    ("unique identifier", "Falsified medicines"),
    ("risk management plan", "RMP & PASS"),
    ("rmp", "RMP & PASS"),
    ("pass", "RMP & PASS"),
    ("shortage", "Shortages & recalls"),
    ("recall", "Shortages & recalls"),
    ("supply disruption", "Shortages & recalls"),
    ("fee", "Fees & guidance"),
    ("guideline", "Fees & guidance"),
    ("guidance", "Fees & guidance"),
]

# words that push impact up / down
_HIGH = ("urgent", "safety concern", "safety signal", "safety restriction",
         "prac recommend", "referral", "suspension", "recall", "falsified",
         "new signal", "signal validated", "restriction", "contraindication", "withdrawn")
_LOW = ("fee", "agenda", "minutes", "consultation", "correction", "editorial", "typo")


def classify_subject(text: str) -> str:
    t = (text or "").lower()
    for kw, subj in _SUBJECT_KEYWORDS:
        if kw in t:
            return subj
    return ""


def rate_impact(text: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in _HIGH):
        return "high"
    if any(w in t for w in _LOW):
        return "low"
    return "med"


def heuristic(title: str, description: str, default_subject: str) -> dict:
    """Deterministic classification used as the no-AI fallback."""
    blob = f"{title}. {description}"
    subject = classify_subject(blob) or default_subject or "GVP modules"
    impact = rate_impact(blob)
    summary = (description or title).strip()
    if len(summary) > 320:
        summary = summary[:317].rstrip() + "…"
    return {"subject": subject, "impact": impact, "summary": summary}
