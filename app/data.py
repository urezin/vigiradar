"""
Sample regulatory/PV updates for the VigiRadar MVP feed.

In production these rows are produced by the ingestion + AI-summary pipeline
(one row per detected change from EMA / EUR-Lex / HMA / national agencies).
The MVP serves this curated sample so the product is fully demoable before the
live source connectors are switched on.
"""

COUNTRIES = {
    "EU": "European Union (EMA / EUR-Lex)",
    "DE": "Germany (BfArM / PEI)",
    "FR": "France (ANSM)",
    "ES": "Spain (AEMPS)",
    "IT": "Italy (AIFA)",
    "NL": "Netherlands (CBG-MEB)",
    "IE": "Ireland (HPRA)",
    "SE": "Sweden (Läkemedelsverket)",
    # + 19 more member states in production
}

SUBJECTS = [
    "Signal management", "PSUR / PBRER", "Labelling & SmPC", "GVP modules",
    "Clinical trials (CTR)", "Falsified medicines", "RMP & PASS",
    "Shortages & recalls", "Fees & guidance",
]

UPDATES = [
    {"country": "EU", "auth": "EMA", "subject": "Signal management", "impact": "high",
     "date": "2026-08-11", "title": "GVP Module IX (Rev 2) — revised expectations for signal validation",
     "summary": "EMA updated Good Pharmacovigilance Practices Module IX. Signal validation must now document data-source lag and include a structured benefit-risk note within 30 days. Applies to all MAHs operating in the EU."},
    {"country": "EU", "auth": "EMA · PRAC", "subject": "Signal management", "impact": "high",
     "date": "2026-08-08", "title": "PRAC signals recommendation — August cycle",
     "summary": "PRAC recommended SmPC updates for two substance classes following new signal assessments. MAHs of affected products should prepare variations within the standard timetable."},
    {"country": "FR", "auth": "ANSM", "subject": "Falsified medicines", "impact": "high",
     "date": "2026-08-06", "title": "Updated national procedure for suspected falsified medicine reporting",
     "summary": "ANSM introduced a new reporting channel and a 24-hour notification expectation for suspected falsified products in the French supply chain. Affects wholesalers and MAHs distributing in France."},
    {"country": "DE", "auth": "BfArM", "subject": "PSUR / PBRER", "impact": "med",
     "date": "2026-08-05", "title": "PSUR submission portal — new metadata requirements",
     "summary": "BfArM now requires additional national metadata on PSUR submissions routed through the German portal. Existing EU single-assessment submissions are unaffected."},
    {"country": "ES", "auth": "AEMPS", "subject": "Shortages & recalls", "impact": "med",
     "date": "2026-08-05", "title": "Revised medicine-shortage notification obligations",
     "summary": "AEMPS shortened the advance-notice window for anticipated supply disruptions and expanded the product scope. MAHs must update their Spanish notification workflows."},
    {"country": "EU", "auth": "EUR-Lex", "subject": "Falsified medicines", "impact": "med",
     "date": "2026-07-31", "title": "Amendment to Delegated Regulation on safety features (FMD)",
     "summary": "A delegated act amends technical requirements for the unique identifier and anti-tampering device. Manufacturers and MAHs should review serialisation and pack-artwork impact."},
    {"country": "IT", "auth": "AIFA", "subject": "Labelling & SmPC", "impact": "low",
     "date": "2026-08-03", "title": "Linguistic requirements for Italian package leaflets",
     "summary": "AIFA clarified formatting and readability requirements for Italian-language leaflets. Low operational impact; affects new submissions and major variations only."},
]
