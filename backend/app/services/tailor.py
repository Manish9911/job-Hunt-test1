"""Phase 3 — LLM tailoring engine.

Given a job (JD) + the master resume, produces:
  (a) a match score with reasoning (master resume, "before tailoring"),
  (b) a tailored resume (structured JSON -> DOCX/PDF export),
  (c) an optional cover letter,
and fills the job's "after tailoring" score (the difference is the lift).

TRUTH CONTRACT (hard rule): the LLM may only reorder, reword, and emphasize.
It must NEVER invent experience, employers, dates, degrees, or skills. Every
LLM output is machine-validated against the master resume; a failed validation
triggers one retry, then falls back to a deterministic reorder-only tailoring
so the pipeline always produces a truthful artifact.
"""
from __future__ import annotations

import difflib
import html as html_mod
import json
import re
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from ..db.models import Job, Profile, Resume
from ..llm import get_llm
from ..llm.base import LLM, LLMUnavailable
from ..config import Settings

# ---------- rendering ----------

def render_resume_text(structure: dict) -> str:
    """Plain-text rendering of a structured resume (prompts, diffs, exports)."""
    lines: list[str] = []
    lines.append(structure.get("name") or "")
    contact = " | ".join(x for x in (structure.get("email"), structure.get("phone")) if x)
    if contact:
        lines.append(contact)
    if structure.get("summary"):
        lines.append("")
        lines.append("SUMMARY")
        lines.append(structure["summary"])
    skills = structure.get("skills") or {}
    if skills:
        lines.append("")
        lines.append("SKILLS")
        for cat, items in skills.items():
            lines.append(f"{cat}: {', '.join(items) if isinstance(items, list) else items}")
    wh = structure.get("work_history") or []
    if wh:
        lines.append("")
        lines.append("EXPERIENCE")
        for job in wh:
            span = f"{job.get('start', '')} - {job.get('end', '')}".strip(" -")
            lines.append(f"{job.get('title', '')} | {job.get('company', '')} | {job.get('location', '')} | {span}")
            if job.get("project"):
                lines.append(f"Project: {job['project']}")
            for b in job.get("bullets") or []:
                lines.append(f"- {b}")
    edu = structure.get("education") or []
    if edu:
        lines.append("")
        lines.append("EDUCATION")
        for e in edu:
            span = f"{e.get('start', '')} - {e.get('end', '')}".strip(" -")
            lines.append(f"{e.get('degree', '')} {e.get('field', '')} | {e.get('school', '')} | {span}")
    return "\n".join(lines)


def diff_master_tailored(master: dict, tailored: dict) -> str:
    a = render_resume_text(master).splitlines()
    b = render_resume_text(tailored).splitlines()
    return "\n".join(
        difflib.unified_diff(a, b, fromfile="master resume", tofile="tailored resume", lineterm="")
    )


# ---------- JD retrieval ----------

