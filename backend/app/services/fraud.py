"""Fraud / legitimacy screening — deterministic, token-free.

User rule (2026-11-09, copied from the job-apply-agent safety model):
- **Never pay a fee.** A legitimate employer never charges to apply.
- **Fraud checks before any personal data is shared.** Suspicious postings
  are screened from the JD text + application URL *before* the agent opens a
  browser or fills anything.

Decisions (mirrors the skill's "verify the employer … if legitimacy is
uncertain, explain the concern and ask the user before proceeding"):

- ``reject`` — hard scam signals (payment/fee demands, identity-document
  demands). The job is dropped with status ``rejected_fraud``; nothing is
  filled or submitted.
- ``review`` — uncertain legitimacy (unknown non-ATS application domain,
  pressure/urgency wording, "guaranteed job" claims). The agent does NOT
  apply; the job is parked as ``flagged_fraud_review`` and the user is
  notified. If the user confirms it's legit, the job can be applied to
  manually (or by re-running the agent).
- ``apply``  — nothing suspicious found.

Everything here is regex/allowlist based — no LLM, no network — so it runs
on every job the agent considers, cheaply and reproducibly.
"""
from __future__ import annotations

import re

# Well-known applicant-tracking / career platforms. An application URL on any
# of these (or on the company's own website) is treated as a normal channel.
_KNOWN_ATS_DOMAINS = {
    "greenhouse.io", "jobs.greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co", "api.lever.co",
    "workable.com", "jobs.workable.com",
    "ashbyhq.com", "jobs.ashbyhq.com",
    "workday.com", "myworkdayjobs.com", "jobapplicationgreen.workday.com",
    "icims.com", "jobs.icims.com", "careers.icims.com",
    "phigent.com", "jobs.phigent.com",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
    "successfactors.com", "jobs.successfactors.com", "sapphire.sf",
    "talenthero.com", "workforce.talenthero.com",
    "everhire.com", "jobs.everhire.com",
    "jobvite.com", "app.jobvite.com",
    "pinwheel.com", "jobs.pinwheel.com",
    "recruitics.com", "apply.recruitics.com",
    "zoho.com", "careers.zoho.com",
    "bamboohr.com", "jobs.bamboohr.com",
    "jazzhr.com", "jobs.jazzhr.com",
    "rippling.com", "jobs.rippling.com", "work.rippling.com",
    "teamtailor.com", "jobs.teamtailor.com",
    # Tenant-hostname ATSes — _domain() returns the last two labels, so the
    # entries below must be registrable domains (3-label strings never match).
    # (Live incident 2026-11-12: 48 real postings — Lockheed Martin, Morgan
    # Stanley, Citi, Santander, Toyota, NTT, CACI, … — held for review because
    # their Phenom/Taleo/Paylocity/Eightfold/Rippling/ADP hosts were unknown.)
    "phenompro.com", "phenompeople.com",
    "taleo.net",
    "paylocity.com",
    "ripplehire.com",
    "eightfold.ai",
    "applytojob.com",
    "zohorecruit.com",
    "breezy.hr",
    "applicantpro.com",
    "dayforcehcm.com",
    "adp.com",  # workforcenow.adp.com -> adp.com after _domain()
    "comeet.com",
    "selectminds.com",
    "pinpointhq.com",
    "oraclecloud.com",  # Oracle Cloud HCM (Taleo cloud) tenants
    "gem.com",  # jobs.gem.com — Workday-based recruiting portal
    "greenhouse", "lever",  # bare tokens for board subdomains we missed above
}

# Generic labels dropped when extracting a company's brand from a hostname.
_BRAND_GENERIC_LABELS = {
    "www", "careers", "careers-", "jobs", "job", "job-", "apply", "applied",
    "recruiting", "recruitment", "applicant", "applicants", "referral",
    "referrals", "work", "hr", "talent", "career", "people", "hiring",
}


