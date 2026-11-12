"""JD eligibility gate — the auto-apply agent's "read the requirements first" step.

User rule (2026-11-02): before applying, the agent must read the job description.
- JD says **US citizens only** (or equivalent)            -> REJECT, do not apply.
- JD says **no visa sponsorship** (or equivalent)         -> REJECT, do not apply.
- JD says **nothing** about citizenship / sponsorship     -> APPLY (user is fine applying).
- JD explicitly **offers** sponsorship                    -> APPLY (recorded as positive).
- JD says **security clearance** required/eligible (rule 2026-11-03) -> REJECT —
  the user is not authorized for clearance work and doesn't want time wasted
  on those postings.
- JD is **STEM-OPT-only** (rule 2026-11-05)               -> REJECT — targets
  recent-grad F-1 candidates; the user is mid-career H1B/L1.
- JD's **experience minimum** is far above the resume (rule 2026-11-05:
  cheap parameter pre-screen) -> REJECT (borderline 5+ still passes).
- JD wants a **completely different stack** (rule 2026-11-05: judge by the JD,
  not the title) -> REJECT via ``assess_jd_skills`` (deterministic, token-free).

The check is deterministic regex over the JD text (these are explicit employer
statements, not judgment calls). Every rejection quotes the exact JD phrase so
the user can verify why the agent declined.
"""
from __future__ import annotations

import re

# (label, regex) — any match is a hard rejection.
_REJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- US-citizens-only language ---
    ("us_citizens_only", re.compile(
        r"\b(?:us|u\.s\.|united states|american) (?:work\s+)?citizens? only\b"
        r"|\bmust (?:be|become) a us citizen\b"
        r"|\bus persons only\b"
        r"|\bmust hold us citizenship\b",
        re.I,
    )),
    ("citizens_only", re.compile(
        r"\bcitizens? only\b", re.I)),
    # --- no-sponsorship language ---
    ("no_sponsorship", re.compile(
        r"\bno (?:visa |work[ -]authorization )?sponsorship\b"
        r"|\bsponsorship (?:is |will be )?not (?:available|offered|provided)\b"
        r"|\bnot (?:able|willing) to (?:sponsor|assist with (?:visa |work authorization)|provide (?:visa |work )?sponsorship)\b"
        r"|\b(?:we|we're|the company) (?:do not|don't) (?:sponsor|provide (?:visa |work )?sponsorship)\b"
        r"|\bwill not sponsor\b"
        r"|\bwe (?:are |can )?(?:not|cannot|can't) (?:be able to )?sponsor\b"
        r"|\bunable to (?:be |be able to )?sponsor\b",
        re.I,
    )),
    ("work_auth_future", re.compile(
        # "authorized to work in the US without now or in the future
        # requiring sponsorship" — the classic US-only boilerplate, in both
        # wordings: "without ... requiring sponsorship" and
        # "not require, now or in the future, sponsorship".
        r"\bwithout now or in the future (?:requiring|needing) (?:us )?(?:work\s+)?(?:visa )?sponsorship\b"
        r"|\bmust not require (?:us )?(?:work\s+)?(?:visa )?sponsorship\b"
        r"|\bnot require,? (?:now or in the future[, ]+|at any time[, ]+)?(?:us )?(?:work\s+)?(?:visa )?sponsorship\b"
        r"|\b(?:now or in the future|at any (?:time|point) in the future)\b.{0,80}\bsponsorship\b",
        re.I,
    )),
    # --- security-clearance language (user rule 2026-11-03: not authorized
    #     for clearance work — reject before wasting time on the form) ---
    ("clearance_required", re.compile(
        r"\bsecurity clearance\b"
        r"|\bu\.s\. (?:security )?clearance\b"
        r"|\b(?:top ?secret|secret|ts/sci|ts\+|sci) (?:security )?clearance\b"
        r"|\b(?:active|current|valid) (?:u\.s\. )?(?:security )?clearance\b"
        r"|\bclearance (?:eligib\w*|is (?:required|needed|preferred)|required\b|needed\b|requirement\b)\b"
        r"|\belegible? to obtain (?:and maintain )?(?:a )?(?:u\.s\. )?(?:security )?clearance\b"
        r"|\brequires? (?:a )?(?:u\.s\. )?(?:security )?clearance\b"
        r"|\bmust (?:hold|obtain|maintain) (?:a )?(?:u\.s\. )?(?:security )?clearance\b",
        re.I,
    )),
    # --- STEM-OPT-only language (user rule 2026-11-05: parameter pre-screen).
    #     "must be eligible for / on / maintain STEM OPT" targets recent-grad
    #     F-1 candidates — a mid-career H1B/L1 applicant is out of scope.
    #     Deliberately narrow: "we sponsor H1B, L1 and STEM OPT" does NOT match.
    ("stem_opt_only", re.compile(
        r"\bmust be (?:eligible for|enrolled in|on) (?:a )?stem opt\b"
        r"|\bmaintain (?:your |a )?stem opt\b"
        r"|\bmust maintain (?:your )?opt (?:status|eligibility)\b"
        r"|\bstem opt (?:extension )?eligibility (?:is |will be )?(?:required|mandatory)\b",
        re.I,
    )),
)

