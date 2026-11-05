"""Unit tests for the JD eligibility gate (agent's read-the-JD-first step)."""
from backend.app.services.eligibility import assess_jd


def test_us_citizens_only_rejects():
    r = assess_jd("We are seeking an engineer. US citizens only.")
    assert r["decision"] == "reject"
    assert any("us_citizens_only" in x for x in r["reasons"])


def test_must_be_us_citizen_rejects():
    r = assess_jd("The selected candidate must be a US citizen at time of hire.")
    assert r["decision"] == "reject"


def test_us_persons_only_rejects():
    r = assess_jd("This role is restricted to US persons only.")
    assert r["decision"] == "reject"


def test_no_sponsorship_rejects():
    r = assess_jd("We are not able to sponsor candidates for work visas.")
    assert r["decision"] == "reject"
    assert any("no_sponsorship" in x for x in r["reasons"])


def test_will_not_sponsor_rejects():
    r = assess_jd("Note: the company will not sponsor for H1B.")
    assert r["decision"] == "reject"


def test_no_visa_sponsorship_rejects():
    r = assess_jd("No visa sponsorship available for this position.")
    assert r["decision"] == "reject"


def test_future_sponsorship_boilerplate_rejects():
    jd = ("You must be authorized to work in the United States and not require, "
          "now or in the future, sponsorship for work authorization.")
    r = assess_jd(jd)
    assert r["decision"] == "reject"


def test_sponsorship_offered_applies():
    r = assess_jd("We offer visa sponsorship for the right candidate.")
    assert r["decision"] == "apply"
    assert r["checked"] is True
    assert any("offers_sponsorship" in x for x in r["signals"])


def test_silence_applies():
    jd = "Build delightful products with React and Go. 4+ years of experience."
    r = assess_jd(jd)
    assert r["decision"] == "apply"
    assert r["checked"] is True
    assert r["reasons"] == []


def test_empty_jd_applies_unchecked():
    r = assess_jd("")
    assert r["decision"] == "apply"
    assert r["checked"] is False
    r2 = assess_jd(None)
    assert r2["decision"] == "apply"
    assert r2["checked"] is False


def test_will_sponsor_is_not_a_reject():
    # "will sponsor" must not be caught by the "will not sponsor" pattern.
    r = assess_jd("We will sponsor your H1B visa if needed.")
    assert r["decision"] == "apply"


def test_sponsorship_not_offered_rejects():
    r = assess_jd("Sponsorship is not available for this role.")
    assert r["decision"] == "reject"


def test_rejection_quotes_the_phrase():
    jd = "This is a long job description. " * 20 + "US citizens only." + " " * 40
    r = assess_jd(jd)
    assert r["decision"] == "reject"
    assert any("US citizens only" in x for x in r["reasons"])


def test_security_clearance_rejects():
    r = assess_jd("This role requires the candidate be eligible to obtain and "
                  "maintain a U.S. security clearance.")
    assert r["decision"] == "reject"
    assert any("clearance_required" in x for x in r["reasons"])


def test_active_clearance_rejects():
    r = assess_jd("Must hold an active TS/SCI clearance and pass background checks.")
    assert r["decision"] == "reject"
    assert any("clearance_required" in x for x in r["reasons"])


def test_clearance_eligibility_question_rejects():
    r = assess_jd("Clearance eligibility: Do you hold a valid security clearance?")
    assert r["decision"] == "reject"


def test_benign_clearance_word_applies():
    # "clearance" outside a security context must NOT trip the gate.
    r = assess_jd("We run inventory clearance sales in our warehouse. "
                  "Spring Boot, PostgreSQL, AWS. We sponsor H1B.")
    assert r["decision"] == "apply"


# ---------------------------------------------------------------------------
# STEM-OPT-only (user rule 2026-11-05: cheap parameter pre-screen)
# ---------------------------------------------------------------------------

