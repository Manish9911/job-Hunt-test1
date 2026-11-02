"""Resume-vs-JD match scoring (0-100).

Two scores are tracked per job:
- ``match_score``          — the MASTER resume vs the JD (the "before tailoring" score).
- ``match_score_tailored`` — the tailored resume vs the JD (the "after tailoring" score).
  This stays ``None`` until Phase 3 produces a tailored resume, then it is filled and
  the dashboard shows the lift (delta) between the two.

Phase 2 scorer is DETERMINISTIC and explainable (no LLM required, runs offline):
it measures skill overlap, role/experience fit, and location/remote/visa fit against
the stored profile + resume skills. Phase 3 swaps in an LLM-backed scorer behind the
same ``score_job()`` signature; the deterministic one remains the fallback when no
LLM provider is configured.

The score is intentionally a *fit estimate* for ranking, not a guarantee of hire.
"""
from __future__ import annotations

import re

# Canonical skill vocabulary. key = normalized label shown to the user;
# value = regex that detects the skill in free-form JD text.
SKILL_VOCAB: dict[str, re.Pattern] = {
    "java": re.compile(r"\bjava\b", re.I),
    "spring boot": re.compile(r"spring(?:\s+boot)?\b", re.I),
    "react": re.compile(r"\breact(js|\.js)?\b", re.I),
    "postgresql": re.compile(r"postgres(?:ql)?\b", re.I),
    "sql": re.compile(r"\bsql\b", re.I),
    "rest api": re.compile(r"\brest(?:ful)?\b|\bapi", re.I),
    "microservices": re.compile(r"micro-?service", re.I),
    "aws": re.compile(r"\baws\b|amazon web services", re.I),
    "javascript": re.compile(r"javascript|\bjs\b", re.I),
    "typescript": re.compile(r"typescript|\bts\b", re.I),
    "python": re.compile(r"\bpython\b", re.I),
    "kubernetes": re.compile(r"kubernetes|\bk8s\b", re.I),
    "docker": re.compile(r"\bdocker\b", re.I),
    "redis": re.compile(r"\bredis\b", re.I),
    "kafka": re.compile(r"\bkafka\b", re.I),
    "cassandra": re.compile(r"cassandra", re.I),
    "mongodb": re.compile(r"mongo(?:db)?", re.I),
    "graphql": re.compile(r"graphql", re.I),
    "ci/cd": re.compile(r"ci/cd|jenkins|github actions|gitlab ci", re.I),
    "testing": re.compile(r"\btest(?:ing|s)?\b|junit|pytest|jest", re.I),
    "design patterns": re.compile(r"design pattern", re.I),
    "machine learning": re.compile(r"\bml\b|machine learning|llm|langchain", re.I),
}

# How many points each axis is worth (sums to 100).
WEIGHTS = {"skills": 60, "role": 25, "meta": 15}


def _jd_text(job: dict) -> str:
    parts = [job.get("title") or ""]
    for c in (job.get("commitment") or []):
        if isinstance(c, str):
            parts.append(c)
    parts.append(job.get("location") or "")
    # Phase 2 feeds store no long description; the title carries the stack signal.
    return " ".join(p for p in parts if p)


def _resume_skill_names(resume_skills: list[str]) -> set[str]:
    """Normalized set of skill labels the resume actually contains."""
    names = set()
    text = " ".join(s for s in (resume_skills or []) if isinstance(s, str)).lower()
    for label, rx in SKILL_VOCAB.items():
        if rx.search(text):
            names.add(label)
    return names


def skill_overlap(job: dict, resume_skills: list[str]) -> tuple[int, int, list[str]]:
    """Return (hits, total, matched_labels): skills mentioned in the JD that the
    resume also has, over all canonical skills the JD mentions."""
    text = _jd_text(job)
    resume = _resume_skill_names(resume_skills)
    total = [label for label, rx in SKILL_VOCAB.items() if rx.search(text)]
    matched = [label for label in total if label in resume]
    return len(matched), len(total), matched


def score_job(job: dict, resume_skills: list[str]) -> tuple[int, dict]:
    """Score a job 0-100 against the given resume skills.

    Returns (score, breakdown). breakdown has per-axis points + matched skills,
    for the UI and for Phase 3 to compare against the tailored-resume score.
    """
    hits, total, matched = skill_overlap(job, resume_skills)
    # Skills axis: fraction of JD-mentioned skills the resume has. If the JD text
    # names no recognizable skill, fall back to a neutral 50% of the axis.
    if total:
        skill_pts = round(WEIGHTS["skills"] * hits / total)
    else:
        skill_pts = round(WEIGHTS["skills"] * 0.5)

    # Role axis: is this an SWE/software title, and does the experience line up?
    title = (job.get("title") or "").lower()
    is_swe = bool(re.search(r"software|full[- ]?stack|back(?:end|[- ]end)|engineer|developer|swe", title))
    yoe = job.get("yoe")
    exp_ok = True if not isinstance(yoe, (int, float)) else yoe <= 6  # resume is 4+ yrs; 6 gives headroom
    role_pts = 0
    if is_swe:
        role_pts += 15
    if is_swe and exp_ok:
        role_pts += WEIGHTS["role"] - 15
    breakdown: dict[str, object] = {
        "skills": {"points": skill_pts, "max": WEIGHTS["skills"], "matched": matched,
                   "jd_skills_mentioned": total},
        "role": {"points": role_pts, "max": WEIGHTS["role"], "is_swe": is_swe,
                 "experience_ok": exp_ok},
    }

    # Meta axis: location/remote/visa fit (already filtered, so mostly a bonus).
    meta_pts = 0
    is_us, is_eu = _regions(job)
    if is_us or is_eu:
        meta_pts += 6
    remote = job.get("remote")
    if is_eu and remote is True:
        meta_pts += 5
    elif not is_eu and remote in (True, None):
        meta_pts += 5
    visa = job.get("visa_sponsorship")
    if is_us and visa in (True, None):
        meta_pts += 4
    elif is_eu:
        meta_pts += 4
    meta_pts = min(meta_pts, WEIGHTS["meta"])
    breakdown["meta"] = {"points": meta_pts, "max": WEIGHTS["meta"]}

    score = min(100, skill_pts + role_pts + meta_pts)
    breakdown["total"] = score
    breakdown["method"] = "deterministic-v1"
    return score, breakdown


def _regions(job: dict) -> tuple[bool, bool]:
    """Best-effort US/EU flag reusing filters.regions_of when available."""
    try:
        from .filters import regions_of
        return regions_of(job)
    except Exception:  # pragma: no cover - filters import should always work
        return False, False


def score_lift(before: int | None, after: int | None) -> int | None:
    """Delta between master and tailored scores (None if either missing)."""
    if before is None or after is None:
        return None
    return after - before
