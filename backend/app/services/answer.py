"""AI-drafted answers for screening questions with no stored answer.

User rule (2026-11-02): if a form asks a random/unexpected question, answer it
with the LLM in a fully human voice — "no AI footprints": nothing that reads
like a chatbot or a marketing blurb.

The truth contract still applies. The LLM may only use facts present in the
master resume and the user's saved profile answers. Every draft is validated
mechanically before it is filled in:

  * any year (19xx/20xx) mentioned must appear in the resume, and any
    "N years" claim must be within the resume's own date span;
  * any multi-word capitalised phrase (the shape of a company name) must be
    found in the resume / question / job context, plus a suffix check for
    lone company-sounding tokens (Acme Corp, Foo Inc, ...).

A failed validation triggers exactly one retry with the violations spelled
out; a second failure returns ``None`` and the field is left blank + logged
(same behaviour as before this feature). With no LLM configured, everything
here returns ``None`` and the deterministic flow is unchanged.
"""
from __future__ import annotations

import re
from datetime import date

# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

HUMANIZE_SYSTEM = """You are writing a short screening-question answer inside a job
application form, AS the applicant themselves. It will be typed into a text box
and read by a recruiter. The answer must sound like a real engineer filling in
a form quickly — natural, direct, specific.

VOICE RULES (no AI footprints — obey strictly):
- 1 to 3 sentences (a fourth only if the question is big). No bullets, no
  lists, no headings, no hashtags, no em-dash chains.
- Plain words. No "leverage", "utilize", "delve", "furthermore", "additionally",
  "seamless", "robust", "proactive", "passionate", "thrilled", "excited to",
  "great fit", "I would like to highlight", "I am confident that", "in summary".
- Vary sentence openings; do not start two sentences with "I".
- Answer exactly what was asked. Do not pad with a self-praise paragraph at the end.
- If the question is simple, the answer is short. If the question is a "why
  us / what motivates you" question, tie it to ONE concrete thing from the
  job description or the applicant's real experience — no generic flattery.

TRUTH RULES (hard contract):
- You may ONLY use facts from the resume and profile answers provided below:
  employers, projects, technologies, dates, years of experience, location,
  notice period, salary position, work authorization, interests.
- You may refer to the company being applied to and what it does (from the
  job context), and you may express normal human interest in the role.
- NEVER invent employers, clients, projects, dates, degrees, certifications,
  numbers, or skills. If a hard fact the question needs is not provided
  (e.g. exact current salary), give a short, honest, non-committal human
  answer (e.g. "open to discussion for the right role") instead of a number.
- Output the answer text only — no preamble, no quotes, no "Answer:" label.
"""


def _clean_draft(text: str) -> str:
    """Strip wrapper garbage an LLM might add, collapse whitespace, cap length."""
    t = (text or "").strip()
    t = re.sub(r"^[\s\"'`*]+", "", t)
    t = re.sub(r"[\s\"'`*]+$", "", t)
    # common LLM lead-ins (longest first so "Here is a draft" beats "Here is")
    for lead in ("Here is a draft: ", "Here's a draft: ", "Here is a ", "Here's a ",
                 "Here is ", "Here's ", "Short answer: ", "My answer: ",
                 "Answer: ", "Answer:", "I would say "):
        if t.lower().startswith(lead.lower()):
            t = t[len(lead):].lstrip()
            break
    t = re.sub(r"\s+", " ", t)
    t = t.strip()
    if len(t) > 700:
        cut = t[:700]
        if ". " in cut[-200:]:
            cut = cut[: cut.rfind(".")]
        t = cut
    return t


# ---------------------------------------------------------------------------
# Mechanical truth validation
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_YEARS_CLAIM_RE = re.compile(r"\b(\d{1,2}|[a-z]+)\s*\+?\s*years\b", re.I)
_YEAR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_COMPANY_SUFFIXES = ("corp", "inc", "llc", "ltd", "co", "inc.", "plc", "gmbh",
                     "systems", "technologies", "labs", "software")