def test_stem_opt_eligibility_rejects():
    r = assess_jd("You must be eligible for a STEM OPT extension to qualify.")
    assert r["decision"] == "reject"
    assert any("stem_opt_only" in x for x in r["reasons"])


def test_maintain_stem_opt_rejects():
    r = assess_jd("Candidates must maintain STEM OPT status during the contract.")
    assert r["decision"] == "reject"
    assert any("stem_opt_only" in x for x in r["reasons"])


def test_stem_opt_in_sponsorship_list_applies():
    # Listing STEM OPT among sponsored visa categories is neutral, not a
    # STEM-OPT-only requirement.
    r = assess_jd("We will sponsor H1B, L1 and STEM OPT candidates.")
    assert r["decision"] == "apply"
    assert not any("stem_opt_only" in x for x in r["reasons"])


# ---------------------------------------------------------------------------
# Experience ceiling (user rule 2026-11-05: parameter pre-screen)
# ---------------------------------------------------------------------------

def test_far_above_experience_rejects():
    r = assess_jd("We require 8+ years of experience in distributed systems.")
    assert r["decision"] == "reject"
    assert any("experience_mismatch" in x for x in r["reasons"])


def test_range_above_experience_rejects():
    r = assess_jd("This is a senior role requiring 10 to 12 years of experience.")
    assert r["decision"] == "reject"
    assert any("experience_mismatch" in x for x in r["reasons"])


def test_borderline_experience_applies():
    # 5+ is borderline against a 4+ year resume — still applies (user reviews).
    r = assess_jd("5 years of experience required. We are open to sponsorship.")
    assert r["decision"] == "apply"


def test_low_experience_applies():
    r = assess_jd("3 to 5 years of Java experience. We sponsor H1B.")
    assert r["decision"] == "apply"


def test_range_masked_uses_low_end():
    from backend.app.services.eligibility import required_experience_years
    assert required_experience_years("8 to 10 years of experience") == 8
    assert required_experience_years("5 to 8 years of experience") == 5
    assert required_experience_years("5+ years of experience") == 5
    assert required_experience_years("minimum of 7 years") == 7
    assert required_experience_years("6 or more years") == 6
    assert required_experience_years("no number here") is None


# ---------------------------------------------------------------------------
# JD stack fit (user rule 2026-11-05: judge by the JD, not the title)
# ---------------------------------------------------------------------------

RESUME_SKILLS = ["Java", "Spring Boot", "React", "PostgreSQL", "SQL",
                 "AWS", "REST APIs", "Microservices"]


def test_stack_mismatch_rejects():
    from backend.app.services.eligibility import assess_jd_skills
    r = assess_jd_skills(
        "We need GraphQL, MongoDB, Redis and Kafka experts for the event platform.",
        RESUME_SKILLS)
    assert r["decision"] == "reject"
    assert any("stack_mismatch" in x for x in r["reasons"])


def test_stack_fit_applies():
    from backend.app.services.eligibility import assess_jd_skills
    r = assess_jd_skills(
        "Java Spring Boot backend with React frontend, PostgreSQL on AWS, "
        "REST APIs and microservices.",
        RESUME_SKILLS)
    assert r["decision"] == "apply"


def test_partial_overlap_applies():
    # Borderline overlap (1 hit) is NOT a clear mismatch — passes for review.
    from backend.app.services.eligibility import assess_jd_skills
    r = assess_jd_skills(
        "Platform team: GraphQL, MongoDB, Redis, plus SQL for reporting.",
        RESUME_SKILLS)
    assert r["decision"] == "apply"


def test_unknown_stack_has_no_signal():
    # JD names no canonical skills at all — nothing to judge, passes.
    from backend.app.services.eligibility import assess_jd_skills
    r = assess_jd_skills("Looking for iOS developers with Swift and UIKit.",
                         RESUME_SKILLS)
    assert r["decision"] == "apply"
