import json, os, sys
os.environ["VIGIRADAR_DB"] = "/tmp/e.db"
sys.path.insert(0, ".")
from app import earnings
st = earnings.ingest(days=10, max_docs=70)
rows = earnings.latest(days=14, limit=60)
os.makedirs("probe/out6", exist_ok=True)
json.dump({"stats": st, "rows": [{k: (v[:300] if isinstance(v, str) and k == "text" else v) for k, v in r.items()} for r in rows]},
          open("probe/out6/earn.json", "w"), indent=1, ensure_ascii=False)
