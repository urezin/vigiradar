# VigiRadar

EU pharmacovigilance & regulatory monitoring — per country, per subject.

VigiRadar watches EMA, EUR-Lex, HMA and the national medicines agencies, then
summarises each regulatory/PV change with AI — organised by country and by
subject, and flagged for the markets and products that matter to a team.

## What's in this MVP

- **Landing page** (`/`) — marketing site with hero, features, coverage, pricing.
- **Workspace** (`/app`) — the monitoring dashboard: filter the radar feed by
  country, subject and impact; each update carries an AI summary, effective date,
  impact flag and a link to the official source.
- **API** — `/api/updates` (filterable feed), `/api/meta` (countries + subjects),
  `/leads` (early-access capture), `/billing/checkout` (Stripe, auto-enables when
  keys are set), `/health`.

Content today is a curated sample (`app/data.py`) so the product is fully
demoable. The live source connectors + AI-summary pipeline plug in behind the
same `/api/updates` shape.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

## Deploy (Render + Docker, same as the reference setup)

1. Create a new GitHub repo and upload this folder.
2. In Render: **New → Web Service** → connect the repo. It auto-detects the
   `Dockerfile` (or the `render.yaml` Blueprint).
3. Add the custom domain **vigiradar.com** and point DNS at Render.
4. Optional environment variables (the app runs fine without them):
   - `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` — AI summaries
   - `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_REPLY_TO` — transactional email
   - `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO_MONTHLY`, `STRIPE_PRICE_TEAM_ANNUAL` — billing

## Next build phases

1. **Source connectors** — EMA, EUR-Lex (SPARQL/API), HMA, national agency feeds.
2. **AI summariser** — normalise each change to summary + effective date + impact.
3. **Accounts & alerts** — per-user country/subject scope, digests, real-time alerts.
4. **Audit export** — inspection-ready timeline of what the team monitored.
