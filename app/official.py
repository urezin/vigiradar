"""
Which outbound links VigiEye / CompePharma may show.

Editorial rule (owner's instruction, 28 Oct 2026): stories are written by VigiEye from scratch and
the only outbound link is to the official primary source: a regulator, an EU / US government body,
an official register or the SEC filing. No links to trade press or other media.
"""
from __future__ import annotations

from urllib.parse import urlparse

# exact hosts or parent domains of official bodies
_OFFICIAL = (
    "europa.eu", "ema.europa.eu", "hma.eu", "who.int", "ich.org", "edqm.eu", "pic-scheme.org",
    "fda.gov", "federalregister.gov", "sec.gov", "clinicaltrials.gov", "cms.gov", "hhs.gov", "nih.gov",
    "justice.gov", "ftc.gov", "regulations.gov", "usdoj.gov", "govinfo.gov", "congress.gov",
    "gov.uk", "canada.ca", "tga.gov.au", "swissmedic.ch",
    # EU/EEA national competent authorities and HTA bodies
    "basg.gv.at", "famhp.be", "afmps.be", "bda.bg", "halmed.hr", "moh.gov.cy", "sukl.gov.cz", "sukl.cz",
    "laegemiddelstyrelsen.dk", "dkma.dk", "ravimiamet.ee", "fimea.fi", "ansm.sante.fr", "has-sante.fr",
    "sante.gouv.fr", "bfarm.de", "pei.de", "g-ba.de", "iqwig.de", "eof.gr", "nngyk.gov.hu", "ogyei.gov.hu",
    "hpra.ie", "aifa.gov.it", "zva.gov.lv", "vvkt.lt", "santesecu.public.lu", "medicinesauthority.gov.mt",
    "cbg-meb.nl", "igj.nl", "zorginstituutnederland.nl", "urpl.gov.pl", "gov.pl", "infarmed.pt", "anm.ro",
    "sukl.sk", "jazmp.si", "aemps.gob.es", "sanidad.gob.es", "lakemedelsverket.se", "tlv.se",
    "dmp.no", "legemiddelverket.no", "ima.is", "lyfjastofnun.is", "nice.org.uk", "mhra.gov.uk",
    "eur-lex.europa.eu", "health.ec.europa.eu",
)


def is_official(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return True            # relative link = our own site
    if host.endswith(".gov") or ".gov." in host or host.endswith(".gv.at"):
        return True
    return any(host == d or host.endswith("." + d) for d in _OFFICIAL)


# Where a company's OWN press release is published (its newsroom or the wire it files on). Allowed only as the
# source link of a company-release item (owner's request, 12 Nov 2026); never for media coverage.
_COMPANY_RELEASE = ("globenewswire.com", "roche.com", "ucb.com", "gedeonrichter.com", "news.cision.com")


def safe_company_url(url: str | None) -> str | None:
    if is_official(url):
        return url
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return None
    return url if host and any(host == d or host.endswith("." + d) for d in _COMPANY_RELEASE) else None


def safe_url(url: str | None) -> str | None:
    """The URL if it is an official source, else None (the caller renders no link)."""
    return url if is_official(url) else None
