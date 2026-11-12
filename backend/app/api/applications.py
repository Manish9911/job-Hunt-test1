"""Application-automation API (Phase 4).

POST /api/jobs/{job_id}/apply          run one (dry-run by default) application
GET  /api/applications                 list application attempts
GET  /api/applications/{id}            detail + full action log
GET  /api/applications/{id}/screenshot the dry-run screenshot
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..automation.runner import run_application
from ..db.deps import get_db
from ..db.models import ActionLog, Application

router = APIRouter(prefix="/api", tags=["applications"])

# Statuses where a staged application is waiting for the user's decision
# (permission model, 2026-11-09, 'approve_each' mode).
_STAGED_STATUSES = {"dry_run_completed", "ready_for_submission"}


def _stage_app(app_id: int, db: Session) -> Application:
    a = db.get(Application, app_id)
    if not a:
        raise HTTPException(404, "application not found")
    if a.status not in _STAGED_STATUSES:
        raise HTTPException(
            409, f"not staged for approval (status: {a.status}) — nothing to do")
    return a


def _notify_app_result(db: Session, a: Application, status: str,
                       approval: bool) -> None:
    """Post-submit notification for the approval flow (runs on its own
    thread, hence its own session)."""
    try:
        from ..services.notify import notify

        company = a.job.company if a.job else "job"
        title = f"{a.job.title} @ {company}" if a.job else str(a.job_id)
        if status == "submitted":
            notify(db, "info", f"Submitted (approved): {company}",
                   f"{title} — approved by you and submitted by the agent.",
                   link=a.job.apply_url if a.job else None)
        elif status in ("captcha_manual", "submit_blocked_captcha"):
            notify(db, "action", f"CAPTCHA before submit: {company}",
                   f"{title} — you approved the submission but the form now "
                   "asks for a CAPTCHA. The agent will NOT solve it; clear it "
                   "and finish the submit yourself.",
                   link=a.job.apply_url if a.job else None)
        elif status in ("rejected_fraud", "rejected_clearance",
                        "rejected_user", "submit_blocked_payment"):
            notify(db, "info", f"Not submitted: {company}",
                   f"{title} — {a.error_text or status}",
                   link=a.job.apply_url if a.job else None)
        else:
            notify(db, "action", f"Submit needs a look: {company}",
                   f"{title} — ended as '{status}' after your approval. "
                   f"{(a.error_text or '')[:200]}",
                   link=a.job.apply_url if a.job else None)
    except Exception:
        pass


def _app_dict(a: Application) -> dict:
    return {
        "id": a.id,
        "job_id": a.job_id,
        "resume_id": a.resume_id,
        "resume": a.resume.original_filename if a.resume else None,
        "status": a.status,
        "dry_run": a.dry_run,
        "error": a.error_text,
        "screenshot_url": f"/api/applications/{a.id}/screenshot" if a.screenshot_path else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "finished_at": a.finished_at.isoformat() if a.finished_at else None,
        "job_title": a.job.title if a.job else None,
        "company": a.job.company if a.job else None,
        "apply_url": a.job.apply_url if a.job else None,
        "notes": json.loads(a.notes_json or "{}"),
    }


@router.post("/jobs/{job_id}/apply")
def api_run_application(
    job_id: int,
    payload: dict | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Run one application attempt. dry_run defaults to TRUE — the form is filled
    and screenshotted but never submitted. Human submits."""
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", True))
    resume_id = payload.get("resume_id")
    return run_application(db, job_id, resume_id=resume_id, dry_run=dry_run)


@router.get("/applications")
def api_list_applications(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(Application)
        .order_by(Application.created_at.desc(), Application.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [_app_dict(a) for a in rows]


@router.get("/applications/{app_id}")
def api_application_detail(app_id: int, db: Session = Depends(get_db)):
    a = db.get(Application, app_id)
    if not a:
        raise HTTPException(404, "application not found")
    data = _app_dict(a)
    log = (
        db.query(ActionLog)
        .filter(ActionLog.application_id == app_id)
        .order_by(ActionLog.id.asc())
        .all()
    )
    data["action_log"] = [
        {"action": l.action, "field": l.field, "detail": l.detail,
         "ts": l.created_at.isoformat() if l.created_at else None}
        for l in log
    ]
    return data


@router.get("/applications/{app_id}/screenshot")
def api_application_screenshot(app_id: int, db: Session = Depends(get_db)):
    a = db.get(Application, app_id)
    if not a or not a.screenshot_path or not os.path.exists(a.screenshot_path):
        raise HTTPException(404, "screenshot not available")
    return FileResponse(a.screenshot_path, media_type="image/png")


@router.post("/applications/{app_id}/submit")
def api_approve_submission(app_id: int, request: Request,
                           db: Session = Depends(get_db)):
    """APPROVE a staged application (permission model, 'approve_each').

    The agent filled the form and stopped; this makes it submit. Runs in a
    background thread (the form is re-opened, verified, and submitted) and
    reuses the staged row so the Applications page shows one row per job.
    """
    a = _stage_app(app_id, db)
    job_id = a.job_id
    a.status = "submitting"
    db.add(ActionLog(application_id=a.id, job_id=job_id,
                     action="user_approved",
                     detail="user approved the staged application from the dashboard"))
    db.commit()

    session_factory = request.app.state.SessionLocal

    def _work() -> None:
        s = session_factory()
        try:
            result = run_application(s, job_id, dry_run=False, headless=True,
                                     auto_submit=True,
                                     existing_application_id=a.id)
            status = result.get("status", "failed")
            row = s.get(Application, a.id)
            if row is not None:
                _notify_app_result(s, row, status, approval=True)
        except Exception as e:
            try:
                row = s.get(Application, a.id)
                if row is not None:
                    row.status = "submit_failed"
                    row.error_text = f"{type(e).__name__}: {str(e)[:400]}"
                    row.finished_at = datetime.utcnow()
            except Exception:
                pass
        finally:
            s.close()

    threading.Thread(target=_work, daemon=True).start()
    return {"started": True, "application_id": a.id}


@router.post("/applications/{app_id}/reject")
def api_reject_submission(app_id: int, db: Session = Depends(get_db)):
    """REJECT a staged application: it is dropped, nothing is submitted, and
    the agent will not retry it (status is in the agent's handled set)."""
    a = _stage_app(app_id, db)
    a.status = "rejected_user"
    a.finished_at = datetime.utcnow()
    a.error_text = "Rejected by the user (approval mode) — not submitted"
    db.add(ActionLog(application_id=a.id, job_id=a.job_id,
                     action="user_rejected",
                     detail="user rejected the staged application from the dashboard"))
    db.commit()
    return {"rejected": True, "application_id": a.id}
