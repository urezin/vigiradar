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


def _xlsx_col(ref: str) -> int:
    """'C12' -> 2 (0-based column index from an A1-style cell reference)."""
    letters = ""
    for ch in ref:
        if ch.isalpha():
            letters += ch
        else:
            break
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def read_xlsx(content: bytes, sheet_index: int = 0, max_rows: int | None = None) -> list[list[str]]:
    """Read an .xlsx (Office Open XML) with the STANDARD LIBRARY only.

    An xlsx is a zip of XML, so zipfile + ElementTree parse it without openpyxl
    (whose install was breaking the Render build). Returns rows of string cells,
    honouring column gaps via each cell's A1 reference so columns stay aligned."""
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    zf = zipfile.ZipFile(io.BytesIO(content))
    names = zf.namelist()

    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))

    sheet_parts = sorted(n for n in names
                         if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not sheet_parts:
        return []
    root = ET.fromstring(zf.read(sheet_parts[min(sheet_index, len(sheet_parts) - 1)]))

    rows: list[list[str]] = []
    for row in root.iter(ns + "row"):
        cells: dict[int, str] = {}
        maxc = -1
        auto = 0
        for c in row.findall(ns + "c"):
            ref = c.get("r") or ""
            ci = _xlsx_col(ref) if ref else auto
            auto = ci + 1
            t = c.get("t")
            val = ""
            v = c.find(ns + "v")
            if t == "s" and v is not None:
                try:
                    val = shared[int(v.text)]
                except (ValueError, IndexError, TypeError):
                    val = ""
            elif t == "inlineStr":
                is_ = c.find(ns + "is")
                if is_ is not None:
                    val = "".join(tt.text or "" for tt in is_.iter(ns + "t"))
            elif v is not None:
                val = v.text or ""
            cells[ci] = val
            if ci > maxc:
                maxc = ci
        rows.append([cells.get(i, "") for i in range(maxc + 1)])
        if max_rows and len(rows) >= max_rows:
            break
    return rows


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


# ---------------------------------------------------------------------------
# Spain — Ministerio de Sanidad "Nomenclátor de facturación" (SNS pharmacy
# billing list). A comma-delimited CSV export ("...&<hex 'export'>=1"); the
# "%%%" wildcard (url-encoded %25%25%25) returns every product. Prices use a DOT
# decimal and names containing commas are double-quoted, so parse with the csv
# module (default comma delimiter + quoting), not a bare split.
# Columns (0-based): 0 Código Nacional, 1 Nombre del producto, 6 Estado
#   (ALTA / BAJA…), 9 Aportación del beneficiario (NORMAL / ESPECIAL…),
#   10 Principio activo, 11 Precio venta al público con IVA (PVP),
#   12 Precio de referencia, 15 Nombre de la agrupación homogénea.
ES_CSV_URL = ("https://www.sanidad.gob.es/profesionales/nomenclator.do"
              "?metodo=buscarProductos&especialidad=%25%25%25&d-4015021-e=1&6578706f7274=1")


def fetch_spain(timeout: float = 120.0) -> list[dict]:
    """Fetch + parse the SNS billing nomenclátor CSV into normalised price rows.

    Keeps only currently-listed products (Estado not BAJA). PVP (col 11) is the
    retail price; the reference price (col 12) rides in price_with_fee_eur, and
    the patient contribution class (col 9) surfaces in the reimbursement slot."""
    import csv
    import io
    import httpx

    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        r = client.get(ES_CSV_URL)
        r.raise_for_status()

    text = _decode_try(r.content, ("utf-8-sig", "utf-8", "cp1252", "latin-1"))
    reader = csv.reader(io.StringIO(text))

    items: list[dict] = []
    for idx, f in enumerate(reader):
        if idx == 0 or len(f) < 13:
            continue
        estado = _col(f, 6)
        if "BAJA" in estado.upper():
            continue  # delisted -> not currently marketed
        pvp = _price_to_float(_col(f, 11))
        ref = _price_to_float(_col(f, 12))
        if pvp is None and ref is None:
            continue
        code = _col(f, 0)
        product = _col(f, 1)
        items.append({
            "id": _row_id("ES", code or product, product),
            "country": "ES",
            "product": product[:300],
            "form": _col(f, 10)[:200],        # active ingredient (Principio activo)
            "presentation": _col(f, 15)[:300],  # agrupación homogénea (normalised pack)
            "cip13": code,                    # Código Nacional reuses the code column
            "price_eur": pvp,
            "price_with_fee_eur": ref,
            "reimbursement": _col(f, 9)[:16],  # Aportación del beneficiario
            "status": estado[:120],
            "source_url": "https://www.sanidad.gob.es/profesionales/nomenclator.do",
        })
    return items


# ---------------------------------------------------------------------------
# Slovenia — JAZMP list of regulated (wholesale) medicine prices. A monthly
# .xlsx at a DATE-STAMPED URL (cene_YYYYMMDD.xlsx, dated the 1st), so we resolve
# the latest by walking back month-by-month from today. Eurozone, so the price
# is already in EUR. Columns (0-based, data from row 2; row 0 = note, row 1 =
# header): 0 Ident, 1 Ime zdravila in pakiranje (name+pack), 2 ATC,
# 3 splošno ime (generic name), 4 company, 5 dispensing regime,
# 6 "Cena na debelo €" (wholesale price).
SI_BASE = "https://www.jazmp.si/fileadmin/datoteke/seznami/SFE/Cene/cene_{ymd}.xlsx"


def _si_candidate_urls() -> list[str]:
    from datetime import date
    today = date.today()
    urls = []
    for i in range(0, 8):
        mm, yy = today.month - i, today.year
        while mm <= 0:
            mm += 12
            yy -= 1
        urls.append(SI_BASE.format(ymd=f"{yy:04d}{mm:02d}01"))
    return urls


def fetch_slovenia(timeout: float = 90.0) -> list[dict]:
    """Resolve the latest JAZMP monthly price xlsx and parse it (stdlib reader)."""
    import httpx

    content = None
    with httpx.Client(timeout=timeout, headers=_UA, follow_redirects=True) as client:
        for u in _si_candidate_urls():
            try:
                r = client.get(u)
            except Exception:
                continue
            if r.status_code == 200 and r.content[:2] == b"PK":
                content = r.content
                break
    if not content:
        raise RuntimeError("no JAZMP price file found in the last 8 months")

    items: list[dict] = []
    for f in read_xlsx(content)[2:]:      # skip note row + header row
        code = _col(f, 0)
        name = _col(f, 1)
        price = _price_to_float(_col(f, 6))
        if not name or price is None:
            continue
        items.append({
            "id": _row_id("SI", code or name, name),
            "country": "SI",
            "product": name[:300],
            "form": _col(f, 3)[:200],      # generic name (splošno ime)
            "presentation": "",
            "cip13": code,
            "price_eur": price,
            "price_with_fee_eur": None,
            "reimbursement": "",
            "status": _col(f, 2)[:120],    # ATC
            "source_url": "https://www.jazmp.si/",
        })
    return items


@dataclass(frozen=True)
class PriceSource:
    id: str
    country: str
    label: str
    fetch: Callable[[], list[dict]]


# Belgium plugs in here once its price file is tractable (INAMI/CBIP publish a
# multi-table reference DB — the public price sits across joined tables / behind
# the SAM portal — rather than one clean priced CSV like FR/IT/ES).
PRICE_SOURCES: list[PriceSource] = [
    PriceSource("fr-bdpm", "FR", "France — Base de données publique des médicaments", fetch_france),
    PriceSource("it-aifa", "IT", "Italy — AIFA Liste di trasparenza", fetch_italy),
    PriceSource("es-nomen", "ES", "Spain — Nomenclátor de facturación (Min. Sanidad)", fetch_spain),
    PriceSource("si-jazmp", "SI", "Slovenia — JAZMP regulated prices", fetch_slovenia),
]
