"""Lessons API — the self-improvement store (habit inherited from the
job-apply-agent skill, 2026-11-09).

GET    /api/lessons               list all lessons
POST   /api/lessons               user adds a tip/preference
DELETE /api/lessons/{id}          user removes a lesson
POST   /api/lessons/wall-cleared  user says a CAPTCHA wall is cleared
"""
from __future__ import annotations

import threading
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.deps import get_db
from ..db.models import Application, Lesson

router = APIRouter(prefix="/api/lessons", tags=["lessons"])

VALID_SCOPES = ("global", "company:", "domain:", "ats:")
VALID_KINDS = ("wall", "fraud_ok", "fraud_bad", "tip", "pref")


class LessonIn(BaseModel):
    scope: str = "global"
    kind: str = "tip"
    text: str


def _dict(l: Lesson) -> dict:
    return {
        "id": l.id,
        "scope": l.scope,
        "kind": l.kind,
        "text": l.text,
        "count": l.count,
        "created_at": l.created_at.isoformat() if l.created_at else None,
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


@router.get("")
def list_lessons(db: Session = Depends(get_db)):
    rows = db.query(Lesson).order_by(Lesson.updated_at.desc(),
                                     Lesson.id.desc()).limit(200).all()
    return [_dict(l) for l in rows]


@router.post("")
def add_lesson(body: LessonIn, db: Session = Depends(get_db)):
    scope = (body.scope or "global").strip().lower()
    kind = (body.kind or "tip").strip().lower()
    if not scope.startswith(VALID_SCOPES):
        raise HTTPException(400,
                            "scope must start with one of: global, company:, domain:, ats:")
    if kind not in VALID_KINDS:
        raise HTTPException(400, f"kind must be one of: {', '.join(VALID_KINDS)}")
    if not (body.text or "").strip():
        raise HTTPException(400, "text is required")
    l = Lesson(scope=scope, kind=kind, text=body.text.strip(), count=1)
    db.add(l)
    db.commit()
    db.refresh(l)
    return _dict(l)


@router.delete("/{lesson_id}")
def delete_lesson(lesson_id: int, db: Session = Depends(get_db)):
    l = db.get(Lesson, lesson_id)
    if not l:
        raise HTTPException(404, "lesson not found")
    db.delete(l)
    db.commit()
    return {"deleted": lesson_id}


@router.post("/wall-cleared")
def wall_cleared(body: dict = Body(...), db: Session = Depends(get_db)):
    """User confirms a CAPTCHA-walled board is cleared — remove its wall
    lessons so the agent will try it again on its next pass."""
    app_id = body.get("application_id")
    a = db.get(Application, app_id) if app_id else None
    if a is None:
        raise HTTPException(404, "application not found")
    from ..services.learn import user_clears_wall

    user_clears_wall(db, a.job.apply_url, a.job.company)
    a.status = "pending"
    a.error_text = "wall cleared by user — agent will retry"
    db.commit()
    return {"cleared": True, "application_id": a.id}


@router.post("/applications/{app_id}/mark-scam")
def mark_scam(app_id: int, db: Session = Depends(get_db)):
    """User confirms a flagged posting is a scam: save the verdict (the agent
    will hard-reject this domain/company forever) and drop the application."""
    from ..services.learn import user_marks_domain

    a = db.get(Application, app_id)
    if a is None or a.status not in ("flagged_fraud_review",
                                     "submit_blocked_payment"):
        raise HTTPException(404, "application is not in a fraud-blocked state")
    user_marks_domain(db, a.job.apply_url, a.job.company, "scam")
    a.status = "rejected_fraud"
    a.error_text = "user confirmed this is a scam — saved as a lesson, " \
                   "never retried"
    a.finished_at = datetime.utcnow()
    db.commit()
    return {"rejected": True, "application_id": a.id}


@router.post("/applications/{app_id}/apply-anyway")
def apply_anyway(app_id: int, request: Request, db: Session = Depends(get_db)):
    """User says a fraud-FLAGGED job is actually legit: save the verdict as a
    lesson (so the same flag is never raised again) and run the agent on that
    job in a background thread."""
    from ..services.agent import get_agent
    from ..services.learn import user_marks_domain

    a = db.get(Application, app_id)
    if a is None or a.status not in ("flagged_fraud_review",):
        raise HTTPException(404, "application not flagged for fraud review")
    user_marks_domain(db, a.job.apply_url, a.job.company, "ok")
    a.status = "pending"
    a.error_text = "user confirmed legit — applying"
    db.commit()
    job_id = a.job_id

    agent = get_agent(request)

    def _run() -> None:
        db2 = request.app.state.SessionLocal()
        try:
            from ..db.models import Job
            j = db2.get(Job, job_id)
            if j is not None:
                agent._process_job(db2, j)
        except Exception as e:
            agent.last_error = f"apply-anyway failed: {type(e).__name__}: {e}"[:200]
        finally:
            db2.close()

    threading.Thread(target=_run, name=f"apply-anyway-{job_id}", daemon=True).start()
    return {"started": True, "job_id": job_id}