# Suffixes that may legitimately follow a company brand in a career-domain
# label ("santander" + "careers", "smbc" + "group"). A prefix match is only
# trusted when the extra tail is one of these — an arbitrary tail
# ("acmecareersapply" vs "acme") is exactly what a phishing domain looks like.
_BRAND_OK_SUFFIXES = {
    "careers", "career", "bank", "group", "people", "financial", "financials",
    "holdings", "corp", "corporation", "inc", "tech", "technologies",
    "solutions", "services", "digital", "global", "enterprise", "systems",
    "insurance", "banking", "capital", "interactive",
}


def _same_brand(a: str, b: str) -> bool:
    """True when two domains look like the same company's properties.

    Exact/subdomain match (``comcast.com`` vs ``comcast.com`` from
    ``jobs.comcast.com``), or brand-prefix match where the longer brand's
    tail is a known innocuous suffix (``santandercareers.com`` vs
    ``santanderbank.com``, ``smbcgroup.com`` vs ``smbc.com``). Brand prefix
    must be >= 4 chars so short domains (``gem.com``, ``adp.com``) never
    cross-match, and the tail must be in ``_BRAND_OK_SUFFIXES`` so a
    registered lookalike domain can't ride on the company's brand.
    """
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True

    def brand(d: str) -> str:
        core = [l for l in d.split(".")[:-1] if l not in _BRAND_GENERIC_LABELS]
        return core[0] if core else ""

    ba, bb = brand(a), brand(b)
    if len(ba) >= 4 and len(bb) >= 4:
        if ba == bb:
            return True
        p = 0
        while p < len(ba) and p < len(bb) and ba[p] == bb[p]:
            p += 1
        if p >= 4 and (ba[p:] in _BRAND_OK_SUFFIXES or bb[p:] in _BRAND_OK_SUFFIXES):
            return True
    return False

# Hard-reject: the posting/form asks the CANDIDATE to pay or hand over
# identity documents. Legit employers never do this.
# NOTE (live false positive, 2026-11-11): the earlier pattern had an
# OPTIONAL prefix and no word boundary, so it matched "fee" INSIDE words —
# "feedback"/"feedback"/"coffee" in ordinary JDs ("the feedback loop is
# short") hard-rejected 7 real Algolia postings. Now a fee claim requires
# an explicit fee-context word ("application fee", "pay a fee", …) or an
# explicit payment verb, and "no application fee" (a reassurance, common in
# real postings) must NOT match — hence the (?<!no\s) guard.
_FEE_PATTERNS = re.compile(
    r"""
    (?<!no\s)(?:registration|application|processing|training|onboarding|enrollment)\s+fees?
    |pay\s+(?:a|an|the)?\s*(?:fees?|cost|amount|charge)
    |pay\s+to\s+apply
    |payment\s+(?:is|required|of)\s
    |wire\s+transfer
    |money\s+order
    |crypto(?:currency)?\s+(?:payment|transfer|wallet)
    |bitcoin
    |upfront\s+payment
    |advance\s+payment
    """,
    re.I | re.X,
)

_ID_DOC_PATTERNS = re.compile(
    r"""
    send\s+(?:us\s+)?(?:a\s+)?(?:copy\s+of\s+)?(?:your\s+)?(?:passport|id(?:entity)?\s*card|driver'?s?\s*license|social\s*security|ssn)
    |verify\s+(?:your\s+)?identity\s+with
    |share\s+your\s+(?:bank\s+account|social\s*security|passport|id\b)
    |background\s+check\s+fee
    """,
    re.I | re.X,
)

# Review flags: not proof of fraud, but exactly the "legitimacy uncertain —
# ask the user first" signals from the safety model.
_URGENCY_PATTERNS = re.compile(
    r"""
    (?:within|in)\s+(?:24|48)\s*hours
    |last\s+chance
    |act\s+now
    |apply\s+immediately
    |immediate\s+decision
    |position\s+(?:will\s+be\s+)?(?:closed|filled)\s+tonight
    """,
    re.I | re.X,
)

