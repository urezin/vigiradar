import json, os, urllib.request, re
UA={"User-Agent":"VigiEye regulatory monitor (+https://vigi-eye.com; info@vigi-eye.com)","Accept":"*/*"}
out={}
for k,u in {"rss":"https://www.ispor.org/rss","rss2":"https://www.ispor.org/news/rss","home":"https://www.ispor.org/",
            "news":"https://www.ispor.org/about/news","pressroom":"https://www.ispor.org/about/press-room",
            "heor_news":"https://www.ispor.org/publications/journals/value-outcomes-spotlight",
            "conferences":"https://www.ispor.org/conferences-education/conferences",
            "vih_rss":"https://www.valueinhealthjournal.com/current.rss","vih_rss2":"https://www.valueinhealthjournal.com/rss/current",
            "heor_hub":"https://www.ispor.org/heor-resources/news-top","feed":"https://www.ispor.org/feed"}.items():
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=30); b=r.read().decode("utf-8","replace")
        links=re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',b,re.S)
        L=[(h,re.sub(r"\s+"," ",re.sub("<[^>]+>","",x)).strip()[:120]) for h,x in links]
        L=[x for x in L if len(x[1])>30][:50]
        items=re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>",b,re.S)[:8]
        out[k]={"status":r.status,"ct":r.headers.get("content-type"),"len":len(b),"links":L,"items":items,"head":b[:300]}
    except Exception as e: out[k]={"error":repr(e)[:200]}
os.makedirs("probe/out5",exist_ok=True); json.dump(out,open("probe/out5/ispor.json","w"),indent=1)