# Words that may appear capitalised in normal answers without being companies.
_STOP_CAPS = {
    "I", "My", "A", "An", "The", "In", "At", "On", "With", "For", "And", "Or",
    "If", "But", "Yes", "No", "Not", "To", "Of", "That", "This", "It", "Is",
    "As", "Be", "By", "From", "When", "Where", "How", "What", "Why", "So",
    "We", "They", "He", "She", "You", "Our", "Their", "Your", "His", "Her",
    "Me", "Am", "Was", "Were", "Been", "Being", "Have", "Has", "Had", "Do",
    "Does", "Did", "Will", "Would", "Can", "Could", "Should", "May", "Might",
    "Just", "Really", "Very", "Also", "Then", "Now", "Still", "Even", "Only",
    "Happy", "Open", "Flexible", "Currently", "Recently", "Looking", "Available",
    "Based", "United", "States", "America", "Europe", "Netherlands", "New",
    "York", "San", "Francisco", "Seattle", "Chicago", "Boston", "Austin",
    "Remote", "Hybrid", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
    # tech acronyms / proper nouns that legitimately appear
    "AWS", "REST", "SQL", "Java", "Spring", "React", "Python", "Go", "Kafka",
    "Redis", "Docker", "Kubernetes", "PostgreSQL", "JavaScript", "TypeScript",
    "GitHub", "GitLab", "Linux", "Unix", "HTTP", "JSON", "API", "APIs", "CI",
    "CD", "LLM", "AI", "MLOps", "DevOps", "SaaS", "B2B", "B2C", "CRM", "ERP",
    "OAuth", "SSL", "TCP", "UDP", "DNS", "UI", "UX", "QA", "JVM", "Node",
    "Postman", "Swagger", "Gradle", "Maven", "JUnit", "Pytest", "Jenkins",
    "Terraform", "CloudFormation", "S3", "EC2", "Lambda", "DynamoDB", "RDS",
    "EKS", "Sagemaker", "OpenAI", "Azure", "GCP", "Oracle", "MongoDB", "Cassandra",
    "GraphQL", "gRPC", "GraphQL", "Apache", "Kafka", "RabbitMQ", "Elasticsearch",
    "Snowflake", "Spark", "Hadoop", "Airflow", "dbt", "BigQuery", "Redshift",
}


def _earliest_resume_year(resume_text: str) -> int | None:
    years = [int(m.group(0)) for m in _YEAR_RE.finditer(resume_text or "")]
    years = [y for y in years if 1985 <= y <= 2035]
    return min(years) if years else None