def html_to_text(fragment: str) -> str:
    """Crude HTML -> readable text (tags out, entities decoded, whitespace kept sane)."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|br)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


_GH_JOB_RE = re.compile(r"greenhouse\.io/([A-Za-z0-9._-]+)/jobs/(\d+)")
_LEVER_JOB_RE = re.compile(r"lever\.co/([A-Za-z0-9._-]+)/([A-Za-z0-9-]+)")


def fetch_jd(job: Job, client: httpx.Client | None = None) -> str:
    """Best-effort JD text for a stored job.

    Greenhouse/Lever apply URLs are resolved through their public JSON APIs
    (which include the full description); anything else is a tagged-page text
    scrape of the apply URL. Returns "" when nothing could be fetched — the
    dashboard then offers a manual JD paste.
    """
    url = (job.apply_url or "").strip()
    if not url:
        return ""
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": "Mozilla/5.0 (jobflow-local)"}, timeout=30)
    try:
        if own:
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (jobflow-local)"}, timeout=30) as c:
                return _fetch_jd_inner(c, url)
        return _fetch_jd_inner(client, url)
    except (httpx.HTTPError, ValueError, KeyError):
        return ""


def _fetch_jd_inner(client: httpx.Client, url: str) -> str:
    m = _GH_JOB_RE.search(url)
    if m:
        board, job_id = m.group(1), m.group(2)
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
        r.raise_for_status()
        data = r.json()
        return html_to_text(data.get("content") or "") + "\n\n" + (data.get("first_content") or "")
    m = _LEVER_JOB_RE.search(url)
    if m:
        slug, job_id = m.group(1), m.group(2)
        r = client.get(f"https://api.lever.co/postings/{slug}")
        r.raise_for_status()
        postings = r.json()
        for p in postings:
            if str(p.get("id")) == str(job_id) or f"/{job_id}" in (p.get("hostedUrl") or ""):
                return html_to_text(p.get("description") or "")
        return ""
    r = client.get(url)
    r.raise_for_status()
    return html_to_text(r.text)[:20000]


# ---------- truth validation ----------

_SYNONYMS = {
    "react": {"react", "react.js", "reactjs", "react js"},
    "spring": {"spring", "spring boot", "spring framework", "spring mvc", "spring ai"},
    "postgres": {"postgres", "postgresql"},
    "aws": {"aws", "amazon web services", "amazon web services (aws)"},
    "sql": {"sql"},
    "java": {"java"},
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#. ]", "", (s or "").lower()).strip()


def _skill_known(master_skills: set[str], candidate: str) -> bool:
    c = _norm(candidate)
    if not c:
        return True
    if c in master_skills:
        return True
    # exact-ish containment (handles "Amazon Web Services (AWS)" vs "AWS")
    for m in master_skills:
        if c and (c in m or m in c) and min(len(c), len(m)) >= 3:
            return True
    for canon, variants in _SYNONYMS.items():
        if c in variants:
            if any(v in master_skills or _norm(v) in master_skills for v in variants):
                return True
    return False


def validate_tailored(master: dict, tailored: dict) -> list[str]:
    """Return a list of truth violations (empty = output is truth-preserving)."""
    violations: list[str] = []
    for field in ("name", "email", "phone"):
        if master.get(field) and master[field] != tailored.get(field):
            violations.append(f"contact field '{field}' changed")

    master_employers = {_norm(j.get("company")) for j in master.get("work_history") or []}
    master_dates = set()
    for j in master.get("work_history") or []:
        master_dates.add(_norm(j.get("start")))
        master_dates.add(_norm(j.get("end")))
    for j in master.get("education") or []:
        master_dates.add(_norm(j.get("start")))
        master_dates.add(_norm(j.get("end")))

    for j in tailored.get("work_history") or []:
        comp = _norm(j.get("company"))
        if comp and comp not in master_employers:
            violations.append(f"unknown employer '{j.get('company')}'")
        for f in ("start", "end"):
            v = _norm(j.get(f))
            if v and v not in master_dates and v not in {"present"}:
                violations.append(f"unknown date '{j.get(f)}' in {j.get('company')}")

    master_schools = {_norm(e.get("school")) for e in master.get("education") or []}
    master_degrees = {_norm(e.get("degree")) for e in master.get("education") or []}
    for e in tailored.get("education") or []:
        if _norm(e.get("school")) and _norm(e.get("school")) not in master_schools:
            violations.append(f"unknown school '{e.get('school')}'")
        if _norm(e.get("degree")) and _norm(e.get("degree")) not in master_degrees:
            violations.append(f"unknown degree '{e.get('degree')}'")

    master_skill_flat = set()
    for items in (master.get("skills") or {}).values():
        if isinstance(items, list):
            master_skill_flat.update(_norm(s) for s in items)
    for items in (tailored.get("skills") or {}).values():
        if isinstance(items, list):
            for s in items:
                if not _skill_known(master_skill_flat, s):
                    violations.append(f"skill not in master resume: '{s}'")
    return violations


# ---------- deterministic (no-LLM) tailoring ----------

_JD_KEYWORDS = re.compile(
    r"(java|spring boot|spring|react|react\.js|postgresql|postgres|\bsql\b|aws|kubernetes"
    r"|\bpython\b|microservice|\bapi\b|rest|docker|kafka|redis|typescript|javascript|cassandra)",
    re.I,
)


def deterministic_tailor(master: dict, jd_text: str) -> tuple[dict, str]:
    """Reorder-only tailoring: 100% truth-preserving without any LLM.

    - skill categories ordered by JD relevance, skills within them likewise
    - work-history bullets reordered by JD keyword overlap (chronology of jobs kept)
    - summary/education untouched
    """
    jd_kw = {m.group(1).lower() for m in _JD_KEYWORDS.finditer(jd_text or "")}

    def kw_hits(text: str) -> int:
        t = (text or "").lower()
        return sum(1 for k in jd_kw if k in t)

    out = json.loads(json.dumps(master))  # deep copy

    skills = out.get("skills") or {}
    if skills:
        ordered = dict(sorted(skills.items(),
                              key=lambda kv: -kw_hits(" ".join(kv[1]) if isinstance(kv[1], list) else str(kv[1]))))
        new_skills = {}
        for cat, items in ordered.items():
            if isinstance(items, list):
                items = sorted(items, key=lambda s: -kw_hits(s))
            new_skills[cat] = items
        out["skills"] = new_skills

    for job in out.get("work_history") or []:
        bullets = job.get("bullets") or []
        if bullets:
            job["bullets"] = sorted(bullets, key=lambda b: -kw_hits(b))

    note = "deterministic reorder-only (no LLM configured)"
    return out, note


# ---------- LLM prompts ----------

TAILOR_SYSTEM = """You are a resume tailoring engine. HARD RULES (non-negotiable):
1. You may ONLY reorder, reword, and emphasize. You must NEVER invent, add, or alter facts.
2. Do not add, remove, or change employers, job titles, dates, locations, degrees, schools, or skills.
3. Every bullet in your output must correspond to a fact that exists in the provided resume.
4. Keep name, email, and phone exactly as given.
5. Emphasize by moving the most relevant skills/experience toward the top and rewording bullets to mirror the job description's vocabulary — only when the underlying fact is unchanged.
Output STRICT JSON only (no markdown, no commentary) with exactly this shape:
{"summary": string, "skills": {"<category>": [string]}, "work_history": [{"company": string, "title": string, "location": string, "start": string, "end": string, "project": string|null, "bullets": [string]}], "education": [{"degree": string, "field": string, "school": string, "start": string, "end": string}]}"""

SCORE_SYSTEM = """You are a strict, specific resume-vs-job match scorer.
Score 0-100 how well the resume matches the job description. Be honest about gaps.
Output STRICT JSON only: {"score": integer 0-100, "reasoning": string, "strongest_matches": [string], "gaps": [string]}"""

COVER_SYSTEM = """You write short cover letters. Rules:
- Use ONLY facts present in the provided resume. No invented achievements, metrics, or employers.
- 150-220 words, plain text, no placeholders, no subject line.
- Address it to the hiring team at the given company."""


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply (tolerates code fences)."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in LLM response")
    return json.loads(t[start: end + 1])


# ---------- orchestration ----------

class TailoringError(Exception):
    """User-facing tailoring failure (bad input, LLM error after retries)."""


def _get_master(db: Session) -> Resume:
    master = db.query(Resume).filter(Resume.is_master.is_(True)).first()
    if master is None or not master.content_json:
        raise TailoringError("No master resume with parsed content — upload/seed one first")
    return master


def tailor_job(
    db: Session,
    job_id: int,
    jd_text: str | None = None,
    cover_letter: bool = False,
    llm: LLM | None = None,
) -> dict:
    """Run the tailoring pipeline for one job. Returns a result summary dict."""
    job = db.get(Job, job_id)
    if job is None:
        raise TailoringError(f"Job {job_id} not found")
    master = _get_master(db)
    master_structure = json.loads(master.content_json)

    jd = (jd_text or "").strip()
    if not jd:
        jd = fetch_jd(job)
    if not jd:
        raise TailoringError(
            "Could not fetch the job description and none was provided — paste the JD text and retry"
        )

    settings = Settings()
    llm = llm or get_llm(settings)
    notes: dict = {"method": "llm" if llm.is_available() else "deterministic"}
    jd_used = jd[:8000]

    tailored: dict | None = None
    violations: list[str] = []

    if llm.is_available():
        # (1) score the MASTER resume vs JD ("before tailoring", LLM-backed)
        try:
            raw = llm.complete(SCORE_SYSTEM, f"JOB DESCRIPTION:\n{jd_used}\n\nRESUME:\n{render_resume_text(master_structure)}")
            score_before = _extract_json(raw)
            job.match_score = max(0, min(100, int(score_before.get("score", 0) or 0)))
            notes["score_before"] = score_before
        except Exception as exc:
            notes["score_before_error"] = repr(exc)[:200]

        # (2) tailor (validate -> one retry -> deterministic fallback)
        tailored = None
        prompt = (
            f"JOB DESCRIPTION:\n{jd_used}\n\n"
            f"MASTER RESUME (the ONLY facts you may use):\n{json.dumps(master_structure, indent=1)}"
        )
        for attempt in (1, 2):
            try:
                raw = llm.complete(TAILOR_SYSTEM, prompt)
                candidate = _extract_json(raw)
                violations = validate_tailored(master_structure, candidate)
                if not violations:
                    tailored = candidate
                    notes["llm_attempts"] = attempt
                    break
                notes["rejected_violations"] = violations
            except Exception as exc:
                notes[f"tailor_error_attempt_{attempt}"] = repr(exc)[:200]
        if tailored is None:
            fallback, note = deterministic_tailor(master_structure, jd_used)
            tailored = fallback
            notes["method"] = "deterministic-fallback"
            notes["fallback_reason"] = note + (f" (violations: {violations})" if violations else "")

        # (3) score the TAILORED resume vs JD ("after tailoring")
        try:
            raw = llm.complete(
                SCORE_SYSTEM,
                f"JOB DESCRIPTION:\n{jd_used}\n\nTAILORED RESUME:\n{render_resume_text(tailored)}",
            )
            score_after = _extract_json(raw)
            job.match_score_tailored = max(0, min(100, int(score_after.get("score", 0) or 0)))
            notes["score_after"] = score_after
        except Exception as exc:
            notes["score_after_error"] = repr(exc)[:200]
    else:
        # No LLM configured: deterministic reorder + deterministic scoring.
        tailored, note = deterministic_tailor(master_structure, jd_used)
        notes["method"] = "deterministic"
        notes["note"] = note
        notes["llm"] = "not configured — set OPENAI_API_KEY in .env for LLM tailoring"
        # deterministic scores: fraction of JD-mentioned skills the resume covers
        from ..ingestion.match import SKILL_VOCAB

        def det_match(resume_text: str) -> int:
            jd_kw = {label for label, rx in SKILL_VOCAB.items() if rx.search(jd_used)}
            res_text = resume_text.lower()
            covered = sum(1 for label in jd_kw if _norm(label) in res_text)
            return round(100 * covered / len(jd_kw)) if jd_kw else 0

        job.match_score = det_match(render_resume_text(master_structure))
        job.match_score_tailored = det_match(render_resume_text(tailored))
        notes["deterministic_note"] = (
            "scores = fraction of JD-mentioned skills covered by the resume (0-100); "
            "add an LLM key for semantic scoring"
        )

    # (4) optional cover letter
    if cover_letter:
        if llm.is_available():
            try:
                job.cover_letter_text = llm.complete(
                    COVER_SYSTEM,
                    f"COMPANY: {job.company}\nJOB: {job.title}\nJOB DESCRIPTION:\n{jd_used[:4000]}\n\nRESUME FACTS:\n{render_resume_text(tailored)}",
                ).strip()
            except Exception as exc:
                notes["cover_letter_error"] = repr(exc)[:200]
        else:
            notes["cover_letter_note"] = "cover letter needs an LLM — skipped (deterministic mode)"

    # (5) persist: new (non-master) resume row + job links
    tailored_row = db.query(Resume).filter(Resume.profile_id == master.profile_id).filter(
        Resume.label == f"tailored: {job.company} — {job.title[:60]}"
    ).first()
    if tailored_row is None:
        tailored_row = Resume(
            profile_id=master.profile_id,
            label=f"tailored: {job.company} — {job.title[:60]}",
            original_filename="generated (LLM-tailored)",
            storage_path="",  # generated artifact; export endpoints render on demand
            is_master=False,
        )
        db.add(tailored_row)
    tailored_row.content_json = json.dumps(tailored)
    job.tailored_resume_id = None  # set after flush
    job.tailor_notes_json = json.dumps(notes)
    db.commit()
    db.refresh(tailored_row)
    job.tailored_resume_id = tailored_row.id
    db.commit()

    return {
        "job_id": job.id,
        "tailored_resume_id": tailored_row.id,
        "method": notes.get("method"),
        "match_score_before": job.match_score,
        "match_score_after": job.match_score_tailored,
        "lift": (job.match_score_tailored - job.match_score
                 if job.match_score is not None and job.match_score_tailored is not None else None),
        "cover_letter": bool(job.cover_letter_text),
        "notes": notes,
    }
