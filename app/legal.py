"""
Trust & legal pages: /about, /contact, /privacy, /terms, /editorial-policy.

Required for Google AdSense review (About and Contact pages, a privacy policy that discloses
advertising cookies) and for EU users (GDPR information, publisher identity). The same module
exists in the CompePharma repo; keep the two in step when the wording changes.
"""
from __future__ import annotations

from datetime import date
from html import escape as _e

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

UPDATED = "12 November 2026"
OPERATOR = "PQRA, the regulatory, quality and pharmacovigilance consultancy of Dimitrios Gkikas"
CITY = "Marousi, Athens, Greece"
EMAIL = "info@pqra.gr"
PHONE = "+30 21 0444 1982"

CSS = """.lg{max-width:780px;margin:34px auto 10px;padding:0 16px;line-height:1.65;font-size:16.5px}
.lg h1{font-size:32px;line-height:1.2;margin-bottom:6px}.lg .upd{color:#64748b;font-size:13.5px;margin-bottom:22px}
.lg h2{font-size:20px;margin:26px 0 8px}.lg p,.lg li{margin:8px 0}.lg ul{padding-left:22px}
.lg a{color:inherit;text-decoration:underline;text-underline-offset:2px}
.lg .card{border:1px solid rgba(100,116,139,.35);border-radius:12px;padding:16px 18px;margin:14px 0}
.lg nav.lgn{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:14px;margin-top:34px;padding-top:16px;border-top:1px solid rgba(100,116,139,.3)}"""


def _nav() -> str:
    return ('<nav class="lgn"><a href="/about">About</a><a href="/contact">Contact</a><a href="/editorial-policy">Editorial policy</a>'
            '<a href="/privacy">Privacy &amp; cookies</a><a href="/terms">Terms of use</a></nav>')


