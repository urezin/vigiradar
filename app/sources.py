"""
Source connectors.

Each SOURCE points at a real, official RSS/Atom feed. Feeds are fetched with
httpx and parsed with feedparser (handles RSS 2.0, Atom and the usual quirks).
Every raw item is normalised to a common shape before summarisation.

Adding a national agency later = append one SOURCE row (most publish RSS).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass(frozen=True)
class Source:
    id: str
    country: str          # ISO-ish key used across the app ("EU", "DE", "FR"...)
    authority: str        # display name, e.g. "EMA", "EUR-Lex", "BfArM"
    feed_url: str
    default_subject: str   # fallback subject if the classifier is unsure
    language: str = "en"   # non-English items get an English headline from the summariser


# Real, current official feeds. EMA exposes ~20 topic feeds; we start with the
# high-signal ones for regulatory affairs & pharmacovigilance teams. Two feeds
# are deliberately excluded: `whats-new` (recently-updated-page churn) and
# `agendas-and-minutes` (individual meeting presentations) — both are noisy for
# a "what regulatory thing changed" feed. Add back later with tighter filters.
SOURCES: list[Source] = [
    Source("ema-news", "EU", "EMA",
           "https://www.ema.europa.eu/en/news.xml", "GVP modules"),
    Source("ema-reg-guideline", "EU", "EMA",
           "https://www.ema.europa.eu/en/regulatory-and-procedural-guideline.xml", "GVP modules"),
    Source("ema-scientific-guideline", "EU", "EMA",
           "https://www.ema.europa.eu/en/scientific-guidelines.xml", "GVP modules"),
    Source("ema-consultations", "EU", "EMA",
           "https://www.ema.europa.eu/en/public-consultations.xml", "GVP modules"),
    Source("ema-fees", "EU", "EMA",
           "https://www.ema.europa.eu/en/fees.xml", "Fees & guidance"),
    Source("ema-inspections", "EU", "EMA",
           "https://www.ema.europa.eu/en/inspections.xml", "GVP modules"),
    # HMA — Heads of Medicines Agencies (the national-agency network). CMDh is the
    # national mutual-recognition / decentralised-procedure layer: PSUR single
    # assessments, referrals, national regulatory positions.
    Source("hma-cmdh", "EU", "HMA · CMDh",
           "https://www.hma.eu/rss/recently-published-cmdh/rss.xml", "PSUR / PBRER"),
    Source("hma-human", "EU", "HMA",
           "https://www.hma.eu/rss/recently-published-human-medicines/rss.xml", "GVP modules"),
    # EUR-Lex: Acts of the Official Journal (L series = EU legislation)
    Source("eurlex-oj-l", "EU", "EUR-Lex",
           "https://eur-lex.europa.eu/EN/display-feed.rss?rssId=165", "Falsified medicines"),
    # National agencies that DO expose RSS behind their JS news pages (found by
    # inspecting the underlying feed endpoints — more robust than HTML scraping).
    # AIFA (IT): Liferay "Notizia" feed (Italian; the AI summariser handles IT).
    Source("aifa", "IT", "AIFA",
           "https://www.aifa.gov.it/o/feedrss/rss?structureId=93604&groupId=20142&companyId=20115&baseportalURL=https://www.aifa.gov.it&size=25&excludeCategoryId=104878&locale=it_IT",
           "Signal management", "it"),
    # BfArM (DE): Government Site Builder press-release RSS (German).
    # United States — FDA official RSS feeds.
    Source("fda-press", "US", "FDA",
           "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
           "Fees & guidance"),
    Source("fda-medwatch", "US", "FDA MedWatch",
           "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
           "Shortages & recalls"),
    # United Kingdom — MHRA (GOV.UK Atom feeds)
    Source("mhra-news", "UK", "MHRA",
           "https://www.gov.uk/government/organisations/medicines-and-healthcare-products-regulatory-agency.atom",
           "Fees & guidance"),
    Source("mhra-dsu", "UK", "MHRA Drug Safety Update",
           "https://www.gov.uk/drug-safety-update.atom", "Signal management"),
    Source("mhra-alerts", "UK", "MHRA alerts",
           "https://www.gov.uk/drug-device-alerts.atom", "Shortages & recalls"),
    # European Commission DG SANTE health news (incl. HTA Regulation, pharma package)
    Source("ec-health", "EU", "European Commission (DG SANTE)",
           "https://health.ec.europa.eu/node/2/rss_en", "Fees & guidance"),
    # National agencies with RSS
    Source("ansm", "FR", "ANSM", "https://ansm.sante.fr/rss/actualites", "Signal management", "fr"),
    Source("aemps", "ES", "AEMPS", "https://www.aemps.gob.es/feed/", "Shortages & recalls", "es"),
    # HTA / market access bodies
    Source("gba", "DE", "G-BA", "https://www.g-ba.de/service/rss/", "Market access", "de"),
    Source("has", "FR", "HAS", "https://www.has-sante.fr/jcms/fc_2875171/fr/rss", "Market access", "fr"),
    # Global
    Source("who", "INT", "WHO", "https://www.who.int/rss-feeds/news-english.xml", "Fees & guidance"),
    # Compliance / enforcement (filtered to health care below)
    Source("doj", "US", "US Department of Justice", "https://www.justice.gov/news/rss?type=press_release&m=1",
           "Compliance & enforcement"),
    Source("bfarm", "DE", "BfArM",
           "https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/DE/Pressemitteilungen/RSSNewsfeed.xml",
           "Signal management", "de"),
    # --- National competent authorities (EU/EEA), official RSS feeds verified from a
    # GitHub runner on 28 Oct 2026. Non-English titles get an English headline from the
    # summariser; links always go to the agency's own page.
    Source("sukl-cz", "CZ", "SÚKL", "https://sukl.gov.cz/feed/", "Shortages & recalls", "cs"),
    Source("dkma", "DK", "DKMA", "https://laegemiddelstyrelsen.dk/da/Feeds/rss-feed", "Shortages & recalls", "da"),
    Source("ravimiamet", "EE", "Ravimiamet", "https://www.ravimiamet.ee/rss-feeds/rss.xml", "Fees & guidance", "et"),
    Source("fimea", "FI", "Fimea", "https://fimea.fi/en/current-events/-/asset_publisher/6Fp1W0OnZUR7/rss",
           "Fees & guidance"),
    Source("bfarm-rhb", "DE", "BfArM Rote-Hand-Brief",
           "https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/DE/Pharmakovigilanz/Rote-Hand-Briefe/RSSNewsfeed.xml?nn=591002",
           "Signal management", "de"),
    Source("bfarm-fsn", "DE", "BfArM field safety notices",
           "https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/EN/Kundeninfo/RSSNewsfeed.xml?nn=708434",
           "Shortages & recalls"),
    Source("pei-news", "DE", "Paul-Ehrlich-Institut",
           "https://www.pei.de/SiteGlobals/Functions/RSSFeed/RSSGenerator_Aktuell.xml?nn=165890", "Fees & guidance", "de"),
    Source("pei-safety", "DE", "Paul-Ehrlich-Institut",
           "https://www.pei.de/SiteGlobals/Functions/RSSFeed/RSSGenerator_Sicherheitsinformationen.xml?nn=165890",
           "Signal management", "de"),
    Source("eof-news", "GR", "EOF", "https://www.eof.gr/category/farmaka/anakoinoseis-farmaka/feed/",
           "Fees & guidance", "el"),
    Source("eof-safety", "GR", "EOF", "https://www.eof.gr/category/anakoinoseis-asfaleias/feed/",
           "Signal management", "el"),
    Source("eof-recalls", "GR", "EOF",
           "https://www.eof.gr/category/farmaka/anakliseis-farmakon-anthropinis-xrisis-farmaka/feed/",
           "Shortages & recalls", "el"),
    Source("eof-device-recalls", "GR", "EOF",
           "https://www.eof.gr/category/iatrotexnologika/anakliseis-iatrotexnologikon-iatrotexnologika/feed/",
           "Shortages & recalls", "el"),
    Source("nngyk", "HU", "NNGYK", "https://nngyk.gov.hu/hu/tovabbi-hirek.html?format=feed&type=rss",
           "Fees & guidance", "hu"),
    Source("aifa-en", "IT", "AIFA",
           "https://www.aifa.gov.it/o/feedrss/rss?structureId=93604&groupId=20142&companyId=20115&basePortalURL=https://www.aifa.gov.it&size=10&excludeCategoryId=104878&locale=en_US",
           "Fees & guidance"),
    Source("igj-warnings", "NL", "IGJ",
           "https://www.igj.nl/api/rss?query=%7B%22filters%22%3A%5B%7B%22field%22%3A%22information_type%22%2C%22values%22%3A%5B%22Waarschuwing%22%5D%2C%22type%22%3A%22all%22%7D%5D%2C%22resultSearchTerm%22%3A%22%22%7D",
           "Compliance & enforcement", "nl"),
    Source("igj-docs", "NL", "IGJ", "https://toezichtdocumenten.igj.nl/api/rss", "Compliance & enforcement", "nl"),
    Source("sukl-sk", "SK", "ŠÚKL", "https://www.sukl.sk/sk/rss?page_id=1355&days=365", "Fees & guidance", "sk"),
    Source("sukl-sk-recalls", "SK", "ŠÚKL", "https://www.sukl.sk/sk/rss?page_id=1355&pid=208&days=365",
           "Shortages & recalls", "sk"),
    Source("jazmp", "SI", "JAZMP", "https://www.jazmp.si/feed", "Signal management", "sl"),
    Source("basg", "AT", "BASG", "https://www.basg.gv.at/whatsnew/rss", "Shortages & recalls", "de"),
    Source("ogyei-rss", "HU", "OGYÉI", "https://ogyei.gov.hu/rss", "Shortages & recalls", "hu"),
    Source("infarmed-alerts", "PT", "INFARMED",
           "https://www.infarmed.pt/web/infarmed/rss-alertas/-/asset_publisher/grlvtkM7UJK8/rss?p_p_cacheability=cacheLevelFull",
           "Shortages & recalls", "pt"),
    Source("infarmed-news", "PT", "INFARMED",
           "https://www.infarmed.pt/web/infarmed/rss-noticias/-/asset_publisher/zQws9cSRmjar/rss?p_p_cacheability=cacheLevelFull",
           "Fees & guidance", "pt"),
    Source("infarmed-press", "PT", "INFARMED",
           "https://www.infarmed.pt/web/infarmed/rss-comunicados-de-imprensa/-/asset_publisher/19JAuDBUnYuY/rss?p_p_cacheability=cacheLevelFull",
           "Fees & guidance", "pt"),
    # --- EU institutions
    Source("act-eu", "EU", "ACT EU", "https://accelerating-clinical-trials.europa.eu/node/2/rss_en",
           "Clinical trials (CTR)"),
    Source("ec-union-register", "EU", "European Commission (Union Register)",
           "https://ec.europa.eu/health/documents/community-register/html/rss/rss_status_h.rss", "Fees & guidance"),
    Source("ema-new-medicines", "EU", "EMA", "https://www.ema.europa.eu/en/new-human-medicine-new.xml",
           "Fees & guidance"),
    # --- United States
    Source("fda-recalls", "US", "FDA recalls",
           "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml", "Shortages & recalls"),
    Source("fda-drug-recalls", "US", "FDA CDER",
           "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drug-recalls/rss.xml",
           "Shortages & recalls"),
    Source("fda-cber", "US", "FDA CBER",
           "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/biologics/rss.xml", "Fees & guidance"),
    Source("cms", "US", "CMS", "https://www.cms.gov/newsroom/rss-feeds", "Market access"),
    Source("ftc-competition", "US", "FTC", "https://www.ftc.gov/feeds/press-release-competition.xml",
           "Compliance & enforcement"),
    Source("ftc-consumer", "US", "FTC", "https://www.ftc.gov/feeds/press-release-consumer-protection.xml",
           "Compliance & enforcement"),
]

# ---------------------------------------------------------------------------
# Roadmap sources (no clean RSS/API — need per-source HTML scraping, so they're
# a heavier build than the RSS connectors above). Documented here so coverage
# gaps are explicit rather than silently missing.
#   - ICH (ich.org): guideline adoptions; partially covered today via EMA's
#     scientific-/regulatory-guideline feeds, which republish adopted ICH texts.
#   - National legislation: 27 member-state agencies + legal gazettes, each a
#     different site/language/format. Start with high-value agencies (BfArM,
#     ANSM, AEMPS, AIFA) that expose usable pages.
#   - Ethics committees: fragmented national bodies; EU clinical-trial ethics is
#     largely inside CTIS. No public feeds.
# ---------------------------------------------------------------------------

# Procedural clutter to drop regardless of source — meeting mechanics, not
# regulatory changes.
_NOISE_PREFIXES = (
    "presentation -", "presentation-", "agenda -", "agenda-", "agenda ",
    "minutes ", "minutes-", "minutes –", "minutes -", "summary report",
    "draft agenda", "product management services",
)


# EUR-Lex's Official Journal feed carries all EU law (fisheries, tariffs, food names...).
# Keep only acts that plausibly concern medicines, devices, health or PV.
_EURLEX_KEEP = ("medicin", "pharmac", "medical device", "in vitro diagnostic", "clinical trial", "health",
                "drug", "vaccin", "blood", "tissue", "cell", "human use", "veterinary", "biocid",
                "european medicines agency", "ema ", "orphan", "paediatric", "pediatric", "falsified",
                "substance", "hta", "health technology", "cosmetic", "narcotic", "precursor", "radiat",
                "ehds", "health data", "artificial intelligence", "gmp", "good manufacturing")


_DOJ_KEEP = ("health care fraud", "healthcare fraud", "pharmac", "medicare", "medicaid", "fda", "food and drug",
             "medical device", "compounding", "physician", "telemedicine", "laborator", "biotech", "kickback",
             "counterfeit medic", "prescription drug", "pill mill", "tricare", "hospital", "false claims act")
_DOJ_DROP = ("cartel", "cocaine", "methamphetamine", "trafficking conspiracy", "firearm", "gang", "homicide")
_WHO_KEEP = ("medicine", "vaccin", "regulat", "prequalif", "safety", "antimicrobial", "falsified",
             "substandard", "essential", "pharma", "drug", "health product", "biosimilar", "clinical trial")


_HEALTH_KEEP = ("pharma", "drug", "medicin", "prescription", "generic", "biosimilar", "patent", "pbm",
                "pharmacy benefit", "hospital", "health", "medical device", "insulin", "vaccin", "opioid",
                "clinical", "fda", "biolog", "glp-1", "weight loss", "supplement", "telehealth")
_FDA_FOOD = ("salad", "cheese", "snack", "chocolate", "cookie", "peanut", "almond", "milk", "flour", "sandwich",
             "soup", "frozen", "beef", "chicken", "pork", "seafood", "fish", "sauce", "spice", "candy", "cereal",
             "bakery", "bread", "ice cream", "juice", "tea ", "coffee", "nuts", "egg", "pet food", "dog", "cat ",
             "undeclared", "listeria", "salmonella", "e. coli", "allergen")


def feed_relevant(source_id: str, title: str, desc: str = "") -> bool:
    t = f"{title} {desc}".lower()
    if source_id.startswith("ftc"):
        return any(k in t for k in _HEALTH_KEEP)
    if source_id == "fda-recalls":
        return not any(k in t for k in _FDA_FOOD)
    if source_id.startswith("eurlex"):
        return eurlex_relevant(title, desc)
    if source_id == "doj":
        return any(k in t for k in _DOJ_KEEP) and not any(k in t for k in _DOJ_DROP)
    if source_id == "who":
        return any(k in t for k in _WHO_KEEP)
    return True


def eurlex_relevant(title: str, summary: str = "") -> bool:
    t = f"{title} {summary}".lower()
    return any(k in t for k in _EURLEX_KEEP)


def _is_noise(title: str) -> bool:
    t = (title or "").strip().lower()
    return any(t.startswith(p) for p in _NOISE_PREFIXES)


def item_id(source_id: str, link: str, title: str) -> str:
    """Stable dedup key for an item (link is usually unique; title backstops it)."""
    basis = f"{source_id}|{link or ''}|{title or ''}".encode("utf-8", "ignore")
    return hashlib.sha1(basis).hexdigest()[:16]


def fetch_raw_items(source: Source, timeout: float = 20.0) -> list[dict]:
    """
    Fetch and parse one feed. Returns a list of normalised raw items:
      {id, source_id, country, authority, title, link, description, published}
    Network + parse errors are swallowed per-source so one bad feed never breaks
    a whole ingestion run (the caller logs the miss).
    """
    import httpx
    import feedparser

    # Realistic browser UA + Accept: some gov sites (e.g. BfArM) reject default
    # library user-agents on their RSS endpoints.
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1"),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en,it;q=0.8,de;q=0.6,fr;q=0.6,es;q=0.6",
    }
    if "fda.gov" in source.feed_url or "ftc.gov" in source.feed_url:
        # FDA's bot protection rejects browser-imitating clients; identify honestly instead
        headers = {**headers, "User-Agent": "VigiEye regulatory monitor (+https://vigi-eye.com; info@vigi-eye.com)"}
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        resp = client.get(source.feed_url)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)

    items: list[dict] = []
    for e in parsed.entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        if link.startswith("/"):   # some feeds (AIFA) use root-relative links
            link = urljoin(source.feed_url, link)
        desc = (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
        published = (getattr(e, "published", "") or getattr(e, "updated", "") or "").strip()
        if not title or _is_noise(title):
            continue
        if not feed_relevant(source.id, title, desc):
            continue
        items.append({
            "id": item_id(source.id, link, title),
            "source_id": source.id,
            "country": source.country,
            "authority": source.authority,
            "title": title,
            "link": link,
            "description": desc,
            "published": published,
        })
    return items