_GUARANTEE_PATTERNS = re.compile(
    r"""
    guaranteed\s+(?:job|position|placement|hiring)
    |100\s*%\s*(?:placement|success|job)\s+guarantee
    |guaranteed\s+salary\s+before
    """,
    re.I | re.X,
)


# Form-level payment guard (checked on the OPEN application form, before a
# single field is typed). A legitimate employer's application form never
# asks for card details or money.
# NOTE (live false positive, 2026-11-11): bare "credit card" / "debit card"
# are NOT in this list on purpose — a fintech employer's form page can carry
# product marketing copy ("one in three credit cards is in our ecosystem")
# that is not a payment request. Only field/request-shaped patterns count.
_PAYMENT_FORM_RE = re.compile(
    r"""
    card\s+number
    |credit\s+card\s+number
    |cvv\b
    |billing\s+address
    |payment\s+method
    |how\s+(?:would|will|do)\s+you\s+(?:like|want)\s+to\s+pay
    |pay\s+(?:a|an|the)\s*(?:fee|cost|amount|charge)
    |registration\s+fee
    |processing\s+fee
    |fee\s+is\s+(?:required|mandatory)
    """,
    re.I | re.X,
)


def scan_payment_in_text(body: str) -> str | None:
    """Return a short quote when application-form text demands payment."""
    try:
        flat = re.sub(r"\s+", " ", body or "")
    except Exception:
        return None
    m = _PAYMENT_FORM_RE.search(flat)
    if not m:
        return None
    start = max(0, m.start() - 40)
    return flat[start: min(len(flat), m.end() + 60)].strip()


def _domain(url: str | None) -> str:
    """Best-effort registrable-ish domain (last two labels), lowercased."""
    if not url:
        return ""
    u = url.lower().strip()
    u = re.sub(r"^[a-z]+://", "", u)
    host = u.split("/")[0].split(":")[0]
    host = re.sub(r"^www\.", "", host)
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def assess_job_fraud(
    jd_text: str,
    apply_url: str | None,
    company_website: str | None = None,
) -> dict:
    """Screen a job for scam signals BEFORE any browser/form work.

    Returns ``{"decision": "apply"|"review"|"reject", "reasons": [str],
    "note": str}``.
    """
    text = jd_text or ""
    reject_reasons: list[str] = []
    review_reasons: list[str] = []

    m = _FEE_PATTERNS.search(text)
    if m:
        reject_reasons.append(
            f"posting asks the candidate to pay ({m.group(0).strip()!r}) — "
            "legitimate employers never charge to apply")
    m = _ID_DOC_PATTERNS.search(text)
    if m:
        reject_reasons.append(
            f"posting demands identity/financial documents ({m.group(0).strip()!r})")

    m = _URGENCY_PATTERNS.search(text)
    if m:
        review_reasons.append(f"pressure/urgency wording: {m.group(0).strip()!r}")
    m = _GUARANTEE_PATTERNS.search(text)
    if m:
        review_reasons.append(f"'guaranteed job' claim: {m.group(0).strip()!r}")

    # Application-channel check: where does the form actually live?
    # Only comparable when the company's own site is known — an unknown
    # company website means "can't tell", not "suspect".
    job_dom = _domain(apply_url)
    site_dom = _domain(company_website)
    if (job_dom and site_dom
            and job_dom not in _KNOWN_ATS_DOMAINS
            and not _same_brand(job_dom, site_dom)):
        review_reasons.append(
            f"application form is on {job_dom} — not a known ATS and not "
            f"the company's own site ({site_dom})")

    if reject_reasons:
        return {
            "decision": "reject",
            "reasons": reject_reasons,
            "note": "Fraud screen rejected — scam signals in the posting "
                    "(fee/identity-document demand). Nothing was filled or "
                    "submitted.",
        }
    if review_reasons:
        return {
            "decision": "review",
            "reasons": review_reasons,
            "note": "Fraud screen flagged this posting as uncertain — held "
                    "for your review before any personal data is shared.",
        }
    return {"decision": "apply", "reasons": [], "note": "fraud screen passed"}