def bodies(s: dict) -> dict[str, tuple[str, str, str]]:
    """s: name, domain, what (one sentence), audience, sister (name, url), accounts (bool), payments (bool).
    Returns path -> (title, description, html)."""
    n, sister_n, sister_u = s["name"], s["sister"][0], s["sister"][1]
    contact = (f'<div class="card"><b>{_e(n)}</b><br>Published by {_e(OPERATOR)}<br>{_e(CITY)}<br>'
               f'Email: <a href="mailto:{EMAIL}">{EMAIL}</a><br>Phone: {_e(PHONE)}</div>')
    acc = ("<li><b>Account data</b> if you sign in to a paid product: your name, email address and the "
           "identifier returned by Google or Microsoft sign-in, used to run your account.</li>") if s.get("accounts") else ""
    pay = ("<li><b>Payment data</b> is handled by Stripe, our payment processor. We never see or store your card details; "
           "we keep the subscription status and invoices Stripe returns to us.</li>") if s.get("payments") else ""

    about = f"""<h1>About {_e(n)}</h1><div class="upd">{_e(s['domain'])}</div>
<p>{_e(s['what'])}</p>
<p>{_e(n)} is written for {_e(s['audience'])}. Every story starts from a primary record (a regulator's website, an official
journal, or a company filing with the US Securities and Exchange Commission), and every story links back to that record so
readers can check it.</p>
<h2>Who publishes {_e(n)}</h2>
<p>{_e(n)} is published by {_e(OPERATOR)}, based in {_e(CITY)}. PQRA has worked in pharmaceutical regulatory affairs,
quality and pharmacovigilance for clients in Greece and Cyprus, and that experience decides what the site covers and how
it explains it.</p>
<p>Our sister site <a href="{_e(sister_u)}">{_e(sister_n)}</a> covers the {_e(s['sister_scope'])}.</p>
<h2>How the site is funded</h2>
<p>The news is free to read. The site is funded by advertising (Google AdSense), by its paid products, and by clearly
labelled house advertisements for PQRA's consulting services. Advertisers have no influence on what we cover.</p>
<h2>How stories are written</h2>
<p>See our <a href="/editorial-policy">editorial policy</a> for our sources, our use of AI tools and how to ask for a correction.</p>
{contact}"""

    contact_html = f"""<h1>Contact {_e(n)}</h1><div class="upd">We usually reply within two working days.</div>
{contact}
<h2>What to write to us about</h2>
<ul><li><b>Corrections:</b> tell us the page address and what is wrong. We check against the official source and correct
the story, with a note on the page when the change is material.</li>
<li><b>News and announcements:</b> regulators and companies can send official documents and releases.</li>
<li><b>Advertising, enterprise plans and custom work:</b> ask for a proposal.</li>
<li><b>Privacy requests:</b> access, correction or deletion of your data, or unsubscribing from our emails
(every email also has an unsubscribe link). See our <a href="/privacy">privacy policy</a>.</li></ul>"""

    editorial = f"""<h1>Editorial policy</h1><div class="upd">Last updated {UPDATED}</div>
<h2>Sources</h2>
<p>{_e(n)} reports from primary records only: medicines regulators (EMA, FDA, MHRA and the national competent
authorities), the European Commission and official journals, courts and enforcement agencies, and company filings with
the SEC. Each story names and links its source. We do not rewrite other publications' articles and we do not link to trade
press.</p>
<h2>Use of AI tools</h2>
<p>We use AI language models (Anthropic's Claude) to read the source documents and draft the stories, analyses and opinion
pieces in our own words, following written instructions on accuracy, attribution and tone. Figures, dates and names come
from the source record that the story links to. The editorial process, its rules and the choice of what we publish are the
responsibility of the publisher, {_e(OPERATOR)}.</p>
<h2>Analysis and opinion</h2>
<p>Pieces labelled <i>Analysis</i> explain a development and its context. Pieces labelled <i>Opinion</i> or
<i>View</i> state the site's argued view. Neither is investment, medical or legal advice.</p>
<h2>Corrections</h2>
<p>If something is wrong, write to <a href="mailto:{EMAIL}">{EMAIL}</a> with the page address. We check against the
official source and correct promptly.</p>
<h2>Independence and conflicts of interest</h2>
<p>{_e(n)} and PQRA have the same owner, and PQRA's services are promoted in labelled house advertisements. Companies do not
pay for coverage, and we do not publish sponsored stories.</p>"""

    privacy = f"""<h1>Privacy &amp; cookie policy</h1><div class="upd">Last updated {UPDATED}</div>
<p>This policy explains what personal data {_e(n)} ({_e(s['domain'])}) collects, why, and your rights under the EU
General Data Protection Regulation (GDPR).</p>
<h2>Who is responsible</h2>
<p>The data controller is {_e(OPERATOR)}, {_e(CITY)}. Contact: <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>
<h2>What we collect and why</h2>
<ul><li><b>Newsletter:</b> your email address, so we can send the daily email you asked for (legal basis: your consent,
which you can withdraw at any time with the unsubscribe link).</li>
{acc}{pay}
<li><b>Messages</b> you send us, to answer them (legitimate interest).</li>
<li><b>Server logs:</b> IP address, browser and pages requested, kept for a short time for security and to keep the site
running (legitimate interest).</li></ul>
<p>We do not sell personal data.</p>
<h2>Advertising and cookies</h2>
<p>This site shows advertising from Google AdSense. Third-party vendors, including Google, use cookies to serve ads based on
a user's prior visits to this website or other websites. Google's use of advertising cookies enables it and its partners to
serve ads to you based on your visits to this site and/or other sites on the Internet.</p>
<ul><li>You can opt out of personalised advertising in Google's
<a href="https://adssettings.google.com" rel="noopener">Ads Settings</a>, or opt out of some third-party vendors' use of
cookies for personalised advertising at <a href="https://www.aboutads.info/choices/" rel="noopener">aboutads.info</a>
and <a href="https://www.youronlinechoices.eu/" rel="noopener">youronlinechoices.eu</a>.</li>
<li>How Google uses data when you use partner sites:
<a href="https://policies.google.com/technologies/partner-sites" rel="noopener">policies.google.com/technologies/partner-sites</a>.</li>
<li>Visitors in the European Economic Area, the UK and Switzerland are asked for consent through a consent message before
personalised ads are shown. You can change your choice at any time from the privacy link in that message.</li></ul>
<p>Besides advertising cookies, the site uses only cookies that are strictly necessary, for example to keep you signed in.</p>
<h2>How long we keep data</h2>
<p>Newsletter addresses until you unsubscribe; account and billing records for as long as the account is open and then as
long as Greek tax law requires; server logs for up to 90 days.</p>
<h2>Who processes data for us</h2>
<p>Our hosting provider (Render), our email delivery provider, Google (advertising and sign-in), Microsoft (sign-in) and
Stripe (payments). Some of them are in the United States; transfers rely on the EU-US Data Privacy Framework or the
European Commission's standard contractual clauses.</p>
<h2>Your rights</h2>
<p>You can ask to access, correct, delete or export your data, to restrict or object to processing, and to withdraw consent.
Write to <a href="mailto:{EMAIL}">{EMAIL}</a>. You can also complain to the Hellenic Data Protection Authority
(<a href="https://www.dpa.gr" rel="noopener">dpa.gr</a>).</p>
<h2>Changes</h2><p>We will post any change to this policy on this page, with the date at the top.</p>"""

    terms = f"""<h1>Terms of use</h1><div class="upd">Last updated {UPDATED}</div>
<p>By using {_e(s['domain'])} you accept these terms. The site is published by {_e(OPERATOR)}, {_e(CITY)}.</p>
<h2>Information only</h2>
<p>Content on {_e(n)} is for professional information. It is not medical, legal, regulatory or investment advice. Dates,
figures and decisions can change after publication: always confirm against the official source before acting on them.</p>
<h2>Intellectual property</h2>
<p>Our stories, analyses, data compilations and design belong to the publisher. You may quote short extracts with a link
to the original page. Republishing whole articles, or systematic copying or scraping, needs our written permission. Official
documents we link to remain the property of their authors.</p>
<h2>Links</h2>
<p>We link to official websites we do not control and are not responsible for their content or availability.</p>
<h2>Paid products</h2>
<p>Subscriptions and enterprise products are also governed by the terms shown when you buy them.</p>
<h2>Liability</h2>
<p>We work to keep the site accurate and available but cannot guarantee either. To the extent the law allows, we are not
liable for losses arising from use of the site or reliance on its content.</p>
<h2>Law</h2>
<p>These terms are governed by Greek law, and the courts of Athens have jurisdiction, without prejudice to any mandatory
consumer protection rules of your country of residence.</p>
<p>Questions: <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>"""

    return {
        "/about": (f"About {n}", f"Who publishes {n}, how it is funded and how its stories are written.", about),
        "/contact": (f"Contact {n}", f"Contact {n} for corrections, news, advertising, enterprise plans or privacy requests.", contact_html),
        "/editorial-policy": (f"Editorial policy | {n}", f"{n}'s sources, use of AI tools, corrections and independence.", editorial),
        "/privacy": (f"Privacy & cookie policy | {n}", f"How {n} handles personal data, cookies and advertising.", privacy),
        "/terms": (f"Terms of use | {n}", f"Terms of use for {s['domain']}.", terms),
    }


def make_router(site: dict, page_fn) -> APIRouter:
    """page_fn(title, description, path, body_html) -> HTMLResponse, using the site's own chrome."""
    router = APIRouter()
    for path, (title, desc, html) in bodies(site).items():
        def handler(_t=title, _d=desc, _p=path, _h=html) -> HTMLResponse:
            return page_fn(_t, _d, _p, f'<style>{CSS}</style><main class="lg">{_h}{_nav()}</main>')
        router.add_api_route(path, handler, methods=["GET"], response_class=HTMLResponse, include_in_schema=False)
    return router


FOOTER_LINKS = ('<a href="/about">About</a> · <a href="/contact">Contact</a> · <a href="/editorial-policy">Editorial policy</a> · '
                '<a href="/privacy">Privacy &amp; cookies</a> · <a href="/terms">Terms</a>')
PATHS = ["/about", "/contact", "/editorial-policy", "/privacy", "/terms"]
