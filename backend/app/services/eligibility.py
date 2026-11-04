"""JD eligibility gate — the auto-apply agent's "read the requirements first" step.

User rule (2026-11-02): before applying, the agent must read the job description.
- JD says **US citizens only** (or equivalent)            -> REJECT, do not apply.
- JD says **no visa sponsorship** (or equivalent)         -> REJECT, do not apply.
- JD says **nothing** about citizenship / sponsorship     -> APPLY (user is fine applying).
- JD explicitly **offers** sponsorship                    -> APPLY (recorded as positive).
- JD says **security clearance** required/eligible (rule 2026-11-03) -> REJECT —
  the user is not authorized for clearance work and doesn't want time wasted
  on those postings.

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
