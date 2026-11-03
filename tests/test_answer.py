"""Tests for AI-drafted, humanized, truth-checked screening answers."""
from __future__ import annotations

import pytest

from backend.app.services.answer import (
    _clean_draft,
    draft_screening_answer,
    make_drafter,
    truth_violations,
)

RESUME = """Manish Badugu
Software Engineer — Java, Spring Boot, React, PostgreSQL, AWS
Verizon: Senior Software Engineer, 2021 - present. REST APIs, microservices on AWS.
Equifax: Software Engineer, 2019 - 2021. Spring Boot backend services.
"""

COMPANY = "Adyen"
JOB = "Software Engineer II (Java)"


class FakeLLM:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def is_available(self):
        return True

    def complete(self, system, user, temperature=0.2, timeout=90.0):
        self.calls += 1
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


# ---------------------------------------------------------------------------
# _clean_draft
# ---------------------------------------------------------------------------

def test_clean_strips_wrapper_and_lead_in():
    assert _clean_draft('"I worked on Spring Boot services at Verizon."') == \
        "I worked on Spring Boot services at Verizon."
    assert _clean_draft("Answer: I am based in the US.") == "I am based in the US."
    assert _clean_draft("Here is a draft: I work remotely.") == "I work remotely."
    # whitespace collapse
    assert _clean_draft("a   b\tc") == "a b c"


def test_clean_caps_length():
    long = "I work on backend services. " * 60
    assert len(_clean_draft(long)) <= 700


# ---------------------------------------------------------------------------
# truth_violations
# ---------------------------------------------------------------------------

def test_truthful_answer_passes():
    a = "I spent four years building REST APIs with Spring Boot at Verizon, " \
        "mostly on AWS."
    assert truth_violations(a, RESUME, "Why Java? " + JOB + " " + COMPANY) == []


def test_fabricated_year_fails():
    a = "I have been building microservices since 2014."
    v = truth_violations(a, RESUME, "tell me about yourself " + JOB)
    assert any("2014" in x for x in v)


def test_fabricated_company_phrase_fails():
    a = "At Global Banking Group I led a payments team."
    v = truth_violations(a, RESUME, "tell me about yourself " + JOB)
    assert any("Global Banking Group" in x for x in v)


def test_fabricated_company_suffix_fails():
    a = "I interned at Acme Corp before joining Verizon."
    v = truth_violations(a, RESUME, "tell me about yourself " + JOB)
    assert any("Acme Corp" in x for x in v)


def test_years_claim_beyond_resume_span_fails():
    a = "I bring twelve years of backend experience."
    v = truth_violations(a, RESUME, "years of experience " + JOB)
    assert any("years" in x for x in v)


def test_known_employer_passes():
    a = "Most recently at Verizon I shipped microservices on AWS."
    assert truth_violations(a, RESUME, "current employer " + JOB) == []


def test_question_and_company_context_allowed():
    a = f"I am interested in {COMPANY} because of the payments platform."
    assert truth_violations(a, RESUME, "why us " + JOB + " " + COMPANY) == []


def test_stopword_caps_pass():
    a = "I am based in the United States and open to remote work in Europe."
    assert truth_violations(a, RESUME, "location " + JOB) == []


# ---------------------------------------------------------------------------
# draft_screening_answer (fake LLM, no network)
# ---------------------------------------------------------------------------

def test_draft_happy_path():
    llm = FakeLLM(["I am based in the US and comfortable with a month's notice."])
    out = draft_screening_answer(
        "Where are you located and what is your notice period?",
        applicant_name="Manish Badugu", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=llm,
    )
    assert out == "I am based in the US and comfortable with a month's notice."
    assert llm.calls == 1


def test_draft_rejects_fabrication_then_succeeds_on_retry():
    bad = "At Acme Corp I led the payments platform."
    good = "At Verizon I built REST APIs on Spring Boot."
    llm = FakeLLM([bad, good])
    out = draft_screening_answer(
        "Tell me about your recent work.",
        applicant_name="Manish Badugu", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=llm,
    )
    assert out == good
    assert llm.calls == 2  # one retry after validation failure


def test_draft_gives_up_after_second_failure():
    llm = FakeLLM(["At Acme Corp I led the payments platform."])
    out = draft_screening_answer(
        "Tell me about your recent work.",
        applicant_name="Manish Badugu", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=llm,
    )
    assert out is None
    assert llm.calls == 2


def test_draft_no_llm_returns_none():
    assert draft_screening_answer(
        "Anything?", applicant_name="M", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=None,
    ) is None

    class Unavailable:
        def is_available(self):
            return False

    assert draft_screening_answer(
        "Anything?", applicant_name="M", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=Unavailable(),
    ) is None


def test_draft_llm_error_returns_none():
    class Boom:
        name = "boom"

        def is_available(self):
            return True

        def complete(self, *a, **k):
            raise RuntimeError("http down")

    assert draft_screening_answer(
        "Anything?", applicant_name="M", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=Boom(),
    ) is None


def test_draft_strips_wrapper_from_llm_output():
    llm = FakeLLM(['"Answer: I work with Java and Spring at Verizon."'])
    out = draft_screening_answer(
        "Recent work?", applicant_name="M", job_title=JOB, company=COMPANY,
        resume_text=RESUME, llm=llm,
    )
    assert out is not None
    assert not out.startswith('"')
    assert "Answer:" not in out


def test_make_drafter_caches_and_none_without_llm():
    assert make_drafter(applicant_name="M", job_title=JOB, company=COMPANY,
                        resume_text=RESUME, llm=None) is None
    llm = FakeLLM(["Blue, honestly."])
    d = make_drafter(applicant_name="M", job_title=JOB, company=COMPANY,
                     resume_text=RESUME, saved_answers=[], llm=llm)
    assert callable(d)
    assert d("What is your favorite color?") == "Blue, honestly."
    assert d("What is your favorite color?") == "Blue, honestly."
    assert llm.calls == 1  # cached, second call did not hit the LLM
