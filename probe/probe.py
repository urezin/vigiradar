import json, os, sys, time, urllib.request, urllib.parse
OUT = "out"; os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "VigiEye research info@vigi-eye.com", "Accept": "*/*"}
def get(name, url, n=6000):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60)
        b = r.read()
        open(f"{OUT}/{name}.head", "wb").write(b[:n])
        open(f"{OUT}/{name}.meta", "w").write(json.dumps({"url": url, "status": r.status, "ct": r.headers.get("content-type"), "len": len(b), "final": r.geturl()}))
        return b
    except Exception as e:
        open(f"{OUT}/{name}.meta", "w").write(json.dumps({"url": url, "error": repr(e)}))
        return b""
# US
get("openfda_orig", "https://api.fda.gov/drug/drugsfda.json?search=submissions.submission_type:ORIG+AND+submissions.submission_status:AP+AND+submissions.submission_status_date:[20260801+TO+20261031]&limit=3", 20000)
get("openfda_shortages", "https://api.fda.gov/drug/shortages.json?limit=2", 8000)
get("openfda_enforcement", "https://api.fda.gov/drug/enforcement.json?sort=report_date:desc&limit=2", 8000)
get("fda_press_rss", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml")
get("fda_medwatch_rss", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml")
get("fda_drugs_rss", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml")
get("fda_adcomm_html", "https://www.fda.gov/advisory-committees/advisory-committee-calendar", 60000)
q = urllib.parse.urlencode({"q": '"PDUFA target action date"', "forms": "8-K", "dateRange": "custom", "startdt": "2026-07-01", "enddt": "2026-10-28"})
b = get("sec_efts", "https://efts.sec.gov/LATEST/search-index?" + q, 30000)
try:
    j = json.loads(b); hits = j.get("hits", {}).get("hits", [])
    open(f"{OUT}/sec_efts_summary.json", "w").write(json.dumps({"total": j.get("hits", {}).get("total"), "first": hits[:3]}, indent=1))
    if hits:
        h = hits[0]; adsh, fn = h["_id"].split(":"); cik = h["_source"]["ciks"][0].lstrip("0")
        time.sleep(0.5)
        get("sec_doc", f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-','')}/{fn}", 40000)
except Exception as e:
    open(f"{OUT}/sec_err", "w").write(repr(e))
# EU
get("ema_medicines_json", "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json", 8000)
get("ema_medicines_xlsx_meta", "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines-report_en.xlsx", 200)
get("ema_download_page", "https://www.ema.europa.eu/en/medicines/download-medicine-data", 120000)
get("ema_news_rss", "https://www.ema.europa.eu/en/news.xml", 12000)
get("ema_dhpc_rss", "https://www.ema.europa.eu/en/human-regulatory-overview/post-authorisation/pharmacovigilance-post-authorisation/medication-errors.xml", 3000)
get("ema_referrals_json", "https://www.ema.europa.eu/en/documents/report/referrals-output-json-report_en.json", 8000)
get("ema_dhpc_json", "https://www.ema.europa.eu/en/documents/report/dhpc-output-json-report_en.json", 8000)
get("ema_shortages_json", "https://www.ema.europa.eu/en/documents/report/shortages-output-json-report_en.json", 8000)
get("ema_psusa_json", "https://www.ema.europa.eu/en/documents/report/psusa-output-json-report_en.json", 8000)
get("ema_opinions_pending_json", "https://www.ema.europa.eu/en/documents/report/medicines-output-opinions-pending-ec-decision-json-report_en.json", 8000)
