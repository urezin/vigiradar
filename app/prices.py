"""
Price & reimbursement connectors.

National medicine price / reimbursement lists, normalised to one common shape so
VigiEye can show prices per country alongside the regulatory feed. Phase 1 ships
France (the richest free, open dataset); additional countries plug in later as
new PriceSource entries (Italy AIFA, Spain Nomenclátor, Belgium INAMI, ...).

Unlike the news feed, a price dataset is a FULL REFRESH: each run replaces a
country's rows wholesale (store.prices_replace_country) rather than appending.

Note: the source files are only reachable from a host with open egress (Render),
not from the build sandbox — so this is verified on deploy, not locally.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# France — Base de données publique des médicaments (ANSM / gouv.fr).
# Two tab-separated, Windows-1252 (latin-1) files joined on the CIS code:
#   CIS_bdpm.txt      -> col1 CIS, col2 name, col3 pharmaceutical form
#   CIS_CIP_bdpm.txt  -> col1 CIS, col3 presentation, col5 marketing state,
#                        col7 CIP13, col9 reimbursement %, col10 price (euros),
#                        col11 price incl. dispensing fee
# (files have NO header row; fields are raw \t-separated; prices use a comma
#  decimal separator, e.g. "2,05".)
FR_CIS_URL = "https://base-donnees-publique.medicaments.gouv.fr/download/file/CIS_bdpm.txt"
FR_CIP_URL = "https://base-donnees-publique.medicaments.gouv.fr/download/file/CIS_CIP_bdpm.txt"

_UA = {"User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VigiEye/0.1")}


def _price_to_float(raw: str):
    """'2,05' or '5,63 €' -> float ; '' -> None.

    Handles the European comma decimal separator plus stray spaces, non-breaking
    spaces and a trailing euro sign (Italy's CSV writes prices as '5,63 €')."""
    s = (raw or "").strip().replace("€", "").replace(" ", "").replace(" ", "")
    s = s.replace(",", ".")
    if not s:
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _row_id(country: str, key: str, product: str) -> str:
    return hashlib.sha1(f"{country}|{key}|{product}".encode("utf-8", "ignore")).hexdigest()[:16]


def _col(fields: list[str], i: int) -> str:
    return fields[i].strip() if len(fields) > i else ""


def _decode(raw: bytes) -> str:
    """BDPM files are inconsistent (one UTF-8, one Windows-1252). Try UTF-8
    strictly first; if that fails it's the latin-1 file, so fall back."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _decode_try(raw: bytes, encodings: tuple[str, ...]) -> str:
    """Decode bytes trying each encoding in turn; last one uses 'replace' so it
    never raises. Used for national files whose encoding isn't documented."""
    for enc in encodings[:-1]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode(encodings[-1], "replace")


def fetch_france(timeout: float = 90.0) -> list[dict]:
    """Fetch + join the two BDPM files into normalised price rows."""
    import httpx

    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        cis = client.get(FR_CIS_URL); cis.raise_for_status()
        cip = client.get(FR_CIP_URL); cip.raise_for_status()

    # CIS code -> (drug name, pharmaceutical form)
    names: dict[str, tuple[str, str]] = {}
    for line in _decode(cis.content).splitlines():
        f = line.split("\t")
        code = _col(f, 0)
        if code:
            names[code] = (_col(f, 1), _col(f, 2))

    items: list[dict] = []
    for line in _decode(cip.content).splitlines():
        f = line.split("\t")
        code = _col(f, 0)
        if not code:
            continue
        cip13 = _col(f, 6)
        reimb = _col(f, 8)
        price = _price_to_float(_col(f, 9))
        price_fee = _price_to_float(_col(f, 10))
        if price is None and not reimb:
            continue  # no price and no reimbursement info -> nothing to show
        name, form = names.get(code, ("", ""))
        presentation = _col(f, 2)
        product = name or presentation
        items.append({
            "id": _row_id("FR", cip13 or _col(f, 1), product),
            "country": "FR",
            "product": product[:300],
            "form": form[:200],
            "presentation": presentation[:300],
            "cip13": cip13,
            "price_eur": price,
            "price_with_fee_eur": price_fee,
            "reimbursement": reimb[:16],
            "status": _col(f, 4)[:120],
            "source_url": "https://base-donnees-publique.medicaments.gouv.fr/",
        })
    return items


# ---------------------------------------------------------------------------
# Italy — AIFA "Liste di trasparenza" (generic-equivalent transparency list).
# One semicolon-delimited CSV, refreshed monthly at a STABLE (undated) URL.
# Columns (0-based):
#   0 Principio attivo, 1 Confezione di riferimento, 2 ATC, 3 AIC,
#   4 Farmaco (brand), 5 Confezione (presentation), 6 Ditta (company),
#   7 Prezzo riferimento SSN (reimbursed reference price),
#   8 Prezzo Pubblico <date> (public price), 9 Differenza, 10 Nota,
#   11 Codice gruppo equivalenza.
# Prices look like "5,63 €" (comma decimal + euro sign). Fields are CSV-quoted
# ("" escapes an inner quote), so parse with the csv module, not a bare split.
IT_CSV_URL = "https://www.aifa.gov.it/documents/20142/825643/Lista_farmaci_equivalenti.csv"


def fetch_italy(timeout: float = 90.0) -> list[dict]:
    """Fetch + parse the AIFA transparency-list CSV into normalised price rows.

    The public price (col 8) is the shelf price; the SSN reference price (col 7)
    is what the national health service reimburses, so we surface it in the
    `reimbursement` slot as a euro figure."""
    import csv
    import io
    import httpx

    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        r = client.get(IT_CSV_URL)
        r.raise_for_status()

    text = _decode_try(r.content, ("utf-8-sig", "utf-8", "cp1252", "latin-1"))
    reader = csv.reader(io.StringIO(text), delimiter=";")

    items: list[dict] = []
    for idx, f in enumerate(reader):
        if idx == 0:
            continue  # header row
        if len(f) < 9:
            continue
        active = _col(f, 0)
        aic = _col(f, 3)
        brand = _col(f, 4)
        presentation = _col(f, 5)
        public = _price_to_float(_col(f, 8))
        ssn_ref = _price_to_float(_col(f, 7))
        if public is None and ssn_ref is None:
            continue
        product = brand or active
        reimb = f"{ssn_ref:.2f} € SSN" if ssn_ref is not None else ""
        items.append({
            "id": _row_id("IT", aic or presentation, product),
            "country": "IT",
            "product": product[:300],
            "form": active[:200],          # active ingredient (form is inside presentation)
            "presentation": presentation[:300],
            "cip13": aic,                  # AIC national code reuses the code column
            "price_eur": public,
            "price_with_fee_eur": ssn_ref,
            "reimbursement": reimb[:16],
            "status": _col(f, 2)[:120],    # ATC code as a lightweight classifier
            "source_url": "https://www.aifa.gov.it/liste-di-trasparenza",
        })
    return items


@dataclass(frozen=True)
class PriceSource:
    id: str
    country: str
    label: str
    fetch: Callable[[], list[dict]]


# Phase 1 — Spain / Belgium plug in here once tractable direct-download files are
# confirmed (Spain's prices live in the Ministerio "nomenclátor de facturación",
# Belgium's in the INAMI multi-table reference DB — neither is a clean CSV yet).
PRICE_SOURCES: list[PriceSource] = [
    PriceSource("fr-bdpm", "FR", "France — Base de données publique des médicaments", fetch_france),
    PriceSource("it-aifa", "IT", "Italy — AIFA Liste di trasparenza", fetch_italy),
]
