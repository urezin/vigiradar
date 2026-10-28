import json, os, re, time
import httpx, feedparser
feeds = json.load(open("probe/agency_feeds.json"))
BR = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1",
      "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*", "Accept-Language": "en,de;q=0.7,fr;q=0.6"}
HON = {**BR, "User-Agent": "VigiEye regulatory monitor (+https://vigi-eye.com; info@vigi-eye.com)"}
out = []
for f in feeds:
    if f.get("type") not in ("rss", "json", "atom"):
        continue
    r = {"country": f["country"], "agency": f["agency"], "url": f["url"], "category": f["category"], "type": f["type"]}
    for name, h in (("browser", BR), ("honest", HON)):
        try:
            t0 = time.time()
            with httpx.Client(timeout=30, headers=h, follow_redirects=True) as c:
                resp = c.get(f["url"])
            r[name] = {"status": resp.status_code, "ct": resp.headers.get("content-type", ""), "len": len(resp.content),
                       "s": round(time.time() - t0, 1)}
            if resp.status_code == 200:
                if f["type"] == "json":
                    j = resp.json(); res = j.get("results", [])
                    r[name]["n"] = len(res); r[name]["sample"] = json.dumps(res[:1])[:600]
                else:
                    p = feedparser.parse(resp.content)
                    es = p.entries
                    r[name]["n"] = len(es)
                    r[name]["sample"] = [((getattr(e, "title", "") or "")[:110], getattr(e, "link", ""),
                                          getattr(e, "published", "") or getattr(e, "updated", "")) for e in es[:5]]
                if r[name].get("n"):
                    break
        except Exception as e:
            r[name] = {"error": repr(e)[:200]}
    out.append(r)
os.makedirs("probe/out5", exist_ok=True)
json.dump(out, open("probe/out5/agencies.json", "w"), indent=1, ensure_ascii=False)
