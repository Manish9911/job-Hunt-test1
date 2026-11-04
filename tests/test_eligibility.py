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