# Explicitly positive — employer offers sponsorship (overrides the silence rule
# and is recorded in the assessment so the user can see it).
_POSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("offers_sponsorship", re.compile(
        r"\b(?:we|we're|we will|the company|we are) (?:offer|provide|will provide|will be (?:happy|glad) to (?:provide|sponsor))"
        r" (?:visa |work[ -]authorization )?sponsorship\b"
        r"|\bvisa sponsorship (?:is|will be) (?:available|offered|provided)\b"
        r"|\bwe (?:are |can )?(?:will )?(?:be able to|able to) sponsor\b"
        r"|\bopen to (?:visa |work )?sponsorship\b"
        r"|\bhappy to sponsor\b",
        re.I,
    )),
)

_MAX_QUOTE = 140


def _quote(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 30)
    end = min(len(text), match.end() + 30)
    snippet = text[start:end].strip()
    snippet = re.sub(r"\s+", " ", snippet)
    if len(snippet) > _MAX_QUOTE:
        snippet = snippet[: _MAX_QUOTE - 1] + "\u2026"
    return snippet


# --- Years-of-experience ceiling (user rule 2026-11-05: cheap parameter
#     pre-screen BEFORE applying). Master resume states 4+ years; a JD whose
#     stated minimum is far beyond that level is a mismatch and wastes a
#     submission slot. Borderline (5+) still passes — the user reviews every
#     rejection in the dashboard.
USER_YOE = 4.0
EXPERIENCE_HEADROOM = 2.0  # reject when JD minimum > USER_YOE + 2 (i.e. 7+)

_EXP_MIN_PATTERNS = (
    # "5+ years", "5+ yrs"
    re.compile(r"\b(\d{1,2})\s*\+\s*(?:years?|yrs?)\b", re.I),
    # "5 years of experience", "5 years relevant exp"
    re.compile(r"\b(\d{1,2})\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+)?(?:experience|exp\b)", re.I),
    # "minimum of 5 years", "at least 5 yrs", "min. 6 years"
    re.compile(r"\b(?:minimum|min\.?|at least)\s+(?:of\s+)?(\d{1,2})\s*(?:years?|yrs?)", re.I),
    # "5 or more years", "6 or higher years"
    re.compile(r"\b(\d{1,2})\s+(?:or\s+more|or\s+higher)\s+(?:years?|yrs?)", re.I),
    # "5 to 8 years", "5-8 years", "5–8 years" (use the low end)
    re.compile(r"\b(\d{1,2})\s*(?:to|-|\u2013|\u2014)\s*\d{1,2}\s*(?:years?|yrs?)", re.I),
)


