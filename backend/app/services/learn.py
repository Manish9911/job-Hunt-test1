"""Self-improvement loop (habit inherited from the job-apply-agent skill,
2026-11-09).

The other agent's habit: *every* run makes it better for the next one —
new answers get saved where future runs can reuse them, blocked boards get
routed around instead of re-battled, and state survives handoffs. This
module is jobflow's version of that habit, as a persistent ``Lesson`` store
the agent re-reads BEFORE acting on each job:

- **Learn from outcomes** — CAPTCHA walls accumulate strikes per domain;
  three strikes in a row turn it into a ``wall`` lesson and the agent stops
  burning a full browser run on that board (it parks the job for the user
  and moves on — "rotate, don't re-attempt the same wall").
- **Learn from the user** — the user's "this is legit" / "this is a scam"
  / "the wall is cleared" decisions become ``fraud_ok`` / ``fraud_bad`` /
  wall-cleared lessons, so the same call is never re-asked or re-fought.
- **Carry state across restarts** — the handoff file
  (``data/agent_status.json``) records phase, counts, walled domains and
  pending approvals, and is re-read on agent start.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..db.models import Job, Lesson

WALL_STRIKE_THRESHOLD = 3  # consecutive captcha walls before a board is walled


def _domain(url: str | None) -> str:
    if not url:
        return ""
    u = re.sub(r"^[a-z]+://", "", (url or "").lower().strip())
    host = u.split("/")[0].split(":")[0]
    host = re.sub(r"^www\.", "", host)
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# ---------------------------------------------------------------- read side
def lessons_for_job(db: Session, job: Job) -> list[Lesson]:
    """Every lesson that applies to this job (global + company + domain).

    Cheap enough to call before every job: a few indexed rows on local
    SQLite.
    """
    dom = _domain(job.apply_url)
    q = (
        db.query(Lesson)
        .filter(
            (Lesson.scope == "global")
            | (Lesson.scope == f"company:{(job.company or '').lower()}")
            | (Lesson.scope == f"domain:{dom}")
        )
        .order_by(Lesson.created_at.desc())
    )
    return q.all()


def board_is_walled(db: Session, job: Job) -> Lesson | None:
    """The wall lesson for this job's form domain/company, if one exists.

    Strike rows below the threshold are not walls yet — they're just
    accumulating CAPTCHA history."""
    for l in lessons_for_job(db, job):
        if l.kind == "wall" and (l.count or 0) >= WALL_STRIKE_THRESHOLD:
            return l
    return None


def domain_verdict(db: Session, job: Job) -> str | None:
    """User-decided legitimacy for this job's domain: 'ok' | 'scam' | None.

    A domain-level verdict always beats company-level (the form is where
    the data actually goes).
    """
    for l in lessons_for_job(db, job):
        if l.kind in ("fraud_ok", "fraud_bad"):
            if l.scope.startswith("domain:"):
                return "ok" if l.kind == "fraud_ok" else "scam"
    for l in lessons_for_job(db, job):
        if l.kind in ("fraud_ok", "fraud_bad"):
            return "ok" if l.kind == "fraud_ok" else "scam"
    return None


# ---------------------------------------------------------------- write side
def record_outcome(db: Session, job: Job, status: str) -> None:
    """Learn from a finished application attempt. Called by the agent after
    every ``run_application`` — this is where the habit lives."""
    try:
        dom = _domain(job.apply_url)
        if status in ("captcha_manual", "submit_blocked_captcha"):
            _strike_wall(db, dom, job.company)
        elif status in ("submitted",):
            _clear_wall(db, dom)
        # NOTE: a payment block is NOT auto-saved as a fraud lesson.
        # submit_blocked_payment is a per-job handled status with a
        # user-facing "Mark as scam (never retry)" button — the user's
        # explicit click (user_marks_domain) is what poisons a domain.
        # Auto-saving here let one regex false-positive (Method's marketing
        # copy on an Ashby form) hard-reject every other job on that ATS.
        pass
    except Exception:
        pass  # learning must never break the run itself


def _strike_wall(db: Session, dom: str, company: str | None) -> None:
    """One more captcha wall on this domain. At the threshold, promote the
    strike row into a real ``wall`` lesson."""
    if not dom:
        return
    scope = f"domain:{dom}"
    row = (db.query(Lesson)
           .filter(Lesson.scope == scope, Lesson.kind == "wall")
           .first())
    if row is None:
        row = Lesson(scope=scope, kind="wall", count=0)
        db.add(row)
    row.count = (row.count or 0) + 1
    if row.count >= WALL_STRIKE_THRESHOLD:
        company_txt = f" ({company})" if company else ""
        row.text = (f"board on {dom}{company_txt} is CAPTCHA-walled — "
                    f"{row.count} consecutive walls. Skip browser runs here; "
                    "the user clears the wall, then remove this lesson.")
    db.commit()


def _clear_wall(db: Session, dom: str) -> None:
    """A successful submission on this domain — the wall (if any) is gone."""
    if not dom:
        return
    rows = (db.query(Lesson)
            .filter(Lesson.scope == f"domain:{dom}", Lesson.kind == "wall")
            .all())
    for row in rows:
        db.delete(row)
    db.commit()


def _upsert(db: Session, scope: str, kind: str, text: str) -> None:
    row = (db.query(Lesson)
           .filter(Lesson.scope == scope, Lesson.kind == kind)
           .first())
    if row is None:
        row = Lesson(scope=scope, kind=kind, count=1)
        db.add(row)
    row.text = text
    row.updated_at = datetime.utcnow()
    db.commit()


# ------------------------------------------------------------- user calls
def user_marks_domain(db: Session, url: str, company: str | None,
                      verdict: str) -> None:
    """The user's legitimacy call on a flagged posting: 'ok' or 'scam'."""
    dom = _domain(url)
    if dom and verdict == "ok":
        _upsert(db, f"domain:{dom}", "fraud_ok",
                "user confirmed this application domain is legit")
    elif dom and verdict == "scam":
        _upsert(db, f"domain:{dom}", "fraud_bad",
                "user confirmed this is a scam — hard reject, do not retry")
    if company:
        _upsert(db, f"company:{company.lower()}",
                "fraud_ok" if verdict == "ok" else "fraud_bad",
                f"user confirmed {company} {'legit' if verdict == 'ok' else 'scam'}")


def user_clears_wall(db: Session, url: str, company: str | None = None) -> None:
    """User says the CAPTCHA wall on this board is cleared (or they will
    handle it manually) — remove wall lessons so the agent tries again."""
    _clear_wall(db, _domain(url))
    if company:
        rows = (db.query(Lesson)
                .filter(Lesson.scope == f"company:{company.lower()}",
                        Lesson.kind == "wall")
                .all())
        for row in rows:
            db.delete(row)
        db.commit()


# ------------------------------------------------------------------ handoff
STATUS_FILE_NAME = "agent_status.json"


def write_status_file(data_dir: Path, payload: dict) -> None:
    """The campaign_status.md equivalent: a handoff snapshot the agent
    re-reads on start, so restarts resume instead of restart from zero."""
    try:
        p = Path(data_dir) / STATUS_FILE_NAME
        p.write_text(json.dumps(payload, indent=2, default=str),
                     encoding="utf-8")
    except Exception:
        pass  # handoff file is a convenience, never fatal


def read_status_file(data_dir: Path) -> dict:
    try:
        p = Path(data_dir) / STATUS_FILE_NAME
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}
