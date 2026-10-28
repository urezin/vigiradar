import json, os, re, httpx, html as H
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.3",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en,de;q=0.8"}
URLS = {
 "at_news": "https://www.basg.gv.at/marktbeobachtung/amtliche-nachrichten",
 "at_dhpc": "https://www.basg.gv.at/gesundheitsberufe/sicherheitsinformationen-dhpc",
 "hr_news": "https://www.halmed.hr/Novosti-i-edukacije/Novosti/",
 "hr_rss_page": "https://www.halmed.hr/en/Novosti-i-edukacije/Newsletter-i-RSS/RSS/",
 "hr_dhpc": "https://www.halmed.hr/Farmakovigilancija/Obavijesti-za-zdravstvene-radnike/",
 "hu_ogyei": "https://ogyei.gov.hu/hirek",
 "ie_news": "https://www.hpra.ie/news-events/news",
 "ie_safety": "https://www.hpra.ie/safety-information/safety-notices",
 "ie_home": "https://www.hpra.ie/",
 "lu_news": "https://santesecu.public.lu/fr/actualites.html",
 "mt_recalls": "https://medicinesauthority.gov.mt/recallsrapidalerts",
 "mt_circ": "https://medicinesauthority.gov.mt/news-archive?id=F2",
 "mt_press": "https://medicinesauthority.gov.mt/news-archive?id=F3",
 "nl_cbg": "https://www.cbg-meb.nl/actueel/nieuws",
 "nl_cbg_rss": "https://www.cbg-meb.nl/actueel/nieuws/rss",
 "pl_gif": "https://www.gov.pl/web/gif/komunikaty",
 "pt_news": "https://www.infarmed.pt/web/infarmed/noticias",
 "pt_rss": "https://www.infarmed.pt/web/infarmed/rss",
 "pt_alerts": "https://www.infarmed.pt/web/infarmed/alertas-de-qualidade",
 "ro_news": "https://www.anm.ro/medicamente-de-uz-uman/anunturi-importante-medicamente-de-uz-uman/",
 "ro_dhpc": "https://www.anm.ro/medicamente-de-uz-uman/farmacovigilenta/comunicari-directe-catre-profesionistii-din-domeniul-sanatatii/",
 "is_new": "https://island.is/s/lyfjastofnun/frettir",
 "se_news": "https://www.lakemedelsverket.se/sv/nyheter",
 "se_rss": "https://www.lakemedelsverket.se/sv/rss/nyheter",
 "no_dmp": "https://www.dmp.no/nyheter",
 "lv_zva": "https://www.zva.gov.lv/lv/jaunumi",
 "lt_vvkt": "https://vvkt.lrv.lt/lt/naujienos",
 "be_news": "https://www.famhp.be/en/news",
 "cy_phs": "https://www.moh.gov.cy/moh/phs/phs.nsf/index_gr/index_gr?opendocument",
 "oig": "https://oig.hhs.gov/fraud/enforcement/",
 "at_rss": "https://www.basg.gv.at/whatsnew/rss",
 "hu_rss": "https://ogyei.gov.hu/rss",
 "pt_rss_pt": "https://www.infarmed.pt/web/infarmed/rss?p_p_id=rss",
}
out = {}
for k, u in URLS.items():
    try:
        r = httpx.get(u, headers=UA, timeout=30, follow_redirects=True)
        t = r.text
        links = []
        for h, x in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', t, re.S | re.I):
            tx = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
            if len(tx) > 22:
                links.append([h[:200], tx[:140]])
        feeds = re.findall(r'(?:href|value|data-url)="([^"]*(?:rss|feed|atom)[^"]*)"', t, re.I)[:40]
        dates = re.findall(r"\b\d{1,2}[./]\d{1,2}[./]20\d\d\b|\b20\d\d-\d\d-\d\d\b", t)[:10]
        out[k] = {"status": r.status_code, "url": str(r.url), "len": len(t), "links": links[:800], "feeds": feeds, "dates": dates}
    except Exception as e:
        out[k] = {"error": repr(e)[:200]}
os.makedirs("probe/out7", exist_ok=True)
json.dump(out, open("probe/out7/html.json", "w"), indent=1, ensure_ascii=False)
