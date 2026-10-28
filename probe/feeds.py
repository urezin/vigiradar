import json, os, urllib.request, re
import xml.etree.ElementTree as ET
UA={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1","Accept":"application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
FEEDS = {
 # regulators / official
 "mhra_atom":"https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
 "mhra_dsu":"https://www.gov.uk/drug-safety-update.atom",
 "mhra_alerts":"https://www.gov.uk/drug-device-alerts.atom",
 "fda_recalls":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml",
 "fda_biologics":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/biologics/rss.xml",
 "fda_whatsnew_drugs":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/what-s-new-drugs/rss.xml",
 "fda_devices":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medical-devices/rss.xml",
 "fda_safety_comms":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drug-safety-communications/rss.xml",
 "fda_warning_letters":"https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/warning-letters/rss.xml",
 "who_news":"https://www.who.int/rss-feeds/news-english.xml",
 "ec_health_news":"https://health.ec.europa.eu/node/2/rss_en",
 "ec_presscorner_health":"https://ec.europa.eu/commission/presscorner/api/rss?language=en&policyarea=HEALTH",
 "ema_whatsnew":"https://www.ema.europa.eu/en/whats-new.xml",
 "ema_press":"https://www.ema.europa.eu/en/press-releases.xml",
 "hpra":"https://www.hpra.ie/rss/news.xml",
 "ansm":"https://ansm.sante.fr/rss/actualites",
 "aemps":"https://www.aemps.gob.es/feed/",
 "swissmedic":"https://www.swissmedic.ch/swissmedic/en/home/news.rss",
 # HTA / market access
 "nice_news":"https://www.nice.org.uk/news/feed",
 "nice_guidance":"https://www.nice.org.uk/guidance/published/rss",
 "gba":"https://www.g-ba.de/service/rss/",
 "gba_news":"https://www.g-ba.de/presse/pressemitteilungen-meldungen/rss/",
 "has":"https://www.has-sante.fr/jcms/fc_2875171/fr/rss",
 "iqwig":"https://www.iqwig.de/rss/news-de.xml",
 "cms_newsroom":"https://www.cms.gov/newsroom/rss-feeds/press-releases",
 "icer":"https://icer.org/feed/",
 # compliance / enforcement
 "oig_hhs":"https://oig.hhs.gov/rss/oig-newsroom.xml",
 "doj_opa":"https://www.justice.gov/news/rss?type=press_release&m=1",
 "efpia":"https://www.efpia.eu/rss/news",
 "ec_competition":"https://ec.europa.eu/commission/presscorner/api/rss?language=en&policyarea=COMP",
 # industry press (headline+link only)
 "biopharmadive":"https://www.biopharmadive.com/feeds/news/",
 "fiercepharma":"https://www.fiercepharma.com/rss/xml",
 "fiercebiotech":"https://www.fiercebiotech.com/rss/xml",
 "endpoints":"https://endpts.com/feed/",
 "statnews":"https://www.statnews.com/feed/",
 "raps":"https://www.raps.org/rss/news",
 "pharmatimes":"https://pharmatimes.com/feed/",
 "europeanpharmaceuticalreview":"https://www.europeanpharmaceuticalreview.com/feed/",
 "pharmaphorum":"https://pharmaphorum.com/rss.xml",
 "biospace":"https://www.biospace.com/rss",
 "pmlive":"https://pmlive.com/feed/",
 "fiercehealthcare":"https://www.fiercehealthcare.com/rss/xml",
 "pharmaceutical_technology":"https://www.pharmaceutical-technology.com/feed/",
 "in_pharmatechnologist":"https://www.in-pharmatechnologist.com/Info/RSS",
 "outsourcing_pharma":"https://www.outsourcing-pharma.com/Info/RSS",
}
out={}
for k,u in FEEDS.items():
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=30); b=r.read()
        try:
            root=ET.fromstring(b)
            items=root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            titles=[]
            for it in items[:5]:
                t=it.find("title") if it.find("title") is not None else it.find("{http://www.w3.org/2005/Atom}title")
                d=None
                for tag in ("pubDate","{http://www.w3.org/2005/Atom}updated","{http://purl.org/dc/elements/1.1/}date","{http://www.w3.org/2005/Atom}published"):
                    if it.find(tag) is not None: d=it.find(tag).text; break
                titles.append(f"{(d or '')[:25]} | {(t.text or '').strip()[:110] if t is not None else ''}")
            out[k]={"ok":True,"status":r.status,"items":len(items),"sample":titles}
        except Exception as e:
            out[k]={"ok":False,"status":r.status,"ct":r.headers.get("content-type"),"parse":repr(e)[:120],"head":b[:200].decode("utf-8","replace")}
    except Exception as e:
        out[k]={"ok":False,"error":repr(e)[:160]}
# Federal Register (why adcomm may fail)
try:
    u="https://www.federalregister.gov/api/v1/documents.json?conditions%5Bagencies%5D%5B%5D=food-and-drug-administration&conditions%5Btype%5D%5B%5D=NOTICE&per_page=2"
    r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=30); out["fedreg"]={"ok":True,"status":r.status}
except Exception as e: out["fedreg"]={"ok":False,"error":repr(e)[:160]}
# SEC: recent biotech press releases with regulatory terms
from urllib.parse import urlencode
for q in ['"CHMP" "positive opinion"','"FDA approval"','"Complete Response Letter"','"European Commission" "marketing authorization"','"reimbursement"']:
    try:
        u="https://efts.sec.gov/LATEST/search-index?"+urlencode({"q":q,"forms":"8-K","dateRange":"custom","startdt":"2026-10-14","enddt":"2026-10-28"})
        j=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"VigiEye research info@vigi-eye.com"}),timeout=30).read())
        out["sec "+q]={"total":j["hits"]["total"]["value"],"sample":[h["_source"]["display_names"][0][:60]+" "+h["_source"]["file_date"] for h in j["hits"]["hits"][:4]]}
    except Exception as e: out["sec "+q]={"error":repr(e)[:120]}
os.makedirs("probe/out3",exist_ok=True); json.dump(out,open("probe/out3/feeds.json","w"),indent=1)
