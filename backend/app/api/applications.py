"""Application-automation API (Phase 4).

POST /api/jobs/{job_id}/apply          run one (dry-run by default) application
GET  /api/applications                 list application attempts
GET  /api/applications/{id}            detail + full action log
GET  /api/applications/{id}/screenshot the dry-run screenshot
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..automation.runner import run_application
from ..db.deps import get_db
from ..db.models import ActionLog, Application

router = APIRouter(prefix="/api", tags=["applications"])


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
