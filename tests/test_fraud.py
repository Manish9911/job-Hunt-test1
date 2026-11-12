"""Tests for the fraud / legitimacy screen (permission model, 2026-11-09).

Deterministic — no LLM, no network. Mirrors the job-apply-agent safety rule:
never pay a fee, verify the employer before sharing personal data.
"""
from __future__ import annotations

from backend.app.services.fraud import assess_job_fraud, scan_payment_in_text


# ---------------------------------------------------------------- fee reject
def test_fee_demand_is_a_hard_reject():
    r = assess_job_fraud(
        "Software Engineer. A registration fee of $250 is required to process "
        "your application.",
        "https://jobs.greenhouse.io/acme",
        "https://acme.com",
    )
    assert r["decision"] == "reject"
    assert any("pay" in x for x in r["reasons"])


def test_wire_transfer_demand_rejected():
    r = assess_job_fraud(
        "Selected candidates must complete a wire transfer for onboarding.",
        "https://acme.com/careers",
        "https://acme.com",
    )
    assert r["decision"] == "reject"


def test_id_document_demand_rejected():
    r = assess_job_fraud(
        "Send us a copy of your passport to verify your identity before we "
        "review your application.",
        "https://acme.com/careers",
        "https://acme.com",
    )
    assert r["decision"] == "reject"


def test_fee_inside_a_word_is_not_a_reject():
    """Algolia incident (2026-11-11): the old pattern matched 'fee' inside
    'feedback' — 7 real Algolia postings were hard-rejected on 'the
    feedback loop is short'. Words containing 'fee' must never trigger."""
    r = assess_job_fraud(
        "Senior SWE. The feedback loop is short; you will give and receive "
        "constructive feedback. We serve coffee daily.",
        "https://job-boards.greenhouse.io/algolia/jobs/6185779004",
        "https://algolia.com",
    )
    assert r["decision"] == "apply"
    assert r["reasons"] == []


def test_explicit_fee_demands_still_rejected():
    r = assess_job_fraud(
        "An application fee of $99 is required before we review your profile.",
        "https://jobs.greenhouse.io/acme",
        "https://acme.com",
    )
    assert r["decision"] == "reject"

    r2 = assess_job_fraud(
        "Candidates must pay a processing fee to be considered.",
        "https://jobs.greenhouse.io/acme",
        "https://acme.com",
    )
    assert r2["decision"] == "reject"


def test_no_fee_reassurance_is_not_a_reject():
    # "no application fee" is a REASSURANCE in real postings — must pass.
    r = assess_job_fraud(
        "Senior Engineer. There is no application fee for this role.",
        "https://jobs.lever.co/acme/abc",
        "https://acme.com",
    )
    assert r["decision"] == "apply"


# ---------------------------------------------------------------- review
def test_unknown_application_domain_is_review():
    # Form lives on a domain that is neither a known ATS nor the company's
    # own site -> legitimacy uncertain, held for the user.
    r = assess_job_fraud(
        "Senior Backend Engineer. We are growing fast.",
        "https://careers-apply.xyz/form",
        "https://acme.com",
    )
    assert r["decision"] == "review"
    assert any("careers-apply.xyz" in x for x in r["reasons"])


def test_known_ats_domain_is_apply():
    r = assess_job_fraud(
        "Senior Backend Engineer. Java, Spring Boot, PostgreSQL.",
        "https://boards.greenhouse.io/acme/jobs/123",
        "https://acme.com",
    )
    assert r["decision"] == "apply"


def test_company_own_site_is_apply():
    r = assess_job_fraud(
        "Senior Backend Engineer. Java, Spring Boot, PostgreSQL.",
        "https://acme.com/careers/123",
        "https://acme.com",
    )
    assert r["decision"] == "apply"


def test_subdomain_of_company_site_is_apply():
    r = assess_job_fraud(
        "Backend Engineer",
        "https://careers.acme.com/apply/123",
        "https://acme.com",
    )
    assert r["decision"] == "apply"