def required_experience_years(text: str) -> int | None:
    """Highest minimum-years-of-experience claim found in the JD, or None.

    Ranges ("5 to 8 years") are judged by their LOW end and their span is
    masked before the single-number patterns run, so the range's upper bound
    is never double-counted as a separate "8 years" requirement.
    """
    best: int | None = None
    masked = text
    for m in _EXP_MIN_PATTERNS[-1].finditer(text):  # the range pattern
        n = int(m.group(1))
        if 1 <= n <= 40:
            masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
            if best is None or n > best:
                best = n
    for rx in _EXP_MIN_PATTERNS[:-1]:
        for m in rx.finditer(masked):
            n = int(m.group(1))
            if 1 <= n <= 40 and (best is None or n > best):
                best = n
    return best


def assess_jd(jd_text: str | None) -> dict:
    """Decide whether the agent may apply, based purely on the JD text.

    Returns ``{"decision": "apply"|"reject", "reasons": [str, ...],
    "signals": [str, ...], "checked": bool}``.
    ``checked`` is False when no JD text is available — the agent then falls
    back to the user's default (apply), but records that it could not read the
    requirements.
    """
    if not jd_text or not jd_text.strip():
        return {
            "decision": "apply",
            "reasons": [],
            "signals": [],
            "checked": False,
            "note": "no JD text available — applied by default (user rule: silence = apply)",
        }

    text = jd_text
    # Normalise: JDs come from HTML/JSON; collapse whitespace for matching.
    text = re.sub(r"\s+", " ", text)

    reasons: list[str] = []
    for label, pattern in _REJECT_PATTERNS:
        m = pattern.search(text)
        if m:
            reasons.append(f"{label}: \"{_quote(text, m)}\"")

    # Experience ceiling: reject roles far above the resume's level.
    req_years = required_experience_years(text)
    if req_years is not None and req_years > USER_YOE + EXPERIENCE_HEADROOM:
        idx = text.lower().find(str(req_years))
        start = max(0, idx - 30)
        snippet = re.sub(r"\s+", " ", text[start:start + 100]).strip()
        reasons.append(
            f"experience_mismatch: JD minimum {req_years} years "
            f"(resume: {int(USER_YOE)}+ years) - \"{snippet}\"")

    signals: list[str] = []
    if not reasons:
        for label, pattern in _POSITIVE_PATTERNS:
            m = pattern.search(text)
            if m:
                signals.append(f"{label}: \"{_quote(text, m)}\"")

    if reasons:
        return {
            "decision": "reject",
            "reasons": reasons,
            "signals": signals,
            "checked": True,
            "note": "JD contains hard exclusion criteria — application refused before filling anything",
        }
    return {
        "decision": "apply",
        "reasons": [],
        "signals": signals,
        "checked": True,
        "note": "no exclusion criteria found" + (" (sponsorship explicitly offered)" if signals else " (JD silent on citizenship/sponsorship)"),
    }


def assess_jd_skills(jd_text: str | None, resume_skills: list[str] | None) -> dict:
    """Deterministic stack-fit check (user rule 2026-11-05: judge by the JD,
    not the title — companies name roles differently, so a Java/Spring
    developer may sit behind a "Data Platform Engineer" title).

    Token-free (no LLM): counts canonical skills the JD mentions and how many
    the resume also has. Rejects only on a clear mismatch — the JD is built
    around 3+ canonical skills and the resume has NONE of them. Borderline
    overlap passes: the user reviews decisions in the dashboard.
    """
    if not jd_text or not resume_skills:
        return {"decision": "apply", "reasons": []}
    from ..ingestion.match import SKILL_VOCAB, _resume_skill_names

    text = re.sub(r"\s+", " ", jd_text)
    mentioned = [label for label, rx in SKILL_VOCAB.items() if rx.search(text)]
    if len(mentioned) < 3:
        return {"decision": "apply", "reasons": []}
    resume = _resume_skill_names(resume_skills)
    hits = [label for label in mentioned if label in resume]
    if not hits:
        return {
            "decision": "reject",
            "reasons": [
                "stack_mismatch: JD is built around " + ", ".join(mentioned)
                + " — the resume has none of these"
            ],
        }
    return {"decision": "apply", "reasons": []}