def truth_violations(answer: str, resume_text: str, context_text: str) -> list[str]:
    """Mechanical check: does the draft stay inside the resume's facts?

    ``context_text`` = question + job title + company (things the answer may
    legitimately reference). Returns a list of violation descriptions
    (empty list = passes).
    """
    violations: list[str] = []
    a = answer or ""

    # 1. Years must exist in the resume.
    resume_years = {
        int(m.group(0)) for m in _YEAR_RE.finditer(resume_text or "")
        if 1985 <= int(m.group(0)) <= 2035
    }
    for m in _YEAR_RE.finditer(a):
        y = int(m.group(0))
        if not (1985 <= y <= 2035):
            violations.append(f"implausible year {y}")
        elif resume_years and y not in resume_years:
            violations.append(f"year {y} not in resume")

    # 2. "N years" claims must fit the resume's date span.
    earliest = _earliest_resume_year(resume_text or "")
    if earliest:
        max_claim = date.today().year - earliest + 1
        for m in _YEARS_CLAIM_RE.finditer(a):
            tok = m.group(1).lower()
            n = int(tok) if tok.isdigit() else _YEAR_WORDS.get(tok)
            if n is None:
                continue
            if n > max_claim:
                violations.append(f"claims {n} years but resume spans only ~{max_claim}")

    # 3. Multi-word capitalised phrases (company-name shape) must be known.
    #    Drop sentence-leading function words first ("At Verizon" -> "Verizon").
    known = (resume_text or "") + " " + (context_text or "")
    known_low = known.lower()
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+)+)\b", a):
        words = m.group(1).split()
        while words and words[0] in _STOP_CAPS:
            words = words[1:]
        if len(words) < 2:
            continue
        phrase = " ".join(words)
        if phrase.lower() not in known_low:
            violations.append(f"unknown entity \"{phrase}\"")

    # 4. Lone company-sounding token + corporate suffix.
    for m in re.finditer(r"\b([A-Z][A-Za-z]*)\s+(" + "|".join(re.escape(s) for s in _COMPANY_SUFFIXES) + r")\b", a):
        head, suffix = m.group(1), m.group(2)
        if head in _STOP_CAPS:
            continue
        if f"{head} {suffix}".lower() not in known_low:
            violations.append(f"unknown company \"{head} {suffix}\"")

    return violations


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def _user_prompt(question: str, applicant_name: str, job_title: str, company: str,
                 jd_excerpt: str, resume_excerpt: str,
                 saved_answers: list[tuple[str, str]]) -> str:
    sa_lines = "\n".join(
        f"- Q: {q}\n  A: {a}" for q, a in (saved_answers or [])[:12] if (q or a).strip()
    )
    return f"""APPLICANT: {applicant_name}

JOB BEING APPLIED FOR: {job_title} at {company}
JOB DESCRIPTION (excerpt):
{jd_excerpt[:2200] or "(not available)"}

APPLICANT'S RESUME (the ONLY source of work facts):
{resume_excerpt[:3200] or "(not available)"}

APPLICANT'S SAVED SCREENING ANSWERS (pre-approved ground truth):
{sa_lines or "(none)"}

SCREENING QUESTION TO ANSWER:
{question[:500]}

Write the answer now (answer text only)."""


def draft_screening_answer(
    question: str,
    *,
    applicant_name: str,
    job_title: str,
    company: str,
    jd_text: str = "",
    resume_text: str = "",
    saved_answers: list[tuple[str, str]] | None = None,
    llm,
) -> str | None:
    """One truth-checked, humanized answer for a single screening question.

    Returns None when no LLM is available or the draft fails validation twice.
    """
    if llm is None or not getattr(llm, "is_available", lambda: False)():
        return None
    q = (question or "").strip()
    if not q:
        return None
    saved = saved_answers or []
    user = _user_prompt(q, applicant_name, job_title, company, jd_text, resume_text, saved)
    context = f"{q} {job_title} {company}"
    for attempt in (1, 2):
        system = HUMANIZE_SYSTEM
        prompt = user
        if attempt == 2:
            system += (
                "\n\nIMPORTANT: your previous draft failed a fact-check and was "
                "rejected. It mentioned employers, years, or claims that are not in "
                "the resume. Rewrite using ONLY the provided facts."
            )
        try:
            raw = llm.complete(system, prompt, temperature=0.7)
        except Exception:
            return None
        draft = _clean_draft(raw)
        if not draft:
            return None
        violations = truth_violations(draft, resume_text, context)
        if not violations:
            return draft
        if attempt == 2:
            return None
    return None


def make_drafter(
    *,
    applicant_name: str,
    job_title: str,
    company: str,
    jd_text: str = "",
    resume_text: str = "",
    saved_answers: list[tuple[str, str]] | None = None,
    llm,
):
    """Build a per-run closure: question text -> answer | None (cached).

    Returns None immediately (not a callable) when the LLM is unavailable,
    so callers can fall back to the blank-field behaviour.
    """
    if llm is None or not getattr(llm, "is_available", lambda: False)():
        return None
    cache: dict[str, str | None] = {}

    def drafter(question_text: str) -> str | None:
        key = re.sub(r"\s+", " ", (question_text or "")).strip()[:300]
        if key in cache:
            return cache[key]
        ans = draft_screening_answer(
            key,
            applicant_name=applicant_name,
            job_title=job_title,
            company=company,
            jd_text=jd_text,
            resume_text=resume_text,
            saved_answers=saved_answers,
            llm=llm,
        )
        cache[key] = ans
        return ans

    return drafter
