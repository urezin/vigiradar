import json, os, urllib.request, re
from urllib.parse import urlencode
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.2","Accept":"*/*"}
def get(u,n=None):
    r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=40); b=r.read(); return r.status,r.headers.get("content-type",""),b
out={}
def try_(k,u,mode="html",n=4000):
    try:
        st,ct,b=get(u); t=b.decode("utf-8","replace")
        info={"status":st,"ct":ct,"len":len(b)}
        if mode=="html":
            links=re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',t,re.S)
            info["links"]=[(h,re.sub(r"\s+"," ",re.sub("<[^>]+>","",x)).strip()[:100]) for h,x in links if len(re.sub("<[^>]+>","",x).strip())>25][:40]
            info["tables"]=t.count("<table"); info["head"]=t[:300]
            i=t.find("<table"); info["table_snip"]=re.sub(r"\s+"," ",t[i:i+3000]) if i>=0 else ""
        elif mode=="json":
            info["json"]=t[:n]
        else:
            info["head"]=t[:n]
        out[k]=info
    except Exception as e: out[k]={"error":repr(e)[:200]}
# FDA guidance + safety
try_("fda_new_guidance","https://www.fda.gov/drugs/guidances-drugs/newly-added-guidance-documents")
try_("fda_dsc","https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications")
try_("fda_guidance_json","https://www.fda.gov/files/api/datatables/static/search-for-guidance.json","json",3000)
# Federal Register FDA guidance availability notices
q=[("conditions[agencies][]","food-and-drug-administration"),("conditions[type][]","NOTICE"),("conditions[term]",'guidance availability'),("order","newest"),("per_page","20")]
for f in ("title","publication_date","html_url","abstract","document_number"): q.append(("fields[]",f))
try_("fedreg_guidance","https://www.federalregister.gov/api/v1/documents.json?"+urlencode(q),"json",6000)
q2=[x for x in q if x[0]!="conditions[term]"]+[("conditions[term]",'"International Council for Harmonisation"')]
try_("fedreg_ich","https://www.federalregister.gov/api/v1/documents.json?"+urlencode(q2),"json",4000)
# ICH
for k,u in {"ich_news":"https://www.ich.org/page/news","ich_home":"https://www.ich.org/","ich_rss":"https://www.ich.org/rss.xml","ich_work":"https://www.ich.org/page/ich-guidelines"}.items(): try_(k,u)
# openFDA drug enforcement (recalls)
try_("openfda_recall","https://api.fda.gov/drug/enforcement.json?search=report_date:[20261001+TO+20261031]&limit=2","json",3000)
# other safety feeds
for k,u in {"hc_recalls":"https://recalls-rappels.canada.ca/en/feed/health-products-alerts-recalls",
            "hc_rss2":"https://recalls-rappels.canada.ca/en/rss.xml",
            "tga_alerts":"https://www.tga.gov.au/feeds/alerts-medicine",
            "tga_safety":"https://www.tga.gov.au/safety/safety-alerts/rss",
            "bfarm_rhb":"https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/DE/Rote-Hand-Briefe/RSSNewsfeed.xml",
            "bfarm_risiko":"https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/DE/Risikoinformationen/RSSNewsfeed.xml",
            "pei":"https://www.pei.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeedAktuelles.xml",
            "who_alerts":"https://www.who.int/rss-feeds/news-english.xml",
            "ema_guidelines":"https://www.ema.europa.eu/en/scientific-guidelines.xml",
            "ema_consult":"https://www.ema.europa.eu/en/public-consultations.xml",
            "ema_regproc":"https://www.ema.europa.eu/en/regulatory-and-procedural-guideline.xml",
            "ema_signals":"https://www.ema.europa.eu/en/human-regulatory-overview/post-authorisation/pharmacovigilance-post-authorisation/signal-management/prac-recommendations-safety-signals",
            "ema_prac_page":"https://www.ema.europa.eu/en/committees/pharmacovigilance-risk-assessment-committee-prac",
            "mhra_guidance":"https://www.gov.uk/search/guidance-and-regulation.atom?organisations%5B%5D=medicines-and-healthcare-products-regulatory-agency",
           }.items(): try_(k,u,"raw",1500)
os.makedirs("probe/out4",exist_ok=True); json.dump(out,open("probe/out4/feeds2.json","w"),indent=1)