# Live incident 2026-11-12: 48 real postings (Lockheed Martin, Morgan
# Stanley, Citi, Santander, Toyota, NTT, CACI, FICO, Allstate, Comcast …)
# held for review because their ATS tenant domains / brand-variant career
# domains were unknown.
def test_tenant_ats_domains_are_apply():
    clean = "Backend Engineer. Java and Spring."
    for url, site in [
        ("https://caci.phenompro.com/global/en/job/330051/x", "https://caci.com"),
        ("https://ntt.phenompeople.com/global/en/job/397544/x", "https://www.nttdata.com"),
        ("https://phg.tbe.taleo.net/phg04/ats/careers/requisition.jsp", "https://www.masterpeacesolutions.com"),
        ("https://recruiting.paylocity.com/Recruiting/Jobs/Details/449", "https://trax.aero"),
        ("https://mphasis.ripplehire.com/candidate/?token=abc", "https://www.mphasis.com"),
        ("https://lockheedmartin.eightfold.ai/job/x", "https://www.lockheedmartin.com"),
        ("https://workforcenow.adp.com/jobs/x", "https://www.ellipsis.com"),
        ("https://www.comeet.com/jobs/x", "https://www.taboola.com"),
    ]:
        r = assess_job_fraud(clean, url, site)
        assert r["decision"] == "apply", f"{url}: {r}"


def test_brand_variant_career_domain_is_apply():
    clean = "Senior Java Developer."
    for url, site in [
        ("https://www.santandercareers.com/job/x", "https://www.santanderbank.com"),
        ("https://jobs.comcast.com/x", "https://www.comcast.com"),
        ("https://www.allstate.jobs/x", "https://www.allstate.com"),
        ("https://careers.smbcgroup.com/x", "https://www.smbc.com"),
        ("https://app.dover.com/x", "https://www.dover.com"),
        ("https://careers.pageuppeople.com/x", "https://www.pageup.com"),
    ]:
        r = assess_job_fraud(clean, url, site)
        assert r["decision"] == "apply", f"{url} vs {site}: {r}"


def test_lookalike_phishing_domain_stays_review():
    # "acmecareersapply.com" is a REGISTERED lookalike: brand tail
    # "careersapply" is not an innocuous suffix -> must stay held.
    r = assess_job_fraud("Backend Engineer", "https://acmecareersapply.com/x", "https://acme.com")
    assert r["decision"] == "review"
    # ...and short brands never cross-match.
    from backend.app.services import fraud
    assert not fraud._same_brand("gem.com", "gemini.example.com")
    assert not fraud._same_brand("adp.com", "advent.com")


def test_unknown_company_site_does_not_flag_domain():
    # No company website on file -> "can't tell", not "suspect": the
    # domain check is skipped (fee/ID/urgency patterns still apply).
    r = assess_job_fraud(
        "Backend Engineer. Java and Spring.",
        "https://careers-apply.xyz/form",
        None,
    )
    assert r["decision"] == "apply"


def test_urgency_wording_is_review():
    r = assess_job_fraud(
        "Hiring now — apply within 24 hours or the position will close.",
        "https://boards.greenhouse.io/acme",
        "https://acme.com",
    )
    assert r["decision"] == "review"


def test_guaranteed_job_claim_is_review():
    r = assess_job_fraud(
        "100% placement guarantee. Guaranteed job before you apply.",
        "https://acme.com/careers",
        "https://acme.com",
    )
    assert r["decision"] == "review"


# ---------------------------------------------------------------- clean pass
def test_clean_posting_on_clean_channel_applies():
    r = assess_job_fraud(
        "Senior Software Engineer — Java, Spring Boot, React, PostgreSQL, AWS. "
        "4+ years experience. We sponsor H1B and L1.",
        "https://jobs.lever.co/acme/abc",
        "https://acme.com",
    )
    assert r["decision"] == "apply"
    assert r["reasons"] == []


# --------------------------------------------------- form-level payment guard
def test_payment_form_text_detected():
    assert scan_payment_in_text("Credit card number is required to continue")
    assert scan_payment_in_text("How would you like to pay your processing fee")
    assert scan_payment_in_text("Billing address: ____")


def test_payment_marketing_copy_not_flagged():
    """Method incident (2026-11-11): a fintech's form page carries product
    copy about credit cards — that is NOT a payment request."""
    assert scan_payment_in_text(
        "One in every three credit cards in the United States is in the "
        "Method ecosystem and leading the way to financial wellness.") is None
    assert scan_payment_in_text(
        "We help people manage their debit cards and savings.") is None


def test_clean_form_text_not_flagged():
    assert scan_payment_in_text("Full name. Email. Years of experience. Skills") is None
    assert scan_payment_in_text("We pay a competitive salary and benefits") is None


def test_empty_inputs():
    assert assess_job_fraud("", None, None)["decision"] == "apply"
