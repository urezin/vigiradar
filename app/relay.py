"""
Relay for sources that block Render's IP range but work from GitHub's runners
(currently the Federal Register API, which returns 403 to Render).

The scheduled workflows fetch these URLs on the runner and POST the JSON to
/admin/portal/relay/{key}; ingesters try the source directly first and fall back to the
relayed copy. Stdlib only, so the workflow can import it without installing the app.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

FEDREG = "https://www.federalregister.gov/api/v1/documents.json"


def fedreg_guidance_url(days: int = 180) -> str:
    params = [("conditions[agencies][]", "food-and-drug-administration"), ("conditions[type][]", "NOTICE"),
              ("conditions[term]", "guidance availability"),
              ("conditions[publication_date][gte]", (date.today() - timedelta(days=days)).isoformat()),
              ("order", "newest"), ("per_page", "200")]
    params += [("fields[]", f) for f in ("title", "publication_date", "html_url", "abstract", "document_number")]
    return f"{FEDREG}?{urlencode(params)}"


def fedreg_adcomm_url(days_back: int = 120) -> str:
    params = [("conditions[agencies][]", "food-and-drug-administration"), ("conditions[type][]", "NOTICE"),
              ("conditions[term]", '"advisory committee" "notice of meeting"'),
              ("conditions[publication_date][gte]", (date.today() - timedelta(days=days_back)).isoformat()),
              ("order", "newest"), ("per_page", "100")]
    params += [("fields[]", f) for f in ("title", "publication_date", "html_url", "abstract", "dates", "document_number")]
    return f"{FEDREG}?{urlencode(params)}"


URLS = {"fedreg-guidance": fedreg_guidance_url, "fedreg-adcomm": fedreg_adcomm_url}


def get_json(key: str, fetch) -> dict:
    """Try the live source with `fetch(url)`; on failure use the copy relayed from GitHub."""
    url = URLS[key]()
    try:
        return fetch(url)
    except Exception as live_err:
        import json
        from . import portal_data as pd
        a = pd.analysis_get(f"relay:{key}")
        if not a:
            raise live_err
        return json.loads(a["body"])


if __name__ == "__main__":      # used by the workflows: print key<TAB>url lines
    for k, f in URLS.items():
        print(f"{k}\t{f()}")
