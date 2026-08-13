# VigiEye

EU pharmacovigilance & regulatory monitoring — per country, per subject.

VigiEye watches EMA, EUR-Lex, HMA and the national medicines agencies, then
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
3. Add the custom domain **vigi-eye.com** and point DNS at Render.
4. Optional environment variables (the app runs fine without them):
   - `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` — AI summaries
   - `RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_REPLY_TO` — transactional email
   - `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO_MONTHLY`, `STRIPE_PRICE_TEAM_ANNUAL` — billing

## Data pipeline (live ingestion + AI summaries)

The feed is populated by a real ingestion pipeline:

- `app/sources.py` — official RSS/Atom connectors (8 EMA topic feeds + EUR-Lex
  Official Journal to start; add a national agency = one `Source` row).
- `app/summarise.py` — AI summariser (Anthropic). Turns each raw item into a
  plain-English "what changed & why it matters" summary, a subject from the
  taxonomy, and an impact rating. Falls back to a deterministic keyword
  classifier (`app/taxonomy.py`) when no `ANTHROPIC_API_KEY` is set.
- `app/store.py` — SQLite storage (idempotent upsert keyed on item id).
- `app/ingest.py` — the runner: fetch → skip seen → summarise → store.

`GET /api/updates` serves live data once the store is populated, and the curated
sample otherwise, so it's never empty.

### Activating it in production

1. Set env vars on Render: `ANTHROPIC_API_KEY` (real AI summaries) and
   `ADMIN_TOKEN` (any strong secret — guards the ingest trigger).
2. Trigger a pass: `POST /admin/ingest` with header `X-Admin-Token: <token>`.
3. Schedule it: add a Render **Cron Job** (or reuse the scheduled-task setup)
   that POSTs `/admin/ingest` a few times a day.

Note: the free instance's disk is ephemeral, so SQLite resets on redeploy —
fine for the MVP (ingestion re-populates). For durable storage add a Render
Disk or swap `store.py` to Postgres (the query interface stays the same).

## Next build phases

1. **More connectors** — HMA + the national agencies (BfArM, ANSM, AEMPS, AIFA…).
2. **Accounts & alerts** — per-user country/subject scope, digests, real-time alerts.
3. **Audit export** — inspection-ready timeline of what the team monitored.
4. **Effective-date extraction** — parse legal effective dates from linked docs.
