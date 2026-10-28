"""Run every portal ingester against live sources into a throwaway SQLite DB
and write a report (used in CI to verify parsers against real data)."""
import json, os, sys, time
os.environ.setdefault("VIGIRADAR_DB", os.path.abspath("selftest.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import portal_data as pd, store
out = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
only = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] else None
out["results"] = pd.run_all(only)
if os.environ.get("SELFTEST_NEWS"):
    from app import ingest, news
    out["radar"] = ingest.run_ingest(per_source=12)
    out["news"] = news.run_all()
    from app import guidelines
    out["guidelines"] = guidelines.run_all()
out["counts"] = pd.counts()
from datetime import date, timedelta
t = date.today().isoformat()
samp = {}
for k in ["pdufa", "adcomm", "fda_approval", "chmp_opinion_pos", "chmp_opinion_neg", "ec_expected",
          "ec_decision", "referral_start", "prac_recommendation", "dhpc", "shortage"]:
    rows = pd.milestones_between(kinds=[k], limit=2000)
    fut = [r for r in rows if r["date"] >= t]
    samp[k] = {"total": len(rows), "future": len(fut),
               "sample_future": [{x: r[x] for x in ("date", "title", "detail", "source_url")} for r in fut[:12]],
               "sample_recent": [{x: r[x] for x in ("date", "title", "detail")} for r in sorted(rows, key=lambda r: r["date"], reverse=True)[:6]]}
out["samples"] = samp
os.makedirs("probe/out2", exist_ok=True)
json.dump(out, open("probe/out2/selftest.json", "w"), indent=1, default=str)
print(json.dumps(out["results"], indent=1, default=str)[:4000])
